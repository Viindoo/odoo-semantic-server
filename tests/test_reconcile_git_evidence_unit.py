# SPDX-License-Identifier: AGPL-3.0-or-later
"""The version reconcile judges orphans and shared-module candidates by git
(ADR-0057 phase 2) - no DB: fakes for the ledger and the graph, real temp git
repositories for the checkouts.

Rules:

* shared-module prune: a stale candidate goes only when the source oracle
  proves it gone (or superseded) in EVERY owner's copy at its HEAD tree; one
  any owner's source still defines is kept (a parser gap), with no ratio and
  no approval; a file path of one owner's copy is judged inside each owner's
  own module directory (the copies may sit at different paths);
* orphan evidence: an orphan is provably gone only when no checkout of an
  owning profile ships a manifest of it in its raw HEAD tree; an unreadable
  checkout proves nothing; a repo the version rule places at another version
  ships nothing at this one.
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from src.indexer.reconcile import ReconcileReport, _reanchor, _Reconciler

V = "99.0"
MOD = "viin_shared"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@e.x",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
    )


def _copy(root: Path, module_dir: str, fields: list[str]) -> Path:
    """A repo shipping MOD at *module_dir* whose model declares *fields*."""
    mod = root / module_dir
    (mod / "models").mkdir(parents=True)
    (mod / "__manifest__.py").write_text(repr({"name": MOD}))
    body = "\n".join(f"    {f} = fields.Char()" for f in fields) or "    pass"
    (mod / "models" / "rec.py").write_text(
        "from odoo import fields, models\n\n\nclass Rec(models.Model):\n"
        "    _name = 'viin.shared.rec'\n" + body + "\n"
    )
    _git(root, "init", "-q")
    _git(root, "checkout", "-q", "-b", V)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


class _Graph:
    def __init__(self, nodes: list[dict]) -> None:
        self.nodes = nodes
        self.calls: list = []

    def module_stale_children(self, v, name, **kw):
        self.calls.append(kw)
        return {"nodes": self.nodes, "rels": []}


def _field(i: str, name: str, file_path: str) -> dict:
    return {"id": i, "label": "Field", "name": name, "model": "viin.shared.rec",
            "file_path": file_path}


def _reconciler(writer, *, allow: bool = False) -> _Reconciler:
    return _Reconciler(
        V, writer=writer, store=object(), conn=object(), run_started_at=None,
        retire=True, allow_mass_retire=allow, dry_run=True, report=ReconcileReport(V),
    )


def test_a_shared_candidate_goes_only_when_every_owner_copy_proves_it_gone(tmp_path):
    """Owner A (CE layout ``addons/viin_shared``) and owner B (repo-root
    ``viin_shared``): ``x_gone`` is in neither copy, ``x_half`` still in B's."""
    a = _copy(tmp_path / "ce", "addons/" + MOD, ["label"])
    b = _copy(tmp_path / "ee", MOD, ["label", "x_half"])
    owners = [
        {"repo_id": 1, "repo_basename": "ce", "local_path": str(a), "path": f"addons/{MOD}"},
        {"repo_id": 2, "repo_basename": "ee", "local_path": str(b), "path": MOD},
    ]
    # The stale nodes were written from A's copy (absolute path) - B must
    # still judge them inside its own module directory.
    in_a = str(a / "addons" / MOD / "models" / "rec.py")
    graph = _Graph([_field("n1", "x_gone", in_a), _field("n2", "x_half", in_a)])

    judged = _reconciler(graph)._judge_shared(
        MOD, owners, datetime.now(UTC), [], [],
    )

    assert judged["node_ids"] == ["n1"]
    assert judged["kept"] == 1 and any("x_half" in s for s in judged["sample"])
    assert graph.calls and "written_before" in graph.calls[0]
    assert ("field", "viin.shared.rec.x_half", None) in judged["chunk_filters"]


def test_a_shared_mass_drop_git_proves_gone_is_pruned_whatever_its_size(tmp_path):
    a = _copy(tmp_path / "ce", MOD, ["label"])
    b = _copy(tmp_path / "ee", MOD, ["label"])
    owners = [
        {"repo_id": 1, "repo_basename": "ce", "local_path": str(a), "path": MOD},
        {"repo_id": 2, "repo_basename": "ee", "local_path": str(b), "path": MOD},
    ]
    nodes = [_field(f"n{i}", f"x_old_{i}", f"{MOD}/models/rec.py") for i in range(40)]

    judged = _reconciler(_Graph(nodes))._judge_shared(MOD, owners, datetime.now(UTC), [], [])

    assert len(judged["node_ids"]) == 40 and judged["kept"] == 0


