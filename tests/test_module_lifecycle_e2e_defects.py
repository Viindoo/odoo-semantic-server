# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module lifecycle: the defects the real-checkout E2E found (11-e2e-report.md).

Each test replays one E2E step over temp git repos with a bare ``origin``, driven
through the REAL CLI / ``index_profile`` against a real Neo4j and a real
PostgreSQL with pgvector. Expected values come from the business rules, never
from what the implementation returns.

* E2E-D1 (HIGH, data loss) - only a genuinely ABSENT manifest ever retires a
  module. A tracked manifest that exists but cannot be read (E8a:
  ``chmod 000 to_approvals/__manifest__.py`` deleted 278 child nodes with exit
  0) makes the scan incomplete: exit 3, attention naming the file, nothing
  retired, the module and ALL its children kept; the next plain run (no
  ``--full``) recovers. A manifest that is read but does not parse keeps the
  indexed module and its test nodes (exit 3, audit ``unparseable_kept``) while
  the repo's other retirements proceed; a fixed manifest is normal again.
* E2E-D2 - a retired ledger row records the HEAD of the repo that retired it,
  and the rendered answer says so - no "not stamped" text, no doubled
  parenthesis.
* E2E-D3 - a 0-byte data file (odoo 16.0 ``mass_mailing/data/mass_mailing_data.xml``)
  defines nothing: it neither marks the module degraded (its entity prune still
  runs) nor sets ``lifecycle_attention``.
* E2E-D4 - a run where nothing changed skips the version-wide post-pass (its
  per-version stamp is not re-recorded); forcing the full post-pass right after
  changes no edge count; any write at the version (a repo change, a crash in
  between) makes the next run re-derive.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from tests._lifecycle_repo import (
    GitRepo,
    V,
    children,
    lc,
    ledger,
    module_node,
    needs_attention,
    register,
    repo_row,
    run,
    write_module,
)

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]


@pytest.fixture
def pg(clean_pg, clean_neo4j):
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg


@pytest.fixture
def cli(monkeypatch, capsys):
    """The real ``python -m src.indexer`` entry point: returns (exit code, stderr)."""
    import src.indexer.__main__ as main_mod
    from tests import conftest

    monkeypatch.setenv("PG_DSN", conftest.get_test_dsn())

    def call(*argv: str) -> tuple[int, str]:
        capsys.readouterr()
        code = main_mod.main(list(argv))
        return code, capsys.readouterr().err

    return call


def _audit(capsys, profile: str) -> dict:
    """``lifecycle-audit --json`` (PG_DSN comes from the ``cli`` fixture)."""
    import src.indexer.__main__ as main_mod

    capsys.readouterr()
    assert main_mod.main(["lifecycle-audit", "--profile", profile, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


_TEST_PY = '''\
from odoo.tests.common import TransactionCase


class TestRecord(TransactionCase):

    def test_touch_returns_true(self):
        record = self.env["{model}"].create({{"label": "x"}})
        self.assertTrue(record.action_touch())

    def test_label_is_kept(self):
        record = self.env["{model}"].create({{"label": "y"}})
        self.assertEqual(record.label, "y")
'''


def _with_tests(repo: GitRepo, name: str) -> None:
    """A real module plus a Python test class (TestClass + TestMethod nodes)."""
    write_module(repo, name)
    tests = repo.path / name / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "__init__.py").write_text("from . import test_record\n")
    (tests / "test_record.py").write_text(
        _TEST_PY.format(model=f"{name.replace('_', '.')}.record"))


def _test_nodes(driver, name: str) -> int:
    with driver.session() as s:
        return s.run(
            "MATCH (t {odoo_version: $v, module: $m}) WHERE t:TestClass OR t:TestMethod "
            "RETURN count(t) AS n", v=V, m=name,
        ).single()["n"]


def _node_count(driver) -> int:
    with driver.session() as s:
        return s.run("MATCH (n {odoo_version: $v}) RETURN count(n) AS n", v=V).single()["n"]


def _approvals_repo(tmp_path: Path, profile: str = "tvtma_99") -> tuple[GitRepo, int]:
    repo = GitRepo(tmp_path, "tvtmaaddons")
    _with_tests(repo, "to_approvals")
    for name in ("viin_hr", "viin_project", "viin_ai_rag"):
        write_module(repo, name)
    repo.commit("add")
    (rid,) = register(profile, repo)
    return repo, rid


@contextmanager
def _unreadable(path: Path):
    path.chmod(0)
    try:
        yield
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)


