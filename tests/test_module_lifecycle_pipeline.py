# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module lifecycle end to end: a plain index run keeps the index equal to git (ADR-0056).

Issue #378: ``check_module_exists`` kept answering "Yes" for ``test_pylint`` @17.0
after tvtmaaddons renamed it to ``test_viin_pylint`` (commit ``0240c6b77f``), and
the ``viin_ai_*`` merge wave left five ghost modules that inflated
``impact_analysis``. The owner decision (08-approved-plan, decision 2) is that a
PLAIN index run - no flag, no cleanup script - must retire every module git no
longer ships, together with everything it wrote (children, embeddings), under
safety gates that refuse to delete on a degraded scan or a mass drop.

Every test here drives the REAL ``index_profile`` / ``index_all`` (or the real CLI
``main``) over temp git repos that each have a bare ``origin`` (the nightly job
fetches + resets to ``origin/<branch>``), against a real Neo4j and a real
PostgreSQL with pgvector. Expected values come from the business rules in the
plan (B7/B8/B9 test tables, review C1/H1/H2/H4/H5/M5/M6/L3), never from reading
what the implementation returns.

Real cases mirrored (see /tmp/osm-373-378/04 + 05):
  * T01/T02 rename ``test_pylint`` -> ``test_viin_pylint`` (tvtma ``0240c6b77f``)
  * T03 a commit that only deletes a module (tvtma ``123f51f217``)
  * T04 merge wave ``viin_ai_rag``/``viin_ai_agent``/``viin_ai_skill`` into ``viin_ai``
  * T05 ``installable`` True -> False (tvtmaaddons19 ``a057495728``)
  * T08 ``viin_meeting_room`` shipped by two repos (CE + EE profiles)
  * T09 ``to_saas_base`` moved between repos in one night (``39474061``)
"""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from src.db.pg import repo_store
from src.indexer.embedder import FakeEmbedder
from src.indexer.pipeline import index_all
from tests._lifecycle_repo import (
    GitRepo,
    V,
    assert_gone,
    assert_live,
    child_profiles,
    children,
    embeddings,
    lc,
    ledger,
    module_node,
    needs_attention,
    register,
    repo_row,
    run,
    run_git,
    write_module,
)

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def pg(clean_pg, clean_neo4j):
    """Migrated PG (with pgvector) + clean Neo4j; the pool comes from pg_conn."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg



# ---------------------------------------------------------------------------
# T01 / T02 - rename is retired by a plain incremental run and by --full
# ---------------------------------------------------------------------------

def _renamed_repo(tmp_path: Path, pg_conn) -> tuple[GitRepo, int, str]:
    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "test_pylint")
    write_module(repo, "viin_ai")
    repo.commit("add test_pylint and viin_ai")
    (rid,) = register("tvtma_99", repo)
    run(pg_conn, "tvtma_99")
    repo.mv("test_pylint", "test_viin_pylint")
    sha = repo.commit("[REF] test_pylint: rename to test_viin_pylint")
    return repo, rid, sha


@pytest.mark.parametrize("full_reindex", [False, True], ids=["incremental", "full"])
def test_renamed_module_is_retired_by_a_plain_run_with_rename_evidence(
    pg, neo4j_driver, tmp_path, full_reindex,
):
    """T01 (incremental) / T02 (--full): the old name, its children and its
    embeddings are gone after ONE run with no lifecycle flag; the new name is
    indexed; the ledger keeps the removing commit and the successor."""
    repo, rid, sha = _renamed_repo(tmp_path, pg)

    summary = run(pg, "tvtma_99", full_reindex=full_reindex)

    assert_gone(neo4j_driver, pg, "test_pylint")
    assert_live(neo4j_driver, pg, "test_viin_pylint")
    assert_live(neo4j_driver, pg, "viin_ai")
    row = ledger(pg, rid, "test_pylint")
    assert row["state"] == "retired" and row["retire_reason"] == "absent"
    assert row["retire_pending"] is False
    assert row["removing_commit_sha"] == sha
    assert row["removing_commit_subject"] == "[REF] test_pylint: rename to test_viin_pylint"
    assert row["successor_names"] == ["test_viin_pylint"]
    assert row["successor_source"] == "git_rename"
    assert ledger(pg, rid, "test_viin_pylint")["state"] == "present"
    assert not needs_attention(summary), lc(summary)
    assert repo_row(pg, rid)["presence_head_sha"] == repo.head()


# GUARD: pre-existing behaviour (ADR-0007 zero-cost skip, kept after a retirement)
def test_next_run_after_a_retirement_is_a_zero_cost_skip(pg, neo4j_driver, tmp_path):
    """Once the ledger reflects HEAD, the next night re-parses nothing (ADR-0007)."""
    repo, rid, _sha = _renamed_repo(tmp_path, pg)
    run(pg, "tvtma_99")

    embedder = FakeEmbedder(dim=1024)
    again = run(pg, "tvtma_99", embedder=embedder)

    assert again["modules"] == 0
    assert embedder.call_count == 0
    assert not needs_attention(again)


# ---------------------------------------------------------------------------
# New total-wipe cases (owner decision 2 + G-B total_wipe redefinition)
# ---------------------------------------------------------------------------

def test_renaming_the_only_module_of_a_repo_is_cleaned_by_a_plain_run(
    pg, neo4j_driver, tmp_path,
):
    """A one-module repo renames its module: 1 of 1 old names drops but the scan
    still has a present module, so this is a rename, not a wipe - retired with
    no flag and no exit 3."""
    repo = GitRepo(tmp_path, "solo_addons")
    write_module(repo, "test_pylint")
    repo.commit("add test_pylint")
    (rid,) = register("solo_99", repo)
    run(pg, "solo_99")
    repo.mv("test_pylint", "test_viin_pylint")
    repo.commit("rename test_pylint to test_viin_pylint")

    summary = run(pg, "solo_99")

    assert not needs_attention(summary), lc(summary)
    assert_gone(neo4j_driver, pg, "test_pylint")
    assert_live(neo4j_driver, pg, "test_viin_pylint")
    assert ledger(pg, rid, "test_pylint")["state"] == "retired"