def test_an_owner_without_a_checkout_keeps_every_shared_candidate(tmp_path):
    a = _copy(tmp_path / "ce", MOD, ["label"])
    owners = [
        {"repo_id": 1, "repo_basename": "ce", "local_path": str(a), "path": MOD},
        {"repo_id": 2, "repo_basename": "ee", "local_path": str(tmp_path / "missing"),
         "path": MOD},
    ]
    nodes = [_field("n1", "x_gone", f"{MOD}/models/rec.py")]

    judged = _reconciler(_Graph(nodes))._judge_shared(MOD, owners, datetime.now(UTC), [], [])

    assert judged["node_ids"] == [] and judged["kept"] == 1
    assert judged["reason"]


def test_allow_mass_retire_prunes_the_whole_shared_selection(tmp_path):
    a = _copy(tmp_path / "ce", MOD, ["label", "x_live"])
    b = _copy(tmp_path / "ee", MOD, ["label", "x_live"])
    owners = [
        {"repo_id": 1, "repo_basename": "ce", "local_path": str(a), "path": MOD},
        {"repo_id": 2, "repo_basename": "ee", "local_path": str(b), "path": MOD},
    ]
    nodes = [_field("n1", "x_live", f"{MOD}/models/rec.py")]

    judged = _reconciler(_Graph(nodes), allow=True)._judge_shared(
        MOD, owners, datetime.now(UTC), [], [],
    )

    assert judged["node_ids"] == ["n1"]


def test_reanchor_moves_a_file_into_each_owner_module_directory():
    owners = [
        {"local_path": "/clones/ce", "path": "addons/m"},
        {"local_path": "/clones/ee", "path": "m"},
        {"local_path": "/clones/root_mod", "path": ""},
    ]
    from src.indexer.reconcile import _owner_anchors

    anchors = _owner_anchors(owners)
    assert _reanchor("/clones/ce/addons/m/models/a.py", owners[1], anchors) == "m/models/a.py"
    assert _reanchor("addons/m/models/a.py", owners[1], anchors) == "m/models/a.py"
    assert _reanchor("m/views/v.xml", owners[0], anchors) == "addons/m/views/v.xml"
    assert _reanchor("/clones/root_mod/models/a.py", owners[0], anchors) == (
        "addons/m/models/a.py"
    )
    assert _reanchor("other/x.py", owners[0], anchors) == "other/x.py", "unmapped stays"


def test_a_shared_module_at_an_owner_repo_root_is_judged_by_that_copy(tmp_path):
    """An owner whose copy is the repo root (ledger ``path`` "") must be read,
    not reported unavailable (git rejects an empty pathspec)."""
    a = _copy(tmp_path / "ce", MOD, ["label"])
    b = _copy(tmp_path / "solo", ".", ["label"])
    owners = [
        {"repo_id": 1, "repo_basename": "ce", "local_path": str(a), "path": MOD},
        {"repo_id": 2, "repo_basename": "solo", "local_path": str(b), "path": ""},
    ]
    nodes = [_field("n1", "x_gone", f"{MOD}/models/rec.py")]

    judged = _reconciler(_Graph(nodes))._judge_shared(MOD, owners, datetime.now(UTC), [], [])

    assert judged["reason"] is None, judged["reason"]
    assert judged["node_ids"] == ["n1"] and judged["kept"] == 0


class _SharedGraph(_Graph):
    """The writer calls ``prune_shared`` makes for one shared module."""

    def __init__(self, nodes):
        super().__init__(nodes)
        self.states: list[str] = []
        self.pruned: list[list[str]] = []

    def module_children_census(self, v, name, **kw):
        return {"module_exists": True, "stale": len(self.nodes), "total": 5,
                "rels_stale": 0, "rels_total": 0}

    def shared_prune_states(self, v, names):
        return {n: (self.states[-1] if self.states else None) for n in names}

    def record_shared_prune(self, v, name, state):
        self.states.append(state)

    def prune_module_children(self, v, name, *, only_ids=None, **kw):
        self.pruned.append(sorted(only_ids or []))
        return {"deleted": len(only_ids or []), "rels_deleted": 0}


class _SharedStore:
    def __init__(self, owners):
        self.owners = owners

    def shared_module_owners(self, v, conn=None):
        return {MOD: [dict(o) for o in self.owners]}


