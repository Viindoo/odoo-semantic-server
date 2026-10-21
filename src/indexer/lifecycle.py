# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module lifecycle classification, git evidence and safety gates (ADR-0056,
ADR-0057 phase 2).

No Neo4j, no Postgres (``removal_evidence`` reads git history). They compare
the previous presence-ledger rows of ONE repo with a fresh ``RegistryScan`` of
that repo and decide what changed, which drops git proves (the raw HEAD tree
no longer has the manifest), whether retirement may proceed, and who
succeeds a retired module. Callers (the per-repo index path and the
per-version reconcile) own every write.

States of a module name inside a repo (ledger ``state``):

- ``present``  - tracked manifest, indexable (in ``scan.modules``);
- ``excluded`` - tracked manifest, not indexable (``installable_false``,
  ``license_skip`` or ``unparseable``);
- ``retired``  - no tracked manifest for that name anywhere in the repo.

Import discipline: models, git_utils and incremental only.
"""
from __future__ import annotations

import functools
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from src.git_utils import MANIFEST_FILENAMES, removing_commit, rev_parse
from src.indexer.incremental import (
    ManifestChange,
    _git_z,
    _parse_name_status,
    compute_manifest_changes,
)
from src.indexer.models import (
    EXCLUSION_INSTALLABLE_FALSE,
    EXCLUSION_LICENSE_SKIP,
    EXCLUSION_UNPARSEABLE,
    RegistryScan,
)

logger = logging.getLogger(__name__)

STATE_PRESENT = "present"
STATE_EXCLUDED = "excluded"
STATE_RETIRED = "retired"

# Transition kinds (Transition.kind).
KIND_ADDED = "added"              # never seen (or no row) -> present/excluded
KIND_RESURRECTED = "resurrected"  # retired -> present/excluded
KIND_RETIRED = "retired"          # present/excluded -> retired
KIND_EXCLUDED = "excluded"        # present -> excluded
KIND_INCLUDED = "included"        # excluded -> present
KIND_UNCHANGED = "unchanged"      # same state (path or reason may differ)

# Exclusion reasons that come from a successfully parsed manifest or explicit
# config: a module moving there was positively observed, so it is never a drop
# (real case: 436 modules flipped installable False at tvtmaaddons 19.0,
# commit 48d741b7de).
GATE_EXEMPT_REASONS = frozenset({EXCLUSION_INSTALLABLE_FALSE, EXCLUSION_LICENSE_SKIP})

GATE_SCAN_INCOMPLETE = "scan_incomplete"
GATE_SCAN_UNTRUSTED = "scan_untrusted"
# The ratio id of mass_gate_trips; no longer a delete gate since ADR-0057
# phase 2 (a drop is judged by the HEAD tree, whatever the count).
GATE_MASS_RETIRE = "mass_retire"
# A total wipe git does not prove (see apply_gates): the one remaining hold.
GATE_TOTAL_WIPE = "total_wipe"
# Soft signal, never blocks the repo's other retirements: a tracked manifest
# of an indexed module was read but does not parse; the module is kept.
GATE_MANIFEST_UNPARSEABLE = "manifest_unparseable"

MASS_RETIRE_FRACTION = 0.5
MASS_RETIRE_FLOOR = 20

# ``module_presence.retire_blocked_by`` vocabulary. The per-repo index path
# writes the first two when it flags names pending; the reconcile writes the
# rest and re-evaluates only rows it blocked itself (or never blocked).
BLOCKED_NO_RETIRE = "no_retire"
BLOCKED_GATE_PREFIX = "gate:"
BLOCKED_UNDECIDABLE_PREFIX = "undecidable:"
BLOCKED_SKIPPED_RECENT = "skipped_recent"
BLOCKED_ERROR_PREFIX = "error:"


def reconcile_may_retry(blocked_by: str | None) -> bool:
    """True when a pending row may be executed by the per-version reconcile:
    never blocked, or blocked only by a previous reconcile (undecidable owner,
    concurrent re-write, transient error). A gate trip or ``--no-retire`` is
    lifted only by that repo's own next clean scan."""
    if not blocked_by:
        return True
    return blocked_by == BLOCKED_SKIPPED_RECENT or blocked_by.startswith(
        (BLOCKED_UNDECIDABLE_PREFIX, BLOCKED_ERROR_PREFIX)
    )