def _wiped_repo(tmp_path: Path, pg_conn) -> tuple[GitRepo, int]:
    repo = GitRepo(tmp_path, "wiped_addons")
    write_module(repo, "wipe_a")
    write_module(repo, "wipe_b")
    repo.commit("add two modules")
    (rid,) = register("wipe_99", repo)
    run(pg_conn, "wipe_99")
    repo.rm("wipe_a")
    repo.rm("wipe_b")
    repo.commit("remove every module")
    return repo, rid


def test_a_scan_with_no_present_module_left_trips_and_deletes_nothing(
    pg, neo4j_driver, tmp_path,
):
    """Every module of a repo vanishes (a botched branch, a wrong checkout): the
    total-wipe gate trips, nothing is deleted, the operator is told, the ledger
    head is held so the next run re-evaluates."""
    repo, rid = _wiped_repo(tmp_path, pg)

    summary = run(pg, "wipe_99")

    assert needs_attention(summary)
    assert any("total_wipe" in g for g in lc(summary).get("gates_tripped", [])), (
        lc(summary)
    )
    assert_live(neo4j_driver, pg, "wipe_a")
    assert_live(neo4j_driver, pg, "wipe_b")
    row = repo_row(pg, rid)
    assert row["lifecycle_attention"], "the tripped gate must reach repos.lifecycle_attention"
    assert row["presence_head_sha"] != repo.head(), "H1: presence must not advance on a trip"
    assert ledger(pg, rid, "wipe_a")["state"] == "present"

    rerun = run(pg, "wipe_99")
    assert needs_attention(rerun), "a sync-path rerun must still refuse the wipe"
    assert module_node(neo4j_driver, "wipe_a") is not None


def test_allow_mass_retire_lets_an_operator_confirm_a_total_wipe(pg, neo4j_driver, tmp_path):
    repo, rid = _wiped_repo(tmp_path, pg)
    run(pg, "wipe_99")  # trips

    confirmed = run(pg, "wipe_99", allow_mass_retire=True)

    assert not needs_attention(confirmed), lc(confirmed)
    assert_gone(neo4j_driver, pg, "wipe_a")
    assert_gone(neo4j_driver, pg, "wipe_b")
    assert ledger(pg, rid, "wipe_a")["state"] == "retired"
    after = run(pg, "wipe_99")
    assert not needs_attention(after)
    assert module_node(neo4j_driver, "wipe_a") is None


# ---------------------------------------------------------------------------
# T03 - a commit that only deletes a module
# ---------------------------------------------------------------------------

def test_delete_only_commit_retires_the_module_with_its_removing_commit(
    pg, neo4j_driver, tmp_path,
):
    repo = GitRepo(tmp_path, "tvtmaaddons")
    for name in ("to_attendance_device", "viin_hr", "viin_project"):
        write_module(repo, name)
    repo.commit("add three modules")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    repo.rm("to_attendance_device")
    sha = repo.commit("[REM] to_attendance_device: dropped")

    summary = run(pg, "tvtma_99")

    assert not needs_attention(summary), lc(summary)
    assert_gone(neo4j_driver, pg, "to_attendance_device")
    assert_live(neo4j_driver, pg, "viin_hr")
    assert_live(neo4j_driver, pg, "viin_project")
    row = ledger(pg, rid, "to_attendance_device")
    assert (row["state"], row["retire_reason"]) == ("retired", "absent")
    assert row["removing_commit_sha"] == sha
    assert row["successor_names"] is None and row["successor_source"] is None


# ---------------------------------------------------------------------------
# T04 - merge wave: no ghost reaches impact_analysis
# ---------------------------------------------------------------------------

def _impact(model: str) -> str:
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _impact_analysis
    return _impact_analysis("model", model, odoo_version=V)


def test_merge_wave_leaves_no_ghost_in_impact_analysis(pg, neo4j_driver, tmp_path, monkeypatch):
    """viin_ai_rag / viin_ai_agent / viin_ai_skill are merged into viin_ai in one
    commit (viin_ai changes, the three are deleted): after the plain run the
    three are gone everywhere and impact_analysis on the survivor model names
    none of them."""
    monkeypatch.setenv("NEO4J_URI", os.environ["NEO4J_TEST_URI"])
    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "viin_ai", model="viin.ai.embedding")
    for name in ("viin_ai_rag", "viin_ai_agent", "viin_ai_skill"):
        write_module(repo, name, depends=["viin_ai"], inherit="viin.ai.embedding",
                     extra_field=f"{name}_flag = fields.Boolean()")
    write_module(repo, "viin_hr")
    repo.commit("viin_ai family")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    before = _impact("viin.ai.embedding")
    assert "viin_ai_rag" in before, "precondition: the extenders are visible before the wave"

    write_module(repo, "viin_ai", model="viin.ai.embedding",
                 extra_field="rag_flag = fields.Boolean()")
    for name in ("viin_ai_rag", "viin_ai_agent", "viin_ai_skill"):
        repo.rm(name)
    repo.commit("[MERGE] viin_ai_rag, viin_ai_agent, viin_ai_skill into viin_ai")

    summary = run(pg, "tvtma_99")

    assert not needs_attention(summary), lc(summary)
    for name in ("viin_ai_rag", "viin_ai_agent", "viin_ai_skill"):
        assert_gone(neo4j_driver, pg, name)
        assert ledger(pg, rid, name)["state"] == "retired"
    assert_live(neo4j_driver, pg, "viin_ai")
    assert_live(neo4j_driver, pg, "viin_hr")
    after = _impact("viin.ai.embedding")
    for name in ("viin_ai_rag", "viin_ai_agent", "viin_ai_skill"):
        assert name not in after, f"ghost {name} still inflates impact_analysis:\n{after}"


# ---------------------------------------------------------------------------
# T05 - installable True -> False
# ---------------------------------------------------------------------------

