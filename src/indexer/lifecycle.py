# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module lifecycle classification and mass-retire gates (ADR-0056).

No Neo4j, no Postgres (``removal_evidence`` reads git history). They compare
the previous presence-ledger rows of ONE repo with a fresh ``RegistryScan`` of
that repo and decide what changed, whether retirement may proceed, and who
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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from src.git_utils import removing_commit, rev_parse
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
# config: a module moving there was positively observed, so it never counts
# toward the mass-retire gate (real case: 436 modules flipped installable False
# at tvtmaaddons 19.0, commit 48d741b7de).
GATE_EXEMPT_REASONS = frozenset({EXCLUSION_INSTALLABLE_FALSE, EXCLUSION_LICENSE_SKIP})

GATE_SCAN_INCOMPLETE = "scan_incomplete"
GATE_SCAN_UNTRUSTED = "scan_untrusted"
GATE_MASS_RETIRE = "mass_retire"
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

        These are the transitions a broken checkout, a parser regression or a
        permission fault would produce en masse, so they feed gate G-B.
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
    """Outcome of the mass-delete safety gates for one repo scan.

    - G-A (``scan_ok``): the scan is complete (no tracked manifest missing on
      disk) AND trusted (the tree is the registered branch).
    - G-B (``mass_ok``): the soft drops (present -> retired, present ->
      excluded(unparseable)) are not a mass event: it trips when
      ``n > 0.5 * n_present_before AND n >= 20``, or on a total wipe (the
      ledger had present modules and the scan has none left) regardless of
      the floor. A rename or merge that leaves >= 1 present module is judged
      by the ratio rule only. ``installable_false``
      and ``license_skip`` exclusions never count. ``allow_mass_retire`` bypasses
      G-B (never G-A).

    ``retire_allowed`` = G-A and G-B. ``tripped`` lists the gate ids that
    blocked (``scan_incomplete``, ``scan_untrusted``, ``mass_retire``,
    ``total_wipe``) - a bypassed G-B is reported in ``bypassed`` instead.
    ``reasons`` are operator-facing sentences for ``lifecycle_attention``.
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

    @property
    def retire_allowed(self) -> bool:
        return self.scan_ok and self.mass_ok


def mass_gate_trips(
    n_soft_drop: int, n_present_before: int, n_present_after: int | None = None,
) -> str | None:
    """Gate id G-B trips with for these counts, or None.

    ``total_wipe``: modules were present before and NONE is present now - the
    shape of a broken clone or checkout, blocked regardless of the floor.
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
) -> GateResult:
    """Evaluate G-A and G-B for one repo (see ``GateResult``).

    ``trusted`` must come from ``git_utils.head_matches_remote_branch`` for the
    repo's registered branch, evaluated after the pre-scan refresh.

    *graph_baseline* (given while the ledger has never reflected the repo,
    ``presence_head_sha`` NULL - the first runs after the ledger was deployed,
    or a newly registered repo) replaces the ledger baseline for G-B: the
    ``(odoo_version, name)`` Module nodes the graph attributes to the repo,
    against :func:`graph_baseline_drops`. Such nodes are removed by the orphan
    sweep, not by a pending retirement, so a trip holds the repo unsynced and
    the sweep keeps every orphan of its profile (exit 3 + attention) until an
    operator-confirmed ``--allow-mass-retire`` run. An empty baseline (a repo
    the graph never saw) never trips.

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

    if graph_baseline is not None:
        baseline_pairs = set(graph_baseline)
        drops = graph_baseline_drops(baseline_pairs, scan)
        n = len(drops)
        n_before = len(baseline_pairs)
        baseline = "graph"
    else:
        n = len(transitions.soft_drops)
        n_before = transitions.n_present_before
        baseline = "ledger"
    mass_ok = True
    gate = mass_gate_trips(n, n_before, len(scan.present_names()))
    if gate is not None:
        if baseline == "graph":
            sample = ", ".join(f"{name}@{v}" for v, name in drops[:3])
            message = (
                f"mass retire: {n} of {n_before} module node(s) the graph attributes "
                f"to this repo are not in the scan (absent, unparseable or keyed at "
                f"another version, e.g. {sample}; ledger not yet synced for this repo)"
            )
        else:
            message = (
                f"mass retire: {n} of {n_before} present module(s) would drop "
                f"(absent or unparseable)"
            )
        if allow_mass_retire:
            bypassed.append(gate)
            reasons.append(message + "; applied (--allow-mass-retire)")
        else:
            mass_ok = False
            tripped.append(gate)
            reasons.append(message + "; no module retired (use --allow-mass-retire)")

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
    )


@dataclass(frozen=True)
class Successor:
    """Who replaced a retired module: ``names`` (sorted) and the evidence
    ``source`` (``git_rename`` or ``old_technical_name``)."""
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


def _directory_moved_into(
    repo: Path, parent: str, sha: str, old_dir: str,
    candidates: list[tuple[str, str]],
) -> list[str]:
    """Names of the candidate ``(module_dir, name)`` whose directory received
    more than half of ``old_dir``'s files in the ``parent..sha`` rename pairing
    of the whole commit (sorted)."""
    if not candidates:
        return []
    old_files = _git_z(repo, "ls-tree", "-r", "-z", "--name-only", parent, "--", old_dir)
    fields = _git_z(repo, "diff", "--name-status", "-z", "-M", f"{parent}..{sha}")
    if not old_files or fields is None:
        return []
    pairs = _parse_name_status(fields)
    old_prefix = old_dir.rstrip("/") + "/"
    out = set()
    for module_dir, name in candidates:
        prefix = module_dir.rstrip("/") + "/"
        moved = sum(
            1 for kind, _sim, path, old in pairs
            if kind == "R" and old and old.startswith(old_prefix) and path.startswith(prefix)
        )
        if 2 * moved > len(old_files):
            out.add(name)
    return sorted(out)


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
    kept only when the destination name is in ``observed_names``.
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
    successor = Successor(tuple(targets), SUCCESSOR_GIT_RENAME) if targets else None
    return evidence, successor
