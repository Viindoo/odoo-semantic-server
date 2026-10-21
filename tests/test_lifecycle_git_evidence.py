# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo-level retirement is decided by git, not by a human (ADR-0057 phase 2) - no DB.

Owner requirement: "Nobody approves these. If code is deleted, graph AND
embeddings must be deleted. OSM reflects the real state of the code."
Rule: delete what git proves gone, keep what git cannot prove. Before this,
gate G-B held any drop of more than half of a repo's modules (at least 20),
and every total wipe, until an operator ran ``--allow-mass-retire``.

Rules protected here (real git repos with a bare origin in ``tmp_path``, the
ledger and graph faked; the real scan, ``observe_lifecycle`` and
``_commit_lifecycle``):

* the HEAD-tree evidence (``git_utils.head_tree_module_names``) reads the git
  object database: a module deleted by a commit is gone even if a copy is
  left on disk, a staged-but-uncommitted delete is not, a legacy
  ``__openerp__.py`` counts, a submodule is read at its recorded commit, and
  an unreadable submodule proves nothing;
* 22 of 25 modules deleted by a commit are flagged pending (retired by the
  reconcile) on the first plain run - no flag, no exit 3;
* a scan that loses 22 modules whose manifests are still in the HEAD tree
  keeps them (not flagged, presence synced, no attention, no exit 3);
* a total wipe that is a commit of the branch is retired; one that is not in
  the branch history (force-push to an unrelated root) is held: gate
  ``total_wipe``, exit 3 (checkout integrity).
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from src.git_utils import head_tree_manifest_dirs, head_tree_module_names
from src.indexer import lifecycle
from src.indexer.pipeline_repo import _commit_lifecycle, observe_lifecycle
from src.indexer.registry import build_registry_scan
from tests._lifecycle_repo import GitRepo, V, run_git, write_module

PROFILE = "evidence_99"


# ---------------------------------------------------------------------------
# HEAD-tree evidence
# ---------------------------------------------------------------------------

def _plain_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run_git(path, "init", "-q")
    run_git(path, "config", "user.email", "d@e.x")
    run_git(path, "config", "user.name", "D")
    return path


def _commit_all(path: Path, msg: str = "c") -> None:
    run_git(path, "add", "-A")
    run_git(path, "commit", "-q", "--allow-empty", "-m", msg)


def _manifest(dir_: Path, name: str = "__manifest__.py") -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / name).write_text("{'name': 'x'}\n")


def test_a_module_deleted_by_a_commit_is_gone_even_with_a_copy_left_on_disk(tmp_path):
    repo = _plain_repo(tmp_path / "addons")
    _manifest(repo / "keep")
    _manifest(repo / "gone")
    _commit_all(repo)
    run_git(repo, "rm", "-r", "-q", "gone")
    _commit_all(repo, "rm")
    _manifest(repo / "gone")  # an untracked copy on disk is no evidence

    assert head_tree_module_names(repo) == {"keep"}


def test_a_staged_delete_that_is_not_committed_still_ships_the_module(tmp_path):
    repo = _plain_repo(tmp_path / "addons")
    _manifest(repo / "kept")
    _commit_all(repo)
    run_git(repo, "rm", "-r", "-q", "kept")  # index and disk lose it, HEAD keeps it

    assert head_tree_module_names(repo) == {"kept"}


def test_any_manifest_counts_whatever_the_version_dispatch(tmp_path):
    repo = _plain_repo(tmp_path / "addons")
    _manifest(repo / "legacy", "__openerp__.py")
    _manifest(repo / "nested" / "deep")
    _commit_all(repo)

    assert head_tree_manifest_dirs(repo) == {"legacy", "nested/deep"}
    assert head_tree_module_names(repo) == {"legacy", "deep"}


def test_a_manifest_at_the_repo_root_is_the_repo_named_module(tmp_path):
    repo = _plain_repo(tmp_path / "single_mod")
    _manifest(repo)
    _commit_all(repo)

    assert head_tree_module_names(repo) == {"single_mod"}


def _with_submodule(tmp_path: Path) -> Path:
    sub = _plain_repo(tmp_path / "vendored")
    _manifest(sub / "vendored_mod")
    _commit_all(sub)
    top = _plain_repo(tmp_path / "top")
    _manifest(top / "own_mod")
    _commit_all(top)
    subprocess.run(
        ["git", "-C", str(top), "-c", "protocol.file.allow=always", "submodule", "add",
         "-q", str(sub), "third_party"],
        check=True, capture_output=True,
    )
    _commit_all(top, "submodule")
    return top