def test_installable_false_excludes_modules_without_tripping_the_gate(
    pg, neo4j_driver, tmp_path,
):
    """Flipping installable to False is a positive observation, not a wipe: even
    when every module of the repo flips, no gate trips, the modules leave the
    index and the ledger records why."""
    repo = GitRepo(tmp_path, "tvtmaaddons19")
    write_module(repo, "viin_legacy_a")
    write_module(repo, "viin_legacy_b")
    repo.commit("add modules")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    write_module(repo, "viin_legacy_a", installable=False)
    write_module(repo, "viin_legacy_b", installable=False)
    repo.commit("[MIG] not ported yet: installable False")

    summary = run(pg, "tvtma_99")

    assert not needs_attention(summary), lc(summary)
    for name in ("viin_legacy_a", "viin_legacy_b"):
        row = ledger(pg, rid, name)
        assert (row["state"], row["exclusion_reason"]) == ("excluded", "installable_false")
        assert_gone(neo4j_driver, pg, name)


# ---------------------------------------------------------------------------
# T06 - resurrection / backport
# ---------------------------------------------------------------------------

def test_a_retired_module_that_comes_back_is_indexed_again(pg, neo4j_driver, tmp_path):
    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "viin_helpdesk")
    write_module(repo, "viin_hr")
    repo.commit("add")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    repo.rm("viin_helpdesk")
    repo.commit("remove viin_helpdesk")
    run(pg, "tvtma_99")
    assert module_node(neo4j_driver, "viin_helpdesk") is None

    write_module(repo, "viin_helpdesk")
    repo.commit("[BACKPORT] bring viin_helpdesk back")
    summary = run(pg, "tvtma_99")

    assert not needs_attention(summary)
    assert_live(neo4j_driver, pg, "viin_helpdesk")
    row = ledger(pg, rid, "viin_helpdesk")
    assert row["state"] == "present" and row["retire_pending"] is False
    assert row["resurrection_count"] >= 1


# ---------------------------------------------------------------------------
# T07 / T08 - two repos, same name / same basename
# ---------------------------------------------------------------------------

def test_same_repo_basename_in_two_profiles_never_cross_deletes(pg, neo4j_driver, tmp_path):
    """Two tenants each register a checkout named ``addons``; tenant A dropping
    modules must never delete what tenant B still ships."""
    repo_a = GitRepo(tmp_path / "tenant_a", "addons")
    repo_b = GitRepo(tmp_path / "tenant_b", "addons")
    for name in ("shared_mod", "only_a", "keep_a"):
        write_module(repo_a, name)
    for name in ("shared_mod", "only_b"):
        write_module(repo_b, name)
    repo_a.commit("a")
    repo_b.commit("b")
    (rid_a,) = register("tenant_a_99", repo_a)
    (rid_b,) = register("tenant_b_99", repo_b)
    run(pg, "tenant_b_99")
    run(pg, "tenant_a_99")
    repo_a.rm("shared_mod")
    repo_a.rm("only_a")
    repo_a.commit("drop two modules")

    summary = run(pg, "tenant_a_99")

    assert not needs_attention(summary), lc(summary)
    assert_gone(neo4j_driver, pg, "only_a")
    assert_live(neo4j_driver, pg, "only_b")
    assert_live(neo4j_driver, pg, "keep_a")
    node = module_node(neo4j_driver, "shared_mod")
    assert node is not None and node["profile"] == ["tenant_b_99"]
    assert child_profiles(neo4j_driver, "shared_mod") == {("tenant_b_99",)}
    assert embeddings(pg, "shared_mod", "tenant_a_99") == 0
    assert embeddings(pg, "shared_mod", "tenant_b_99") > 0
    assert ledger(pg, rid_a, "shared_mod")["state"] == "retired"
    assert ledger(pg, rid_b, "shared_mod")["state"] == "present"


def test_shared_module_retired_in_one_repo_is_rewritten_by_the_surviving_owner(
    pg, neo4j_driver, tmp_path,
):
    """viin_meeting_room is shipped by a CE-profile repo (LGPL-3) and an EE-profile
    repo (OPL-1). The CE copy was written last, so the node carries its license.
    When CE drops it, the node stays, owned by EE only, and EE's next run
    rewrites it so the node carries EE's own attributes (M5); the run after
    that skips again."""
    ce = GitRepo(tmp_path / "ce", "viindoo_ce")
    ee = GitRepo(tmp_path / "ee", "tvtmaaddons")
    write_module(ce, "viin_meeting_room", license="LGPL-3")
    write_module(ce, "viin_hr")
    write_module(ee, "viin_meeting_room", license="OPL-1", author="Viindoo")
    write_module(ee, "viin_ee_only")
    ce.commit("ce")
    ee.commit("ee")
    (rid_ce,) = register("ce_99", ce)
    (rid_ee,) = register("ee_99", ee)
    run(pg, "ee_99")
    run(pg, "ce_99")
    assert module_node(neo4j_driver, "viin_meeting_room")["license"] == "LGPL-3"

    ce.rm("viin_meeting_room")
    ce.commit("viin_meeting_room moves to the EE repo only")
    run(pg, "ce_99")

    node = module_node(neo4j_driver, "viin_meeting_room")
    assert node is not None and node["profile"] == ["ee_99"]
    assert child_profiles(neo4j_driver, "viin_meeting_room") == {("ee_99",)}
    assert ledger(pg, rid_ce, "viin_meeting_room")["state"] == "retired"
    assert ledger(pg, rid_ee, "viin_meeting_room")["needs_rewrite"] is True

    rewrite = run(pg, "ee_99")
    assert rewrite["modules"] == 1, "the survivor must re-write the shared module, not skip"
    assert module_node(neo4j_driver, "viin_meeting_room")["license"] == "OPL-1"
    assert ledger(pg, rid_ee, "viin_meeting_room")["needs_rewrite"] is False
    assert run(pg, "ee_99")["modules"] == 0


# ---------------------------------------------------------------------------
# T09 - cross-repo move in one night, B paused between MERGE and ledger commit
# ---------------------------------------------------------------------------