SUCCESSOR_GIT_RENAME = "git_rename"
SUCCESSOR_OLD_TECHNICAL_NAME = "old_technical_name"
# N:1 merge (#380 item 3): the removing commit moved at least half of the
# module's files into ONE module directory that existed before the commit and
# still exists after it (viin_ai_rag, viin_ai_agent, ... -> viin_ai at 17.0).
SUCCESSOR_GIT_MERGE = "git_merge"

# Directory-share thresholds, one convention for both git oracles (see
# ``_share_reached``):
# - merge (``merged_into``): INCLUSIVE half (owner decision 2026-10-20): a merge
#   needs 2 * moved >= files of the old directory, so viin_ai_memory (exactly 17
#   of its 34 files git-renamed into viin_ai/) qualifies; AND at least
#   MERGE_MIN_MOVED_FILES moved files, so a tiny module (a
#   manifest plus one file) whose single file -M-pairs with a file added to some
#   module in a large commit is not taken for a merge;
# - rename fallback (``_directory_moved_into``): STRICT majority (2 * moved >
#   files), which keeps a rename to ONE destination; an exact half split names
#   no rename destination and is left to the merge rule.
MERGE_MIN_SHARE_NUMERATOR = 1
MERGE_MIN_SHARE_DENOMINATOR = 2
MERGE_MIN_MOVED_FILES = 3

# "[MERGE] viin_ai_rag: merge into viin_ai" / "merged inside of sale_loyalty":
# prose, so only a cross-check of the git evidence, never evidence itself.
_MERGE_SUBJECT_RE = re.compile(
    r"\bmerged?\s+(?:in|into|inside(?:\s+of)?|to)\s+[`'\"]?([A-Za-z0-9_]+)",
    re.IGNORECASE,
)


def _field(row: object, key: str) -> object:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


@dataclass(frozen=True)
class Transition:
    """How one module name of the repo moved between the previous ledger row and
    the scan. ``before`` is None when the ledger had no row for the name.
    ``path`` is the current module directory (the last known one when retired);
    ``old_path`` is the previous directory when it differs."""
    name: str
    before: str | None
    after: str
    before_reason: str | None = None
    after_reason: str | None = None
    path: str | None = None
    old_path: str | None = None

    @property
    def kind(self) -> str:
        if self.before is None:
            return KIND_ADDED
        if self.before == STATE_RETIRED:
            return KIND_RESURRECTED
        if self.after == STATE_RETIRED:
            return KIND_RETIRED
        if self.before == self.after:
            return KIND_UNCHANGED
        if self.after == STATE_EXCLUDED:
            return KIND_EXCLUDED
        return KIND_INCLUDED

    @property
    def is_soft_drop(self) -> bool:
        """A previously present module that vanished, or became unparseable.

        These are the transitions a broken checkout, a scan regression or a
        permission fault would produce en masse, so each is judged by the
        git evidence of :func:`apply_gates`.
        """
        if self.before != STATE_PRESENT:
            return False
        if self.after == STATE_RETIRED:
            return True
        return self.after == STATE_EXCLUDED and self.after_reason == EXCLUSION_UNPARSEABLE


@dataclass(frozen=True)
class Transitions:
    """All transitions of one repo, sorted by name."""
    items: tuple[Transition, ...]
    n_present_before: int

    def of_kind(self, kind: str) -> tuple[Transition, ...]:
        return tuple(t for t in self.items if t.kind == kind)

    @property
    def retired(self) -> tuple[Transition, ...]:
        return self.of_kind(KIND_RETIRED)

    @property
    def retired_names(self) -> list[str]:
        return [t.name for t in self.retired]

    @property
    def soft_drops(self) -> tuple[Transition, ...]:
        return tuple(t for t in self.items if t.is_soft_drop)

    @property
    def changed(self) -> tuple[Transition, ...]:
        """Every transition except an unchanged one at the same path."""
        return tuple(
            t for t in self.items
            if t.kind != KIND_UNCHANGED or t.old_path is not None
            or t.before_reason != t.after_reason
        )


