# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end pipeline test — needs both Neo4j and PostgreSQL."""
import textwrap
from pathlib import Path

import pytest

from src.db.migrate import run_migrations
from src.db.pg import repo_store
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
    pid = repo_store().add_profile(name="test_prof", odoo_version=TEST_VERSION)
    repo_store().add_repo(profile_id=pid, url="local/test", branch=TEST_VERSION,
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
    pid = repo_store().add_profile("test_prof", TEST_VERSION)
    repo_store().add_repo(pid, "local/ok", TEST_VERSION, str(repo))

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
        pid = repo_store().add_profile(prof, TEST_VERSION)
        repo_store().add_repo(pid, f"local/{prof}", TEST_VERSION, str(repo))

    summary = index_all(clean_pg)
    # Migration 0004 seeds 5 root profiles (no repos); they index as ok with 0 modules.
    assert summary["profiles_ok"] >= 2
    assert summary["modules"] >= 2
    assert summary["profiles_failed"] == []


def test_index_all_continues_after_profile_failure(pg_conn, clean_pg, neo4j_driver):
    """index_all continues with remaining profiles if one fails, reports failures."""
    from src.indexer.pipeline import index_all

    run_migrations(clean_pg)

    # Profile 1: bad path — triggers FileNotFoundError from _index_repo()
    pid1 = repo_store().add_profile(name="bad_prof", odoo_version=TEST_VERSION, description="")
    repo_store().add_repo(profile_id=pid1, url="x", branch="b",
             local_path="/nonexistent/__bad__/path")

    # Profile 2: no repos — returns {modules:0} without error
    repo_store().add_profile(name="empty_prof", odoo_version=TEST_VERSION, description="")

    summary = index_all(clean_pg)

    # Migration 0004 seeds 5 root profiles (no repos, always ok) + empty_prof (ok) = 6 ok.
    assert summary["profiles_ok"] >= 1, f"Expected at least 1 ok profile, got {summary}"
    assert "bad_prof" in summary["profiles_failed"]
    assert len(summary["profiles_failed"]) == 1
    assert summary["modules"] == 0


def test_index_repo_raises_for_missing_path(
    clean_neo4j, clean_pg, neo4j_driver
):
    """_index_repo raises FileNotFoundError when local_path does not exist."""
    import os

    from src.indexer.pipeline import _index_repo
    from src.indexer.writer_neo4j import Neo4jWriter

    run_migrations(clean_pg)
    pid = repo_store().add_profile(name="test_path_val", odoo_version=TEST_VERSION,
                      description="")
    repo_store().add_repo(profile_id=pid, url="x", branch="b",
             local_path="/nonexistent/__does_not_exist__/path")

    repos = repo_store().get_repos_for_profile("test_path_val")
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


@pytest.mark.neo4j
def test_pipeline_writes_embeddings_when_embedder_provided(
    clean_neo4j, pg_conn, neo4j_driver, tmp_path
):
    """index_profile must populate embeddings table when embedder is passed.

    Uses FakeEmbedder (deterministic, no GPU) so this test runs in CI.
    Skips automatically when pgvector extension is not installed.
    """
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import _vector_extension_available, run_migrations
    from src.db.pg import repo_store as _repo_store
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.pipeline import index_profile
    from tests.conftest import PG_EMBED_VERSION

    run_migrations(pg_conn)
    if not _vector_extension_available(pg_conn):
        pytest.skip("pgvector extension not installed — run as superuser: CREATE EXTENSION vector;")
    register_vector(pg_conn)

    # Isolate test data — clean up before and after
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (PG_EMBED_VERSION,))
        cur.execute("DELETE FROM repos WHERE branch = %s", (PG_EMBED_VERSION,))
        cur.execute(
            "DELETE FROM profiles WHERE name = %s", ("embed_test_prof",)
        )

    try:
        repo_path = make_git_repo(tmp_path / "repo_embed", branch=PG_EMBED_VERSION)
        # Reuse _seed_module but with PG_EMBED_VERSION so Neo4j + embeddings use same version
        module_dir = repo_path / "embed_mod"
        make_manifest(module_dir, name="embed_mod",
                      version=f"{PG_EMBED_VERSION}.1.0.0", depends=[])
        (module_dir / "models").mkdir()
        (module_dir / "models" / "__init__.py").write_text("")
        (module_dir / "models" / "em.py").write_text(
            "from odoo import models, fields\n\n"
            "class EmbedModel(models.Model):\n"
            "    _name = 'embed.model'\n\n"
            "    name = fields.Char()\n\n"
            "    def action_confirm(self):\n"
            "        self.write({'state': 'confirmed'})\n"
        )

        pid = _repo_store().add_profile("embed_test_prof", PG_EMBED_VERSION)
        _repo_store().add_repo(pid, "local/embed", PG_EMBED_VERSION, str(repo_path))

        summary = index_profile(
            pg_conn,
            profile_name="embed_test_prof",
            embedder=FakeEmbedder(dim=1024),
        )

        assert summary["embeddings"] > 0, (
            "index_profile must return embeddings > 0 when embedder is provided"
        )
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM embeddings WHERE odoo_version = %s AND module = 'embed_mod'",
                (PG_EMBED_VERSION,),
            )
            count = cur.fetchone()[0]
        assert count > 0, f"expected rows in embeddings table for embed_mod, got {count}"

    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (PG_EMBED_VERSION,))
            cur.execute("DELETE FROM repos WHERE branch = %s", (PG_EMBED_VERSION,))
            cur.execute("DELETE FROM profiles WHERE name = %s", ("embed_test_prof",))