# ---------------------------------------------------------------------------
# E2E-D1 - an unreadable manifest never retires its module
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 000 file")
def test_unreadable_manifest_makes_the_scan_incomplete_and_keeps_every_node(
    pg, neo4j_driver, tmp_path, cli,
):
    """E8a replayed: the same night ships a real removal (viin_ai_rag) and an
    unreadable to_approvals manifest. Exit 3, attention names the file, NOTHING
    is retired - to_approvals keeps its Module and every child (tests included)
    and viin_ai_rag waits. Readable again: the next plain run (no --full)
    retires viin_ai_rag, keeps to_approvals and clears the attention."""
    repo, rid = _approvals_repo(tmp_path)
    assert cli("index-repo", "--profile", "tvtma_99", "--no-embed")[0] == 0
    before_children = children(neo4j_driver, "to_approvals")
    before_tests = _test_nodes(neo4j_driver, "to_approvals")
    assert before_children > 0 and before_tests >= 3, "precondition: module + tests indexed"
    repo.rm("viin_ai_rag")
    repo.commit("[MOV] viin_ai_rag to another repo")
    manifest = repo.path / "to_approvals" / "__manifest__.py"
    total_before = _node_count(neo4j_driver)

    with _unreadable(manifest):
        code, err = cli("index-repo", "--profile", "tvtma_99", "--no-embed", "--no-fetch")

        assert code == 3, err
        assert "scan_incomplete" in err, err
        assert module_node(neo4j_driver, "to_approvals") is not None
        assert children(neo4j_driver, "to_approvals") == before_children
        assert _test_nodes(neo4j_driver, "to_approvals") == before_tests
        assert module_node(neo4j_driver, "viin_ai_rag") is not None, (
            "a degraded scan must retire nothing, not even a real removal")
        assert _node_count(neo4j_driver) == total_before
        attention = repo_row(pg, rid)["lifecycle_attention"] or ""
        assert "cannot be read" in attention and "to_approvals/__manifest__.py" in attention, (
            attention)
        assert ledger(pg, rid, "to_approvals")["state"] == "present"

    code, err = cli("index-repo", "--profile", "tvtma_99", "--no-embed", "--no-fetch")

    assert code == 0, err
    assert repo_row(pg, rid)["lifecycle_attention"] is None
    assert module_node(neo4j_driver, "to_approvals") is not None
    assert children(neo4j_driver, "to_approvals") == before_children
    assert _test_nodes(neo4j_driver, "to_approvals") == before_tests
    assert module_node(neo4j_driver, "viin_ai_rag") is None
    assert ledger(pg, rid, "viin_ai_rag")["state"] == "retired"


def test_unparseable_manifest_keeps_the_indexed_module_and_its_tests(
    pg, neo4j_driver, tmp_path, cli, capsys,
):
    """A commit breaks to_approvals' manifest text (tracked, readable, not
    Python) and removes viin_ai_rag. The audit predicts ``unparseable_kept``;
    the run exits 3 (``manifest_unparseable``), keeps to_approvals with every
    node (tests included) and still retires viin_ai_rag. The fixing commit
    makes the next run normal (exit 0, no finding)."""
    repo, rid = _approvals_repo(tmp_path)
    assert cli("index-repo", "--profile", "tvtma_99", "--no-embed")[0] == 0
    before_children = children(neo4j_driver, "to_approvals")
    before_tests = _test_nodes(neo4j_driver, "to_approvals")
    assert before_tests >= 3
    manifest = repo.path / "to_approvals" / "__manifest__.py"
    good = manifest.read_text()
    # A botched edit (missing colon, unquoted value): read fine, no key recoverable.
    manifest.write_text("{\n    'name' 'to_approvals',\n    'license': LGPL-3,\n")
    repo.rm("viin_ai_rag")
    repo.commit("[WIP] to_approvals manifest half-edited, viin_ai_rag moved")

    audit = _audit(capsys, "tvtma_99")
    [entry] = audit["repos"]
    assert entry.get("unparseable_kept") == ["to_approvals"], json.dumps(entry, default=str)
    assert audit["findings"].get("unparseable_kept") == 1
    assert "viin_ai_rag" in [i["name"] for i in entry["would_retire"]]

    code, err = cli("index-repo", "--profile", "tvtma_99", "--no-embed")

    assert code == 3, err
    assert "manifest_unparseable" in err, err
    assert module_node(neo4j_driver, "to_approvals") is not None
    assert children(neo4j_driver, "to_approvals") == before_children
    assert _test_nodes(neo4j_driver, "to_approvals") == before_tests
    assert module_node(neo4j_driver, "viin_ai_rag") is None, "other retirements proceed"
    assert "to_approvals" in (repo_row(pg, rid)["lifecycle_attention"] or "")

    manifest.write_text(good)
    repo.commit("[FIX] to_approvals manifest")
    code, err = cli("index-repo", "--profile", "tvtma_99", "--no-embed")

    assert code == 0, err
    assert repo_row(pg, rid)["lifecycle_attention"] is None
    assert ledger(pg, rid, "to_approvals")["state"] == "present"
    assert _test_nodes(neo4j_driver, "to_approvals") == before_tests
    assert _audit(capsys, "tvtma_99")["findings"].get("unparseable_kept") == 0


