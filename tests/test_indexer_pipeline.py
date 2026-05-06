"""End-to-end pipeline test — needs both Neo4j and PostgreSQL."""
import textwrap
from pathlib import Path

import pytest

from src.db.migrate import run_migrations
from src.db.repo_registry import add_profile, add_repo
from src.indexer.pipeline import index_profile
from tests.conftest import TEST_VERSION, make_git_repo, make_manifest

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]


def _seed_module(repo: Path, name: str) -> None:
    """Create a single Odoo module under repo/<name>."""
    module = repo / name
    make_manifest(module, name=name, version=f"{TEST_VERSION}.1.0.0", depends=[])
    (module / "models").mkdir()
    (module / "models" / "__init__.py").write_text("")
    (module / "models" / f"{name}.py").write_text(textwrap.dedent(f"""
        from odoo import models, fields

        class FooModel(models.Model):
            _name = '{name}.foo'
            x = fields.Char()
    """).strip())
    (module / "views").mkdir()
    (module / "views" / "views.xml").write_text(textwrap.dedent(f"""
        <?xml version="1.0"?>
        <odoo>
            <record id="view_{name}_form" model="ir.ui.view">
                <field name="name">{name}.form</field>
                <field name="model">{name}.foo</field>
                <field name="arch" type="xml"><form/></field>
            </record>
            <template id="{name}_portal_tmpl"><div/></template>
        </odoo>
    """).strip())


def test_pipeline_writes_models_views_qweb_to_neo4j(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path
):
    run_migrations(clean_pg)
    repo = make_git_repo(tmp_path / "repo_test", branch=TEST_VERSION)
    _seed_module(repo, "demo_mod")
    pid = add_profile(clean_pg, name="test_prof", odoo_version=TEST_VERSION)
    add_repo(clean_pg, profile_id=pid, url="local/test", branch=TEST_VERSION,
             local_path=str(repo))

    summary = index_profile(clean_pg, profile_name="test_prof")
    assert summary["modules"] >= 1
    assert summary["views"] >= 1
    assert summary["qweb"] >= 1

    with neo4j_driver.session() as session:
        model_rec = session.run(
            "MATCH (m:Model {name: 'demo_mod.foo', odoo_version: $v}) RETURN m",
            v=TEST_VERSION
        ).single()
        view_rec = session.run(
            "MATCH (v:View {odoo_version: $v}) RETURN v LIMIT 1",
            v=TEST_VERSION
        ).single()
        qweb_rec = session.run(
            "MATCH (t:QWebTmpl {odoo_version: $v}) RETURN t LIMIT 1",
            v=TEST_VERSION
        ).single()
    assert model_rec is not None, "pipeline must write Model node from parser_python"
    assert view_rec is not None, "pipeline must write View node from parser_xml"
    assert qweb_rec is not None, "pipeline must write QWebTmpl node from parser_qweb"


def test_pipeline_marks_repo_indexed_on_success(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path
):
    run_migrations(clean_pg)
    repo = make_git_repo(tmp_path / "repo_ok", branch=TEST_VERSION)
    _seed_module(repo, "ok_mod")
    pid = add_profile(clean_pg, "test_prof", TEST_VERSION)
    add_repo(clean_pg, pid, "local/ok", TEST_VERSION, str(repo))

    index_profile(clean_pg, profile_name="test_prof")

    with clean_pg.cursor() as cur:
        cur.execute("SELECT status, last_indexed_at FROM repos")
        rows = cur.fetchall()
    assert rows[0][0] == "indexed"
    assert rows[0][1] is not None


def test_pipeline_index_all_iterates_every_profile(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path
):
    from src.indexer.pipeline import index_all
    run_migrations(clean_pg)
    for prof in ("p_a", "p_b"):
        repo = make_git_repo(tmp_path / f"repo_{prof}", branch=TEST_VERSION)
        _seed_module(repo, f"mod_{prof}")
        pid = add_profile(clean_pg, prof, TEST_VERSION)
        add_repo(clean_pg, pid, f"local/{prof}", TEST_VERSION, str(repo))

    summary = index_all(clean_pg)
    assert summary["profiles_ok"] == 2
    assert summary["modules"] >= 2


def test_index_all_continues_after_profile_failure(pg_conn, clean_pg, neo4j_driver):
    """index_all continues with remaining profiles if one fails, reports failures."""
    from src.db.migrate import run_migrations
    from src.db.repo_registry import add_profile, add_repo
    from src.indexer.pipeline import index_all

    run_migrations(pg_conn)

    # Profile 1: bad path — triggers FileNotFoundError from _index_repo()
    pid1 = add_profile(pg_conn, name="bad_prof", odoo_version=TEST_VERSION, description="")
    add_repo(pg_conn, profile_id=pid1, url="x", branch="b",
             local_path="/nonexistent/__bad__/path")

    # Profile 2: no repos — returns {modules:0} without error
    add_profile(pg_conn, name="empty_prof", odoo_version=TEST_VERSION, description="")

    summary = index_all(pg_conn)

    assert summary["profiles_ok"] == 1, f"Expected 1 ok profile, got {summary}"
    assert "bad_prof" in summary["profiles_failed"]
    assert summary["modules"] == 0


def test_index_repo_raises_for_missing_path(
    clean_neo4j, clean_pg, neo4j_driver
):
    """_index_repo raises FileNotFoundError when local_path does not exist."""
    import os

    from src.db.repo_registry import add_profile, add_repo, get_repos_for_profile
    from src.indexer.pipeline import _index_repo
    from src.indexer.writer_neo4j import Neo4jWriter

    run_migrations(clean_pg)
    pid = add_profile(clean_pg, name="test_path_val", odoo_version=TEST_VERSION,
                      description="")
    add_repo(clean_pg, profile_id=pid, url="x", branch="b",
             local_path="/nonexistent/__does_not_exist__/path")

    repos = get_repos_for_profile(clean_pg, "test_path_val")
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    try:
        with pytest.raises(FileNotFoundError, match="local_path does not exist"):
            _index_repo(repos[0], writer)
    finally:
        writer.close()
