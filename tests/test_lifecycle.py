# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module lifecycle: classify, git evidence and gates, successors (OSM #378 node B5).

Business rules (plan 08 T13, M3, G-A, successors):

- Every name of the repo moves between ``present`` / ``excluded`` / ``retired``;
  a ledger row absent from the scan is retired (last known path kept); an
  already retired, still absent row yields nothing.
- Gate G-A: retirement needs a complete AND trusted scan.
- Git evidence (ADR-0057 phase 2): a drop whose manifest the raw HEAD tree no
  longer has retires whatever the count, with no approval; one whose
  manifest is still tracked is kept (``unproven``), never an exit 3. A total
  wipe (all present modules dropped) retires only when every drop is proven
  and the last reflected commit is an ancestor of HEAD; otherwise
  ``total_wipe`` holds it (exit 3). Soft drops = present -> retired and
  present -> unparseable. ``installable_false`` / ``license_skip`` flips
  never count: tvtmaaddons19 lands hundreds of modules as installable False
  on purpose (48d741b7de: 436).
- ``allow_mass_retire`` (emergency bypass) lifts a held wipe and retires the
  unproven drops, never G-A.
- Successors come from hard evidence only: git rename first, manifest
  ``old_technical_name`` second, nothing else.
"""
from __future__ import annotations

import subprocess
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from src.indexer.incremental import ManifestChange, compute_manifest_changes
from src.indexer.lifecycle import (
    GATE_MASS_RETIRE,
    GATE_SCAN_INCOMPLETE,
    GATE_SCAN_UNTRUSTED,
    GATE_TOTAL_WIPE,
    KIND_ADDED,
    KIND_EXCLUDED,
    KIND_INCLUDED,
    KIND_RESURRECTED,
    KIND_RETIRED,
    KIND_UNCHANGED,
    SUCCESSOR_GIT_RENAME,
    SUCCESSOR_OLD_TECHNICAL_NAME,
    apply_gates,
    classify,
    mass_gate_trips,
    pick_successors,
)
from src.indexer.models import (
    EXCLUSION_INSTALLABLE_FALSE,
    EXCLUSION_LICENSE_SKIP,
    EXCLUSION_UNPARSEABLE,
    ExcludedModule,
    ModuleInfo,
    RegistryScan,
)
from src.indexer.registry import build_registry_scan
from tests._odoo_checkouts import checkouts_parent

V = "17.0"


# --------------------------------------------------------------------------
# builders (public dataclasses only)
# --------------------------------------------------------------------------

def _scan(
    present: dict[str, str] | list[str] = (),
    excluded: dict[str, str] | None = None,
    *,
    complete: bool = True,
    old_technical_names: dict[str, str] | None = None,
    root: str = "/repo",
) -> RegistryScan:
    """present: names (dir = name) or {name: repo-relative dir};
    excluded: {name: reason}."""
    if not isinstance(present, dict):
        present = {n: n for n in present}
    otn = old_technical_names or {}
    mods = {
        n: ModuleInfo(
            name=n, odoo_version=V, repo="repo", path=f"{root}/{d}", depends=[],
            old_technical_name=otn.get(n), repo_root=Path(root),
        )
        for n, d in present.items()
    }
    ex = {
        n: ExcludedModule(name=n, reason=r, path=n, manifest_file="__manifest__.py")
        for n, r in (excluded or {}).items()
    }
    return RegistryScan(
        repo_path=root, odoo_version=V, branch=V,
        modules={V: mods} if mods else {},
        excluded=ex, shadowed={},
        tracked_paths=frozenset(), finder_paths=frozenset(),
        untracked=frozenset(), missing=frozenset(),
        complete=complete,
    )


def _rows(present=(), excluded=None, retired=()):
    rows = [{"name": n, "state": "present", "path": n} for n in present]
    rows += [{"name": n, "state": "excluded", "exclusion_reason": r, "path": n}
             for n, r in (excluded or {}).items()]
    rows += [{"name": n, "state": "retired", "path": n} for n in retired]
    return rows


def _names(n: int, prefix: str = "m") -> list[str]:
    return [f"{prefix}{i:03d}" for i in range(n)]


def _by_name(transitions):
    return {t.name: t for t in transitions.items}


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------

def test_every_state_change_gets_its_kind():
    prev = _rows(
        present=["kept", "gone", "to_wip"],
        excluded={"back": EXCLUSION_INSTALLABLE_FALSE},
        retired=["reborn", "still_gone"],
    )
    scan = _scan(
        present=["kept", "back", "reborn", "newcomer"],
        excluded={"to_wip": EXCLUSION_INSTALLABLE_FALSE},
    )
    t = _by_name(classify(prev, scan))

    assert t["kept"].kind == KIND_UNCHANGED
    assert t["gone"].kind == KIND_RETIRED and t["gone"].after == "retired"
    assert t["to_wip"].kind == KIND_EXCLUDED
    assert t["to_wip"].after_reason == EXCLUSION_INSTALLABLE_FALSE
    assert t["back"].kind == KIND_INCLUDED
    assert t["reborn"].kind == KIND_RESURRECTED
    assert t["newcomer"].kind == KIND_ADDED and t["newcomer"].before is None
    # A retired row that is still absent produces nothing.
    assert "still_gone" not in t


def test_retired_transition_keeps_the_last_known_path():
    prev = [{"name": "gone", "state": "present", "path": "old/place/gone"}]
    t = _by_name(classify(prev, _scan(present=[])))
    assert t["gone"].kind == KIND_RETIRED
    assert t["gone"].path == "old/place/gone"


def test_directory_move_sets_old_path_but_stays_unchanged():
    prev = [{"name": "stock", "state": "present", "path": "addons/stock"}]
    t = _by_name(classify(prev, _scan(present={"stock": "extra/stock"})))["stock"]
    assert t.kind == KIND_UNCHANGED
    assert t.old_path == "addons/stock"
    assert t.path == "extra/stock"


def test_rows_may_be_objects_not_only_mappings():
    class Row:
        def __init__(self, name, state):
            self.name, self.state = name, state

    tr = classify([Row("a", "present"), Row("b", "present")], _scan(present=["a"]))
    assert tr.retired_names == ["b"]
    assert tr.n_present_before == 2


def test_n_present_before_counts_only_present_rows():
    prev = _rows(present=["a", "b"], excluded={"c": EXCLUSION_INSTALLABLE_FALSE}, retired=["d"])
    assert classify(prev, _scan(present=["a", "b"])).n_present_before == 2


def test_soft_drops_are_vanished_or_unparseable_present_modules():
    prev = _rows(present=["gone", "broken", "wip", "lic", "kept"])
    scan = _scan(
        present=["kept"],
        excluded={
            "broken": EXCLUSION_UNPARSEABLE,
            "wip": EXCLUSION_INSTALLABLE_FALSE,
            "lic": EXCLUSION_LICENSE_SKIP,
        },
    )
    tr = classify(prev, scan)
    assert sorted(t.name for t in tr.soft_drops) == ["broken", "gone"]


def test_changed_omits_only_truly_unchanged_rows():
    prev = _rows(present=["same", "moved", "gone"])
    scan = _scan(present={"same": "same", "moved": "elsewhere/moved"})
    assert sorted(t.name for t in classify(prev, scan).changed) == ["gone", "moved"]


# --------------------------------------------------------------------------
# Git evidence for drops (ADR-0057 phase 2): delete what git proves gone,
# keep what it cannot prove - no count threshold, nobody approves
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("n", "before", "expected"), [
    (3, 3, GATE_TOTAL_WIPE),      # small repo wiped out: the wipe shape regardless of floor
    (1, 1, GATE_TOTAL_WIPE),
    (2, 3, None),                 # 2 of 3: below the floor, not a wipe
    (20, 39, GATE_MASS_RETIRE),   # 20 > 19.5 and 20 >= 20 (informational shape)
    (20, 40, None),               # exactly half is not "more than half"
    (19, 30, None),               # above half but below the floor
    (11, 20, None),
    (21, 40, GATE_MASS_RETIRE),
    (0, 0, None),
    (0, 10, None),
])
def test_mass_gate_threshold(n, before, expected):
    assert mass_gate_trips(n, before) == expected


def test_a_mass_drop_git_proves_gone_retires_without_a_flag():
    """21 of 30 modules deleted by a commit: the old G-B held them until an
    operator ran --allow-mass-retire; the HEAD tree proves each one gone."""
    names = _names(30)
    scan = _scan(present=names[:9])
    tr = classify(_rows(present=names), scan)
    g = apply_gates(tr, scan, trusted=True, shipped_at_head=names[:9])
    assert g.retire_allowed is True
    assert g.tripped == () and g.reasons == ()
    assert g.unproven == () and g.kept_names == frozenset()
    assert GATE_MASS_RETIRE not in g.tripped


def test_drops_whose_manifest_git_still_tracks_are_kept_not_alerted():
    """A scan that loses 21 modules whose manifests are still in the HEAD tree
    (a dispatch or registry regression): nothing retires, nothing alerts."""
    names = _names(30)
    scan = _scan(present=names[:9])
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True,
                    shipped_at_head=names)
    assert g.unproven == tuple(names[9:])
    assert g.kept_names == frozenset(names[9:])
    assert g.tripped == () and g.reasons == (), "keeping unproven data is never an exit 3"
    assert g.retire_allowed is True, "the repo's proven retirements proceed"


def test_only_the_proven_part_of_a_mixed_drop_retires():
    names = _names(15)
    scan = _scan(present=names[:5])
    still_tracked = names[10:]
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True,
                    shipped_at_head=names[:5] + still_tracked)
    assert g.kept_names == frozenset(still_tracked)
    assert set(names[5:10]).isdisjoint(g.kept_names)


def test_an_unreadable_head_tree_proves_nothing():
    names = _names(3)
    scan = _scan(present=names[:2])
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True,
                    shipped_at_head=None)
    assert g.kept_names == frozenset(names[2:])


def test_two_of_three_retire_is_allowed():
    prev = _rows(present=["a", "b", "c"])
    scan = _scan(present=["a"])
    g = apply_gates(classify(prev, scan), scan, trusted=True, shipped_at_head=["a"])
    assert g.retire_allowed is True
    assert g.tripped == () and g.kept_names == frozenset()


def test_total_wipe_in_the_branch_history_retires_without_a_flag():
    """Every module deleted by a commit of the registered branch."""
    calls = []

    def in_history():
        calls.append(1)
        return True

    prev = _rows(present=["a", "b", "c"])
    scan = _scan(present=[])
    g = apply_gates(classify(prev, scan), scan, trusted=True, shipped_at_head=[],
                    wipe_in_history=in_history)
    assert g.retire_allowed is True and g.tripped == ()
    assert calls == [1]


@pytest.mark.parametrize(("in_history", "shipped", "words"), [
    (False, [], "not an ancestor"),          # force-push, branch swap, wrong clone
    (None, [], "no earlier commit"),         # nothing recorded to compare with
    (True, ["b"], "still have a manifest"),  # the tree still ships a module
    (True, None, "cannot list"),             # git cannot read the tree
])
def test_total_wipe_git_does_not_prove_is_held_and_alerted(in_history, shipped, words):
    prev = _rows(present=["a", "b", "c"])
    scan = _scan(present=[])
    g = apply_gates(classify(prev, scan), scan, trusted=True, shipped_at_head=shipped,
                    wipe_in_history=lambda: in_history)
    assert g.retire_allowed is False and g.mass_ok is False
    assert GATE_TOTAL_WIPE in g.tripped
    assert g.n_soft_drop == 3 and g.n_present_before == 3
    assert any(words in r for r in g.reasons), g.reasons
    assert all(r.isascii() for r in g.reasons)


def test_the_ancestry_check_runs_only_for_a_total_wipe():
    def boom():
        raise AssertionError("ancestry checked for a partial drop")

    prev = _rows(present=["a", "b", "c"])
    scan = _scan(present=["a"])
    apply_gates(classify(prev, scan), scan, trusted=True, shipped_at_head=["a"],
                wipe_in_history=boom)


def test_mass_installable_false_flip_does_not_trip():
    """tvtmaaddons19 48d741b7de shape: 436 modules become installable False
    at once - an intentional resting state, not a broken checkout."""
    names = _names(500)
    scan = _scan(
        present=names[436:],
        excluded={n: EXCLUSION_INSTALLABLE_FALSE for n in names[:436]},
    )
    tr = classify(_rows(present=names), scan)
    g = apply_gates(tr, scan, trusted=True, shipped_at_head=names)
    assert len(tr.of_kind(KIND_EXCLUDED)) == 436
    assert g.n_soft_drop == 0
    assert g.retire_allowed is True
    assert g.tripped == () and g.unproven == ()


def test_mass_license_skip_flip_does_not_trip():
    names = _names(40)
    scan = _scan(excluded={n: EXCLUSION_LICENSE_SKIP for n in names})
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True,
                    shipped_at_head=names)
    assert g.n_soft_drop == 0 and g.retire_allowed is True


def test_mass_unparseable_is_kept_and_signalled_not_held_by_a_mass_gate():
    """M3: a parser regression turns many manifests unparseable. Those modules
    are kept by the manifest_unparseable rule (exit 3 + attention); no ratio
    gate holds the repo's other, git-proven retirements any more."""
    names = _names(40)
    scan = _scan(present=names[:5], excluded={n: EXCLUSION_UNPARSEABLE for n in names[5:]})
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True,
                    shipped_at_head=names)
    assert g.n_soft_drop == 35
    assert GATE_MASS_RETIRE not in g.tripped
    assert "manifest_unparseable" in g.tripped
    assert g.retire_allowed is True and g.unproven == ()