def test_unparseable_manifest_never_drops_its_repo_from_a_co_owned_module(
    pg, neo4j_driver, tmp_path,
):
    """viin_meeting_room is shipped by the CE and the EE repo. EE's manifest
    stops parsing: EE still ships the module, so the node keeps EE's profile."""
    ce = GitRepo(tmp_path / "ce", "viindoo_ce")
    ee = GitRepo(tmp_path / "ee", "erponline_ee")
    for r in (ce, ee):
        write_module(r, "viin_meeting_room")
        write_module(r, f"{r.path.name}_keep")
        r.commit("init")
    register("ce_99", ce)
    (rid_ee,) = register("ee_99", ee)
    run(pg, "ce_99")
    run(pg, "ee_99")
    assert sorted(module_node(neo4j_driver, "viin_meeting_room")["profile"]) == [
        "ce_99", "ee_99"]
    (ee.path / "viin_meeting_room" / "__manifest__.py").write_text("{'name': \n")
    ee.commit("broken manifest")

    summary = run(pg, "ee_99")

    assert needs_attention(summary)
    assert sorted(module_node(neo4j_driver, "viin_meeting_room")["profile"]) == [
        "ce_99", "ee_99"], "an unparseable exclusion must not drop the repo that ships it"
    assert ledger(pg, rid_ee, "viin_meeting_room")["state"] != "retired"


# ---------------------------------------------------------------------------
# E2E-D2 - a retired row records the retiring repo's HEAD, and says so
# ---------------------------------------------------------------------------

def _hubs():
    from src.mcp import describe
    from src.mcp.tools import guidance

    seen, out = set(), []
    for hub in (guidance._srv, describe._srv, sys.modules.get("src.mcp.server")):
        if hub is not None and id(hub) not in seen:
            seen.add(id(hub))
            out.append(hub)
    return out


def test_retired_row_records_the_repo_head_and_the_answer_reads_it(
    pg, neo4j_driver, tmp_path, monkeypatch,
):
    """test_pylint is renamed away; the retirement row carries the HEAD the
    retiring repo was indexed at, and check_module_exists shows that HEAD with
    balanced parentheses and no "not stamped" claim."""
    from src.mcp import session
    from src.mcp.tools import guidance

    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "test_pylint")
    write_module(repo, "viin_hr")
    repo.commit("add")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    repo.mv("test_pylint", "test_viin_pylint")
    head = repo.commit("[REF] test_pylint: rename the module to test_viin_pylint")

    run(pg, "tvtma_99")

    row = ledger(pg, rid, "test_pylint")
    assert row["state"] == "retired"
    assert row["state_changed_sha"] == head, "the row must record the retiring repo's HEAD"
    assert repo_row(pg, rid)["head_sha"] == head

    for h in _hubs():
        monkeypatch.setattr(h, "_get_driver", lambda: neo4j_driver)
    session.invalidate_allowed_profiles()
    try:
        out = guidance._srv._check_module_exists("test_pylint", V)
    finally:
        session.invalidate_allowed_profiles()
    [state] = [ln for ln in out.splitlines() if "State: retired" in ln]
    assert f"(recorded at HEAD {head[:7]} on " in state, out
    assert "not stamped" not in out, out
    assert state.count("(") == state.count(")") and not state.endswith("))"), state


# ---------------------------------------------------------------------------
# E2E-D3 - an empty XML file defines nothing
# ---------------------------------------------------------------------------

_VIEW_XML = """<?xml version="1.0"?>
<odoo>
    <record id="{mod}_record_view_form" model="ir.ui.view">
        <field name="name">{model}.form</field>
        <field name="model">{model}</field>
        <field name="arch" type="xml">
            <form><sheet><field name="label"/></sheet></form>
        </field>
    </record>
</odoo>
"""


def _fields_of(driver, module: str) -> set[str]:
    with driver.session() as s:
        return set(s.run(
            "MATCH (f:Field {odoo_version: $v, module: $m}) RETURN collect(f.name) AS n",
            v=V, m=module,
        ).single()["n"])


