# SPDX-License-Identifier: AGPL-3.0-or-later
"""Install-set resolution: depends closure plus the auto_install fixpoint (#380).

Pure logic, no database: the caller supplies two lookups and this module runs
Odoo's install rule over them. ``src.mcp.describe`` binds the lookups to
bounded, per-hop Neo4j reads (ADR-0048, no variable-length path) behind the
tenant choke-point (ADR-0034); the unit tests bind them to dicts.

The rule follows ``ir.module.module.button_install`` (v8-v19) and
``odoo.modules.db.initialize``:

- installing a module marks it and its whole ``depends`` closure;
- then, until nothing changes, every not-yet-installed auto-install module
  whose triggers are all in the set is added with its ``depends`` closure,
  provided at least one trigger is newly installed by this request (Odoo's
  "at least one dependency is 'to install'"), and provided every module of
  that closure is available (Odoo refuses a dependency it does not know);
- v18+ (``country_gate``): a candidate whose manifest declares ``countries``
  is installed by a later install only when a company of the database is in
  one of them. ``must_install`` reads ``not module.country_ids or
  module.country_ids & company_countries`` in odoo_18
  ``odoo/addons/base/models/ir_module.py:425-429`` and odoo_19 ``:419-423``;
  odoo_17 ``:420-422`` has no such clause. A fresh database's company has no
  country yet, so such a candidate is never added: it is reported apart in
  ``country_conditional``. ``db.initialize`` (the baseline) has no such gate;
- the baseline of a fresh database is the same fixpoint run from ``base``
  without the "newly installed" condition (database creation installs
  everything ``base`` triggers). Modules in the baseline are not reported as
  pulled in by a later install.

Availability (ADR-0034): a module the lookup reports unavailable
(not indexed, or outside the caller's scope) is never added to the install set
and its dependencies are never walked, so nothing reached only through an
invisible module can be installed or named. A candidate whose closure needs an
unavailable module is not installable (``unsatisfiable``); the name reported is
always a declared dependency of an available module (the same names
``describe_module`` already lists as that module's depends).

Triggers come from ``Module.auto_install_triggers`` (resolved per version by
``src.indexer.registry.auto_install_triggers``): an empty trigger list is the
"install at database creation" form, so it joins the baseline but is never
triggered by a later install.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

# Each round adds at least one module, so the module count bounds the loop;
# this cap only guards a lookup that keeps returning new names.
MAX_AUTO_INSTALL_ROUNDS = 50
BASELINE_ROOT = "base"
# First major whose button_install applies the manifest `countries` gate.
COUNTRY_GATE_MIN_MAJOR = 18

# depends_of(names) -> {name: (available, [direct dependency names])}
DependsLookup = Callable[[list[str]], dict[str, tuple[bool, list[str]]]]
# candidates(installed, excluded) -> [(name, triggers)] or
# [(name, triggers, countries)] for available auto-install modules not in
# installed/excluded whose triggers are all in installed (a prefilter: the
# rule is re-checked here).
CandidateLookup = Callable[[frozenset[str], frozenset[str]], list[Sequence]]


@dataclass(frozen=True)
class AutoInstalled:
    """One module an install pulls in beyond the requested depends closure."""

    name: str
    round: int
    triggers: tuple[str, ...] = ()
    # Set when the module is not auto-installed itself but is a dependency of
    # the auto-installed module named here.
    required_by: str | None = None


@dataclass(frozen=True)
class Baseline:
    """What database creation installs (the fixpoint from ``base``)."""

    installed: frozenset[str]
    rejected: frozenset[str]
    truncated: bool = False


@dataclass
class InstallSet:
    roots: tuple[str, ...]
    installed: frozenset[str]
    baseline: frozenset[str]
    auto: list[AutoInstalled] = field(default_factory=list)
    # Candidates whose own dependency closure is not available, with the
    # first missing module.
    unsatisfiable: dict[str, str] = field(default_factory=dict)
    # v18+: candidates whose triggers are met but whose manifest declares
    # `countries`; installed only when a company is in one of them.
    country_conditional: dict[str, tuple[str, ...]] = field(default_factory=dict)
    truncated: bool = False
    # Set by the caller (src.mcp.describe) when the scope spans several
    # profiles, so the result assumes they share one addons path.
    caveat: str | None = None


class _CachedDepends:
    """``depends_of`` memoised per name: each name is looked up at most once,
    and every call is one lookup for the uncached names only."""

    def __init__(self, depends_of: DependsLookup):
        self._lookup = depends_of
        self._info: dict[str, tuple[bool, list[str]]] = {}

    def __call__(self, names: list[str]) -> dict[str, tuple[bool, list[str]]]:
        todo = sorted({n for n in names if n not in self._info})
        if todo:
            got = self._lookup(todo)
            for n in todo:
                self._info[n] = got.get(n, (False, []))
        return {n: self._info[n] for n in names}


def _depends_closure(
    start: Iterable[str],
    known: set[str],
    depends_of: DependsLookup,
) -> tuple[list[str], str | None]:
    """BFS the depends graph from ``start``, skipping ``known`` names.

    Returns (new AVAILABLE names in discovery order, first unavailable name or
    None). An unavailable name is never returned and its dependencies are never
    walked. One lookup per hop, deduplicated by name.
    """
    seen: set[str] = set()
    order: list[str] = []
    frontier = [n for n in dict.fromkeys(start) if n not in known]
    missing: str | None = None
    while frontier:
        info = depends_of(sorted(frontier))
        next_frontier: list[str] = []
        for name in frontier:
            if name in seen:
                continue
            seen.add(name)
            available, deps = info.get(name, (False, []))
            if not available:
                if missing is None:
                    missing = name
                continue
            order.append(name)
            for dep in deps:
                if dep not in known and dep not in seen:
                    next_frontier.append(dep)
        frontier = sorted(set(next_frontier) - seen)
    return order, missing


def _fixpoint(
    installed: set[str],
    newly: set[str],
    depends_of: DependsLookup,
    candidates: CandidateLookup,
    result: InstallSet,
    rejected: set[str],
    *,
    require_new: bool,
    record: bool,
    max_rounds: int,
    country_gate: bool = False,
) -> bool:
    """Add auto-install modules to ``installed`` until nothing changes.

    ``require_new`` applies Odoo's later-install condition (one trigger must be
    in ``newly``, the modules this request installs); database creation runs
    without it. ``country_gate`` (v18+, later install only) keeps a candidate
    that declares countries out of the set. ``record`` appends what is added to
    ``result.auto`` and the rejected candidates to ``result.unsatisfiable`` /
    ``result.country_conditional``. Mutates ``installed``, ``newly``,
    ``rejected`` and ``result``. Returns True when the round cap was hit.

    Each round walks the depends closure of ALL its accepted candidates in one
    BFS (one lookup per hop); each candidate's own closure is then read from
    the memoised lookup without further queries.
    """
    for rnd in range(1, max_rounds + 1):
        rows = candidates(frozenset(installed), frozenset(rejected))
        accepted: list[tuple[str, tuple[str, ...]]] = []
        for row in sorted(rows, key=lambda r: r[0]):
            name, triggers = row[0], row[1]
            countries = tuple(sorted(set(row[2] or ()))) if len(row) > 2 else ()
            trig = tuple(sorted(set(triggers)))
            if name in installed or name in rejected:
                continue
            if not set(trig) <= installed:
                continue
            if require_new and not set(trig) & newly:
                continue
            if country_gate and countries:
                rejected.add(name)
                if record:
                    result.country_conditional[name] = countries
                continue
            accepted.append((name, trig))
        if not accepted:
            return False
        # One BFS for the round: fills the memo for every accepted closure.
        _depends_closure([name for name, _ in accepted], installed, depends_of)
        accepted_names = {name for name, _ in accepted}
        added: set[str] = set()
        for name, trig in accepted:
            closure, missing = _depends_closure([name], installed | added, depends_of)
            if missing is not None:
                rejected.add(name)
                if record:
                    result.unsatisfiable[name] = missing
                continue
            added.update(closure)
            if record:
                result.auto.append(AutoInstalled(name=name, round=rnd, triggers=trig))
                result.auto.extend(
                    AutoInstalled(name=dep, round=rnd, required_by=name)
                    for dep in sorted(closure)
                    if dep != name and dep not in accepted_names
                )
        if not added:
            return False
        installed.update(added)
        newly.update(added)
    return True


def compute_baseline(
    depends_of: DependsLookup,
    candidates: CandidateLookup,
    *,
    max_rounds: int = MAX_AUTO_INSTALL_ROUNDS,
) -> Baseline:
    """What a fresh database has: ``base``'s closure plus what it triggers.

    Independent of the request, so a caller may cache it per (version, scope).
    """
    lookup = depends_of if isinstance(depends_of, _CachedDepends) else _CachedDepends(depends_of)
    baseline: set[str] = set()
    rejected: set[str] = set()
    base_closure, _ = _depends_closure([BASELINE_ROOT], set(), lookup)
    baseline.update(base_closure)
    scratch = InstallSet(roots=(), installed=frozenset(), baseline=frozenset())
    truncated = _fixpoint(baseline, set(baseline), lookup, candidates, scratch, rejected,
                          require_new=False, record=False, max_rounds=max_rounds)
    return Baseline(frozenset(baseline), frozenset(rejected), truncated)


def resolve_install_set(
    roots: Iterable[str],
    depends_of: DependsLookup,
    candidates: CandidateLookup,
    *,
    max_rounds: int = MAX_AUTO_INSTALL_ROUNDS,
    country_gate: bool = False,
    baseline: Baseline | None = None,
) -> InstallSet:
    """What installing ``roots`` on a fresh database installs.

    ``installed`` is the full resulting set (baseline included); ``auto``
    lists, in round order, the modules pulled in by auto_install beyond the
    baseline and the roots' depends closure. ``country_gate`` applies the
    v18+ ``countries`` rule (:data:`COUNTRY_GATE_MIN_MAJOR`). ``baseline``
    reuses a :func:`compute_baseline` result computed over the same lookups.
    """
    roots_t = tuple(dict.fromkeys(roots))
    result = InstallSet(roots=roots_t, installed=frozenset(), baseline=frozenset())
    lookup = _CachedDepends(depends_of)

    is_creation = BASELINE_ROOT in roots_t
    if is_creation:
        base = Baseline(frozenset(), frozenset())
    elif baseline is not None:
        base = baseline
    else:
        base = compute_baseline(lookup, candidates, max_rounds=max_rounds)
    result.baseline = base.installed
    result.truncated = base.truncated
    rejected = set(base.rejected)

    installed = set(base.installed)
    newly, _ = _depends_closure(roots_t, installed, lookup)
    installed.update(newly)
    # Installing `base` IS database creation: everything it triggers comes
    # along, so the creation variant runs (no country gate: db.initialize has
    # none) and its additions are reported.
    if _fixpoint(installed, set(newly), lookup, candidates, result, rejected,
                 require_new=not is_creation, record=True, max_rounds=max_rounds,
                 country_gate=country_gate and not is_creation):
        result.truncated = True
    result.installed = frozenset(installed)
    return result