def test_all_unparseable_is_a_total_wipe():
    names = _names(3)
    scan = _scan(excluded={n: EXCLUSION_UNPARSEABLE for n in names})
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True,
                    shipped_at_head=names, wipe_in_history=lambda: True)
    assert GATE_TOTAL_WIPE in g.tripped and g.retire_allowed is False


# --------------------------------------------------------------------------
# The GRAPH baseline: the first run after the ledger deploy (ADR-0056 D6). The
# ledger is empty, so the drops are counted against what the graph attributes
# to the repo; since ADR-0057 phase 2 each is judged by git.
# --------------------------------------------------------------------------

def _graph(names, version=V):
    return [(version, n) for n in names]


def test_first_run_retires_graph_nodes_git_proves_gone_whatever_their_count():
    """A 400-module repo whose scan now finds 150 - the
    other 250 deleted upstream; the old gate held them for an operator."""
    names = _names(400)
    scan = _scan(present=names[:150])
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names),
                    shipped_at_head=names[:150])
    assert g.baseline == "graph"
    assert g.tripped == () and g.mass_ok is True and g.unproven == ()
    assert (g.n_soft_drop, g.n_present_before) == (250, 400)


def test_first_run_reports_graph_nodes_whose_manifest_git_still_tracks():
    names = _names(400)
    scan = _scan(present=names[:150])
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names),
                    shipped_at_head=names)
    assert g.tripped == ()
    assert g.unproven == tuple(sorted(f"{n}@{V}" for n in names[150:]))
    assert g.kept_names == frozenset(), "graph drops have no ledger row to keep"