def test_index_repo_returns_js_graph_counters(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path
):
    """_index_repo must return js_patches and owl_comps counters when parsing JS files."""
    import os

    from src.indexer.pipeline import _index_repo
    from src.indexer.writer_neo4j import Neo4jWriter

    run_migrations(clean_pg)
    repo = make_git_repo(tmp_path / "repo_js", branch=TEST_VERSION)

    # Create module with static/src/test.js containing an OWL component
    module_dir = repo / "js_test_mod"
    make_manifest(module_dir, name="js_test_mod",
                  version=f"{TEST_VERSION}.1.0.0", depends=[])
    (module_dir / "static" / "src").mkdir(parents=True)
    (module_dir / "static" / "src" / "test.js").write_text(textwrap.dedent("""
        import { Component } from "@odoo/owl";

        export class FooComponent extends Component {
            static template = "module.FooTemplate";
        }
    """).strip())

    pid = repo_store().add_profile("js_prof", TEST_VERSION)
    repo_store().add_repo(pid, "local/js", TEST_VERSION, str(repo))
    repos = repo_store().get_repos_for_profile("js_prof")

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    try:
        summary = _index_repo(repos[0], writer)

        # Must have js_patches and owl_comps counters
        assert "js_patches" in summary, f"summary missing 'js_patches' key: {summary}"
        assert "owl_comps" in summary, f"summary missing 'owl_comps' key: {summary}"

        # js_patches should be 0 (no patch() calls in the code)
        assert summary["js_patches"] == 0, f"expected 0 patches, got {summary['js_patches']}"

        # owl_comps should be 1 (FooComponent class)
        assert summary["owl_comps"] >= 1, f"expected >= 1 component, got {summary['owl_comps']}"
    finally:
        writer.close()


def test_index_profile_aggregates_js_counters(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path
):
    """index_profile must return aggregated js_patches and owl_comps counters."""
    run_migrations(clean_pg)
    repo = make_git_repo(tmp_path / "repo_js_agg", branch=TEST_VERSION)

    # Create module with JS files
    module_dir = repo / "agg_mod"
    make_manifest(module_dir, name="agg_mod",
                  version=f"{TEST_VERSION}.1.0.0", depends=[])
    (module_dir / "static" / "src").mkdir(parents=True)
    (module_dir / "static" / "src" / "comp.js").write_text(textwrap.dedent("""
        import { Component } from "@odoo/owl";

        export class MyComp extends Component {
            static template = "agg_mod.MyTemplate";
        }
    """).strip())

    pid = repo_store().add_profile("agg_prof", TEST_VERSION)
    repo_store().add_repo(pid, "local/agg", TEST_VERSION, str(repo))

    summary = index_profile(clean_pg, profile_name="agg_prof")

    # Must have js_patches and owl_comps in profile summary
    assert "js_patches" in summary, f"summary missing 'js_patches': {summary}"
    assert "owl_comps" in summary, f"summary missing 'owl_comps': {summary}"


def test_index_profile_warns_when_ancestor_not_indexed(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path, caplog
):
    """index_profile emits a warning when an ancestor profile has no indexed repos.

    Covers the ADR-0016 D4 warning path: indexing a child before its parent
    should not fail but should warn the admin.
    """
    import logging

    run_migrations(clean_pg)

    # --- Set up 2-tier hierarchy: root (no repos indexed) → child ---
    root_id = repo_store().add_profile("root_warn_prof", TEST_VERSION)
    child_id = repo_store().add_profile("child_warn_prof", TEST_VERSION)
    repo_store().set_profile_parent(child_id, root_id)

    # Root profile has a repo but it stays in 'pending' status (never indexed)
    repo_store().add_repo(root_id, "local/root_warn", TEST_VERSION, "/nonexistent/root")

    # Child profile has a real (but empty) repo
    repo = make_git_repo(tmp_path / "repo_child_warn", branch=TEST_VERSION)
    repo_store().add_repo(child_id, "local/child_warn", TEST_VERSION, str(repo))

    with caplog.at_level(logging.WARNING, logger="src.indexer.pipeline"):
        index_profile(clean_pg, profile_name="child_warn_prof")

    # At least one warning must mention the unindexed ancestor
    warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    ancestor_warnings = [
        t for t in warning_texts if "root_warn_prof" in t and "no indexed repos" in t
    ]
    assert ancestor_warnings, (
        f"Expected a warning about unindexed ancestor 'root_warn_prof', "
        f"got warnings: {warning_texts}"
    )
