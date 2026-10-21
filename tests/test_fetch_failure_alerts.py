# SPDX-License-Identifier: AGPL-3.0-or-later
"""A pre-scan fetch that keeps failing, or an embedder down all run, alerts.

The nightly index run fetches every repo before it scans it. A failed fetch
is fail-safe (the run indexes the on-disk state), but if it keeps failing OSM
silently stops following the remote branch - the 2026-10-18/19 DNS nights
exited 0 and nobody was told. Rules protected here:

* one failed fetch is a WARNING plus a ``repos.lifecycle_attention`` line,
  no exit 3 (a blip that heals by the next run needs nobody);
* the same repo's fetch failing in two consecutive runs is ``stale_repos``:
  exit 3, so ``OnFailure=`` mails the admins;
* every fetch of a run failing (at least two repos) is a run-wide outage
  (``outages``: ``fetch_outage``), exit 3 on that very run;
* the embedder returning no vector for any chunk the run sent is an outage
  (``embedder_down``), exit 3; a partial failure is not.

The unit half needs no database (real git over ``file://`` remotes in
``tmp_path``); the last tests drive ``index_profile`` against throwaway
Neo4j + PostgreSQL (``-m "postgres and neo4j"``).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.indexer import pipeline as pipeline_mod
from src.indexer.pipeline_repo import FETCH_FAILED_MARK, _fetch_attention, refresh_before_scan


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@e.x", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _clone_with_origin(tmp_path: Path, branch: str = "17.0") -> tuple[Path, Path]:
    work = tmp_path / "up"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "checkout", "-q", "-b", branch)
    (work / "README").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "-b", branch, str(bare), str(clone)], check=True)
    return clone, bare


def _repo(clone: Path, *, attention: str | None = None) -> dict:
    return {"id": 7, "url": "https://example.com/addons.git", "branch": "17.0",
            "local_path": str(clone), "lifecycle_attention": attention}


# ---------------------------------------------------------------------------
# refresh_before_scan reports what failed
# ---------------------------------------------------------------------------

def test_a_fetch_that_cannot_reach_its_remote_returns_the_cause(tmp_path):
    clone, bare = _clone_with_origin(tmp_path)
    assert refresh_before_scan(_repo(clone)) is None, "a working fetch reports nothing"

    shutil.rmtree(bare)  # the remote is unreachable (DNS, host down, repo gone)
    cause = refresh_before_scan(_repo(clone))

    assert cause and "CalledProcessError" in cause
    assert "origin" in cause or "repository" in cause, cause


def test_the_cause_names_git_s_reason_and_never_a_url_credential():
    """The cause lands in lifecycle_attention and the alert mail."""
    from src.indexer.pipeline_repo import _fetch_cause

    exc = subprocess.CalledProcessError(
        128, ["git", "fetch"],
        stderr=b"fatal: unable to access 'https://bot:ghp_SECRET@github.com/x.git/': "
               b"Could not resolve host: github.com\n",
    )
    cause = _fetch_cause(exc)
    assert "Could not resolve host" in cause
    assert "ghp_SECRET" not in cause and "https://***@github.com" in cause


def test_one_failed_fetch_is_noted_and_the_second_in_a_row_is_consecutive(tmp_path):
    line, consecutive = _fetch_attention(_repo(tmp_path), "TimeoutExpired: fetch")
    assert FETCH_FAILED_MARK in line and not consecutive

    line2, consecutive2 = _fetch_attention(_repo(tmp_path, attention=line), "same again")
    assert consecutive2 and "two consecutive runs" in line2

    other = "scan untrusted: checked-out tree is not origin/17.0; no module retired"
    _line3, consecutive3 = _fetch_attention(_repo(tmp_path, attention=other), "x")
    assert not consecutive3, "another attention line is no previous fetch failure"


# ---------------------------------------------------------------------------
# The run summary: which failures need an operator
# ---------------------------------------------------------------------------

def _counters(*, attempted=True, failed=None, persistent=False, sent=0, ok=0) -> dict:
    return {"modules": 0, "fetch": {"attempted": attempted, "failed": failed,
                                    "persistent": persistent},
            "embed_attempted": sent, "embed_ok": ok}


def _summary(*repo_counters: dict) -> dict:
    lc = pipeline_mod._empty_lifecycle()
    for i, counters in enumerate(repo_counters, start=1):
        pipeline_mod._absorb_repo_lifecycle(lc, {"id": i, "url": f"r{i}"}, counters)
    return pipeline_mod._finish_lifecycle(lc)


def test_a_single_transient_fetch_failure_needs_nobody():
    lc = _summary(_counters(failed="DNS"), _counters())
    assert lc["fetch_failed"] == ["repo id=1 (r1): DNS"]
    assert lc["stale_repos"] == [] and lc["outages"] == []
    assert lc["needs_attention"] is False


def test_a_fetch_failing_two_runs_in_a_row_needs_an_operator():
    lc = _summary(_counters(failed="DNS", persistent=True), _counters())
    assert lc["stale_repos"] == ["repo id=1 (r1): DNS"]
    assert lc["needs_attention"] is True


def test_every_fetch_of_the_run_failing_is_an_outage_on_the_first_night():
    lc = _summary(_counters(failed="DNS"), _counters(failed="DNS"))
    assert [o.split(":")[0] for o in lc["outages"]] == ["fetch_outage"]
    assert lc["needs_attention"] is True

    single = _summary(_counters(failed="DNS"))
    assert single["outages"] == [], "one repo failing once is the consecutive rule's job"
    offline = _summary(_counters(attempted=False), _counters(attempted=False))
    assert offline["needs_attention"] is False, "--no-fetch attempts nothing"


def test_the_merged_all_profiles_summary_is_judged_as_a_whole(monkeypatch):
    """index_all merges profiles: a profile whose one repo failed while the
    other profiles fetched fine is no outage."""
    a = _summary(_counters(failed="DNS"))
    b = _summary(_counters(), _counters())
    merged = pipeline_mod._empty_lifecycle()
    for plc in (a, b):
        for key in ("fetch_failed", "stale_repos"):
            merged[key].extend(plc[key])
        for key in ("fetch_attempted", "embed_attempted", "embed_ok"):
            merged[key] += plc[key]
    merged = pipeline_mod._finish_lifecycle(merged)
    assert merged["fetch_attempted"] == 3 and merged["outages"] == []


def test_an_embedder_down_for_the_whole_run_is_an_outage_a_partial_failure_is_not():
    down = _summary(_counters(sent=40, ok=0), _counters(sent=10, ok=0))
    assert [o.split(":")[0] for o in down["outages"]] == ["embedder_down"]
    assert down["needs_attention"] is True

    partial = _summary(_counters(sent=40, ok=39))
    assert partial["outages"] == [] and partial["needs_attention"] is False
    nothing = _summary(_counters(sent=0, ok=0))
    assert nothing["needs_attention"] is False, "a run that embedded nothing sent nothing"


def test_the_cli_exits_3_and_names_the_stale_repo_and_the_outage(capsys):
    from src.indexer.__main__ import EXIT_LIFECYCLE_ATTENTION, _lifecycle_exit_code

    lc = _summary(_counters(failed="DNS", persistent=True), _counters(failed="DNS"))
    assert _lifecycle_exit_code(lc) == EXIT_LIFECYCLE_ATTENTION
    err = capsys.readouterr().err
    assert "stale_repos: repo id=1 (r1): DNS" in err
    assert "outages: fetch_outage:" in err


def test_the_embedder_counts_what_it_was_sent_and_what_it_returned():
    """write_module_embeddings reports attempted / embedded chunks, so a dead
    embedder (zero vectors) is told apart from a partial failure."""
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    class Dead:
        def embed(self, texts):
            raise RuntimeError("connection refused")

    chunks = [EmbeddingChunk(chunk_type="field", entity_name=f"m.f{i}", content=f"f{i}",
                             model_name="m",
                             module="m", odoo_version="99.0", file_path="m/x.py",
                             chunk_idx=0)
              for i in range(3)]
    stats: dict = {}
    write_module_embeddings("m", "99.0", chunks, Dead(), profile_name="p", stats=stats)
    assert stats == {"attempted": 3, "embedded": 0}


# ---------------------------------------------------------------------------
# End to end: index_profile against throwaway Neo4j + PostgreSQL
# ---------------------------------------------------------------------------

@pytest.fixture
def pg(clean_pg, clean_neo4j):
    from src.db.migrate import run_migrations

    run_migrations(clean_pg)
    return clean_pg


def _break(repo) -> Path:
    moved = repo.origin.with_name(repo.origin.name + ".away")
    repo.origin.rename(moved)
    return moved


def _attention(repo_id: int) -> str:
    from src.db.pg import repo_store

    return (repo_store().get_repo_by_id(repo_id) or {}).get("lifecycle_attention") or ""


@pytest.mark.postgres
@pytest.mark.neo4j
def test_a_remote_unreachable_two_nights_in_a_row_alerts_on_the_second(pg, tmp_path):
    from tests._lifecycle_repo import GitRepo, register, run, write_module

    repo = GitRepo(tmp_path, "addons")
    healthy = GitRepo(tmp_path, "healthy")
    for r, name in ((repo, "viin_fetch_a"), (healthy, "viin_fetch_b")):
        write_module(r, name)
        r.commit("add")
    rid, _other = register("fetch_99", repo, healthy)
    first = run(pg, "fetch_99")
    assert not first["lifecycle"]["needs_attention"], first["lifecycle"]

    moved = _break(repo)
    night1 = run(pg, "fetch_99")["lifecycle"]
    assert night1["fetch_failed"] and night1["stale_repos"] == []
    assert night1["needs_attention"] is False, "one failure that may heal is no alert"
    assert FETCH_FAILED_MARK in _attention(rid)

    night2 = run(pg, "fetch_99")["lifecycle"]
    assert [s.split(":")[0] for s in night2["stale_repos"]] == [f"repo id={rid} (file"]
    assert night2["needs_attention"] is True
    assert "two consecutive runs" in _attention(rid)

    moved.rename(repo.origin)
    healed = run(pg, "fetch_99")["lifecycle"]
    assert healed["needs_attention"] is False and healed["fetch_failed"] == []
    assert FETCH_FAILED_MARK not in _attention(rid), "a working fetch clears the note"


@pytest.mark.postgres
@pytest.mark.neo4j
def test_every_remote_unreachable_alerts_on_the_first_night(pg, tmp_path):
    from tests._lifecycle_repo import GitRepo, register, run, write_module

    repos = [GitRepo(tmp_path, f"addons_{i}") for i in range(2)]
    for i, r in enumerate(repos):
        write_module(r, f"viin_outage_{i}")
        r.commit("add")
    register("outage_99", *repos)
    assert not run(pg, "outage_99")["lifecycle"]["needs_attention"]

    for r in repos:
        _break(r)
    night = run(pg, "outage_99")["lifecycle"]
    assert [o.split(":")[0] for o in night["outages"]] == ["fetch_outage"]
    assert night["needs_attention"] is True
