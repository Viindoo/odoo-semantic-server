# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module lifecycle: classify, mass-retire gates, successors (OSM #378 node B5).

Business rules (plan 08 T13, M3, G-A, successors):

- Every name of the repo moves between ``present`` / ``excluded`` / ``retired``;
  a ledger row absent from the scan is retired (last known path kept); an
  already retired, still absent row yields nothing.
- Gate G-A: retirement needs a complete AND trusted scan.
- Gate G-B: a mass event blocks retirement - total wipe (all present modules
  dropped) always; otherwise more than half of the present modules AND at
  least 20. Soft drops = present -> retired and present -> unparseable
  (a parser regression looks like mass deletion). ``installable_false`` /
  ``license_skip`` flips never count: tvtmaaddons19 lands hundreds of modules
  as installable False on purpose (48d741b7de: 436).
- ``allow_mass_retire`` bypasses G-B only, never G-A.
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
# G-B thresholds (T13)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("n", "before", "expected"), [
    (3, 3, GATE_TOTAL_WIPE),      # small repo wiped out: trips regardless of floor
    (1, 1, GATE_TOTAL_WIPE),
    (2, 3, None),                 # 2 of 3: below the floor, not a wipe
    (20, 39, GATE_MASS_RETIRE),   # 20 > 19.5 and 20 >= 20
    (20, 40, None),               # exactly half is not "more than half"
    (19, 30, None),               # above half but below the floor
    (11, 20, None),
    (21, 40, GATE_MASS_RETIRE),
    (0, 0, None),
    (0, 10, None),
])
def test_mass_gate_threshold(n, before, expected):
    assert mass_gate_trips(n, before) == expected


def test_total_wipe_blocks_retirement():
    prev = _rows(present=["a", "b", "c"])
    tr = classify(prev, _scan(present=[]))
    g = apply_gates(tr, _scan(present=[]), trusted=True)
    assert g.retire_allowed is False
    assert g.mass_ok is False
    assert GATE_TOTAL_WIPE in g.tripped
    assert g.n_soft_drop == 3 and g.n_present_before == 3
    assert g.reasons and all(r.isascii() for r in g.reasons)


def test_two_of_three_retire_is_allowed():
    prev = _rows(present=["a", "b", "c"])
    scan = _scan(present=["a"])
    g = apply_gates(classify(prev, scan), scan, trusted=True)
    assert g.retire_allowed is True
    assert g.tripped == ()


def test_more_than_half_of_at_least_twenty_blocks_retirement():
    names = _names(30)
    scan = _scan(present=names[:9])  # 21 of 30 vanish
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True)
    assert g.retire_allowed is False
    assert GATE_MASS_RETIRE in g.tripped


def test_mass_installable_false_flip_does_not_trip():
    """tvtmaaddons19 48d741b7de shape: 436 modules become installable False
    at once - an intentional resting state, not a broken checkout."""
    names = _names(500)
    scan = _scan(
        present=names[436:],
        excluded={n: EXCLUSION_INSTALLABLE_FALSE for n in names[:436]},
    )
    tr = classify(_rows(present=names), scan)
    g = apply_gates(tr, scan, trusted=True)
    assert len(tr.of_kind(KIND_EXCLUDED)) == 436
    assert g.n_soft_drop == 0
    assert g.retire_allowed is True
    assert g.tripped == ()


def test_mass_license_skip_flip_does_not_trip():
    names = _names(40)
    scan = _scan(excluded={n: EXCLUSION_LICENSE_SKIP for n in names})
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True)
    assert g.n_soft_drop == 0 and g.retire_allowed is True


def test_mass_unparseable_trips_like_mass_deletion():
    """M3: a parser regression turns every manifest unparseable; that must
    block like a mass deletion."""
    names = _names(40)
    scan = _scan(present=names[:5], excluded={n: EXCLUSION_UNPARSEABLE for n in names[5:]})
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True)
    assert g.n_soft_drop == 35
    assert g.retire_allowed is False
    assert GATE_MASS_RETIRE in g.tripped


def test_all_unparseable_is_a_total_wipe():
    names = _names(3)
    scan = _scan(excluded={n: EXCLUSION_UNPARSEABLE for n in names})
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True)
    assert GATE_TOTAL_WIPE in g.tripped and g.retire_allowed is False


# --------------------------------------------------------------------------
# G-B on the GRAPH baseline: the first run after the ledger deploy (final
# review D3, owner decision 2026-09-24; ADR-0056 D6). The ledger is empty, so a
# ledger baseline would see 0 present modules and never trip; the baseline is
# what the graph attributes to the repo, judged by the same rule (drops > 50%
# of the baseline AND >= 20, or a total wipe).
# --------------------------------------------------------------------------