def _moved_setup(tmp_path: Path, pg_conn):
    a = GitRepo(tmp_path / "a", "viindoo_saas")
    b = GitRepo(tmp_path / "b", "tvtmaaddons")
    write_module(a, "to_saas_base")
    write_module(a, "saas_keep")
    write_module(a, "saas_core")
    write_module(b, "viin_b_keep")
    a.commit("a")
    b.commit("b")
    (rid_a,) = register("saas_99", a)
    (rid_b,) = register("tvtma_99", b)
    run(pg_conn, "saas_99")
    run(pg_conn, "tvtma_99")
    a.rm("to_saas_base")
    a.commit("[MOV] to_saas_base to tvtmaaddons")
    write_module(b, "to_saas_base")
    b.commit("[MOV] to_saas_base from viindoo_saas")
    return a, b, rid_a, rid_b


def _open_conn(dsn: str):
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


@pytest.mark.parametrize("a_started", ["before_b_merge", "after_b_merge"])
def test_module_moved_between_repos_in_one_night_survives_a_concurrent_retirement(
    pg, neo4j_driver, tmp_path, monkeypatch, _ephemeral_pg_db, a_started,
):
    """H2. Night run: repo B (new owner) has MERGEd to_saas_base but not yet
    committed its ledger row when repo A's run (old owner) reconciles its
    removal. Whatever the interleaving, the node must end up owned by B once
    B's own run (and, when A deleted it in the window, B's next run) is done;
    A's row ends retired."""
    from src.indexer import pipeline_repo
    from src.indexer.writer_neo4j import Neo4jWriter

    _a, b, rid_a, rid_b = _moved_setup(tmp_path, pg)
    b_merged = threading.Event()
    a_done = threading.Event()
    b_errors: list[BaseException] = []

    def barrier(repo):
        if repo["id"] == rid_b:
            b_merged.set()
            assert a_done.wait(120), "A's run never finished"

    monkeypatch.setattr(pipeline_repo, "_LIFECYCLE_TEST_BARRIER", barrier, raising=False)
    clock = Neo4jWriter(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"],
                        os.environ["NEO4J_PASSWORD"])
    try:
        a_start = clock.server_now()
    finally:
        clock.close()
    b_conn = _open_conn(_ephemeral_pg_db)

    def run_b():
        try:
            run(b_conn, "tvtma_99")
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            b_errors.append(exc)
        finally:
            b_merged.set()

    worker = threading.Thread(target=run_b)
    worker.start()
    try:
        assert b_merged.wait(120), "B never reached its ledger commit"
        kw = {"run_started_at": a_start} if a_started == "before_b_merge" else {}
        run(pg, "saas_99", **kw)
    finally:
        a_done.set()
        worker.join(180)
        b_conn.close()
    assert not b_errors, b_errors
    monkeypatch.setattr(pipeline_repo, "_LIFECYCLE_TEST_BARRIER", None, raising=False)

    run(pg, "tvtma_99")  # B's next night (self-heal if A's window removed the node)
    run(pg, "saas_99")

    node = module_node(neo4j_driver, "to_saas_base")
    assert node is not None, "the moved module must survive the night"
    assert node["profile"] == ["tvtma_99"]
    assert children(neo4j_driver, "to_saas_base") > 0
    assert embeddings(pg, "to_saas_base", "tvtma_99") > 0
    assert ledger(pg, rid_b, "to_saas_base")["state"] == "present"
    assert ledger(pg, rid_a, "to_saas_base")["state"] == "retired"


# ---------------------------------------------------------------------------
# T11 + C1/H1 - degraded scans retire nothing; the fixed run retires
# ---------------------------------------------------------------------------

def test_incomplete_scan_retires_nothing_and_the_fixed_run_retires(pg, neo4j_driver, tmp_path):
    """A committed deletion arrives while another tracked module is missing on
    disk (half checkout): the scan is incomplete, so nothing is retired - not
    even the genuine deletion - presence is held, and exit 3 is signalled.
    Once the checkout is whole again, the next run retires the deletion."""
    repo = GitRepo(tmp_path, "tvtmaaddons")
    for name in ("viin_gone", "viin_hr", "viin_project"):
        write_module(repo, name)
    repo.commit("add")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    repo.rm("viin_gone")
    repo.commit("remove viin_gone")
    shutil.rmtree(repo.path / "viin_project")  # tracked, missing on disk

    degraded = run(pg, "tvtma_99", refresh=False)

    assert needs_attention(degraded)
    assert any("scan_incomplete" in g for g in lc(degraded).get("gates_tripped", []))
    assert module_node(neo4j_driver, "viin_gone") is not None
    assert module_node(neo4j_driver, "viin_project") is not None
    assert repo_row(pg, rid)["presence_head_sha"] != repo.head()
    assert repo_row(pg, rid)["lifecycle_attention"]
    assert ledger(pg, rid, "viin_gone")["state"] != "retired"

    run_git(repo.path, "checkout", "--", "viin_project")
    fixed = run(pg, "tvtma_99", refresh=False)

    assert not needs_attention(fixed), lc(fixed)
    assert_gone(neo4j_driver, pg, "viin_gone")
    assert_live(neo4j_driver, pg, "viin_project")
    assert ledger(pg, rid, "viin_gone")["state"] == "retired"
    assert repo_row(pg, rid)["presence_head_sha"] == repo.head()


def test_untrusted_checkout_retires_nothing(pg, neo4j_driver, tmp_path):
    """HEAD is not origin/<branch> (a failed refresh left local commits): the scan
    cannot be trusted, so a module missing from it is not retired."""
    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "viin_gone")
    write_module(repo, "viin_hr")
    repo.commit("add")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    repo.rm("viin_gone")
    repo.commit("local only: remove viin_gone", push=False)

    summary = run(pg, "tvtma_99", refresh=False)

    assert needs_attention(summary)
    assert any("scan_untrusted" in g for g in lc(summary).get("gates_tripped", []))
    assert_live(neo4j_driver, pg, "viin_gone")
    assert ledger(pg, rid, "viin_gone")["state"] == "present"


# ---------------------------------------------------------------------------
# C1 - crash between delete and ledger commit; --no-retire
# ---------------------------------------------------------------------------