def classify(prev_rows: Iterable[object], scan: RegistryScan) -> Transitions:
    """Classify the previous ledger rows of one repo against a scan of it.

    ``prev_rows``: the repo's ledger rows (mappings or objects) with at least
    ``name``, ``state`` and optionally ``exclusion_reason`` and ``path``. Rows
    already ``retired`` and still absent produce no transition.

    Every name in the scan yields exactly one transition (``present`` if in
    ``scan.modules``, else ``excluded``); every non-retired row whose name is
    not in the scan yields a ``retired`` transition. ``n_present_before`` counts
    the rows in state ``present``.
    """
    prev: dict[str, object] = {}
    for row in prev_rows:
        name = _field(row, "name")
        if isinstance(name, str) and name:
            prev[name] = row
    n_present_before = sum(
        1 for row in prev.values() if _field(row, "state") == STATE_PRESENT
    )

    now: dict[str, tuple[str, str | None, str | None]] = {}
    for name in scan.present_names():
        info = scan.module(name)
        rel = info.relative_path(info.path) if info is not None else None
        now[name] = (STATE_PRESENT, None, rel)
    for name, ex in scan.excluded.items():
        if name not in now:
            now[name] = (STATE_EXCLUDED, ex.reason, ex.path)

    items: list[Transition] = []
    for name in sorted(set(prev) | set(now)):
        row = prev.get(name)
        before = _field(row, "state") if row is not None else None
        before_reason = _field(row, "exclusion_reason") if row is not None else None
        before_path = _field(row, "path") if row is not None else None
        if name in now:
            after, after_reason, path = now[name]
            old_path = (
                before_path
                if before_path and path and before_path != path and before != STATE_RETIRED
                else None
            )
            items.append(Transition(
                name=name,
                before=before if isinstance(before, str) else None,
                after=after,
                before_reason=before_reason if before == STATE_EXCLUDED else None,
                after_reason=after_reason,
                path=path,
                old_path=old_path,
            ))
        elif before in (STATE_PRESENT, STATE_EXCLUDED):
            items.append(Transition(
                name=name,
                before=before,
                after=STATE_RETIRED,
                before_reason=before_reason if before == STATE_EXCLUDED else None,
                path=before_path if isinstance(before_path, str) else None,
            ))
    return Transitions(items=tuple(items), n_present_before=n_present_before)


@dataclass(frozen=True)
class GateResult:
    """Outcome of the safety gates and the git evidence for one repo scan.

    - G-A (``scan_ok``): the scan is complete (no tracked manifest missing on
      disk) AND trusted (the tree is the registered branch). Never bypassable.
    - Git evidence (ADR-0057 phase 2, :func:`apply_gates`): a drop (present ->
      retired) whose manifest the raw HEAD tree no longer has is retired with
      no threshold and no approval; a drop whose manifest git still tracks is
      ``unproven`` and kept (``kept_names``), never an exit 3.
    - ``total_wipe`` (``mass_ok`` False): every module vanished and the wipe is
      not proven by git (a manifest still tracked, the HEAD tree unreadable, or
      the last reflected commit not an ancestor of HEAD). Nothing of the repo
      retires; exit 3. ``allow_mass_retire`` bypasses it (``bypassed``) and
      lets the unproven drops retire too (``allow_unproven``).

    ``retire_allowed`` = G-A and no held total wipe. ``tripped`` lists the gate
    ids that blocked (``scan_incomplete``, ``scan_untrusted``, ``total_wipe``)
    plus the soft signal ``manifest_unparseable``. ``reasons`` are
    operator-facing sentences for ``lifecycle_attention``. ``unproven`` holds
    names (graph baseline: ``name@version``) kept because git still tracks
    their manifest; they are not in ``reasons`` (no attention, like a parser
    gap) but in the run log and the ``lifecycle-audit`` report.
    """
    scan_ok: bool
    mass_ok: bool
    n_soft_drop: int
    n_present_before: int
    tripped: tuple[str, ...] = ()
    bypassed: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    # "ledger" (the repo's present rows) or "graph" (the Module nodes the
    # graph attributes to a repo the ledger never reflected, see apply_gates).
    baseline: str = "ledger"
    unproven: tuple[str, ...] = ()
    allow_unproven: bool = False

    @property
    def retire_allowed(self) -> bool:
        return self.scan_ok and self.mass_ok

    @property
    def kept_names(self) -> frozenset[str]:
        """Absent names NOT to flag pending: the unproven ledger drops, unless
        ``--allow-mass-retire`` lets them go."""
        if self.allow_unproven or self.baseline != "ledger":
            return frozenset()
        return frozenset(self.unproven)


