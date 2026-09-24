# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who may still ship module M: the per-MODULE owner decision (F48) and the
entity prune under a sibling that has not synced yet or runs concurrently.

Owner decision F48 (FINDINGS 2026-09-24): decide per MODULE, not per repo. For
module M an unsynced or never-indexed sibling repo blocks M's entity prune and
M's retirement ONLY if that repo actually ships M - for a repo without any
ledger observation, if git tracks a manifest of M in its checkout. Rules
protected here (lane-idx contract 3.1, 3.7, 3.8):

- a cloned, never-indexed sibling that does NOT track M blocks neither the
  prune of M's removed entities nor the retirement of a module it lacks;
- one that tracks M holds the prune (``shared_unsynced``) without a retry and
  without operator attention; when it syncs WITHOUT owning M (installable False,
  or dropped before its first run) the survivor's M is re-armed and its next
  run prunes; when it syncs owning M, M is genuinely shared: nothing re-armed;
- a checkout git cannot read stays fail-safe: counts as an owner, attention;
- the dry-run lifecycle-audit predicts the same retire / undecidable verdict;
- a child or relationship written by ANOTHER profile's run after this run
  began is never deleted by this run's prune (28da41b).

Real case: ``viin_ai_rag`` lost entities in the viin_ai consolidation while a
customer fork, registered for another tenant and not yet indexed, sat on disk.
Every test drives the real ``index_profile`` (or the real CLI) over temp git
repos with a bare origin, against real Neo4j and PostgreSQL + pgvector.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from src.db.pg import repo_store
from tests._lifecycle_repo import (
    GitRepo,
    V,
    assert_gone,
    assert_live,
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

M = "viin_ai_rag"
NOTE = "rag_note"


@pytest.fixture
def pg(clean_pg, clean_neo4j):
    """Migrated PG (with pgvector) + clean Neo4j; the pool comes from pg_conn."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg


# ---------------------------------------------------------------------------
# Builders + observations
# ---------------------------------------------------------------------------

def _note_field(extra: str = "") -> str:
    return f"{NOTE} = fields.Text()" + (f"\n    {extra}" if extra else "")


def _owner(tmp_path: Path) -> GitRepo:
    """The owner repo: M with the field NOTE, a module to delete, two keepers."""
    a = GitRepo(tmp_path / "main", "viindoo_addons")
    write_module(a, M, extra_field=_note_field())
    write_module(a, "rag_gone")
    write_module(a, "viin_ai")
    write_module(a, "viin_ai_base")
    a.commit("add viin_ai_rag, rag_gone, viin_ai, viin_ai_base")
    return a


def _drop_note_and_rag_gone(a: GitRepo) -> None:
    write_module(a, M)  # same module, NOTE removed
    a.rm("rag_gone")
    a.commit(f"[REM] {M}: move {NOTE} out; drop rag_gone")


def _drop_note(a: GitRepo) -> None:
    write_module(a, M)
    a.commit(f"[REM] {M}: move {NOTE} out")


def _fields(driver, name: str, module: str = M) -> int:
    with driver.session() as s:
        return s.run(
            "MATCH (f:Field {name: $n, module: $m, odoo_version: $v}) RETURN count(f) AS n",
            n=name, m=module, v=V,
        ).single()["n"]


def _note_embeddings(pg_conn) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM embeddings WHERE odoo_version = %s AND module = %s "
            "AND chunk_type = 'field' AND entity_name LIKE %s",
            (V, M, f"%.{NOTE}"),
        )
        return cur.fetchone()[0]


def _attention(pg_conn, rid: int) -> str:
    return repo_row(pg_conn, rid).get("lifecycle_attention") or ""


def _fork(tmp_path: Path, *, tracks_m: bool, installable: bool = True) -> GitRepo:
    """A customer fork (another tenant), cloned but never indexed."""
    fork = GitRepo(tmp_path / "fork", "customer_fork")
    write_module(fork, "fork_only")
    write_module(fork, "fork_extra")
    if tracks_m:
        write_module(fork, M, installable=installable)
    fork.commit("fork")
    return fork


# ---------------------------------------------------------------------------
# F48 - a never-indexed sibling blocks only the modules it tracks
# ---------------------------------------------------------------------------

def test_never_indexed_sibling_not_shipping_the_module_blocks_neither_prune_nor_retirement(
    pg, neo4j_driver, tmp_path,
):
    """The fork tracks fork_only and fork_extra, never viin_ai_rag or rag_gone.
    It cannot decide anything about them, so the owner's ONE plain run prunes
    the removed field AND retires rag_gone, with no operator attention and no
    retry flag; the next run is the zero-cost skip."""
    a = _owner(tmp_path)
    fork = _fork(tmp_path, tracks_m=False)
    (rid,) = register("owner_99", a)
    register("fork_99", fork)
    run(pg, "owner_99")
    assert _fields(neo4j_driver, NOTE) == 1
    _drop_note_and_rag_gone(a)

    summary = run(pg, "owner_99")

    assert _fields(neo4j_driver, NOTE) == 0, "the removed field must leave the graph"
    assert_gone(neo4j_driver, pg, "rag_gone")
    assert ledger(pg, rid, "rag_gone")["state"] == "retired"
    assert not needs_attention(summary), lc(summary)
    assert lc(summary).get("undecidable", []) == []
    assert "customer_fork" not in _attention(pg, rid)
    assert ledger(pg, rid, M)["needs_rewrite"] is False
    assert run(pg, "owner_99")["modules"] == 0


@pytest.mark.parametrize("fork_outcome", ["excluded", "absent", "shared"])
def test_sibling_that_tracks_the_module_holds_its_prune_until_it_syncs(
    pg, neo4j_driver, tmp_path, fork_outcome,
):
    """The fork tracks viin_ai_rag, so it may still ship it: the owner's prune of
    the removed field is held - but it is not an error the operator must act on
    (no attention) and re-parsing cannot change it (no retry, the next runs do
    zero work). The fork's first run decides:

    - ``excluded`` (its copy is installable False) or ``absent`` (the fork drops
      its copy before its first run): the fork does not own viin_ai_rag, so the
      owner's copy is re-armed and its next run - with no new commit - prunes;
    - ``shared`` (installable copy): viin_ai_rag is genuinely shared; nothing is
      re-armed and one copy's parse still never prunes it (B14 rule c) - but the
      fork's copy does not define rag_note either, so once both owners recorded
      a complete parse the shared-module rule removes it (F49), graph and
      embeddings, and the owner's next run does zero work.

    Updated for F49: the ``shared`` outcome used to assert rag_note STAYS after
    the fork synced. That pinned the defect F49 fixes (an entity neither copy
    defines kept forever); rule 1 of the F49 contract says it leaves in the run
    in which the last owner re-parsed - here the fork's first run. The hold
    before the fork syncs is unchanged."""
    a = _owner(tmp_path)
    fork = _fork(tmp_path, tracks_m=True, installable=(fork_outcome != "excluded"))
    (rid,) = register("owner_99", a)
    (rid_fork,) = register("fork_99", fork)
    run(pg, "owner_99")
    _drop_note(a)

    held = run(pg, "owner_99")

    assert _fields(neo4j_driver, NOTE) == 1, "an unsynced repo tracking the module holds its prune"
    assert _note_embeddings(pg) >= 1, "a held prune keeps the embeddings too"
    assert not needs_attention(held), lc(held)
    assert "customer_fork" not in _attention(pg, rid)
    assert ledger(pg, rid, M)["needs_rewrite"] is False, "a held-for-sibling prune is not retried"
    assert run(pg, "owner_99")["modules"] == 0, "no retry while the sibling has not synced"

    if fork_outcome == "absent":
        fork.rm(M)
        fork.commit(f"[REM] {M}: not ours")
    run(pg, "fork_99")
    if fork_outcome == "excluded":
        assert ledger(pg, rid_fork, M)["state"] == "excluded"

    if fork_outcome == "shared":
        assert ledger(pg, rid_fork, M)["state"] == "present"
        assert _fields(neo4j_driver, NOTE) == 0, "neither copy defines rag_note any more"
        assert _note_embeddings(pg) == 0, "rag_note kept an embedding in some profile"
        assert ledger(pg, rid, M)["needs_rewrite"] is False
        assert run(pg, "owner_99")["modules"] == 0
        assert_live(neo4j_driver, pg, M)
        return

    assert ledger(pg, rid, M)["needs_rewrite"] is True, (
        "the sibling synced without owning the module: the held prune is re-armed"
    )
    rerun = run(pg, "owner_99")
    assert rerun["modules"] >= 1
    assert _fields(neo4j_driver, NOTE) == 0
    assert ledger(pg, rid, M)["needs_rewrite"] is False
    assert_live(neo4j_driver, pg, M)
    assert run(pg, "owner_99")["modules"] == 0


def test_unreadable_sibling_checkout_stays_a_potential_owner_with_attention(
    pg, neo4j_driver, tmp_path,
):
    """A registered sibling whose directory git cannot read (not a work tree)
    might ship anything: the owner's prune of viin_ai_rag is deferred with a
    retry flag and an operator message naming the sibling, and rag_gone is not
    retired (undecidable). Fail-safe: nothing is deleted on an unknown answer."""
    # GUARD: pre-existing behaviour (before F48 every never-indexed repo with a
    # checkout blocked; F48 must keep that for a checkout it cannot read).
    a = _owner(tmp_path)
    blind = tmp_path / "blind" / "customer_blind"
    blind.mkdir(parents=True)
    (blind / M).mkdir()
    (blind / M / "__manifest__.py").write_text(repr({"name": M, "installable": True}) + "\n")
    (rid,) = register("owner_99", a)
    pid = repo_store().add_profile("blind_99", V)
    repo_store().add_repo(pid, "file:///nowhere/customer_blind.git", V, str(blind))
    run(pg, "owner_99")
    _drop_note_and_rag_gone(a)

    summary = run(pg, "owner_99")

    assert _fields(neo4j_driver, NOTE) == 1
    assert module_node(neo4j_driver, "rag_gone") is not None
    assert any("rag_gone" in u for u in lc(summary).get("undecidable", []))
    assert needs_attention(summary)
    assert "customer_blind" in _attention(pg, rid)
    assert ledger(pg, rid, M)["needs_rewrite"] is True


# ---------------------------------------------------------------------------
# F48 - the dry-run audit takes the same per-module decision
# ---------------------------------------------------------------------------

def _audit(monkeypatch, capsys, *argv: str) -> dict:
    from src.indexer.__main__ import main
    from tests import conftest

    monkeypatch.setenv("PG_DSN", conftest.get_test_dsn())
    capsys.readouterr()
    code = main(["lifecycle-audit", *argv, "--json"])
    out = capsys.readouterr().out
    assert code == 0
    return json.loads(out)


@pytest.mark.parametrize("fork_tracks", [False, True], ids=["fork_lacks_it", "fork_tracks_it"])
def test_audit_predicts_the_per_module_owner_verdict_the_real_run_takes(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, fork_tracks,
):
    """rag_gone is deleted by the owner. The audit says would_retire when the
    never-indexed fork does not track rag_gone and undecidable when it does; the
    real run that follows does exactly that."""
    a = _owner(tmp_path)
    fork = GitRepo(tmp_path / "fork", "customer_fork")
    write_module(fork, "fork_only")
    write_module(fork, "fork_extra")
    if fork_tracks:
        write_module(fork, "rag_gone")
    fork.commit("fork")
    register("owner_99", a)
    register("fork_99", fork)
    run(pg, "owner_99")
    a.rm("rag_gone")
    a.commit("drop rag_gone")

    report = _audit(monkeypatch, capsys, "--profile", "owner_99")
    [entry] = [r for r in report["repos"] if r["basename"] == "viindoo_addons"]
    predicted_retire = [i["name"] for i in entry["would_retire"]]
    predicted_undecidable = [i["name"] for i in entry["undecidable"]]

    if fork_tracks:
        assert (predicted_retire, predicted_undecidable) == ([], ["rag_gone"])
    else:
        assert (predicted_retire, predicted_undecidable) == (["rag_gone"], [])

    summary = run(pg, "owner_99")

    retired = module_node(neo4j_driver, "rag_gone") is None
    undecidable = any("rag_gone" in u for u in lc(summary).get("undecidable", []))
    assert (retired, undecidable) == (bool(predicted_retire), bool(predicted_undecidable))


# ---------------------------------------------------------------------------
# 28da41b - a concurrent run of another profile keeps what it wrote
# ---------------------------------------------------------------------------

def _field_rels(driver, name: str, module: str = M) -> int:
    with driver.session() as s:
        return s.run(
            "MATCH (f:Field {name: $n, module: $m, odoo_version: $v})-[r]-() "
            "RETURN count(r) AS n",
            n=name, m=module, v=V,
        ).single()["n"]


def test_prune_never_deletes_what_a_concurrent_run_of_another_profile_wrote_after_it_began(
    pg, neo4j_driver, tmp_path, monkeypatch, _ephemeral_pg_db,
):
    """Night run, two profiles in parallel. The owner's run of viin_ai_rag begins
    and re-parses it without NOTE. Before its prune, the tenant repo's run -
    which just gained viin_ai_rag with an extra ``tenant_score`` field - writes
    the module's nodes and relationships and pauses before its ledger commit, so
    the ledger does not know it ships viin_ai_rag yet. The owner's prune must
    delete NOTE (written by an earlier night, not re-produced) and nothing the
    tenant's run wrote after the owner's run began: tenant_score and its
    relationships survive, and so does every node both copies define."""
    import psycopg2

    from src.indexer import pipeline_repo

    a = _owner(tmp_path)
    tenant = GitRepo(tmp_path / "tenant", "tenant_addons")
    write_module(tenant, "tenant_only")
    write_module(tenant, "tenant_extra")
    tenant.commit("tenant")
    (rid_a,) = register("owner_99", a)
    (rid_t,) = register("tenant_99", tenant)
    run(pg, "owner_99")
    run(pg, "tenant_99")
    _drop_note(a)
    write_module(tenant, M, extra_field="tenant_score = fields.Integer()")
    tenant.commit(f"[ADD] {M}: tenant copy with tenant_score")

    tenant_written = threading.Event()
    owner_done = threading.Event()
    errors: list[BaseException] = []
    seen: dict = {}
    real_prune = pipeline_repo._prune_reparsed_modules

    def barrier(repo):
        if repo["id"] == rid_t:
            seen["score_rels"] = _field_rels(neo4j_driver, "tenant_score")
            seen["label_rels"] = _field_rels(neo4j_driver, "label")
            tenant_written.set()
            assert owner_done.wait(180), "the owner's run never finished"

    def run_tenant():
        conn = psycopg2.connect(_ephemeral_pg_db)
        conn.autocommit = True
        try:
            run(conn, "tenant_99")
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            errors.append(exc)
        finally:
            tenant_written.set()
            conn.close()

    worker = threading.Thread(target=run_tenant)

    def prune_after_the_tenant_wrote(writer, presence, repo, **kw):
        if repo["id"] == rid_a and not worker.is_alive() and "started" not in seen:
            seen["started"] = True
            worker.start()
            assert tenant_written.wait(180), "the tenant run never reached its ledger commit"
        return real_prune(writer, presence, repo, **kw)

    monkeypatch.setattr(pipeline_repo, "_LIFECYCLE_TEST_BARRIER", barrier, raising=False)
    monkeypatch.setattr(pipeline_repo, "_prune_reparsed_modules", prune_after_the_tenant_wrote)
    try:
        run(pg, "owner_99")
    finally:
        owner_done.set()
        if seen.get("started"):
            worker.join(300)
    assert not errors, errors
    assert seen.get("started"), "the owner's run never reached its prune of viin_ai_rag"
    assert seen["score_rels"] > 0, (
        "precondition: the tenant run wrote tenant_score with relationships"
    )

    assert _fields(neo4j_driver, NOTE) == 0, "an entity no run re-produced is still pruned"
    assert _fields(neo4j_driver, "tenant_score") == 1, "the concurrent run's field was deleted"
    assert _field_rels(neo4j_driver, "tenant_score") == seen["score_rels"]
    assert _field_rels(neo4j_driver, "label") == seen["label_rels"]
    assert _fields(neo4j_driver, "label") == 1
    assert ledger(pg, rid_t, M)["state"] == "present"