def _mailing_repo(tmp_path: Path, data_xml: str) -> tuple[GitRepo, int]:
    repo = GitRepo(tmp_path, "odoo")
    write_module(repo, "mass_mailing", extra_field="subject = fields.Char()")
    write_module(repo, "mail")
    mod = repo.path / "mass_mailing"
    (mod / "views").mkdir()
    (mod / "views" / "record_views.xml").write_text(
        _VIEW_XML.format(mod="mass_mailing", model="mass.mailing.record"))
    (mod / "data").mkdir()
    (mod / "data" / "mass_mailing_data.xml").write_text(data_xml)
    repo.commit("add")
    (rid,) = register("odoo_99", repo)
    return repo, rid


@pytest.mark.parametrize("data_xml", ["", "  \n\t\n"], ids=["zero_bytes", "whitespace_only"])
def test_empty_xml_data_file_neither_degrades_the_module_nor_raises_attention(
    pg, neo4j_driver, tmp_path, data_xml,
):
    """The module parses cleanly (no attention, exit 0) and is entity-pruned
    like any other: a field removed from the source disappears next run."""
    repo, rid = _mailing_repo(tmp_path, data_xml)

    first = run(pg, "odoo_99")

    assert not needs_attention(first), lc(first)
    assert repo_row(pg, rid)["lifecycle_attention"] is None
    assert "subject" in _fields_of(neo4j_driver, "mass_mailing")
    write_module(repo, "mass_mailing")  # the subject field is removed
    repo.commit("mass_mailing: drop subject")

    second = run(pg, "odoo_99")

    assert not needs_attention(second), lc(second)
    assert repo_row(pg, rid)["lifecycle_attention"] is None
    assert "subject" not in _fields_of(neo4j_driver, "mass_mailing"), (
        "a module with an empty data file must still be entity-pruned")


def test_broken_xml_data_file_still_degrades_the_module(pg, neo4j_driver, tmp_path):
    # GUARD: pre-existing behaviour (B14: a file that does not parse keeps the prune off)
    repo, rid = _mailing_repo(tmp_path, "<odoo><record id='x'")
    run(pg, "odoo_99")
    write_module(repo, "mass_mailing")
    repo.commit("mass_mailing: drop subject")

    run(pg, "odoo_99")

    assert "parse degraded" in (repo_row(pg, rid)["lifecycle_attention"] or "")
    assert "subject" in _fields_of(neo4j_driver, "mass_mailing"), (
        "a degraded parse must not prune")


# ---------------------------------------------------------------------------
# E2E-D4 - the version-wide post-pass runs only when the version changed
# ---------------------------------------------------------------------------

_EDGE_TYPES = ("INHERITS", "INHERITS_TEST", "COVERS_MODEL", "COVERS_FIELD", "BOUND_TO")


def _edge_counts(driver) -> dict[str, int]:
    out: dict[str, int] = {}
    with driver.session() as s:
        for t in _EDGE_TYPES:
            out[t] = s.run(
                f"MATCH (a {{odoo_version: $v}})-[r:{t}]->() RETURN count(r) AS n", v=V,
            ).single()["n"]
        out["is_helper"] = s.run(
            "MATCH (n {odoo_version: $v}) WHERE n.is_helper = true RETURN count(n) AS n", v=V,
        ).single()["n"]
    return out


def _post_pass_state(driver, *, required: bool = True) -> dict | None:
    """The per-version post-pass stamp; asserted present unless *required* is False."""
    with driver.session() as s:
        row = s.run(
            "MATCH (s:PostPassState {odoo_version: $v}) RETURN properties(s) AS p", v=V,
        ).single()
    assert row is not None or not required, f"no post-pass state recorded for {V}"
    return dict(row["p"]) if row else None


def _surface_repo(tmp_path: Path) -> tuple[GitRepo, int]:
    """Definitions, an extender and Python tests: every post-pass edge kind."""
    repo = GitRepo(tmp_path, "tvtmaaddons")
    _with_tests(repo, "viin_base")
    write_module(repo, "viin_ext", depends=["viin_base"], inherit="viin.base.record",
                 extra_field="ext_flag = fields.Boolean()")
    _with_tests(repo, "viin_other")
    repo.commit("add")
    (rid,) = register("tvtma_99", repo)
    return repo, rid