def test_an_owner_checkout_off_its_trusted_commit_votes_present(tmp_path):
    """Owner R2's last run tripped G-A (its checkout sits on another branch),
    so its ledger still reflects the trusted commit, which defines x_wip. R1
    and R2's wrong-branch HEAD both lack it: the shared prune must keep it -
    the wrong tree proves nothing - and judge it again once R2's checkout is
    back on a commit its ledger reflects."""
    a = _copy(tmp_path / "ce", MOD, ["label"])
    b = _copy(tmp_path / "ee", MOD, ["label", "x_wip"])
    trusted = subprocess.run(["git", "-C", str(b), "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
    _git(b, "checkout", "-q", "-b", "wrong")
    (b / MOD / "models" / "rec.py").write_text(
        "from odoo import fields, models\n\n\nclass Rec(models.Model):\n"
        "    _name = 'viin.shared.rec'\n    label = fields.Char()\n")
    _git(b, "commit", "-q", "-am", "wip on another branch")
    head_a = subprocess.run(["git", "-C", str(a), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()
    parsed = datetime.now(UTC)
    base = {"needs_rewrite": False, "retire_pending": False, "last_full_parse_at": parsed,
            "last_full_parse_run": "run-1", "last_full_parse_unobserved": [],
            "last_full_parse_embedded_at": None, "profile_name": "p", "path": MOD}
    owners = [
        {**base, "repo_id": 1, "repo_basename": "ce", "local_path": str(a),
         "presence_head_sha": head_a},
        {**base, "repo_id": 2, "repo_basename": "ee", "local_path": str(b),
         "presence_head_sha": trusted},
    ]
    graph = _SharedGraph([_field("n1", "x_wip", f"{MOD}/models/rec.py")])
    store = _SharedStore(owners)

    def reconcile():
        rec = _Reconciler(
            V, writer=graph, store=store, conn=object(), run_started_at=None,
            retire=True, allow_mass_retire=False, dry_run=False, report=ReconcileReport(V),
        )
        rec._owners.blockers = lambda *a, **k: []
        rec.prune_shared()
        return rec

    reconcile()
    assert graph.pruned == [], "a checkout off its trusted commit must not vote 'gone'"
    assert graph.states and "untrusted=[2]" in graph.states[-1]

    reconcile()
    assert graph.pruned == [] and len(graph.states) == 1, "unchanged state: not re-judged"

    _git(b, "checkout", "-q", V)
    _git(b, "merge", "-q", "--ff-only", "wrong")  # the work lands on the branch
    store.owners[1]["presence_head_sha"] = subprocess.run(
        ["git", "-C", str(b), "rev-parse", "HEAD"], check=True, capture_output=True,
        text=True).stdout.strip()  # its next trusted run reflects it
    reconcile()
    assert graph.pruned == [["n1"]], "trusted again: both copies prove it gone"


# ---------------------------------------------------------------------------
# Orphan evidence
# ---------------------------------------------------------------------------

def _row(repo_id: int, path: Path | None, *, branch: str = V) -> dict:
    return {"repo_id": repo_id, "repo_basename": f"r{repo_id}", "profile_name": "p",
            "local_path": str(path) if path else None, "repo_branch": branch,
            "profile_version": V}


def test_an_orphan_no_checkout_ships_is_provably_gone(tmp_path):
    repo = _copy(tmp_path / "r", "other_mod", [])
    rec = _reconciler(object())

    assert rec._checkout_claim([_row(1, repo)], "ghost_mod") is None
    assert rec._checkout_claim([], "ghost_mod") is None, "nothing registered ships it"


def test_an_orphan_a_checkout_still_ships_is_kept(tmp_path):
    repo = _copy(tmp_path / "r", MOD, [])
    why = _reconciler(object())._checkout_claim([_row(1, repo)], MOD)

    assert why and "HEAD tree" in why


def test_a_missing_checkout_proves_nothing(tmp_path):
    why = _reconciler(object())._checkout_claim([_row(1, tmp_path / "gone")], MOD)

    assert why and "unreadable" in why


def test_a_repo_placed_at_another_version_ships_nothing_here(tmp_path):
    """A module re-keyed to the version of its repo's branch leaves its old
    node: that repo's tree proves nothing about this version."""
    repo = _copy(tmp_path / "r", MOD, [])
    _git(repo, "branch", "-q", "-m", "98.0")

    assert _reconciler(object())._checkout_claim([_row(1, repo, branch="98.0")], MOD) is None