def test_empty_graph_baseline_never_trips():
    """A repo the graph never saw (newly registered) has nothing to lose."""
    scan = _scan(present=_names(3))
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=[],
                    shipped_at_head=_names(3))
    assert g.tripped == () and g.retire_allowed is True


def test_graph_baseline_does_not_count_installable_false_or_license_skip_exclusions():
    """tvtmaaddons19 lands hundreds of modules as installable False on purpose."""
    names = _names(40)
    scan = _scan(present=names[:10], excluded={
        **{n: EXCLUSION_INSTALLABLE_FALSE for n in names[10:30]},
        **{n: EXCLUSION_LICENSE_SKIP for n in names[30:]},
    })
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names),
                    shipped_at_head=names)
    assert g.n_soft_drop == 0 and g.tripped == ()


def test_a_re_keyed_graph_module_is_proven_and_an_unparseable_one_is_left_to_its_rule():
    """A module the scan now keys at another version leaves its old node (the
    sweep's job, proven by the re-key even though its manifest is tracked); an
    unparseable one is kept by the manifest_unparseable rule, not reported as
    an unproven drop."""
    from dataclasses import replace

    names = _names(40)
    base = _scan(present=names[:10], excluded={n: EXCLUSION_UNPARSEABLE for n in names[10:25]})
    moved = _scan(present=names[25:])
    other_v = "98.0"
    scan = replace(base, modules={V: base.modules[V], other_v: moved.modules[V]})
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names),
                    shipped_at_head=names)
    assert g.n_soft_drop == 30
    assert g.tripped == () and g.unproven == ()


