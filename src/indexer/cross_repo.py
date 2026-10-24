# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/cross_repo.py
"""Cross-repo dependency change propagation (M7 W14, narrowed 2026-10-24).

When an incremental indexer run re-writes modules of repo A, the modules of
OTHER repos that depend on them may hold graph edges resolved against the old
state of A's modules at their own write time (a ``USES_FIELD`` to a field A
just added, an ``INHERITS_VIEW`` to a view A just moved, ...). Those modules
are re-written on their repo's next run.

:func:`propagate_to_dependents` decides which: the modules that depend on a
changed module directly or transitively (``DEPENDS_ON`` closure at the same
version), flagged ``needs_rewrite`` in the ``module_presence`` ledger of
every other repo that ships them. That repo's next run re-writes just those
modules (sync path when its HEAD did not move), instead of a full re-index of
the whole repo. A dependent repo without ledger rows at the version keeps the
pre-ledger fallback: its ``repos.head_sha`` is NULLed (full re-index), for
the repos of every dependent found, direct or transitive.

:func:`find_dependent_repos` (repo basenames of the direct dependents) stays
for the reconcile, which resets the repos depending on retired modules, and
for the fallback when the dependency walk fails.

The caller seeds the propagation with the modules whose graph its run can
have changed, not with every module it re-wrote
(``pipeline_repo.propagation_seed``).
"""
import logging
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)

# type(r), not a [:DEPENDS_ON] pattern: a graph without any DEPENDS_ON edge yet
# would make Neo4j warn about an unknown relationship type on every run.
_CYPHER_FIND_DEPENDENT_REPOS = """
MATCH (dependent:Module {odoo_version: $version})-[r]->(changed:Module)
WHERE type(r) = 'DEPENDS_ON' AND changed.odoo_version = $version
  AND changed.name IN $changed_names AND NOT dependent.name IN $changed_names
RETURN DISTINCT dependent.repo AS repo
"""


def find_dependent_repos(
    driver,
    odoo_version: str,
    changed_module_names: set[str],
) -> list[str]:
    """Return list of repo identifiers (m.repo property values from Neo4j) of
    Modules at this version that DEPENDS_ON any module in changed_module_names,
    EXCLUDING those that own a changed module themselves (don't reset the
    repo we just indexed).

    Args:
        driver:               Neo4j driver (already open).
        odoo_version:         Odoo version string, e.g. "17.0".
        changed_module_names: Set of module names that changed in this run.

    Returns:
        List of distinct repo identifiers (the ``m.repo`` property value,
        which equals ``Path(local_path).name`` as stored in Neo4j by the
        indexer). Empty list when no dependent repos are found or
        changed_module_names is empty.
    """
    if not changed_module_names:
        return []

    with driver.session() as session:
        result = session.run(
            _CYPHER_FIND_DEPENDENT_REPOS,
            version=odoo_version,
            changed_names=list(changed_module_names),
        )
        return [row["repo"] for row in result.data() if row["repo"] is not None]


_CYPHER_DIRECT_DEPENDENT_MODULES = """
MATCH (dependent:Module {odoo_version: $version})-[r]->(changed:Module)
WHERE type(r) = 'DEPENDS_ON' AND changed.odoo_version = $version
  AND changed.name IN $frontier AND NOT dependent.name IN $seen
RETURN DISTINCT dependent.name AS name, dependent.repo AS repo
"""


def find_dependent_modules(
    driver,
    odoo_version: str,
    changed_module_names: set[str],
) -> dict[str, tuple[str | None, int]]:
    """Modules depending on *changed_module_names*, directly or transitively.

    Breadth-first over ``DEPENDS_ON`` at *odoo_version*, one flat query per
    hop (no variable-length pattern). A changed module is never its own
    dependent; a dependency cycle terminates because a name is visited once.

    Returns ``{name: (repo, depth)}``: ``repo`` is the Module node's ``repo``
    property (the checkout basename, None on a stub), ``depth`` 1 for a
    direct dependent, 2 for a dependent of a direct dependent, and so on.
    """
    if not changed_module_names:
        return {}
    found: dict[str, tuple[str | None, int]] = {}
    seen: set[str] = set(changed_module_names)
    frontier: set[str] = set(changed_module_names)
    depth = 0
    with driver.session() as session:
        while frontier:
            depth += 1
            rows = session.run(
                _CYPHER_DIRECT_DEPENDENT_MODULES,
                version=odoo_version,
                frontier=sorted(frontier),
                seen=sorted(seen),
            ).data()
            frontier = set()
            for row in rows:
                name = row["name"]
                if name in seen:
                    continue
                seen.add(name)
                frontier.add(name)
                found[name] = (row["repo"], depth)
    return found