def test_a_submodule_is_read_at_the_commit_its_gitlink_records(tmp_path):
    top = _with_submodule(tmp_path)

    assert head_tree_manifest_dirs(top) == {"own_mod", "third_party/vendored_mod"}


def test_an_unreadable_submodule_proves_nothing(tmp_path):
    top = _with_submodule(tmp_path)
    shutil.rmtree(top / "third_party")

    assert head_tree_module_names(top) is None


def test_no_repository_proves_nothing(tmp_path):
    assert head_tree_module_names(tmp_path) is None


# ---------------------------------------------------------------------------
# observe_lifecycle + _commit_lifecycle over a real checkout
# ---------------------------------------------------------------------------

class _Ledger:
    """The ledger calls the per-repo path makes, recorded."""

    def __init__(self, rows: list[dict], presence_head: str | None) -> None:
        self.rows = rows
        self.head = presence_head
        self.pending: list[tuple[list[str], str | None]] = []
        self.synced: list[str] = []

    def rows_for_repo(self, _repo_id):
        return [dict(r) for r in self.rows]

    def presence_head_sha(self, _repo_id):
        return self.head

    def commit_observed(self, *a, **k):
        return None

    def clear_needs_rewrite(self, *a, **k):
        return None

    def mark_retire_pending(self, _repo_id, names, _reason, *, evidence, successors,
                            blocked_by):
        self.pending.append((sorted(names), blocked_by))
        return sorted(names)

    def present_owner_basenames(self, _v, names):
        return {n: ["addons"] for n in names}

    def mark_presence_synced(self, _repo_id, head):
        self.synced.append(head)


class _Graph:
    def repo_module_baseline(self, *_a):
        return set()

    def module_identity(self, _v, names):
        return {}

    def stamp_module_presence(self, _v, rows, _head):
        return len(rows)


def _repo_row(repo: GitRepo) -> dict:
    return {
        "id": 31, "url": repo.url, "branch": repo.branch, "local_path": str(repo.path),
        "odoo_version": V, "profile_name": PROFILE,
    }


def _ledger_rows(names: list[str]) -> list[dict]:
    return [
        {"name": n, "state": "present", "path": n, "manifest_file": "__manifest__.py"}
        for n in names
    ]


def _night(repo: GitRepo, ledger: _Ledger, *, scan=None, last_head=None, allow=False):
    """One run's lifecycle half: observe (gates + git evidence) then commit."""
    head = repo.head()
    scan = scan or build_registry_scan(str(repo.path), V, branch=repo.branch)
    row = _repo_row(repo)
    obs = observe_lifecycle(
        row, scan, ledger, head, allow_mass_retire=allow, writer=_Graph(),
        owning_profile=PROFILE, last_head=last_head,
    )
    counters = _commit_lifecycle(
        ledger, _Graph(), row, scan=scan, rows=obs.rows, transitions=obs.transitions,
        gates=obs.gates, current_head=head, diff_base=ledger.head, owning_profile=PROFILE,
        retire=True, written=set(), rewrite_names=[], attention=obs.attention,
    )
    return obs, counters


@pytest.fixture
def addons(tmp_path):
    """25 modules indexed and reflected by the ledger at the current HEAD."""
    repo = GitRepo(tmp_path, "addons")
    names = [f"viin_mod_{i:02d}" for i in range(25)]
    for n in names:
        write_module(repo, n)
    repo.commit("add 25")
    return repo, names, _Ledger(_ledger_rows(names), repo.head())


def test_a_commit_that_deletes_22_of_25_modules_retires_them_without_a_flag(addons):
    repo, names, ledger = addons
    for n in names[3:]:
        repo.rm(n)
    repo.commit("[REM] 22 modules")

    obs, counters = _night(repo, ledger)

    assert obs.gates.tripped == () and obs.gates.unproven == ()
    assert ledger.pending == [(names[3:], None)], "flagged pending, blocked by nothing"
    assert counters["gates_tripped"] == []
    assert counters["presence_deferred_head"] == repo.head(), (
        "presence follows once the reconcile retired them")
    assert not any("mass" in a for a in obs.attention), obs.attention


def test_a_scan_that_loses_22_tracked_modules_keeps_them_and_does_not_alert(addons):
    """A dispatch / registry regression: the manifests are still in the HEAD
    tree, so git cannot prove anything gone."""
    repo, names, ledger = addons
    write_module(repo, "viin_mod_00", extra_field="x_note = fields.Char()")
    repo.commit("[IMP] touch")
    real = build_registry_scan(str(repo.path), V, branch=repo.branch)
    keep = set(names[:3])
    scan = replace(real, modules={
        v: {n: i for n, i in mods.items() if n in keep} for v, mods in real.modules.items()
    })

    obs, counters = _night(repo, ledger, scan=scan)

    assert obs.gates.unproven == tuple(names[3:])
    assert ledger.pending == [], "nothing git cannot prove is flagged for retirement"
    assert obs.gates.tripped == () and counters["gates_tripped"] == []
    assert obs.attention == [], "keeping unproven data is not an operator signal"
    assert ledger.synced == [repo.head()]