def test_a_graph_baseline_total_wipe_needs_the_branch_history():
    names = _names(5)
    scan = _scan(present=[])
    held = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names),
                       shipped_at_head=[], wipe_in_history=lambda: False)
    assert GATE_TOTAL_WIPE in held.tripped and held.mass_ok is False
    ok = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names),
                     shipped_at_head=[], wipe_in_history=lambda: True)
    assert ok.tripped == () and ok.mass_ok is True


# --------------------------------------------------------------------------
# E2E-D1: a manifest that was read but does not parse keeps its module
# --------------------------------------------------------------------------

def test_unparseable_manifest_of_a_present_module_is_kept_and_signalled_not_blocking():
    """to_approvals was present; its manifest text now does not parse. The
    module is kept (``manifest_unparseable`` -> exit 3 + attention) but the
    repo's other retirements proceed (G-A and G-B stay green)."""
    from src.indexer import lifecycle as lifecycle_mod

    prev = _rows(present=["to_approvals", "viin_hr", "viin_ai_rag"])
    scan = _scan(present=["viin_hr"], excluded={"to_approvals": EXCLUSION_UNPARSEABLE})
    g = apply_gates(classify(prev, scan), scan, trusted=True)
    assert lifecycle_mod.unparseable_kept(classify(prev, scan), scan) == ["to_approvals"]
    assert lifecycle_mod.GATE_MANIFEST_UNPARSEABLE in g.tripped
    assert g.scan_ok is True and g.mass_ok is True and g.retire_allowed is True
    assert any("to_approvals" in r for r in g.reasons), g.reasons


