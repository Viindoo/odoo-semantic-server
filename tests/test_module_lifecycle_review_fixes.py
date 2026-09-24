# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module lifecycle: the final-review data and tenant fixes, end to end (ADR-0056).

Every test drives the REAL ``index_profile`` (or the real CLI) over temp git
repos with a bare ``origin`` against a real Neo4j and a real PostgreSQL with
pgvector. Expected values come from the business rules of the final review
(/tmp/osm-373-378/12-final-review.md) and the owner decision on D3, never from
what the implementation returns.

Rules protected:

* T2 (record time) - the successor an orphan sweep records for tenant A's module
  comes from A's own scope. Another tenant's private module that declares A's
  old name in ``old_technical_name`` is never recorded as A's successor.
* D1 - an owner drop waits while an unsynced repo may still ship the module:
  repo A (synced) and repo B (unsynced) both ship ``sale``, repo C removes it.
  The node keeps B's profile, the name is undecidable, the run exits 3 and
  nothing is dropped. After B syncs, the next reconcile drops C's ownership.
  The same wait holds for a co-owner that EXCLUDES the module
  (``excluded_owner_waiting``).
* D2 - a retirement never deletes what a concurrent run wrote after it
  started: the concurrent run's children survive, the Module survives, the
  name is reported ``skipped_recent`` (ledger row still pending, embeddings
  kept, not recorded retired).