def _graph(names, version=V):
    return [(version, n) for n in names]


def test_first_run_on_an_empty_ledger_trips_when_the_scan_covers_far_less_than_the_graph():
    """The review's example: a 400-module repo whose scan now finds 150."""
    names = _names(400)
    scan = _scan(present=names[:150])
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names))
    assert g.baseline == "graph"
    assert GATE_MASS_RETIRE in g.tripped and g.mass_ok is False
    assert (g.n_soft_drop, g.n_present_before) == (250, 400)
    assert any("400" in r and "250" in r for r in g.reasons), g.reasons


def test_first_run_with_a_few_ghosts_does_not_trip():
    names = _names(400)
    scan = _scan(present=names[:395])
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names))
    assert g.baseline == "graph" and g.tripped == () and g.retire_allowed is True
    assert g.n_soft_drop == 5


def test_empty_graph_baseline_never_trips():
    """A repo the graph never saw (newly registered) has nothing to lose."""
    scan = _scan(present=_names(3))
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=[])
    assert g.tripped == () and g.retire_allowed is True


@pytest.mark.parametrize(("missing", "trips"), [(20, False), (21, True)])
def test_graph_baseline_uses_the_more_than_half_and_at_least_twenty_rule(missing, trips):
    names = _names(40)
    scan = _scan(present=names[missing:])
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names))
    assert (GATE_MASS_RETIRE in g.tripped) is trips, g


def test_graph_baseline_does_not_count_installable_false_or_license_skip_exclusions():
    """tvtmaaddons19 lands hundreds of modules as installable False on purpose."""
    names = _names(40)
    scan = _scan(present=names[:10], excluded={
        **{n: EXCLUSION_INSTALLABLE_FALSE for n in names[10:30]},
        **{n: EXCLUSION_LICENSE_SKIP for n in names[30:]},
    })
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names))
    assert g.n_soft_drop == 0 and g.tripped == ()


def test_graph_baseline_counts_unparseable_and_re_keyed_modules_as_drops():
    """Absent, unparseable, and now keyed at another version: each turns the
    graph node at its old version into an orphan the sweep would remove."""
    from dataclasses import replace

    names = _names(40)
    base = _scan(present=names[:10], excluded={n: EXCLUSION_UNPARSEABLE for n in names[10:25]})
    moved = _scan(present=names[25:])
    other_v = "98.0"
    scan = replace(base, modules={V: base.modules[V], other_v: moved.modules[V]})
    g = apply_gates(classify([], scan), scan, trusted=True, graph_baseline=_graph(names))
    assert g.n_soft_drop == 30 and GATE_MASS_RETIRE in g.tripped


def test_allow_mass_retire_bypasses_a_graph_baseline_trip():
    names = _names(40)
    scan = _scan(present=names[:5])
    g = apply_gates(classify([], scan), scan, trusted=True, allow_mass_retire=True,
                    graph_baseline=_graph(names))
    assert g.retire_allowed is True and GATE_MASS_RETIRE in g.bypassed


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


def test_allow_mass_retire_bypasses_mass_gate_only():
    names = _names(30)
    scan = _scan(present=names[:5])
    tr = classify(_rows(present=names), scan)

    ok = apply_gates(tr, scan, trusted=True, allow_mass_retire=True)
    assert ok.retire_allowed is True
    assert ok.mass_ok is True
    assert GATE_MASS_RETIRE in ok.bypassed
    assert GATE_MASS_RETIRE not in ok.tripped

    untrusted = apply_gates(tr, scan, trusted=False, allow_mass_retire=True)
    assert untrusted.retire_allowed is False
    assert GATE_SCAN_UNTRUSTED in untrusted.tripped

    incomplete_scan = _scan(present=names[:5], complete=False)
    inc = apply_gates(classify(_rows(present=names), incomplete_scan), incomplete_scan,
                      trusted=True, allow_mass_retire=True)
    assert inc.retire_allowed is False
    assert GATE_SCAN_INCOMPLETE in inc.tripped


def test_allow_mass_retire_bypasses_total_wipe_too():
    names = _names(3)
    scan = _scan(present=[])
    g = apply_gates(classify(_rows(present=names), scan), scan, trusted=True,
                    allow_mass_retire=True)
    assert g.retire_allowed is True
    assert GATE_TOTAL_WIPE in g.bypassed


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