def test_crash_between_delete_and_ledger_commit_is_completed_by_the_rerun(
    pg, neo4j_driver, tmp_path, monkeypatch,
):
    """C1: the ledger says retired only after the delete succeeded, and a crash in
    between leaves the row pending so a rerun (same HEAD) finishes the job."""
    from src.db.module_presence import ModulePresenceStore

    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "viin_gone")
    write_module(repo, "viin_hr")
    repo.commit("add")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    repo.rm("viin_gone")
    repo.commit("remove viin_gone")

    real_commit = ModulePresenceStore.commit_retired
    calls = {"n": 0}

    def crash_once(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash after the graph delete")
        return real_commit(self, *a, **kw)

    monkeypatch.setattr(ModulePresenceStore, "commit_retired", crash_once)
    crashed = run(pg, "tvtma_99")

    assert calls["n"] >= 1, "precondition: the retirement reached the ledger commit"
    assert needs_attention(crashed)
    row = ledger(pg, rid, "viin_gone")
    assert row["state"] == "present" and row["retire_pending"] is True
    assert repo_row(pg, rid)["presence_head_sha"] != repo.head()

    rerun = run(pg, "tvtma_99")

    assert not needs_attention(rerun), lc(rerun)
    assert_gone(neo4j_driver, pg, "viin_gone")
    row = ledger(pg, rid, "viin_gone")
    assert row["state"] == "retired" and row["retire_pending"] is False
    assert row["removing_commit_sha"] == repo.head()
    assert repo_row(pg, rid)["presence_head_sha"] == repo.head()


def test_no_retire_keeps_the_module_pending_until_a_plain_run(pg, neo4j_driver, tmp_path):
    """--no-retire is the one escape hatch: it scans and writes but deletes
    nothing; the next plain run retires what it left pending."""
    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "viin_gone")
    write_module(repo, "viin_hr")
    repo.commit("add")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    repo.rm("viin_gone")
    repo.commit("remove viin_gone")

    held = run(pg, "tvtma_99", retire=False)

    assert_live(neo4j_driver, pg, "viin_gone")
    row = ledger(pg, rid, "viin_gone")
    assert row["state"] == "present" and row["retire_pending"] is True
    assert row["retire_blocked_by"] == "no_retire"
    assert row["removing_commit_sha"] == repo.head(), "evidence is kept for the later retire"
    assert not needs_attention(held), "an operator's --no-retire is not an alarm"

    plain = run(pg, "tvtma_99")

    assert_gone(neo4j_driver, pg, "viin_gone")
    assert ledger(pg, rid, "viin_gone")["state"] == "retired"
    assert not needs_attention(plain)


# ---------------------------------------------------------------------------
# T14 - force-push dropping a module
# ---------------------------------------------------------------------------

def test_force_push_that_drops_a_module_retires_it(pg, neo4j_driver, tmp_path):
    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "viin_hr")
    write_module(repo, "viin_project")
    keep_sha = repo.commit("add two")
    write_module(repo, "viin_experimental")
    repo.commit("add experimental")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    run_git(repo.path, "reset", "--hard", keep_sha)
    (repo.path / "README.md").write_text("rewritten history\n")
    repo.commit("rewrite: drop experimental", force=True)

    summary = run(pg, "tvtma_99")

    assert not needs_attention(summary), lc(summary)
    assert_gone(neo4j_driver, pg, "viin_experimental")
    assert_live(neo4j_driver, pg, "viin_hr")
    assert ledger(pg, rid, "viin_experimental")["state"] == "retired"


# ---------------------------------------------------------------------------
# T20 - dependents of a retired module are re-indexed
# ---------------------------------------------------------------------------

def test_dependents_of_a_retired_module_get_their_head_reset(pg, neo4j_driver, tmp_path):
    """Repo D ships a module depending on lib_mod from repo L; when L drops
    lib_mod, D's head_sha is reset so D's next run re-evaluates its edges."""
    lib = GitRepo(tmp_path / "l", "lib_addons")
    dep = GitRepo(tmp_path / "d", "dep_addons")
    write_module(lib, "lib_mod")
    write_module(lib, "lib_keep")
    write_module(dep, "dep_mod", depends=["lib_mod"])
    lib.commit("lib")
    dep.commit("dep")
    rid_lib, rid_dep = register("stack_99", lib, dep)
    run(pg, "stack_99")
    assert repo_store().get_repo_head_sha(rid_dep) == dep.head()
    lib.rm("lib_mod")
    lib.commit("drop lib_mod")

    run(pg, "stack_99")

    assert module_node(neo4j_driver, "lib_mod") is None
    assert repo_store().get_repo_head_sha(rid_dep) is None, (
        "the dependent repo must be re-indexed after its dependency retired"
    )
    assert repo_store().get_repo_head_sha(rid_lib) == lib.head()
    assert run(pg, "stack_99")["modules"] >= 1


# ---------------------------------------------------------------------------
# T22 - first run after deploy: pre-ledger ghosts
# ---------------------------------------------------------------------------

