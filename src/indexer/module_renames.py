# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-version module renames and merges (issue #380 item 2, ADR-0055).

Inside one branch, git tells who replaced a retired module (``lifecycle``:
``git_rename`` / ``git_merge`` / manifest ``old_technical_name``). Across Odoo
majors there is no git history to read: ``sale_coupon`` 15.0 and
``sale_loyalty`` 16.0 live on different branches. The data for that is
OpenUpgrade's ``apriori.py``: the ``renamed_modules`` and ``merged_modules`` of
the branch ``N.0`` list what was renamed / merged between ``N-1`` and ``N``.

Curated data, oracle and provenance (ADR-0055):

* ``spec_data/module_renames.json`` is generated, never hand-edited, by
  ``scripts/sync_module_renames.py --write`` (weekly workflow
  ``module-renames-resync.yml``) from the ``apriori.py`` of every ``N.0``
  branch of the source repository. Each branch block carries its provenance
  (repo, branch, commit sha, file path); every entry lives inside the block it
  came from. The file is parsed with :mod:`ast` (``literal_eval`` of the two
  assignments), never executed.
* apriori is a CLAIM about history (it lists OCA modules too, and reuses names:
  ``sale_coupon -> coupon`` at 14.0 while ``sale_coupon`` still ships at 14.0
  and 15.0). The read-time oracle is the index: :func:`resolve_chain` shows an
  edge ``old -> new`` at ``N`` only when, in the caller's scope, ``old`` is
  indexed at ``N-1`` and at no version from ``N`` to the asked version, and
  ``new`` is indexed at ``N``. Chains are composed hop by hop with increasing
  versions, each hop validated the same way, so the name-reuse trap never
  chains.

Import discipline: stdlib only (no Neo4j, no Postgres); the MCP read side
passes a presence callback.
"""
from __future__ import annotations

import ast
import functools
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SPEC_PATH = Path(__file__).parent / "spec_data" / "module_renames.json"

DEFAULT_SOURCE_REPO = "OCA/OpenUpgrade"
SOURCE_LABEL = "OpenUpgrade apriori"

# Where apriori.py lives, newest layout first: 14.0+ / 10.0-13.0 / 8.0-9.0.
# The generator takes the first one the branch has, so a new layout only needs
# a line here.
APRIORI_PATHS: tuple[str, ...] = (
    "openupgrade_scripts/apriori.py",
    "odoo/addons/openupgrade_records/lib/apriori.py",
    "openerp/addons/openupgrade_records/lib/apriori.py",
)

# The oldest Odoo major OSM indexes (v8, ManifestFinder ADR-0002). An edge of an
# older branch could never validate (its N-1 is never indexed).
MIN_MAJOR = 8

KIND_RENAME = "rename"
KIND_MERGE = "merge"
_KIND_OF_KEY = {"renamed_modules": KIND_RENAME, "merged_modules": KIND_MERGE}

_MODULE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")  # OCA ships account_e-invoice_generate
_BRANCH_RE = re.compile(r"^(\d+)\.0\Z")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\Z")

# Hops followed at most (one per Odoo major is the real bound).
MAX_HOPS = 16


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def version_key(version: str | None) -> tuple[int, int] | None:
    """``"16.0"`` -> ``(16, 0)``; None for anything that is not ``major.minor``."""
    m = _VERSION_RE.match(version or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def previous_version(version: str) -> str | None:
    """The major before *version* (``"16.0"`` -> ``"15.0"``); None if not ``N.0``-like."""
    key = version_key(version)
    if key is None or key[0] <= 0:
        return None
    return f"{key[0] - 1}.0"


def branch_major(branch: str) -> int | None:
    """Major of an ``N.0`` branch name, None for anything else (``18.0-update-...``)."""
    m = _BRANCH_RE.match(branch)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# apriori.py parser (generator side)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AprioriModules:
    """``renamed_modules`` / ``merged_modules`` of one apriori.py, sanitized."""
    renamed: dict[str, str]
    merged: dict[str, str]
    dropped: tuple[str, ...] = ()


def parse_apriori(source: str) -> AprioriModules:
    """Parse the two module maps out of an ``apriori.py`` without executing it.

    Every era is accepted: a dict literal (``{'old': 'new'}``) or, for
    ``merged_modules`` in 9.0/10.0, a list of ``(old, new)`` tuples. A later
    duplicate key wins, as it does when OpenUpgrade imports the file. Names are
    stripped (16.0 lists ``"gift_card "``); an entry whose name is not a module
    name, or that maps a name to itself, is dropped and reported in
    ``dropped``. A missing assignment is an empty map. Raises ``ValueError``
    when the file does not parse or a present assignment is not a literal of
    those shapes (the generator then fails loudly instead of writing a gap).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"apriori.py does not parse: {exc}") from exc
    found: dict[str, list[tuple[object, object]]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if not (isinstance(target, ast.Name) and target.id in _KIND_OF_KEY):
                continue
            try:
                literal = ast.literal_eval(value)
            except (ValueError, SyntaxError) as exc:
                raise ValueError(f"{target.id} is not a literal: {exc}") from exc
            if isinstance(literal, dict):
                items = list(literal.items())
            elif isinstance(literal, (list, tuple)) and all(
                isinstance(p, (list, tuple)) and len(p) == 2 for p in literal
            ):
                items = [tuple(p) for p in literal]
            else:
                raise ValueError(
                    f"{target.id} must be a dict or a list of pairs, got "
                    f"{type(literal).__name__}"
                )
            found[target.id] = items  # a later assignment replaces an earlier one
    dropped: list[str] = []
    out: dict[str, dict[str, str]] = {"renamed_modules": {}, "merged_modules": {}}
    for key, items in found.items():
        for old, new in items:
            o = old.strip() if isinstance(old, str) else None
            n = new.strip() if isinstance(new, str) else None
            if not o or not n or not _MODULE_NAME_RE.match(o) or not _MODULE_NAME_RE.match(n):
                dropped.append(f"{key}: {old!r} -> {new!r} (not a module name)")
                continue
            if o == n:
                dropped.append(f"{key}: {o!r} -> itself")
                continue
            out[key][o] = n
    return AprioriModules(
        renamed=dict(sorted(out["renamed_modules"].items())),
        merged=dict(sorted(out["merged_modules"].items())),
        dropped=tuple(dropped),
    )


# ---------------------------------------------------------------------------
# Curated data (read side)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Edge:
    """``old`` was renamed (or merged) into ``new`` at Odoo ``version`` (from N-1)."""
    old: str
    new: str
    version: str
    kind: str


def edges_from_data(data: Mapping) -> tuple[Edge, ...]:
    """Every edge of a ``module_renames.json`` document, sorted."""
    edges: list[Edge] = []
    for branch, block in (data.get("branches") or {}).items():
        if branch_major(branch) is None or not isinstance(block, Mapping):
            continue
        for key, kind in _KIND_OF_KEY.items():
            for old, new in (block.get(key) or {}).items():
                edges.append(Edge(old, new, branch, kind))
    return tuple(sorted(edges, key=lambda e: (version_key(e.version), e.old, e.kind, e.new)))


class RenameIndex:
    """Edges keyed by the old name."""

    def __init__(self, edges: Iterable[Edge]) -> None:
        self._by_old: dict[str, list[Edge]] = {}
        for e in edges:
            self._by_old.setdefault(e.old, []).append(e)

    def edges_from(self, name: str) -> list[Edge]:
        return list(self._by_old.get(name, ()))

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_old.values())


