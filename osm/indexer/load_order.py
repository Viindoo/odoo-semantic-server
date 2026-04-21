"""Fix-point dependency graph and load-order simulator.

Reimplements the algorithm from ``odoo/modules/graph.py:31-151``:

1. Fix-point loop: repeatedly try to admit modules whose every declared
   dependency is already in the graph (``depth_map``).  Iterate until no new
   module is admitted in a full pass.  Remaining modules are examined: those
   whose unresolved deps point only at other remaining modules form a cycle
   (raised); those whose deps include names outside the provided set (or names
   already dropped) are warned and dropped.

2. Depth assignment: for each admitted module, depth = 1 + max(parent depths).
   Odoo uses ``>=`` (not ``>``), so the last parent in ``depends`` that
   achieves the current max depth becomes the canonical father. For our
   purposes we only need the depth value, not the father pointer.

3. Iteration: ``(depth ASC, name ASC)`` -- alphabetical tie-break within
   each depth level.

``CyclicDependencyError`` is raised only when remaining unresolved modules form
a closed cycle among themselves (every unresolved dep is itself unresolved).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from osm.indexer.manifest import ManifestRecord

_logger = logging.getLogger(__name__)


class CyclicDependencyError(Exception):
    """Raised when a dependency cycle is detected among the provided modules."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(f"Dependency cycle detected: {' -> '.join(cycle)}")


class MissingDependencyError(Exception):
    """Not raised at runtime; present so callers can import and reference it."""

    def __init__(self, module: str, missing_parent: str) -> None:
        self.module = module
        self.missing_parent = missing_parent
        super().__init__(f"Module {module!r} depends on missing {missing_parent!r}")


@dataclass(frozen=True)
class LoadOrderRecord:
    """Per-module result of load-order simulation."""

    name: str
    depth: int
    load_order: int


def compute_load_order(manifests: Sequence[ManifestRecord]) -> list[LoadOrderRecord]:
    """Compute the canonical Odoo load order for the given manifests.

    Raises ``CyclicDependencyError`` when a closed dependency cycle is found
    among provided modules (all unresolved deps are also unresolved, none are
    external or dropped).  Modules with deps absent from the provided set, or
    whose dep was dropped, are warned and silently dropped -- matching Odoo's
    ``"Unmet dependencies"`` log behaviour.

    Returns a list sorted by ``(depth ASC, name ASC)`` with ``load_order``
    set to the 0-based index in that sorted list.
    """
    module_deps: dict[str, tuple[str, ...]] = {m.name: m.depends for m in manifests}
    declared: set[str] = set(module_deps)

    depth_map: dict[str, int] = {}
    dropped: set[str] = set()
    pending: set[str] = set(declared)

    changed = True
    while changed and pending:
        changed = False
        newly_dropped: set[str] = set()

        for name in list(pending):
            deps = module_deps[name]

            bad_deps = [d for d in deps if d not in declared or d in dropped]
            if bad_deps:
                missing_external = [d for d in bad_deps if d not in declared]
                missing_dropped = [d for d in bad_deps if d in dropped]
                first_bad = (missing_external or missing_dropped)[0]
                _logger.warning(
                    "module %r: unmet dependency %r; dropping",
                    name,
                    first_bad,
                )
                newly_dropped.add(name)
                changed = True
                continue

            unresolved = [d for d in deps if d not in depth_map]
            if unresolved:
                continue

            max_depth = -1
            for dep in deps:
                d = depth_map.get(dep, 0)
                if d >= max_depth:
                    max_depth = d

            depth_map[name] = max_depth + 1
            changed = True

        pending -= set(depth_map)
        dropped |= newly_dropped
        pending -= newly_dropped

    if pending:
        cycle = _find_cycle(list(pending), module_deps)
        raise CyclicDependencyError(cycle)

    sorted_modules = sorted(depth_map.items(), key=lambda x: (x[1], x[0]))
    return [
        LoadOrderRecord(name=name, depth=depth, load_order=idx)
        for idx, (name, depth) in enumerate(sorted_modules)
    ]


def _find_cycle(
    members: list[str],
    module_deps: dict[str, tuple[str, ...]],
) -> list[str]:
    """Return one cycle path (first + last element equal) among *members*."""
    member_set = set(members)
    visited: set[str] = set()
    rec_stack: list[str] = []

    def dfs(node: str) -> bool:
        visited.add(node)
        rec_stack.append(node)
        for dep in module_deps.get(node, ()):
            if dep not in member_set:
                continue
            if dep not in visited:
                if dfs(dep):
                    return True
            elif dep in rec_stack:
                idx = rec_stack.index(dep)
                del rec_stack[:idx]
                rec_stack.append(dep)
                return True
        rec_stack.pop()
        return False

    for start in members:
        if start not in visited:
            rec_stack.clear()
            if dfs(start):
                return list(rec_stack)

    return members[:2] + [members[0]]