def test_first_run_after_deploy_cleans_pre_ledger_ghosts_then_skips(
    pg, neo4j_driver, tmp_path, caplog,
):
    """0.18 left ghost modules in the graph and had no ledger. The first plain
    run after deploy takes the sync path (HEAD already indexed, nothing
    re-parsed), sweeps the ghost with its children and embeddings and records
    how it left; the run after that is a zero-cost skip."""
    import logging

    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "test_pylint")
    write_module(repo, "viin_hr")
    write_module(repo, "viin_project")
    repo.commit("add")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")
    repo.mv("test_pylint", "test_viin_pylint")
    rename_sha = repo.commit("rename test_pylint to test_viin_pylint")
    run(pg, "tvtma_99", retire=False)  # graph now holds both names
    # Rewind to the 0.18 state: head indexed, no ledger, no presence head.
    with pg.cursor() as cur:
        cur.execute("DELETE FROM module_presence")
        cur.execute(
            "UPDATE repos SET presence_head_sha = NULL, lifecycle_attention = NULL, "
            "head_sha = %s WHERE id = %s",
            (repo.head(), rid),
        )
    assert module_node(neo4j_driver, "test_pylint") is not None

    embedder = FakeEmbedder(dim=1024)
    first = run(pg, "tvtma_99", embedder=embedder)

    assert first["modules"] == 0, "sync path: nothing is re-parsed"
    assert embedder.call_count == 0
    assert not needs_attention(first), lc(first)
    assert_gone(neo4j_driver, pg, "test_pylint")
    for name in ("test_viin_pylint", "viin_hr", "viin_project"):
        assert_live(neo4j_driver, pg, name)
        assert ledger(pg, rid, name)["state"] == "present"
    ghost = ledger(pg, rid, "test_pylint")
    assert (ghost["state"], ghost["retire_reason"]) == ("retired", "orphan_sweep")
    assert ghost["removing_commit_sha"] == rename_sha
    assert ghost["successor_names"] == ["test_viin_pylint"]
    assert repo_row(pg, rid)["presence_head_sha"] == repo.head()

    with caplog.at_level(logging.INFO, logger="src.indexer"):
        second = run(pg, "tvtma_99")
    assert second["modules"] == 0
    assert "skipping reindex" in caplog.text


# ---------------------------------------------------------------------------
# H5 - per-name undecidability
# ---------------------------------------------------------------------------

def test_unsynced_repo_blocks_only_the_names_it_could_own(pg, neo4j_driver, tmp_path):
    """Tenant X's repo last saw shared_mod as excluded (installable False) and its
    latest run was degraded, so X might ship shared_mod again at HEAD: tenant Y
    dropping shared_mod is undecidable (kept, attention, exit 3) while Y's
    unrelated y_gone is retired in the same run. When X syncs, shared_mod is
    retired by whichever run reconciles next."""
    x = GitRepo(tmp_path / "x", "x_addons")
    y = GitRepo(tmp_path / "y", "y_addons")
    write_module(x, "x_keep")
    write_module(x, "x_other")
    write_module(x, "shared_mod", installable=False)
    write_module(y, "shared_mod")
    write_module(y, "y_gone")
    write_module(y, "y_keep")
    x.commit("x")
    y.commit("y")
    (rid_x,) = register("tenant_x_99", x)
    (rid_y,) = register("tenant_y_99", y)
    run(pg, "tenant_x_99")
    run(pg, "tenant_y_99")
    assert ledger(pg, rid_x, "shared_mod")["state"] == "excluded"
    # X's next night is degraded: new commit, half checkout -> X stays unsynced.
    write_module(x, "x_keep", extra_field="note = fields.Text()")
    x.commit("x: touch x_keep")
    shutil.rmtree(x.path / "x_other")
    assert needs_attention(run(pg, "tenant_x_99", refresh=False))
    y.rm("shared_mod")
    y.rm("y_gone")
    y.commit("y: drop shared_mod and y_gone")

    summary = run(pg, "tenant_y_99")

    assert_gone(neo4j_driver, pg, "y_gone")
    assert ledger(pg, rid_y, "y_gone")["state"] == "retired"
    assert module_node(neo4j_driver, "shared_mod") is not None
    assert needs_attention(summary)
    assert any("shared_mod" in u for u in lc(summary).get("undecidable", []))
    assert not any("y_gone" in u for u in lc(summary).get("undecidable", []))
    row = ledger(pg, rid_y, "shared_mod")
    assert row["retire_pending"] is True and row["state"] == "present"
    assert "undecidable" in (row["retire_blocked_by"] or "")
    assert "shared_mod" in (repo_row(pg, rid_y)["lifecycle_attention"] or "")

    run_git(x.path, "checkout", "--", "x_other")
    run(pg, "tenant_x_99", refresh=False)

    assert_gone(neo4j_driver, pg, "shared_mod")
    assert ledger(pg, rid_y, "shared_mod")["state"] == "retired"


@pytest.mark.parametrize(
    "z_checkout", ["never_cloned", "cloned_ships_it", "cloned_without_it"],
)
def test_never_synced_repo_blocks_retirement_only_when_its_checkout_ships_the_name(
    pg, neo4j_driver, tmp_path, z_checkout,
):
    """A repo registered for another tenant but never indexed could ship
    y_gone - but only if a checkout exists AND git tracks a y_gone manifest in
    it (owner decision F48: per MODULE, not per repo). A never-cloned
    registration, or a clone that does not track y_gone, cannot hold y_gone's
    retirement hostage.

    Rewritten for F48: the old ``cloned`` case gave z a checkout tracking only
    ``z_mod`` and expected it to block y_gone. Under the per-module rule that
    clone no longer blocks (``cloned_without_it``, retired); the blocking intent
    is kept by ``cloned_ships_it``, whose checkout tracks y_gone."""
    y = GitRepo(tmp_path / "y", "y_addons")
    write_module(y, "y_gone")
    write_module(y, "y_keep")
    y.commit("y")
    register("tenant_y_99", y)
    run(pg, "tenant_y_99")
    z_path = tmp_path / "z" / "z_addons"
    if z_checkout != "never_cloned":
        z = GitRepo(tmp_path / "z", "z_addons")
        write_module(z, "z_mod")
        if z_checkout == "cloned_ships_it":
            write_module(z, "y_gone")
        z.commit("z")
    pid = repo_store().add_profile("tenant_z_99", V)
    repo_store().add_repo(pid, "file:///nowhere/z.git", V, str(z_path))
    y.rm("y_gone")
    y.commit("drop y_gone")

    summary = run(pg, "tenant_y_99")

    if z_checkout == "cloned_ships_it":
        assert module_node(neo4j_driver, "y_gone") is not None
        assert any("y_gone" in u for u in lc(summary).get("undecidable", []))
        assert needs_attention(summary)
    else:
        assert_gone(neo4j_driver, pg, "y_gone")
        assert not needs_attention(summary), lc(summary)


# ---------------------------------------------------------------------------
# M6 - legacy residue sweep; L3 - INHERITS re-link
# ---------------------------------------------------------------------------