def test_the_test_node_gc_keeps_the_tests_of_modules_an_unproven_drop_keeps(addons):
    """A module the scan lost while git still tracks its manifest keeps its
    ledger row and Module node; the module-level test GC (graph-only, no
    embedding rows) must keep its TestClass / TestMethod nodes too, or
    find_test_examples would serve ghost rows of a live module whose tests
    nothing re-writes until --full. With --allow-mass-retire the drops
    retire, and their tests leave through the retirement cascade."""
    from src.indexer.pipeline_repo import _test_gc_live_names

    repo, names, ledger = addons
    write_module(repo, "viin_mod_00", extra_field="x_note = fields.Char()")
    repo.commit("[IMP] touch")
    real = build_registry_scan(str(repo.path), V, branch=repo.branch)
    keep = set(names[:3])
    scan = replace(real, modules={
        v: {n: i for n, i in mods.items() if n in keep} for v, mods in real.modules.items()
    })
    live = {v: sorted(m) for v, m in scan.modules.items()}

    obs, _counters = _night(repo, ledger, scan=scan)
    assert _test_gc_live_names(live, scan, obs.gates)[V] == set(names)

    obs, _counters = _night(repo, _Ledger(_ledger_rows(names), repo.head()), scan=scan,
                            allow=True)
    assert _test_gc_live_names(live, scan, obs.gates)[V] == keep


def test_the_test_node_gc_keeps_graph_baseline_unproven_drops_at_scanned_versions_only():
    """Against the graph baseline the unproven drops are ``name@version``: kept
    at a version the GC runs at, never a reason to start a GC at another."""
    from types import SimpleNamespace

    from src.indexer.pipeline_repo import _test_gc_live_names

    gates = lifecycle.GateResult(
        scan_ok=True, mass_ok=True, n_soft_drop=2, n_present_before=3,
        baseline="graph", unproven=(f"kept_mod@{V}", "elsewhere_mod@98.0"),
    )
    scan = SimpleNamespace(odoo_version=V, excluded={})
    live = _test_gc_live_names({V: ["scanned_mod"]}, scan, gates)
    assert live == {V: {"scanned_mod", "kept_mod"}}


def test_a_total_wipe_committed_on_the_branch_is_retired(addons):
    repo, names, ledger = addons
    for n in names:
        repo.rm(n)
    repo.commit("[REM] everything")

    obs, counters = _night(repo, ledger)

    assert obs.gates.tripped == () and obs.gates.retire_allowed
    assert ledger.pending == [(names, None)]


def test_a_total_wipe_outside_the_branch_history_is_held_and_alerted(addons):
    """Force-push of an unrelated, empty root: the ledger's HEAD is no
    ancestor of the new HEAD, so the wipe is no evidence of a deletion."""
    repo, names, ledger = addons
    run_git(repo.path, "checkout", "-q", "--orphan", "rewritten")
    run_git(repo.path, "rm", "-r", "-q", "--cached", ".")
    for child in repo.path.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    (repo.path / "README").write_text("new root\n")
    run_git(repo.path, "add", "-A")
    run_git(repo.path, "commit", "-q", "-m", "new root")
    run_git(repo.path, "push", "-q", "--force", "origin", f"HEAD:refs/heads/{repo.branch}")
    run_git(repo.path, "fetch", "-q", "origin")
    run_git(repo.path, "branch", "-q", "-D", repo.branch)
    run_git(repo.path, "checkout", "-q", "-b", repo.branch)
    run_git(repo.path, "branch", "-q", "--set-upstream-to", f"origin/{repo.branch}")

    obs, counters = _night(repo, ledger)

    assert lifecycle.GATE_TOTAL_WIPE in obs.gates.tripped
    assert any("not in the branch history" in a for a in obs.attention), obs.attention
    assert ledger.pending == [(names, f"gate:{lifecycle.GATE_TOTAL_WIPE}")]
    assert ledger.synced == [] and counters["presence_deferred_head"] is None
    assert lifecycle.GATE_TOTAL_WIPE in counters["gates_tripped"]

    # The emergency bypass lifts it (never on the normal path).
    obs, _counters = _night(repo, _Ledger(_ledger_rows(names), ledger.head), allow=True)
    assert lifecycle.GATE_TOTAL_WIPE in obs.gates.bypassed