def test_unparseable_manifest_of_a_graph_module_is_kept_even_without_a_ledger_row():
    from src.indexer import lifecycle as lifecycle_mod

    scan = _scan(present=["viin_hr"], excluded={"to_approvals": EXCLUSION_UNPARSEABLE})
    tr = classify([], scan)
    assert lifecycle_mod.unparseable_kept(tr, scan, ["to_approvals"]) == ["to_approvals"]
    assert lifecycle_mod.unparseable_kept(tr, scan) == [], (
        "a never-indexed module with a broken manifest has nothing to keep")
    g = apply_gates(tr, scan, trusted=True, indexed_names=["to_approvals"])
    assert lifecycle_mod.GATE_MANIFEST_UNPARSEABLE in g.tripped


# --------------------------------------------------------------------------
# G-A (scan trust) and the bypass
# --------------------------------------------------------------------------

def test_incomplete_scan_blocks_even_a_single_retirement():
    prev = _rows(present=["a", "b", "c"])
    scan = _scan(present=["a", "b"], complete=False)
    g = apply_gates(classify(prev, scan), scan, trusted=True)
    assert g.scan_ok is False
    assert g.retire_allowed is False
    assert GATE_SCAN_INCOMPLETE in g.tripped


def test_untrusted_scan_blocks_retirement():
    prev = _rows(present=["a", "b", "c"])
    scan = _scan(present=["a", "b"])
    g = apply_gates(classify(prev, scan), scan, trusted=False)
    assert g.scan_ok is False
    assert g.retire_allowed is False
    assert GATE_SCAN_UNTRUSTED in g.tripped


def test_allow_mass_retire_is_an_emergency_bypass_never_of_g_a():
    """It retires what git cannot prove gone, never on a bad scan."""
    names = _names(30)
    scan = _scan(present=names[:5])
    tr = classify(_rows(present=names), scan)

    ok = apply_gates(tr, scan, trusted=True, allow_mass_retire=True, shipped_at_head=names)
    assert ok.retire_allowed is True
    assert ok.unproven == tuple(names[5:]), "still reported"
    assert ok.kept_names == frozenset(), "and retired anyway"

    untrusted = apply_gates(tr, scan, trusted=False, allow_mass_retire=True,
                            shipped_at_head=names)
    assert untrusted.retire_allowed is False
    assert GATE_SCAN_UNTRUSTED in untrusted.tripped

    incomplete_scan = _scan(present=names[:5], complete=False)
    inc = apply_gates(classify(_rows(present=names), incomplete_scan), incomplete_scan,
                      trusted=True, allow_mass_retire=True, shipped_at_head=names)
    assert inc.retire_allowed is False
    assert GATE_SCAN_INCOMPLETE in inc.tripped