@dataclass
class PropagationResult:
    """What one propagation did, for the run log and the tests.

    ``marked``: ``{repo_id: [module names]}`` flagged ``needs_rewrite``.
    ``reset_repo_ids``: repos whose ``head_sha`` was NULLed (no ledger rows at
    the version, the ledger could not be written, or the dependency walk
    failed). ``dependents``: every dependent module found (direct +
    transitive), before the repo filter. ``walk_failed``: the walk raised;
    only the direct dependents' repos could be reset, best effort.
    ``complete``: every dependent found was flagged or had its repo reset;
    False after a failed walk (the transitive dependents are unknown), so the
    caller keeps its ``head_sha`` and the next run repeats the propagation.
    """
    dependents: int = 0
    marked: dict[int, list[str]] = field(default_factory=dict)
    reset_repo_ids: list[int] = field(default_factory=list)
    walk_failed: bool = False
    complete: bool = True
    # Pending entries written for dependents without a ledger row yet.
    pending: int = 0

    @property
    def marked_count(self) -> int:
        return sum(len(names) for names in self.marked.values())


def propagate_to_dependents(
    driver,
    odoo_version: str,
    changed_module_names: set[str],
    *,
    exclude_repo_id: int | None,
    exclude_basenames: set[str],
    presence,
    repo_store,
) -> PropagationResult:
    """Flag the dependents of *changed_module_names* for a rewrite.

    *presence* is the ledger store (``ModulePresenceStore``-like, or None
    when the run has no ledger); *repo_store* the ``RepoStore``. The repo
    being indexed is excluded by id from the ledger flags and by basename
    from the fallback reset (it just wrote its own modules).

    With a ledger: every ``present`` row of a dependent module at the version
    in another repo is flagged ``needs_rewrite``; a dependent whose repo has
    no ``present`` row of it yet (it wrote the module to the graph, its
    ledger commit is still to come) gets a pending entry that repo's ledger
    commit applies - both in one transaction and one ledger lock hold
    (``flag_dependents``). Then the
    repos shipping a dependent (any depth) that have no ledger row at the
    version are reset (``head_sha`` NULL), the only way to get them
    re-written. When the flag write fails (lock timeout, connection error)
    the propagation falls back to the reset of the repos of EVERY dependent
    found, direct or transitive, logs a WARNING and returns
    ``complete=False`` (a reset is lost when the dependent repo's run is in
    progress, and no pending entry was written): a missed propagation would
    leave stale edges, an extra full re-index costs time.

    Without a ledger: the repos of every dependent found are reset. (Before
    the transitive walk only the direct dependents' repos were; a full
    re-index of a ledger-less repo does not propagate further, so a one-hop
    reset would never reach a transitive dependent.)

    A failed dependency walk (a Neo4j error) logs a WARNING, resets the
    repos of the direct dependents best effort (``find_dependent_repos``, one
    query; an error there is logged too) and returns ``complete=False``: the
    transitive dependents are unknown, so the caller keeps its ``head_sha``
    and the next run repeats the walk. An error of the ``head_sha`` reset
    after a successful walk is raised: the caller then keeps its own
    ``head_sha`` as well (``pipeline_repo._propagate_to_dependents``).
    """
    result = PropagationResult()
    try:
        dependents = find_dependent_modules(driver, odoo_version, changed_module_names)
    except Exception as exc:  # noqa: BLE001 - one-hop reset, then repeat next run
        _logger.warning(
            "Cross-repo dep propagation: the dependency walk failed at %s (%s: %s) - "
            "resetting head_sha of the repos of the direct dependents; the walk is "
            "repeated by the next run", odoo_version, type(exc).__name__, exc,
        )
        result.walk_failed = True
        result.complete = False
        try:
            result.reset_repo_ids = _reset_direct_dependent_repos(
                driver, odoo_version, changed_module_names,
                exclude_basenames=exclude_basenames, repo_store=repo_store,
            )
        except Exception as reset_exc:  # noqa: BLE001 - the caller keeps head_sha
            _logger.warning(
                "Cross-repo dep propagation: the direct-dependent reset failed too at "
                "%s (%s: %s)", odoo_version, type(reset_exc).__name__, reset_exc,
            )
        return result
    result.dependents = len(dependents)
    if not dependents:
        return result

    dependent_basenames = sorted({
        repo for repo, _depth in dependents.values()
        if repo is not None and repo not in exclude_basenames
    })
    fallback_ids: list[int] = (
        repo_store.get_repo_ids_by_local_path_basenames(dependent_basenames, odoo_version)
        if dependent_basenames else []
    )
    if len(fallback_ids) > len(dependent_basenames):
        # Two repos share a basename AT THE SAME odoo_version: both are
        # candidates for the reset - over-eager but safe (ADR-0007 W14 note 2).
        _logger.warning(
            "Cross-repo dep propagation: basename collision detected - "
            "%d repo IDs returned for %d basenames (%s). "
            "All matching repos are reset candidates (safe, over-eager). "
            "See ADR-0007 W14 for fix path.",
            len(fallback_ids), len(dependent_basenames), ", ".join(dependent_basenames),
        )

    if presence is not None:
        # The direct flags and, for a dependent its repo wrote to the graph
        # without a ledger row yet (a run in progress, a new module), a pending
        # entry that repo's ledger commit applies (migration 0007): one
        # transaction under one ledger lock hold (flag_dependents), so that
        # repo's ledger commit cannot slip in between and lose the module.
        pending = sorted({
            (repo, name) for name, (repo, _depth) in dependents.items()
            if repo is not None and repo not in exclude_basenames
        })
        try:
            pairs, result.pending = presence.flag_dependents(
                odoo_version, sorted(dependents), pending=pending,
                exclude_repo_ids=[exclude_repo_id] if exclude_repo_id is not None else [],
            )
        except Exception as exc:  # noqa: BLE001 - reset, and retried next run
            _logger.warning(
                "Cross-repo dep propagation: could not flag %d dependent module(s) "
                "needs_rewrite at %s (%s) - resetting head_sha of the %d repo(s) "
                "shipping them; the propagation is retried by the next run",
                len(dependents), odoo_version, exc, len(fallback_ids),
            )
            pairs = None
            # A reset is lost when the dependent repo's run is in progress (it
            # re-stamps head_sha at its end), and no pending entry was
            # written: not recorded, so the caller keeps its head_sha.
            result.complete = False
        if pairs is not None:
            for repo_id, name in pairs:
                result.marked.setdefault(repo_id, []).append(name)
            if fallback_ids:
                try:
                    fallback_ids = presence.repo_ids_without_rows(fallback_ids, odoo_version)
                except Exception as exc:  # noqa: BLE001 - reset them all (safe)
                    _logger.warning(
                        "Cross-repo dep propagation: could not read which dependent "
                        "repos have ledger rows at %s (%s) - resetting head_sha of all %d",
                        odoo_version, exc, len(fallback_ids),
                    )
    if fallback_ids:
        n = repo_store.reset_head_sha(fallback_ids)
        if n:
            result.reset_repo_ids = sorted(fallback_ids)
    return result


def _reset_direct_dependent_repos(
    driver,
    odoo_version: str,
    changed_module_names: set[str],
    *,
    exclude_basenames: set[str],
    repo_store,
) -> list[int]:
    """NULL ``head_sha`` of the repos shipping a DIRECT dependent of
    *changed_module_names* (the pre-ledger propagation); the reset ids, sorted.
    Raises on a Neo4j or PostgreSQL error."""
    basenames = sorted({
        b for b in find_dependent_repos(driver, odoo_version, changed_module_names)
        if b not in exclude_basenames
    })
    if not basenames:
        return []
    ids = repo_store.get_repo_ids_by_local_path_basenames(basenames, odoo_version)
    if not ids:
        return []
    repo_store.reset_head_sha(ids)
    return sorted(ids)