def mass_gate_trips(
    n_soft_drop: int, n_present_before: int, n_present_after: int | None = None,
) -> str | None:
    """The mass shape of these counts, or None (``apply_gates`` and log lines).

    ``total_wipe``: modules were present before and NONE is present now - the
    shape of a broken clone or checkout; ``apply_gates`` holds it unless git
    proves the wipe. ``mass_retire`` (the ratio) is informational only.
    ``n_present_after`` defaults to ``n_present_before - n_soft_drop`` (no
    module added). A rename or merge that leaves at least one present module
    (a one-module repo whose module is renamed) is judged only by the ratio
    rule: ``n_soft_drop > 0.5 * n_present_before AND n_soft_drop >= 20``.
    """
    after = n_present_before - n_soft_drop if n_present_after is None else n_present_after
    if n_present_before > 0 and n_soft_drop > 0 and after <= 0:
        return GATE_TOTAL_WIPE
    if (
        n_soft_drop > MASS_RETIRE_FRACTION * n_present_before
        and n_soft_drop >= MASS_RETIRE_FLOOR
    ):
        return GATE_MASS_RETIRE
    return None


def graph_baseline_drops(
    baseline: Iterable[tuple[str, str]], scan: RegistryScan,
) -> list[tuple[str, str]]:
    """``(odoo_version, name)`` pairs of *baseline* the scan no longer covers.

    *baseline* is what the graph attributes to the repo (every version key).
    A pair is covered when the scan indexes the name at that version key, or
    excludes the name for a gate-exempt reason (``installable_false``,
    ``license_skip``: positively observed). An absent name, an unparseable
    one, and a name the scan now keys at another version all count - each
    turns the graph node into an orphan the version sweep removes.
    """
    covered = {(v, n) for v, mods in scan.modules.items() for n in mods}
    exempt = {n for n, ex in scan.excluded.items() if ex.reason in GATE_EXEMPT_REASONS}
    return sorted(
        (v, n) for v, n in set(baseline) if (v, n) not in covered and n not in exempt
    )


def unparseable_kept(
    transitions: Transitions,
    scan: RegistryScan,
    indexed_names: Iterable[str] | None = None,
) -> list[str]:
    """Names whose tracked manifest was read but does not parse, of a module
    the index has: present in the repo's previous ledger rows, or with a graph
    node (*indexed_names*). Such a module is still shipped; it is kept (never
    retired or swept) until the manifest parses again. Sorted."""
    indexed = set(indexed_names or ())
    was_present = {t.name for t in transitions.items if t.before == STATE_PRESENT}
    return sorted(
        n for n, ex in scan.excluded.items()
        if ex.reason == EXCLUSION_UNPARSEABLE and (n in was_present or n in indexed)
    )