def test_module_less_children_and_embeddings_are_swept_framework_helpers_kept(
    pg, neo4j_driver, tmp_path,
):
    """A legacy Module-only --gc stranded a module's children and embeddings; the
    first synced run sweeps them, and never touches '@framework' helpers."""
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "viin_hr")
    write_module(repo, "viin_project")
    repo.commit("add")
    register("tvtma_99", repo)
    run(pg, "tvtma_99")
    with neo4j_driver.session() as s:
        s.run(
            "CREATE (:Model {name: 'legacy.ghost', module: 'legacy_ghost', odoo_version: $v, "
            " profile: ['tvtma_99']})"
            "-[:HAS_FIELD]->(:Field {name: 'x', model: 'legacy.ghost', "
            " module: 'legacy_ghost', odoo_version: $v, profile: ['tvtma_99']})",
            v=V,
        )
        framework_before = s.run(
            "MATCH (t:TestHelper {module: '@framework', odoo_version: $v}) RETURN count(t) AS n",
            v=V,
        ).single()["n"]
    assert framework_before > 0, "precondition: the pipeline seeds framework helpers"
    write_module_embeddings(
        "legacy_ghost", V,
        [EmbeddingChunk("field", "legacy_ghost", V, "legacy.ghost", "legacy.ghost",
                    "legacy_ghost/models/x.py", 0, "class Ghost: pass")],
        FakeEmbedder(dim=1024), profile_name="tvtma_99",
    )
    assert embeddings(pg, "legacy_ghost") == 1
    write_module(repo, "viin_hr", extra_field="note = fields.Text()")
    repo.commit("touch viin_hr")

    summary = run(pg, "tvtma_99")

    assert not needs_attention(summary), lc(summary)
    assert children(neo4j_driver, "legacy_ghost") == 0
    assert embeddings(pg, "legacy_ghost") == 0
    with neo4j_driver.session() as s:
        framework_after = s.run(
            "MATCH (t:TestHelper {module: '@framework', odoo_version: $v}) RETURN count(t) AS n",
            v=V,
        ).single()["n"]
    assert framework_after == framework_before
    assert_live(neo4j_driver, pg, "viin_hr")


def test_extender_is_relinked_to_the_new_definition_after_the_old_one_retires(
    pg, neo4j_driver, tmp_path,
):
    """L3: viin_ai_rag defined viin.ai.embedding and viin_ai_ext extended it; the
    merge moves the definition into viin_ai and deletes viin_ai_rag. The
    unchanged extender must end with an INHERITS edge to viin_ai's definition."""
    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "viin_ai")
    write_module(repo, "viin_ai_rag", depends=["viin_ai"], model="viin.ai.embedding")
    write_module(repo, "viin_ai_ext", depends=["viin_ai_rag"], inherit="viin.ai.embedding",
                 extra_field="ext_flag = fields.Boolean()")
    repo.commit("add")
    register("tvtma_99", repo)
    run(pg, "tvtma_99")
    write_module(repo, "viin_ai", model="viin.ai.embedding")
    repo.rm("viin_ai_rag")
    repo.commit("[MERGE] viin_ai_rag into viin_ai")

    run(pg, "tvtma_99")

    assert module_node(neo4j_driver, "viin_ai_rag") is None
    with neo4j_driver.session() as s:
        targets = s.run(
            "MATCH (e:Model {name: 'viin.ai.embedding', module: 'viin_ai_ext', odoo_version: $v})"
            "-[:INHERITS]->(d:Model {name: 'viin.ai.embedding', odoo_version: $v}) "
            "RETURN collect(d.module) AS mods",
            v=V,
        ).single()["mods"]
    assert targets == ["viin_ai"], f"extender INHERITS targets: {targets}"


# ---------------------------------------------------------------------------
# Persisted Module properties for the read side
# ---------------------------------------------------------------------------

def test_module_carries_version_rule_verdict_and_is_stamped_every_run(
    pg, neo4j_driver, tmp_path,
):
    """version_mismatch / version_raw follow the manifest (set, then cleared when
    fixed); last_seen_sha / last_seen_at / repos are stamped on every scanned run,
    also on modules the run did not re-parse."""
    repo = GitRepo(tmp_path, "tvtmaaddons")
    write_module(repo, "viin_backport", version="98.0.1.0.0")
    write_module(repo, "viin_hr")
    repo.commit("add")
    (rid,) = register("tvtma_99", repo)
    run(pg, "tvtma_99")

    node = module_node(neo4j_driver, "viin_backport")
    assert node.get("version_mismatch") is True
    assert node.get("version_raw") == "98.0.1.0.0"
    assert node["odoo_version"] == V
    assert module_node(neo4j_driver, "viin_hr").get("version_mismatch") is False
    assert node.get("last_seen_sha") == repo.head()
    assert node.get("repos") == ["tvtmaaddons"]
    first_seen_at = node.get("last_seen_at")

    write_module(repo, "viin_backport", version=f"{V}.1.0.0")
    fixed_sha = repo.commit("fix version")
    run(pg, "tvtma_99")

    node = module_node(neo4j_driver, "viin_backport")
    assert node.get("version_mismatch") is False
    assert node.get("version_raw") == f"{V}.1.0.0"
    untouched = module_node(neo4j_driver, "viin_hr")
    assert untouched.get("last_seen_sha") == fixed_sha, "an unchanged module is stamped too"
    assert (untouched.get("last_seen_at") or first_seen_at) > first_seen_at
    assert untouched.get("repos") == ["tvtmaaddons"]
    assert ledger(pg, rid, "viin_backport")["version_mismatch"] is False


def test_module_repos_lists_every_repo_that_ships_it(pg, neo4j_driver, tmp_path):
    ce = GitRepo(tmp_path / "ce", "viindoo_ce")
    ee = GitRepo(tmp_path / "ee", "tvtmaaddons")
    write_module(ce, "viin_meeting_room")
    write_module(ee, "viin_meeting_room")
    ce.commit("ce")
    ee.commit("ee")
    register("ce_99", ce)
    register("ee_99", ee)
    run(pg, "ce_99")
    run(pg, "ee_99")

    assert module_node(neo4j_driver, "viin_meeting_room").get("repos") == [
        "tvtmaaddons", "viindoo_ce",
    ]