* D3 (owner decision) - the first run after the ledger deploy (empty ledger,
  ``presence_head_sha`` NULL) judges G-B against the modules the GRAPH says the
  repo owns: a scan that now covers far fewer trips ``mass_retire`` (exit 3,
  attention, nothing swept); a few ghosts are swept as before; the dry-run
  audit predicts both (``gates.baseline = "graph"``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from tests._lifecycle_repo import (
    GitRepo,
    V,
    assert_gone,
    embeddings,
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
    """Migrated PG (with pgvector) + clean Neo4j; the pool comes from pg_conn."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg


def _report(summary: dict) -> dict:
    """The run's ReconcileReport (as a dict) for version V."""
    reports = [r for r in lc(summary).get("reports", []) if r.get("odoo_version") == V]
    assert reports, f"no reconcile report for {V}: {lc(summary)}"
    return reports[-1]


def _rewind_to_pre_ledger(pg_conn, repo: GitRepo, repo_id: int) -> None:
    """The 0.18 state of ONE repo: HEAD indexed, no ledger rows, no presence head."""
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM module_presence WHERE repo_id = %s", (repo_id,))
        cur.execute(
            "UPDATE repos SET presence_head_sha = NULL, lifecycle_attention = NULL, "
            "head_sha = %s WHERE id = %s",
            (repo.head(), repo_id),
        )


def _audit(monkeypatch, capsys, profile: str) -> dict:
    """``lifecycle-audit --profile <p> --json`` through the real CLI."""
    from src.indexer.__main__ import main
    from tests import conftest

    monkeypatch.setenv("PG_DSN", conftest.get_test_dsn())
    capsys.readouterr()
    code = main(["lifecycle-audit", "--profile", profile, "--json"])
    out = capsys.readouterr().out
    assert code == 0, out
    return json.loads(out)


# ---------------------------------------------------------------------------
# T2 - the orphan sweep records a successor from the caller's own scope
# ---------------------------------------------------------------------------

def test_orphan_sweep_never_records_another_tenants_module_as_the_successor(
    pg, neo4j_driver, tmp_path,
):
    """acme's repo dropped ``acme_old`` and ``acme_old2`` before the ledger existed.
    globex's private repo ships ``globex_new`` declaring ``old_technical_name:
    acme_old``; acme's own ``acme_new2`` declares ``acme_old2``. The first ledger
    run sweeps both ghosts: acme_old gets NO successor (globex_new is outside
    acme's scope), acme_old2 gets acme_new2."""
    globex = GitRepo(tmp_path / "g", "globex_addons")
    write_module(globex, "globex_new", old_technical_name="acme_old")
    globex.commit("globex_new")
    register("globex_99", globex)
    run(pg, "globex_99")

    acme = GitRepo(tmp_path / "a", "acme_addons")
    for name in ("acme_old", "acme_old2", "acme_keep"):
        write_module(acme, name)
    acme.commit("acme")
    (rid,) = register("acme_99", acme)
    run(pg, "acme_99")
    # Separate commits so git sees plain deletions (no rename evidence).
    acme.rm("acme_old")
    acme.commit("[REM] acme_old")
    acme.rm("acme_old2")
    acme.commit("[REM] acme_old2")
    write_module(acme, "acme_new2", old_technical_name="acme_old2", extra_field=(
        "total = fields.Float()\n    note = fields.Text()\n    flag = fields.Boolean()"))
    acme.commit("[ADD] acme_new2 replaces acme_old2")
    run(pg, "acme_99", retire=False)  # graph holds both ghosts, like 0.18 left them
    _rewind_to_pre_ledger(pg, acme, rid)
    assert module_node(neo4j_driver, "acme_old") is not None, "precondition: ghost indexed"

    summary = run(pg, "acme_99")

    assert not needs_attention(summary), lc(summary)
    assert_gone(neo4j_driver, pg, "acme_old")
    assert_gone(neo4j_driver, pg, "acme_old2")
    old = ledger(pg, rid, "acme_old")
    assert (old["state"], old["retire_reason"]) == ("retired", "orphan_sweep")
    assert old["successor_names"] is None, (
        f"globex's private module was recorded as acme's successor: {old['successor_names']}")
    old2 = ledger(pg, rid, "acme_old2")
    assert (old2["state"], old2["retire_reason"]) == ("retired", "orphan_sweep")
    assert old2["successor_names"] == ["acme_new2"]
    assert old2["successor_source"] == "old_technical_name"
    assert module_node(neo4j_driver, "globex_new") is not None


# ---------------------------------------------------------------------------
# D1 - an owner drop waits for an unsynced repo that may still ship the name
# ---------------------------------------------------------------------------

def _three_owners(tmp_path: Path, pg_conn, driver):
    """A, B and C each ship ``sale`` (plus one module of their own), all synced."""
    a = GitRepo(tmp_path / "a", "odoo_a")
    b = GitRepo(tmp_path / "b", "odoo_b")
    c = GitRepo(tmp_path / "c", "vendored_c")
    write_module(a, "sale")
    write_module(a, "a_keep")
    write_module(b, "sale")
    write_module(b, "b_keep")
    write_module(c, "sale")
    write_module(c, "c_keep")
    for r in (a, b, c):
        r.commit("init")
    (rid_a,) = register("pa_99", a)
    (rid_b,) = register("pb_99", b)
    (rid_c,) = register("pc_99", c)
    for p in ("pa_99", "pb_99", "pc_99"):
        run(pg_conn, p)
    assert sorted(module_node(driver, "sale")["profile"]) == [
        "pa_99", "pb_99", "pc_99"], "precondition: three owners"
    return a, b, c, rid_a, rid_b, rid_c


def _unsync(pg_conn, repo: GitRepo, repo_id: int) -> None:
    """B is in the rollout window: indexed (the graph node carries its profile)
    but its ledger never reflected it (no rows, ``presence_head_sha`` NULL), and
    its checkout tracks ``sale`` - it may still ship it."""
    _rewind_to_pre_ledger(pg_conn, repo, repo_id)


def test_owner_drop_waits_while_an_unsynced_repo_may_still_ship_the_module(
    pg, neo4j_driver, tmp_path,
):
    """The review's rollout scenario: C removes ``sale`` while B is unsynced.
    Nothing is dropped (B's tenant keeps seeing ``sale``), the run exits 3 with
    ``sale`` undecidable; once B syncs, C's ownership is dropped."""
    a, b, c, rid_a, rid_b, rid_c = _three_owners(tmp_path, pg, neo4j_driver)
    _unsync(pg, b, rid_b)
    c.rm("sale")
    c.commit("[REM] sale: vendored copy dropped")
    emb_c_before = embeddings(pg, "sale", "pc_99")
    assert emb_c_before > 0, "precondition: C's profile has embeddings of sale"

    summary = run(pg, "pc_99")

    assert needs_attention(summary), "an undecidable owner drop must exit 3"
    assert any("sale" in u for u in lc(summary)["undecidable"]), lc(summary)
    node = module_node(neo4j_driver, "sale")
    assert node is not None
    assert "pb_99" in node["profile"], f"B's tenant lost sale while B was unsynced: {node}"
    assert "pa_99" in node["profile"]
    row = ledger(pg, rid_c, "sale")
    assert row["state"] == "present" and row["retire_pending"] is True, dict(row)
    assert (row["retire_blocked_by"] or "").startswith("undecidable"), row["retire_blocked_by"]
    assert embeddings(pg, "sale", "pc_99") == emb_c_before, "nothing may be dropped yet"
    assert "sale" in (repo_row(pg, rid_c)["lifecycle_attention"] or "")

    healed = run(pg, "pb_99")  # B's first ledger run: B syncs

    assert not needs_attention(healed), lc(healed)
    node = module_node(neo4j_driver, "sale")
    assert sorted(node["profile"]) == ["pa_99", "pb_99"], node
    assert ledger(pg, rid_c, "sale")["state"] == "retired"
    assert embeddings(pg, "sale", "pc_99") == 0
    assert embeddings(pg, "sale", "pb_99") > 0 and embeddings(pg, "sale", "pa_99") > 0


def test_excluding_co_owner_drop_waits_while_an_unsynced_repo_may_ship_the_module(
    pg, neo4j_driver, tmp_path,
):
    """C flips its ``sale`` to installable False while B is unsynced: C's
    ownership is not dropped yet (``excluded_owner_waiting``); after B syncs the
    drop proceeds and the node is owned by A and B only."""
    a, b, c, rid_a, rid_b, rid_c = _three_owners(tmp_path, pg, neo4j_driver)
    _unsync(pg, b, rid_b)
    write_module(c, "sale", installable=False)
    c.commit("sale: not installable here")

    summary = run(pg, "pc_99")

    report = _report(summary)
    assert "sale" in (report.get("excluded_owner_waiting") or {}), report
    assert "sale" not in (report.get("excluded_owner_dropped") or {}), report
    node = module_node(neo4j_driver, "sale")
    assert sorted(node["profile"]) == ["pa_99", "pb_99", "pc_99"], (
        f"the node must be untouched while B may still ship sale: {node}")

    healed = run(pg, "pb_99")  # B's first ledger run: B syncs

    assert "sale" not in (_report(healed).get("excluded_owner_waiting") or {})
    assert sorted(module_node(neo4j_driver, "sale")["profile"]) == ["pa_99", "pb_99"]
    assert ledger(pg, rid_c, "sale")["state"] == "excluded"


# ---------------------------------------------------------------------------
# D2 - a retirement keeps what a concurrent run wrote after it started
# ---------------------------------------------------------------------------

def test_concurrent_rewrite_during_the_cascade_keeps_the_module_and_its_fresh_children(
    pg, neo4j_driver, tmp_path, monkeypatch,
):
    """A retires ``viin_ai_rag``. While A's cascade runs (after its first race
    check, before any child is deleted) profile B's run - which does not hold
    ``retire:<v>`` - MERGEs the module and its Python model. Every node B wrote
    survives, the Module survives, and A's reconcile reports the name
    ``skipped_recent``: A's row stays pending, A's embeddings stay, nothing is
    recorded retired. Deterministic: the concurrent write runs inside a hook on
    the cascade's first child-delete statement (no sleeps)."""
    from src.indexer import writer_neo4j as wn
    from tests import _retirement_fixture as fx

    a = GitRepo(tmp_path / "a", "addons_a")
    write_module(a, "viin_ai_rag")
    write_module(a, "a_keep")
    write_module(a, "a_other")
    a.commit("a")
    (rid_a,) = register("pa_99", a)
    run(pg, "pa_99")
    a.rm("viin_ai_rag")
    a.commit("[REM] viin_ai_rag")
    b = GitRepo(tmp_path / "b", "addons_b")
    write_module(b, "viin_ai_rag")
    b.commit("b gets viin_ai_rag")
    # B's run is concurrent and only at its graph-write phase: not registered
    # here, so it is not an unsynced blocker of A's decision (that is D1).
    rid_b = 7802
    emb_a_before = embeddings(pg, "viin_ai_rag", "pa_99")
    assert emb_a_before > 0

    real = wn._run_single_with_retry
    concurrent: dict = {}

    def cascade_hook(session, name, *args, **kwargs):
        if (name == "retire_modules[LintViolation]" and not concurrent
                and "viin_ai_rag" in (kwargs.get("names") or [])):
            concurrent["fired"] = True
            w2 = wn.Neo4jWriter(*_neo4j_auth())
            try:
                with pytest.MonkeyPatch.context() as mp:
                    fx.index_repo_dir(w2, mp, b.path, profile="pb_99", repo_id=rid_b)
            finally:
                w2.close()
            with neo4j_driver.session() as s:
                concurrent["ids"] = set(s.run(
                    "MATCH (n {odoo_version: $v, module: 'viin_ai_rag'}) "
                    "WHERE NOT n:Module AND 'pb_99' IN coalesce(n.profile, []) "
                    "RETURN collect(elementId(n)) AS ids", v=V,
                ).single()["ids"])
        return real(session, name, *args, **kwargs)

    monkeypatch.setattr(wn, "_run_single_with_retry", cascade_hook)
    summary = run(pg, "pa_99")
    monkeypatch.setattr(wn, "_run_single_with_retry", real)

    assert concurrent.get("fired"), "the cascade never reached its first child delete"
    assert concurrent["ids"], "precondition: the concurrent run wrote children"
    with neo4j_driver.session() as s:
        left = set(s.run(
            "MATCH (n) WHERE elementId(n) IN $ids RETURN collect(elementId(n)) AS ids",
            ids=sorted(concurrent["ids"]),
        ).single()["ids"])
    assert left == concurrent["ids"], (
        f"{len(concurrent['ids'] - left)} node(s) the concurrent run wrote were deleted")
    assert module_node(neo4j_driver, "viin_ai_rag") is not None
    assert "viin_ai_rag" in _report(summary)["skipped_recent"], _report(summary)
    row = ledger(pg, rid_a, "viin_ai_rag")
    assert row["state"] != "retired" and row["retire_pending"] is True, dict(row)
    assert row["retire_blocked_by"] == "skipped_recent"
    assert embeddings(pg, "viin_ai_rag", "pa_99") == emb_a_before


def _neo4j_auth() -> tuple[str, str, str]:
    import os
    return (os.environ["NEO4J_TEST_URI"], os.getenv("NEO4J_TEST_USER", "neo4j"),
            os.getenv("NEO4J_TEST_PASSWORD", "password"))


# ---------------------------------------------------------------------------
# D3 (owner decision) - first run with an empty ledger: G-B on the graph baseline
# ---------------------------------------------------------------------------

_N_MODULES = 24


def _pre_ledger_repo(tmp_path: Path, pg_conn, driver, profile: str, drop: int):
    """A repo of 24 modules indexed by 0.18 (no ledger), whose checkout now lacks
    *drop* of them."""
    repo = GitRepo(tmp_path, "tvtmaaddons")
    names = [f"viin_mod_{i:02d}" for i in range(_N_MODULES)]
    for n in names:
        write_module(repo, n)
    repo.commit("24 modules")
    (rid,) = register(profile, repo)
    run(pg_conn, profile)
    gone = names[:drop]
    for n in gone:
        repo.rm(n)
    repo.commit(f"drop {drop} modules")
    run(pg_conn, profile, retire=False)  # the graph still holds every name
    _rewind_to_pre_ledger(pg_conn, repo, rid)
    for n in names:
        assert module_node(driver, n) is not None, f"precondition: {n} indexed"
    return repo, rid, names, gone


def test_first_run_trips_mass_retire_when_the_scan_covers_far_less_than_the_graph(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """21 of the 24 modules the graph attributes to a never-synced repo are not
    in its scan (> 50% and >= 20): the audit predicts the trip on the graph
    baseline, the run exits 3 with attention and sweeps nothing; the repo stays
    unsynced until an operator confirms with --allow-mass-retire."""
    import src.indexer.__main__ as cli
    from tests import conftest

    repo, rid, names, gone = _pre_ledger_repo(tmp_path, pg, neo4j_driver, "tvtma_99", drop=21)

    audit = _audit(monkeypatch, capsys, "tvtma_99")
    [entry] = audit["repos"]
    assert entry["gates"].get("baseline") == "graph", entry["gates"]
    assert "mass_retire" in entry["gates"]["tripped"], entry["gates"]
    assert entry["gates"]["n_soft_drop"] == 21 and entry["gates"]["n_present_before"] == 24

    monkeypatch.setenv("PG_DSN", conftest.get_test_dsn())
    code = cli.main(["index-repo", "--profile", "tvtma_99", "--no-embed"])

    assert code == 3
    for n in names:
        assert module_node(neo4j_driver, n) is not None, f"{n} was swept on a mass drop"
    attention = repo_row(pg, rid)["lifecycle_attention"] or ""
    assert "mass retire" in attention and "21 of 24" in attention, attention
    assert repo_row(pg, rid)["presence_head_sha"] is None, "a trip keeps the repo unsynced"

    assert cli.main(["index-repo", "--profile", "tvtma_99", "--no-embed"]) == 3, (
        "the next unconfirmed run must still refuse")
    confirmed = cli.main(
        ["index-repo", "--profile", "tvtma_99", "--no-embed", "--allow-mass-retire"])

    assert confirmed == 0
    for n in gone:
        assert module_node(neo4j_driver, n) is None, f"{n}: confirmed run must sweep it"
    for n in set(names) - set(gone):
        assert module_node(neo4j_driver, n) is not None
    assert repo_row(pg, rid)["presence_head_sha"] == repo.head()


def test_first_run_sweeps_a_few_ghosts_and_the_audit_predicts_it(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """3 of 24 graph modules are gone from the scan: no trip; the audit lists
    the 3 as orphans the sweep removes (graph baseline, nothing tripped) and the
    run removes exactly those, exit 0."""
    repo, rid, names, gone = _pre_ledger_repo(tmp_path, pg, neo4j_driver, "tvtma_99", drop=3)

    audit = _audit(monkeypatch, capsys, "tvtma_99")
    [entry] = audit["repos"]
    assert entry["gates"].get("baseline") == "graph"
    assert entry["gates"]["tripped"] == [], entry["gates"]
    [ver] = [v for v in audit["versions"] if v["odoo_version"] == V]
    assert sorted(o["name"] for o in ver["orphan_modules"]) == sorted(gone), ver

    summary = run(pg, "tvtma_99")

    assert not needs_attention(summary), lc(summary)
    for n in gone:
        assert_gone(neo4j_driver, pg, n)
        assert ledger(pg, rid, n)["retire_reason"] == "orphan_sweep"
    for n in set(names) - set(gone):
        assert module_node(neo4j_driver, n) is not None
    assert repo_row(pg, rid)["presence_head_sha"] == repo.head()
