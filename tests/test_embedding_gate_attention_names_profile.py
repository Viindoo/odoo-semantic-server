# SPDX-License-Identifier: AGPL-3.0-or-later
"""The orphan-embedding sweep is judged by git, and a held sweep always names
what holds it (#381 F3, ADR-0057 phase 2) - no DB.

Before ADR-0057 phase 2 a ratio gate (more than half of a version's groups,
at least 20) held a mass orphan-embedding delete until an operator ran
``--allow-mass-retire``. Now:

* a graph that lost the Module node of a module a synced profile has
  ``present`` holds every orphan group (``embedding_sweep:graph_unhealthy``,
  exit 3): an orphan cannot be told from a live module whose node is gone;
  the attention names the held profile(s), reaches a repo even when no repo of
  a held profile is at the version (fallback), and names the profile whose
  ``--full`` run re-writes the missing nodes;
* with a healthy graph, an orphan group goes when no checkout of its profile
  ships the module in its raw HEAD tree - 20 groups or 2000, no approval -
  and is kept (``embedding_orphans_kept``, no exit 3) when one does.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

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

    def module_identity(self, _v, names):
        return {}  # lost_mod has no Module node at all


class _Store:
    def __init__(self, pairs):
        self.attention: dict[int, str] = {}
        self._pairs = pairs

    def present_pairs(self, _v, conn=None):
        return set(self._pairs)

    def excluded_names(self, _v, conn=None):
        return set()

    def set_lifecycle_attention(self, repo_id, text, conn=None):
        self.attention[repo_id] = text


class _RepoStore:
    def get_repo_by_id(self, _repo_id):
        return {"lifecycle_attention": ""}


GHOSTS = [f"ghost_{i:02d}" for i in range(20)]
HEALTHY = {("live_mod", "other_profile")}
# A module other_profile has present in the ledger whose node the graph lost.
UNHEALTHY = HEALTHY | {("lost_mod", "other_profile")}


def _run(monkeypatch, sync_rows, pairs, *, allow=False):
    groups = [(g, HELD, 3) for g in GHOSTS]
    monkeypatch.setattr(
        writer_pgvector, "embedding_groups",
        lambda _conn, _v: [*groups, ("live_mod", "other_profile", 5)],
    )
    deleted: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        writer_pgvector, "delete_module_embeddings",
        lambda _conn, name, _v, profiles, expected=None: deleted.append(
            (name, tuple(profiles))) or expected or 0,
    )
    monkeypatch.setattr(pg_mod, "repo_store", lambda: _RepoStore())
    store = _Store(pairs)
    report = ReconcileReport(V)
    rec = _Reconciler(
        V, writer=_Writer(), store=store, conn=object(), run_started_at=None,
        retire=True, allow_mass_retire=allow, dry_run=False, report=report,
    )
    rec._embedding_orphans({"live_mod"}, set(), sync_rows, delete=True)
    rec.flush_attention()
    return report, store, deleted


OTHER_ONLY = [{"repo_id": 7, "profile_name": "other_profile"}]
WITH_HELD = [*OTHER_ONLY, {"repo_id": 8, "profile_name": HELD}]


# -- held by an unhealthy graph -------------------------------------------------

def test_a_graph_that_lost_present_modules_holds_every_orphan_group(monkeypatch):
    report, _store, deleted = _run(monkeypatch, OTHER_ONLY, UNHEALTHY)

    assert "embedding_sweep:graph_unhealthy" in report.gates_tripped
    assert deleted == [], "nothing may be deleted while the graph is incomplete"
    assert sorted(m for m, _p, _n in report.embedding_orphans_held) == GHOSTS


def test_held_profile_without_a_repo_at_the_version_still_gets_named(monkeypatch):
    report, store, _deleted = _run(monkeypatch, OTHER_ONLY, UNHEALTHY)

    assert any(g.startswith("embedding_sweep:") for g in report.gates_tripped)
    assert 7 in store.attention, "the version's repos carry the held gate as a fallback"
    assert HELD in store.attention[7]
    assert "lost_mod" in store.attention[7], "the attention names the missing module"


def test_the_held_profile_and_the_profile_that_lost_nodes_carry_the_attention(monkeypatch):
    _report, store, _deleted = _run(monkeypatch, WITH_HELD, UNHEALTHY)

    assert set(store.attention) == {7, 8}
    assert HELD in store.attention[8]


def test_the_attention_names_the_profile_whose_full_run_rewrites_the_lost_nodes(monkeypatch):
    """A run reconciles only its repos' versions; the profile that ships the
    lost module both re-writes it and reconciles the version."""
    _report, store, _deleted = _run(monkeypatch, WITH_HELD, UNHEALTHY)
    for text in store.attention.values():
        assert "index-repo --profile other_profile --full" in text, text
        assert f"--profile {HELD}" not in text


def test_exit_3_stderr_names_the_held_profile_and_the_run_to_do(monkeypatch, capsys):
    report, _store, _deleted = _run(monkeypatch, OTHER_ONLY, UNHEALTHY)
    lifecycle = pipeline._empty_lifecycle()
    pipeline._absorb_report(lifecycle, report)
    pipeline._finish_lifecycle(lifecycle)

    code = _lifecycle_exit_code(lifecycle)

    assert code == 3
    err = capsys.readouterr().err
    assert HELD in err
    assert "index-repo --profile other_profile --full" in err


# -- a healthy graph: git decides, no ratio, no approval -------------------------

def test_a_mass_of_orphan_groups_no_checkout_ships_is_deleted_without_a_flag(monkeypatch):
    """20 of 21 groups (the old gate's trip shape): the held profile has no
    repo at the version, so nothing can still ship them."""
    report, store, deleted = _run(monkeypatch, OTHER_ONLY, HEALTHY)

    assert report.gates_tripped == []
    assert sorted(name for name, _p in deleted) == GHOSTS
    assert all(p == (HELD,) for _name, p in deleted)
    assert store.attention == {}


def _checkout(tmp_path: Path, modules: list[str]) -> Path:
    repo = tmp_path / "held_addons"
    repo.mkdir()
    for args in (("init", "-q"), ("checkout", "-q", "-b", V),
                 ("config", "user.email", "d@e.x"), ("config", "user.name", "D")):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    for m in modules:
        (repo / m).mkdir()
        (repo / m / "__manifest__.py").write_text("{'name': 'x'}\n")
    (repo / ".keep").write_text("")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "m"], check=True)
    return repo


def test_an_orphan_group_whose_module_a_checkout_still_ships_is_kept(monkeypatch, tmp_path):
    """The profile's checkout still has ghost_00's manifest in its HEAD tree:
    git cannot prove it gone, so its rows stay (reported, never exit 3)."""
    repo = _checkout(tmp_path, ["ghost_00"])
    rows = [*OTHER_ONLY, {
        "repo_id": 8, "profile_name": HELD, "repo_basename": "held_addons",
        "local_path": str(repo), "repo_branch": V, "profile_version": V,
    }]
    report, _store, deleted = _run(monkeypatch, rows, HEALTHY)

    assert report.gates_tripped == []
    assert [m for m, _p, _n in report.embedding_orphans_kept] == ["ghost_00"]
    assert "ghost_00" not in {name for name, _p in deleted}
    assert sorted(name for name, _p in deleted) == GHOSTS[1:]


def test_allow_mass_retire_deletes_held_and_kept_groups(monkeypatch, tmp_path):
    repo = _checkout(tmp_path, ["ghost_00"])
    rows = [*OTHER_ONLY, {
        "repo_id": 8, "profile_name": HELD, "repo_basename": "held_addons",
        "local_path": str(repo), "repo_branch": V, "profile_version": V,
    }]
    report, _store, deleted = _run(monkeypatch, rows, UNHEALTHY, allow=True)

    assert report.gates_tripped == []
    assert sorted(name for name, _p in deleted) == GHOSTS