def apply_gates(
    transitions: Transitions,
    scan: RegistryScan,
    *,
    trusted: bool,
    allow_mass_retire: bool = False,
    graph_baseline: Iterable[tuple[str, str]] | None = None,
    indexed_names: Iterable[str] | None = None,
    shipped_at_head: Iterable[str] | None = None,
    wipe_in_history: Callable[[], bool | None] | None = None,
) -> GateResult:
    """Evaluate G-A and the git evidence for one repo's drops (see ``GateResult``).

    ``trusted`` must come from ``git_utils.head_matches_remote_branch`` for the
    repo's registered branch, evaluated after the pre-scan refresh.

    *shipped_at_head*: the module names the repo's raw HEAD tree has a
    manifest for (``git_utils.head_tree_module_names``, no version dispatch),
    None when git cannot list it. A drop is **proven** when its name is not
    there (ADR-0057 phase 2: git proves the module gone; no count threshold,
    nobody approves) or, against the graph baseline, when the scan now keys
    the name at another version (re-keyed). Every other drop is **unproven**
    (``unproven``): its manifest is still in the tree, so the scan, not git,
    lost it - it is kept (not flagged pending), logged, and reported by
    ``lifecycle-audit`` (``unproven_drops``), never an exit 3.

    *graph_baseline* (given while the ledger has never reflected the repo,
    ``presence_head_sha`` NULL - the first runs after the ledger was deployed,
    or a newly registered repo) replaces the ledger baseline: the
    ``(odoo_version, name)`` Module nodes the graph attributes to the repo,
    against :func:`graph_baseline_drops`. Such nodes leave through the orphan
    sweep, which judges each against the checkouts itself; an empty baseline
    never trips.

    **Total wipe** (modules were present before and none is present now):
    retired only when every drop is proven AND *wipe_in_history* (called only
    for this shape) answers True - the commit the ledger last reflected is an
    ancestor of HEAD, so the wipe is a commit of the branch's own history, not
    a force-push, a branch swap or a wrong clone. Otherwise gate
    ``total_wipe`` trips: nothing of the repo retires, its presence is not
    synced, the run exits 3 (checkout integrity: OSM cannot tell what the
    repo ships). ``allow_mass_retire`` (emergency bypass, never in a timer)
    lifts that trip and also retires the unproven drops (never G-A).

    ``manifest_unparseable`` (:func:`unparseable_kept`, *indexed_names* = the
    scan's unparseable names that have a graph node) is listed in ``tripped``
    with a reason, so the run exits 3, but it changes neither ``scan_ok`` nor
    ``mass_ok``: the repo's other retirements proceed, the module is kept by
    the reconcile (never swept, never re-owned) until it parses again.
    """
    tripped: list[str] = []
    bypassed: list[str] = []
    reasons: list[str] = []

    if not scan.complete:
        tripped.append(GATE_SCAN_INCOMPLETE)
        if scan.missing:
            sample = ", ".join(sorted(scan.missing)[:3])
            reasons.append(
                f"scan incomplete: {len(scan.missing)} tracked manifest(s) missing on "
                f"disk ({sample}); no module retired"
            )
        unreadable = getattr(scan, "unreadable", frozenset())
        if unreadable:
            sample = ", ".join(sorted(unreadable)[:3])
            reasons.append(
                f"scan incomplete: {len(unreadable)} tracked manifest(s) cannot be "
                f"read (permission or I/O error: {sample}); no module retired"
            )
    if not trusted:
        tripped.append(GATE_SCAN_UNTRUSTED)
        branch = scan.branch or "(none)"
        reasons.append(
            f"scan untrusted: checked-out tree is not origin/{branch}; no module retired"
        )
    scan_ok = not tripped

    shipped = None if shipped_at_head is None else frozenset(shipped_at_head)
    unproven: list[str] = []
    if graph_baseline is not None:
        baseline_pairs = set(graph_baseline)
        drops = graph_baseline_drops(baseline_pairs, scan)
        n = len(drops)
        n_before = len(baseline_pairs)
        baseline = "graph"
        keyed_elsewhere = {
            (v, name) for v, name in drops
            if any(name in mods for v2, mods in scan.modules.items() if v2 != v)
        }
        n_unparseable = 0
        for v, name in drops:
            if (v, name) in keyed_elsewhere:
                continue
            ex = scan.excluded.get(name)
            if ex is not None and ex.reason == EXCLUSION_UNPARSEABLE:
                n_unparseable += 1  # kept by the manifest_unparseable rule
                continue
            if shipped is None or name in shipped:
                unproven.append(f"{name}@{v}")
    else:
        n = len(transitions.soft_drops)
        n_before = transitions.n_present_before
        baseline = "ledger"
        n_unparseable = sum(1 for t in transitions.soft_drops if t.after != STATE_RETIRED)
        for t in transitions.soft_drops:
            # A present -> unparseable drop is kept by the manifest_unparseable
            # rule and never retires; only an absent name can be proven gone.
            if t.after == STATE_RETIRED and (shipped is None or t.name in shipped):
                unproven.append(t.name)
    mass_ok = True
    gate = mass_gate_trips(n, n_before, len(scan.present_names()))
    if gate == GATE_TOTAL_WIPE:
        n_kept = len(unproven) + n_unparseable
        if shipped is None:
            cause = "git cannot list the HEAD tree, so the wipe cannot be proven"
        elif n_kept:
            cause = (
                f"{n_kept} of {n} dropped module(s) still have a manifest in the HEAD "
                "tree (scan or checkout problem)"
            )
        else:
            in_history = wipe_in_history() if wipe_in_history is not None else None
            cause = None if in_history else (
                "the wipe is not in the branch history (the commit the ledger last "
                "reflected is not an ancestor of HEAD: force-push, branch swap or "
                "wrong clone)" if in_history is False
                else "no earlier commit of this repo is recorded to prove the wipe "
                "is in the branch history"
            )
        if cause is not None:
            message = (
                f"total wipe: none of the {n_before} module(s) present before is in "
                f"the scan; {cause}"
            )
            if allow_mass_retire:
                bypassed.append(GATE_TOTAL_WIPE)
                reasons.append(message + "; applied (--allow-mass-retire)")
            else:
                mass_ok = False
                tripped.append(GATE_TOTAL_WIPE)
                reasons.append(
                    message + "; no module retired, the repo's checkout must be checked"
                )

    kept = unparseable_kept(transitions, scan, indexed_names)
    if kept:
        tripped.append(GATE_MANIFEST_UNPARSEABLE)
        sample = ", ".join(kept[:3])
        reasons.append(
            f"manifest unparseable: {len(kept)} indexed module(s) kept as they are "
            f"until their manifest parses again ({sample})"
        )

    return GateResult(
        scan_ok=scan_ok,
        mass_ok=mass_ok,
        n_soft_drop=n,
        n_present_before=n_before,
        tripped=tuple(tripped),
        bypassed=tuple(bypassed),
        reasons=tuple(reasons),
        baseline=baseline,
        unproven=tuple(sorted(unproven)),
        allow_unproven=allow_mass_retire,
    )