def test_allow_mass_retire_lifts_a_held_total_wipe():
    names = _names(3)
    scan = _scan(present=[])
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True,
                    allow_mass_retire=True, shipped_at_head=[],
                    wipe_in_history=lambda: False)
    assert g.retire_allowed is True
    assert GATE_TOTAL_WIPE in g.bypassed and GATE_TOTAL_WIPE not in g.tripped


# --------------------------------------------------------------------------
# pick_successors
# --------------------------------------------------------------------------

def _rename(old: str, new: str) -> ManifestChange:
    return ManifestChange("R", f"{new}/__manifest__.py", f"{old}/__manifest__.py", 93)


def test_git_rename_names_the_successor():
    scan = _scan(present=["test_viin_pylint"])
    tr = classify(_rows(present=["test_pylint"]), scan)
    got = pick_successors(tr, [_rename("test_pylint", "test_viin_pylint")], scan)
    assert set(got) == {"test_pylint"}
    assert got["test_pylint"].names == ("test_viin_pylint",)
    assert got["test_pylint"].source == SUCCESSOR_GIT_RENAME


def test_git_rename_is_preferred_over_old_technical_name():
    scan = _scan(present=["renamed", "claimer"], old_technical_names={"claimer": "old"})
    tr = classify(_rows(present=["old"]), scan)
    got = pick_successors(tr, [_rename("old", "renamed")], scan)
    assert got["old"].names == ("renamed",)
    assert got["old"].source == SUCCESSOR_GIT_RENAME


def test_old_technical_name_names_every_claimer_sorted():
    """No git rename: modules declaring old_technical_name = retired name are
    its successors; several claimers = a split."""
    scan = _scan(present=["viin_b", "viin_a", "unrelated"],
                 old_technical_names={"viin_a": "to_old", "viin_b": "to_old"})
    tr = classify(_rows(present=["to_old", "unrelated"]), scan)
    got = pick_successors(tr, [], scan)
    assert got["to_old"].names == ("viin_a", "viin_b")
    assert got["to_old"].source == SUCCESSOR_OLD_TECHNICAL_NAME


def test_no_evidence_means_no_successor_even_for_a_similar_name():
    """Never inferred from name similarity: test_pylint -> test_viin_pylint
    without a rename pair or an old_technical_name claim is NOT a successor."""
    scan = _scan(present=["test_viin_pylint"])
    tr = classify(_rows(present=["test_pylint"]), scan)
    unrelated_add = ManifestChange("A", "test_viin_pylint/__manifest__.py")
    assert pick_successors(tr, [unrelated_add], scan) == {}


def test_rename_to_a_name_the_scan_does_not_observe_is_not_evidence():
    scan = _scan(present=[])
    tr = classify(_rows(present=["old"]), scan)
    assert pick_successors(tr, [_rename("old", "phantom")], _scan(present=["x"])) == {}


def test_rename_to_an_excluded_module_is_still_the_successor():
    """D7: the rename is a git fact even when the new name is installable False."""
    scan = _scan(excluded={"new_name": EXCLUSION_INSTALLABLE_FALSE})
    tr = classify(_rows(present=["old"]), scan)
    got = pick_successors(tr, [_rename("old", "new_name")], scan)
    assert got["old"].names == ("new_name",)


def test_old_technical_name_on_an_excluded_module_is_not_evidence():
    """Rule 2 counts PRESENT modules only."""
    scan = RegistryScan(
        repo_path="/repo", odoo_version=V, branch=V, modules={},
        excluded={"viin_x": ExcludedModule("viin_x", EXCLUSION_INSTALLABLE_FALSE, "viin_x",
                                           "__manifest__.py")},
        shadowed={}, tracked_paths=frozenset(), finder_paths=frozenset(),
        untracked=frozenset(), missing=frozenset(), complete=True,
    )
    tr = classify(_rows(present=["to_x"]), scan)
    assert pick_successors(tr, [], scan) == {}


