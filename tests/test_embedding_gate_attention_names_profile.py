# SPDX-License-Identifier: AGPL-3.0-or-later
"""A held orphan-embedding sweep always names the profile it holds (#381 F3) - no DB.

When gate G-B holds a version's orphan embedding groups, the run exits 3.
The attention used to go only to repos of the held profiles found in the
version's repo list; a profile with no repo at that version (its rows are
leftovers) got none, and the nightly run exited 3 with nothing that named
what to look at. Now:

* the attention text names the held profile(s);
* with no repo of a held profile at the version, every repo of the version
  carries it (fallback), so it is never dropped;
* the exit-3 stderr lines name the held profile(s).
"""
from __future__ import annotations

import src.db.pg as pg_mod
import src.indexer.writer_pgvector as writer_pgvector
from src.indexer import pipeline
from src.indexer.__main__ import _lifecycle_exit_code
from src.indexer.reconcile import ReconcileReport, _Reconciler

V = "99.0"
HELD = "gone_profile"


class _Writer:
    def module_profiles(self, _v):
        return {"live_mod": ["other_profile"]}


class _Store:
    def __init__(self):
        self.attention: dict[int, str] = {}

    def present_pairs(self, _v, conn=None):
        return {("live_mod", "other_profile")}

    def set_lifecycle_attention(self, repo_id, text, conn=None):
        self.attention[repo_id] = text


class _RepoStore:
    def get_repo_by_id(self, _repo_id):
        return {"lifecycle_attention": ""}


def _held_run(monkeypatch, sync_rows):
    groups = [(f"ghost_{i:02d}", HELD, 3) for i in range(20)]
    monkeypatch.setattr(
        writer_pgvector, "embedding_groups",
        lambda _conn, _v: [*groups, ("live_mod", "other_profile", 5)],
    )
    monkeypatch.setattr(pg_mod, "repo_store", lambda: _RepoStore())
    store = _Store()
    report = ReconcileReport(V)
    rec = _Reconciler(
        V, writer=_Writer(), store=store, conn=object(), run_started_at=None,
        retire=True, allow_mass_retire=False, dry_run=False, report=report,
    )
    rec._embedding_orphans({"live_mod"}, set(), sync_rows, delete=True)
    rec.flush_attention()
    return report, store


def test_held_profile_without_a_repo_at_the_version_still_gets_named(monkeypatch):
    report, store = _held_run(
        monkeypatch, [{"repo_id": 7, "profile_name": "other_profile"}],
    )

    assert any(g.startswith("embedding_sweep:") for g in report.gates_tripped)
    assert 7 in store.attention, "the version's repos carry the held gate as a fallback"
    assert HELD in store.attention[7]


def test_held_profile_with_its_own_repo_names_it_on_that_repo_only(monkeypatch):
    # GUARD: pre-existing routing - the repo of the held profile gets the attention.
    report, store = _held_run(
        monkeypatch,
        [{"repo_id": 7, "profile_name": "other_profile"}, {"repo_id": 8, "profile_name": HELD}],
    )

    assert set(store.attention) == {8}
    assert HELD in store.attention[8]


def test_exit_3_stderr_names_the_held_profile(monkeypatch, capsys):
    report, _store = _held_run(
        monkeypatch, [{"repo_id": 7, "profile_name": "other_profile"}],
    )
    lifecycle = pipeline._empty_lifecycle()
    pipeline._absorb_report(lifecycle, report)
    pipeline._finish_lifecycle(lifecycle)

    code = _lifecycle_exit_code(lifecycle)

    assert code == 3
    assert HELD in capsys.readouterr().err