@dataclass(frozen=True)
class Successor:
    """Who replaced a retired module: ``names`` (sorted) and the evidence
    ``source`` (``git_rename``, ``git_merge`` or ``old_technical_name``)."""
    names: tuple[str, ...]
    source: str


def pick_successors(
    transitions: Transitions,
    manifest_changes: Iterable[ManifestChange],
    scan: RegistryScan,
) -> dict[str, Successor]:
    """Successor of every retired module, from hard evidence only.

    1. ``git_rename``: a manifest rename pair (``compute_manifest_changes``,
       directory-majority checked) from the retired name to a DIFFERENT name that
       the scan observes (present or excluded). Real case: tvtmaaddons17
       ``0240c6b77f`` test_pylint -> test_viin_pylint.
    2. otherwise ``old_technical_name``: every present module of this repo whose
       manifest ``old_technical_name`` equals the retired name (several = split).

    Never inferred from similarity of names or commit subjects. Retired names with
    no evidence are absent from the result.
    """
    observed = scan.present_names() | set(scan.excluded)
    renames: dict[str, set[str]] = {}
    for change in manifest_changes:
        if change.status != "R" or not change.old_name:
            continue
        if change.old_name == change.name or change.name not in observed:
            continue
        renames.setdefault(change.old_name, set()).add(change.name)

    declared: dict[str, set[str]] = {}
    for name in scan.present_names():
        info = scan.module(name)
        old = (info.old_technical_name or "").strip() if info is not None else ""
        if old and old != name:
            declared.setdefault(old, set()).add(name)

    out: dict[str, Successor] = {}
    for t in transitions.retired:
        if t.name in renames:
            out[t.name] = Successor(tuple(sorted(renames[t.name])), SUCCESSOR_GIT_RENAME)
        elif t.name in declared:
            out[t.name] = Successor(
                tuple(sorted(declared[t.name])), SUCCESSOR_OLD_TECHNICAL_NAME,
            )
    return out


@dataclass(frozen=True)
class RemovalEvidence:
    """The commit that deleted a module's manifest, and its parent (the last
    commit at which the manifest still existed)."""
    sha: str
    date: str
    subject: str
    manifest_file: str
    parent_sha: str | None = None


@functools.lru_cache(maxsize=16)
def _commit_rename_pairs(
    repo: str, parent: str, sha: str,
) -> tuple[tuple[str, str], ...] | None:
    """``(old_path, new_path)`` of every file the whole-tree ``-M`` pairing of
    ``parent..sha`` renamed (one source per destination file); None on git
    failure. Cached by commit pair (content-addressed, so never stale): a merge
    wave retires several modules removed by the same commit."""
    fields = _git_z(Path(repo), "diff", "--name-status", "-z", "-M", f"{parent}..{sha}")
    if fields is None:
        return None
    return tuple(
        (old, path) for kind, _sim, path, old in _parse_name_status(fields)
        if kind == "R" and old
    )