def test_only_retired_names_get_successors():
    scan = _scan(present=["stock", "inventory"])
    tr = classify(_rows(present=["stock"]), scan)
    assert pick_successors(tr, [_rename("stock", "inventory")], scan) == {}


# --------------------------------------------------------------------------
# Real cases (manifest-only archives of real commits - no checkout mutation)
# --------------------------------------------------------------------------

def _manifest_archive_scan(repo: Path, rev: str, dest: Path, version: str) -> RegistryScan:
    """Scan the manifests of *rev* extracted to *dest* (a non-git dir)."""
    out = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", rev, "--",
         ":(glob)**/__manifest__.py"],
        capture_output=True, check=True,
    ).stdout
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=BytesIO(out)) as tar:
        tar.extractall(dest, filter="data")
    return build_registry_scan(str(dest), version)


def _ledger_from(scan: RegistryScan) -> list[dict]:
    rows = [{"name": n, "state": "present",
             "path": str(Path(scan.module(n).path).relative_to(scan.repo_path))}
            for n in scan.present_names()]
    rows += [{"name": n, "state": "excluded", "exclusion_reason": e.reason, "path": e.path}
             for n, e in scan.excluded.items()]
    return rows


def _real_repo_with(name: str, sha: str) -> Path:
    repo = checkouts_parent() / name
    if not repo.is_dir():
        pytest.skip(f"{name} checkout not on disk")
    r = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                       capture_output=True)
    if r.returncode != 0:
        pytest.skip(f"commit {sha[:10]} not in {name}")
    return repo


@pytest.mark.odoo_source
def test_real_tvtmaaddons17_rename_retires_test_pylint_with_git_successor(tmp_path, monkeypatch):
    """#378 end to end at the pure layer: ledger at 0240c6b77f^, scan at
    0240c6b77f -> exactly test_pylint retires, successor test_viin_pylint
    from git, and the gates allow it."""
    monkeypatch.setattr("src.indexer.registry.get_module_commit_sha", lambda *a: None)
    sha = "0240c6b77fd567422440d6962d536da81866e12a"
    repo = _real_repo_with("tvtmaaddons17", sha)
    before = _manifest_archive_scan(repo, f"{sha}^", tmp_path / "before", "17.0")
    after = _manifest_archive_scan(repo, sha, tmp_path / "after", "17.0")

    tr = classify(_ledger_from(before), after)
    assert tr.retired_names == ["test_pylint"]
    assert [t.name for t in tr.of_kind(KIND_ADDED)] == ["test_viin_pylint"]

    got = pick_successors(tr, compute_manifest_changes(repo, f"{sha}^", sha), after)
    assert {k: (v.names, v.source) for k, v in got.items()} == {
        "test_pylint": (("test_viin_pylint",), SUCCESSOR_GIT_RENAME),
    }
    assert apply_gates(tr, after, trusted=True).retire_allowed is True


@pytest.mark.odoo_source
def test_real_tvtmaaddons19_installable_flip_does_not_trip(tmp_path, monkeypatch):
    """tvtmaaddons19 a057495728 flips a batch of present modules to
    installable False (sample viin_fleet_booking_approval): excluded
    transitions only, zero soft drops, gate passes."""
    monkeypatch.setattr("src.indexer.registry.get_module_commit_sha", lambda *a: None)
    sha = "a057495728"
    repo = _real_repo_with("tvtmaaddons19", sha)
    before = _manifest_archive_scan(repo, f"{sha}^", tmp_path / "before", "19.0")
    after = _manifest_archive_scan(repo, sha, tmp_path / "after", "19.0")

    tr = classify(_ledger_from(before), after)
    flipped = {t.name for t in tr.of_kind(KIND_EXCLUDED)
               if t.after_reason == EXCLUSION_INSTALLABLE_FALSE}
    assert "viin_fleet_booking_approval" in flipped
    assert len(flipped) >= 20, "positive control: a mass flip"
    g = apply_gates(tr, after, trusted=True)
    assert g.n_soft_drop == 0
    assert g.retire_allowed is True and g.tripped == ()