# ---------------------------------------------------------------------------
# index_all - one reconcile per version after every profile joined
# ---------------------------------------------------------------------------

def test_index_all_retires_after_every_profile_joined(pg, neo4j_driver, tmp_path, monkeypatch,
                                                       _ephemeral_pg_db):
    """--all with parallel profile workers: a module moved from profile A's repo to
    profile B's repo in the same night is kept (owned by B), a module simply
    deleted is retired, and the run needs no attention."""
    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    a, b, rid_a, rid_b = _moved_setup(tmp_path, pg)
    a.rm("saas_keep")
    a.commit("drop saas_keep")

    summary = index_all(pg, embedder=FakeEmbedder(dim=1024), profile_workers=2)

    assert summary["profiles_failed"] == []
    assert not needs_attention(summary), lc(summary)
    assert_gone(neo4j_driver, pg, "saas_keep")
    node = module_node(neo4j_driver, "to_saas_base")
    assert node is not None and node["profile"] == ["tvtma_99"]
    assert ledger(pg, rid_a, "to_saas_base")["state"] == "retired"
    assert ledger(pg, rid_b, "to_saas_base")["state"] == "present"


# ---------------------------------------------------------------------------
# Pool - lock waits never pin shared pool connections (0eddf1c)
# ---------------------------------------------------------------------------

def test_workers_waiting_on_the_ledger_lock_do_not_exhaust_the_shared_pool(
    pg, neo4j_driver, tmp_path, monkeypatch, _ephemeral_pg_db,
):
    """Another process (a Web UI reconcile) holds retire:<v>. Parallel repo
    workers of this run wait for it at their ledger commit; while they wait the
    shared pool must stay usable (embedding writes, repo_store reads), and once
    the lock is released every repo is indexed."""
    from src.db.module_presence import retire_lock_id
    from src.db.pg import get_pool
    from src.indexer import pipeline_repo

    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    repos = [GitRepo(tmp_path / f"r{i}", f"addons_{i}") for i in range(4)]
    for i, r in enumerate(repos):
        write_module(r, f"pool_mod_{i}")
        r.commit("add")
    ids = register("pool_99", *repos)
    run(pg, "pool_99")
    for i, r in enumerate(repos):
        write_module(r, f"pool_mod_{i}", extra_field="note = fields.Text()")
        r.commit("touch")

    arrived: set[int] = set()
    all_arrived = threading.Event()
    lock_guard = threading.Lock()

    def barrier(repo):
        with lock_guard:
            arrived.add(repo["id"])
            if arrived >= set(ids):
                all_arrived.set()

    monkeypatch.setattr(pipeline_repo, "_LIFECYCLE_TEST_BARRIER", barrier, raising=False)
    holder = _open_conn(_ephemeral_pg_db)
    with holder.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (retire_lock_id(V),))
    result: dict = {}

    def run_profile():
        try:
            result["summary"] = run(pg, "pool_99", max_workers=len(repos))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            result["error"] = exc

    worker = threading.Thread(target=run_profile)
    worker.start()
    try:
        assert all_arrived.wait(120), f"workers never reached the ledger commit: {arrived}"
        # Every worker is inside the lock wait once its session's last statement
        # is the failed try of retire:<v> (the wait polls pg_try_advisory_lock
        # while the holder keeps the lock, so it cannot have been granted).
        import time
        waiting_sql = f"SELECT pg_try_advisory_lock({retire_lock_id(V)})"
        deadline = time.monotonic() + 120
        waiters = 0
        while time.monotonic() < deadline:
            with holder.cursor() as cur:
                cur.execute(
                    "SELECT count(DISTINCT pid) FROM pg_stat_activity "
                    "WHERE pid <> pg_backend_pid() AND datname = current_database() "
                    "AND query = %s",
                    (waiting_sql,),
                )
                waiters = cur.fetchone()[0]
            if waiters >= len(repos):
                break
            time.sleep(0.05)
        assert waiters >= len(repos), f"only {waiters} worker(s) entered the lock wait"
        for _ in range(3):
            with get_pool().checkout() as c, c.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone() == (1,)
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (retire_lock_id(V),))
        holder.close()
        worker.join(300)
    assert "error" not in result, result.get("error")
    for i in range(len(repos)):
        assert module_node(neo4j_driver, f"pool_mod_{i}") is not None
    for rid, r in zip(ids, repos, strict=True):
        assert repo_store().get_repo_head_sha(rid) == r.head()


# ---------------------------------------------------------------------------
# B9 / H4 - the real CLI exits 3 on a trip; --allow-mass-retire proceeds
# ---------------------------------------------------------------------------

def test_cli_exits_3_on_a_tripped_gate_and_the_confirmed_rerun_exits_0(
    pg, neo4j_driver, tmp_path, monkeypatch, _ephemeral_pg_db, capsys,
):
    import src.indexer.__main__ as cli

    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    repo = GitRepo(tmp_path, "wiped_addons")
    write_module(repo, "wipe_a")
    write_module(repo, "wipe_b")
    repo.commit("add")
    (rid,) = register("wipe_99", repo)
    assert cli.main(["index-repo", "--profile", "wipe_99", "--no-embed"]) == 0
    repo.rm("wipe_a")
    repo.rm("wipe_b")
    repo.commit("remove every module")

    tripped = cli.main(["index-repo", "--profile", "wipe_99", "--no-embed"])

    assert tripped == 3
    assert "total_wipe" in capsys.readouterr().err
    assert module_node(neo4j_driver, "wipe_a") is not None

    confirmed = cli.main(
        ["index-repo", "--profile", "wipe_99", "--no-embed", "--allow-mass-retire"],
    )

    assert confirmed == 0
    assert module_node(neo4j_driver, "wipe_a") is None
    assert ledger(pg, rid, "wipe_b")["state"] == "retired"