@functools.lru_cache(maxsize=32)
def _manifest_dirs_at(repo: str, rev: str) -> frozenset[str] | None:
    """Repo-relative directories holding a manifest in the tree of *rev* (no
    submodules: a rename pairing never crosses a gitlink); None on git failure."""
    files = _git_z(Path(repo), "ls-tree", "-r", "-z", "--name-only", rev)
    if files is None:
        return None
    return frozenset(
        str(Path(f).parent) for f in files
        if Path(f).name in MANIFEST_FILENAMES and str(Path(f).parent) != "."
    )


def _share_reached(moved: int, total: int, *, inclusive: bool) -> bool:
    """``moved`` of ``total`` files reach the half share (MERGE_MIN_SHARE_*).

    ``inclusive`` (merge): ``moved / total >= 1/2``; otherwise (rename) the
    strict majority ``moved / total > 1/2``. Integer arithmetic, no float.
    """
    lhs = moved * MERGE_MIN_SHARE_DENOMINATOR
    rhs = total * MERGE_MIN_SHARE_NUMERATOR
    return lhs >= rhs if inclusive else lhs > rhs


def _directory_moved_into(
    repo: Path, parent: str, sha: str, old_dir: str,
    candidates: list[tuple[str, str]],
) -> list[str]:
    """Names of the candidate ``(module_dir, name)`` whose directory received
    more than half of ``old_dir``'s files in the ``parent..sha`` rename pairing
    of the whole commit (strict majority, ``_share_reached(inclusive=False)``;
    sorted)."""
    if not candidates:
        return []
    old_files = _git_z(repo, "ls-tree", "-r", "-z", "--name-only", parent, "--", old_dir)
    pairs = _commit_rename_pairs(str(repo), parent, sha)
    if not old_files or pairs is None:
        return []
    old_prefix = old_dir.rstrip("/") + "/"
    out = set()
    for module_dir, name in candidates:
        prefix = module_dir.rstrip("/") + "/"
        moved = sum(
            1 for old, path in pairs
            if old.startswith(old_prefix) and path.startswith(prefix)
        )
        if _share_reached(moved, len(old_files), inclusive=False):
            out.add(name)
    return sorted(out)


def merge_subject_target(subject: str | None) -> str | None:
    """The module a commit subject says the removed one was merged into, or None.

    Prose (ETHOS #12): used only to cross-check :func:`merged_into`, which
    decides from the rename pairing alone. ``"[MERGE] viin_ai_rag: merge into
    viin_ai"`` -> ``viin_ai``.
    """
    if not subject:
        return None
    m = _MERGE_SUBJECT_RE.search(subject)
    return m.group(1) if m else None


def merged_into(
    repo: Path | str, parent: str, sha: str, old_dir: str,
    observed_names: Iterable[str],
) -> list[str]:
    """Module names that absorbed ``old_dir`` in the commit ``parent..sha`` (N:1 merge).

    The oracle (git only, no curated list, owner rule #380 item 3): the
    commit's whole-tree rename pairing (``git diff -M``) moved at least half of
    the files ``old_dir`` had at *parent* (:data:`MERGE_MIN_SHARE_NUMERATOR` /
    :data:`MERGE_MIN_SHARE_DENOMINATOR`, inclusive, and at least
    :data:`MERGE_MIN_MOVED_FILES` files) into ONE module directory
    that has a manifest both at *parent* (it existed before: a merge, not a
    rename to a new module) and at *sha* (it survived the commit), whose name
    differs from the old one and is in *observed_names*. A destination file
    belongs to the nearest enclosing directory with a manifest at *sha*.

    Neither the manifest status (the target's manifest is usually only
    Modified, or not touched at all, so ``compute_manifest_changes`` never lists
    it) nor the commit subject play a part. Two directories can both qualify
    only on an exact half split; both are returned. Sorted; [] on git failure.
    """
    repo_s = str(repo)
    old_dir = old_dir.rstrip("/")
    old_name = Path(old_dir).name
    old_files = _git_z(Path(repo_s), "ls-tree", "-r", "-z", "--name-only", parent, "--", old_dir)
    pairs = _commit_rename_pairs(repo_s, parent, sha)
    before = _manifest_dirs_at(repo_s, parent)
    after = _manifest_dirs_at(repo_s, sha)
    if not old_files or pairs is None or before is None or after is None:
        return []
    old_prefix = old_dir + "/"
    moved: dict[str, int] = {}
    for old, path in pairs:
        if not old.startswith(old_prefix) or path.startswith(old_prefix):
            continue
        target = Path(path).parent
        while str(target) != "." and str(target) not in after:
            target = target.parent
        if str(target) == ".":
            continue
        moved[str(target)] = moved.get(str(target), 0) + 1
    observed = set(observed_names)
    out = sorted({
        Path(d).name for d, n in moved.items()
        if d in before and Path(d).name != old_name and Path(d).name in observed
        and n >= MERGE_MIN_MOVED_FILES
        and _share_reached(n, len(old_files), inclusive=True)
    })
    return out