def load_data(path: Path = SPEC_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=4)
def _cached_index(path: str, mtime_ns: int) -> RenameIndex:
    return RenameIndex(edges_from_data(load_data(Path(path))))


def rename_index(path: Path = SPEC_PATH) -> RenameIndex:
    """The edges of the curated file, cached per file version. A missing or
    unreadable file degrades to no edges (WARNING), never to a tool error."""
    try:
        mtime = Path(path).stat().st_mtime_ns
        return _cached_index(str(path), mtime)
    except (OSError, ValueError) as exc:
        logger.warning("module renames data unavailable (%s): %s", path, exc)
        return RenameIndex(())


# ---------------------------------------------------------------------------
# Read-time oracle: validate + compose against the index
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Chain:
    """Validated hops from the asked name; ``resolved`` when the last hop's
    module is indexed at the asked version."""
    name: str
    version: str
    hops: tuple[Edge, ...]
    resolved: bool

    @property
    def final(self) -> str:
        return self.hops[-1].new


Presence = Callable[[list[str]], Mapping[str, Iterable[str]]]


def _absent_from(versions: Iterable[str], start: str, end: str) -> bool:
    lo, hi = version_key(start), version_key(end)
    for v in versions:
        k = version_key(v)
        if k is not None and lo <= k <= hi:
            return False
    return True


def hop_is_valid(edge: Edge, asked: str, presence: Mapping[str, Iterable[str]]) -> bool:
    """The index rule for one edge (see module docstring): ``old`` indexed at
    ``N-1`` and at no version in ``[N, asked]``, ``new`` indexed at ``N``."""
    prev = previous_version(edge.version)
    old_at = set(presence.get(edge.old) or ())
    new_at = set(presence.get(edge.new) or ())
    return (
        prev is not None
        and prev in old_at
        and _absent_from(old_at, edge.version, asked)
        and edge.version in new_at
    )


def resolve_chain(
    name: str,
    version: str,
    index: RenameIndex,
    presence: Presence,
    *,
    known: Mapping[str, Iterable[str]] | None = None,
) -> Chain | None:
    """The validated rename / merge chain of *name*, NOT indexed at *version*.

    Hop by hop from *name* (one *presence* call per hop, for the names not yet
    known; *known* seeds it, e.g. the asked name's other versions): candidate
    edges are those of the current name with ``previous hop < N <= version``;
    the valid ones (:func:`hop_is_valid`) all share the version where the name
    last disappeared, a rename preferred over a merge, then by name. The chain
    stops at a module indexed at *version* (``resolved``) or when no hop is
    valid. None when the first hop is not valid.
    """
    target = version_key(version)
    if target is None:
        return None
    pres: dict[str, set[str]] = {k: set(v) for k, v in (known or {}).items()}
    hops: list[Edge] = []
    current, after = name, None
    seen = {name}
    for _ in range(MAX_HOPS):
        lo = version_key(after) if after else None
        candidates = [
            e for e in index.edges_from(current)
            if (k := version_key(e.version)) is not None and k <= target
            and (lo is None or k > lo)
        ]
        if not candidates:
            break
        missing = sorted({current, *(e.new for e in candidates)} - set(pres))
        if missing:
            for n, vs in (presence(missing) or {}).items():
                pres.setdefault(n, set()).update(vs)
            for n in missing:
                pres.setdefault(n, set())
        valid = [e for e in candidates if hop_is_valid(e, version, pres)]
        if not valid:
            break
        valid.sort(key=lambda e: (
            tuple(-x for x in version_key(e.version)), e.kind != KIND_RENAME, e.new,
        ))
        hop = valid[0]
        hops.append(hop)
        if version in pres.get(hop.new, ()):
            return Chain(name, version, tuple(hops), resolved=True)
        if hop.new in seen:
            break
        seen.add(hop.new)
        current, after = hop.new, hop.version
    if not hops:
        return None
    return Chain(name, version, tuple(hops), resolved=False)