def test_no_change_run_skips_the_post_pass_and_a_forced_one_changes_no_edge(
    pg, neo4j_driver, tmp_path, caplog,
):
    """Run 1 derives and stamps the version. Run 2 changes nothing: the stamp is
    not re-recorded (the post-pass did not run). A forced full post-pass right
    after leaves every edge count identical - the skip lost nothing."""
    from src.indexer.pipeline import reconcile_test_surface
    from src.indexer.writer_neo4j import Neo4jWriter

    _surface_repo(tmp_path)
    run(pg, "tvtma_99")
    stamp = _post_pass_state(neo4j_driver, required=False)
    assert stamp is not None and stamp.get("dirty") is False, (
        f"a completed post-pass must leave a clean per-version stamp: {stamp}")
    edges = _edge_counts(neo4j_driver)
    assert edges["INHERITS"] > 0 and edges["INHERITS_TEST"] > 0 and edges["COVERS_MODEL"] > 0, (
        f"precondition: the fixture exercises the post-pass: {edges}")

    with caplog.at_level(logging.INFO, logger="src.indexer"):
        again = run(pg, "tvtma_99")

    assert again["modules"] == 0
    assert _post_pass_state(neo4j_driver)["recorded_at"] == stamp["recorded_at"], (
        "a run where nothing changed must not re-run the post-pass")
    assert f"post-pass {V}: skipped" in caplog.text
    assert _edge_counts(neo4j_driver) == edges

    w = Neo4jWriter(os.environ["NEO4J_TEST_URI"], os.getenv("NEO4J_TEST_USER", "neo4j"),
                    os.getenv("NEO4J_TEST_PASSWORD", "password"))
    try:
        w.reconcile_same_name_inherits(V)
        w.reconcile_owl_edges(V)
        reconcile_test_surface(w, [V], framework_profiles=["tvtma_99"])
    finally:
        w.close()
    assert _edge_counts(neo4j_driver) == edges, "the skipped post-pass must have lost nothing"


def test_any_change_at_the_version_invalidates_the_skip(pg, neo4j_driver, tmp_path):
    """A source change re-derives in the same run (the repo's writes precede the
    post-pass); a retirement (the reconcile runs after the post-pass) leaves the
    stamp dirty so the NEXT run re-derives; then the run after it is a skip."""
    repo, _rid = _surface_repo(tmp_path)
    run(pg, "tvtma_99")
    first = _post_pass_state(neo4j_driver)["recorded_at"]
    run(pg, "tvtma_99")
    assert _post_pass_state(neo4j_driver)["recorded_at"] == first, "precondition: a skip"

    write_module(repo, "viin_ext", depends=["viin_base"], inherit="viin.base.record",
                 extra_field="ext_note = fields.Text()")
    repo.commit("viin_ext: another field")
    run(pg, "tvtma_99")
    changed = _post_pass_state(neo4j_driver)
    assert changed["recorded_at"] > first and changed["dirty"] is False

    repo.rm("viin_other")
    repo.commit("[REM] viin_other")
    run(pg, "tvtma_99")
    assert module_node(neo4j_driver, "viin_other") is None
    assert _post_pass_state(neo4j_driver)["dirty"] is True, (
        "a retirement after the post-pass must leave the version dirty")

    run(pg, "tvtma_99")
    rederived = _post_pass_state(neo4j_driver)
    assert rederived["recorded_at"] > changed["recorded_at"] and rederived["dirty"] is False

    run(pg, "tvtma_99")
    assert _post_pass_state(neo4j_driver)["recorded_at"] == rederived["recorded_at"]


def test_a_crash_between_a_write_and_the_post_pass_never_leaves_a_clean_stamp(
    pg, neo4j_driver, tmp_path, monkeypatch,
):
    """The graph changed, then the run died in the post-pass: the stamp stays
    dirty, so the next run (nothing new in git) re-derives."""
    from src.indexer import pipeline

    repo, _rid = _surface_repo(tmp_path)
    run(pg, "tvtma_99")
    before = _post_pass_state(neo4j_driver)["recorded_at"]
    write_module(repo, "viin_ext", depends=["viin_base"], inherit="viin.base.record",
                 extra_field="ext_note = fields.Text()")
    repo.commit("viin_ext: another field")

    def crash(*_a, **_k):
        raise RuntimeError("injected crash in the post-pass")

    monkeypatch.setattr(pipeline, "reconcile_test_surface", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        run(pg, "tvtma_99")
    monkeypatch.undo()

    state = _post_pass_state(neo4j_driver)
    assert state["dirty"] is True, f"a write without a finished post-pass left {state}"
    assert state["recorded_at"] == before

    run(pg, "tvtma_99")

    healed = _post_pass_state(neo4j_driver)
    assert healed["dirty"] is False and healed["recorded_at"] > before