def removal_evidence(
    local_path: str,
    module_path: str,
    manifest_files: Iterable[str],
    observed_names: Iterable[str],
) -> tuple[RemovalEvidence | None, Successor | None]:
    """Git evidence for a module directory that no longer has a manifest.

    Tries each candidate manifest file name under ``module_path`` (repo-relative)
    and takes the newest deleting commit (``git_utils.removing_commit``). The
    successor is the rename destination of that manifest in that same commit
    (``compute_manifest_changes`` over ``parent..commit``, directory-majority
    checked; when the manifest paired with another manifest, the commit's
    whole-tree rename pairing decides which added module received the old
    directory),
    kept only when the destination name is in ``observed_names``. With no
    rename destination, an N:1 merge (:func:`merged_into`: at least half of the
    old directory moved into one pre-existing module) gives a ``git_merge``
    successor; the commit subject is only compared with it (a disagreement is
    logged, never decides).
    Returns ``(None, None)`` when git never deleted the manifest there (or the
    path is not a git repo); successor alone may be None.
    """
    found: tuple[str, str, str, str] | None = None
    for manifest_file in manifest_files:
        rel = f"{module_path.rstrip('/')}/{manifest_file}" if module_path else manifest_file
        hit = removing_commit(local_path, rel)
        if hit is not None and (found is None or hit[1] > found[1]):
            found = (*hit, rel)
    if found is None:
        return None, None
    sha, date, subject, rel = found
    parent = rev_parse(local_path, f"{sha}^")
    evidence = RemovalEvidence(
        sha=sha, date=date, subject=subject, manifest_file=Path(rel).name,
        parent_sha=parent,
    )
    if parent is None:
        return evidence, None
    observed = set(observed_names)
    old_dir = str(Path(rel).parent)
    old_name = Path(rel).parent.name
    changes = compute_manifest_changes(Path(local_path), parent, sha)
    targets = sorted({
        change.name
        for change in changes
        if change.status == "R" and change.old_path == rel
        and change.name != old_name and change.name in observed
    })
    if not targets:
        # Manifest-only rename detection can pair the moved manifest with a
        # near-identical sibling deleted in the same commit (a merge wave), which
        # the directory check then rejects. Fall back to the commit's whole-tree
        # rename pairing (one source per destination file) and accept a manifest
        # the commit added whose directory received most of the old directory.
        targets = _directory_moved_into(
            Path(local_path), parent, sha, old_dir,
            [
                (change.module_dir, change.name) for change in changes
                if change.status in ("A", "R") and change.name != old_name
                and change.name in observed
            ],
        )
    if targets:
        return evidence, Successor(tuple(targets), SUCCESSOR_GIT_RENAME)
    merged = merged_into(local_path, parent, sha, old_dir, observed)
    said = merge_subject_target(subject)
    if said and said != old_name and said not in merged:
        logger.info(
            "removal_evidence %s: commit %s says %r was merged into %r, the rename "
            "pairing shows %s - the pairing decides",
            local_path, sha[:10], old_name, said, merged or "no merge target",
        )
    successor = Successor(tuple(merged), SUCCESSOR_GIT_MERGE) if merged else None
    return evidence, successor
