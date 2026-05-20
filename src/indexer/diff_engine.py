# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/diff_engine.py
"""Cross-version CoreSymbol diff (M4.5 WI2.1, per ADR-0002 §2).

Pure function: no DB call, no IO. Inputs are two lists of CoreSymbolInfo
(typically `parser_odoo_core.parse_odoo_core(...)` for two versions). Output
is a 4-bucket DiffResult that the writer translates into Neo4j edges:

- added:    qualified_name only in `new`        → status='added'
- removed:  qualified_name only in `old`        → status='removed'
- stable:   present in both, no replacement     → no edge
- replaced: old has `replacement_qname` that exists in new → REPLACED_BY edge
"""
from dataclasses import dataclass, field

from .models import CoreSymbolInfo


@dataclass
class DiffResult:
    """Result of compute_diff. All fields default to empty lists.

    Per ADR-0002 §2 (revised): lifecycle expressed as properties on CoreSymbol
    nodes, not as separate edges. This dataclass records the diff for the
    writer to apply as SET properties (added_in, removed_in, deprecated_in)
    and as a REPLACED_BY edge (the only true cross-symbol edge).

    Fields:
        added:      Symbols only in `symbols_new` (appear for first time).
        removed:    Symbols only in `symbols_old` and NOT replaced.
        deprecated: Symbols present in both versions where new.status changed
                    to 'deprecated' and old.status was NOT 'deprecated'.
        stable:     Symbols present in both, no status change.
        replaced:   (old_qname, new_qname) pairs where old had replacement_qname.
    """
    added: list[CoreSymbolInfo] = field(default_factory=list)
    removed: list[CoreSymbolInfo] = field(default_factory=list)
    deprecated: list[CoreSymbolInfo] = field(default_factory=list)
    stable: list[tuple[CoreSymbolInfo, CoreSymbolInfo]] = field(default_factory=list)
    replaced: list[tuple[str, str]] = field(default_factory=list)
    """Each entry: (old_qualified_name, new_qualified_name)."""


def compute_diff(
    symbols_old: list[CoreSymbolInfo],
    symbols_new: list[CoreSymbolInfo],
) -> DiffResult:
    """Diff two CoreSymbol lists (typically from two consecutive Odoo versions).

    Rules:
    - REPLACED_BY edge is created ONLY when old carries a `replacement_qname`
      pointing to a real symbol in `symbols_new`.
    - A removed symbol with a dangling replacement_qname is treated as plain
      removed — no ghost nodes (mirrors `:INHERITS {unresolved}` policy).
    - deprecated: present in both, new.status == 'deprecated' and old.status != 'deprecated'.
    """
    by_qname_old = {s.qualified_name: s for s in symbols_old}
    by_qname_new = {s.qualified_name: s for s in symbols_new}

    common = by_qname_old.keys() & by_qname_new.keys()
    only_old = by_qname_old.keys() - by_qname_new.keys()
    only_new = by_qname_new.keys() - by_qname_old.keys()

    added = [by_qname_new[qn] for qn in only_new]

    # Deprecated: present in both, status changed to 'deprecated'
    deprecated: list[CoreSymbolInfo] = []
    stable: list[tuple[CoreSymbolInfo, CoreSymbolInfo]] = []
    for qn in common:
        old_sym = by_qname_old[qn]
        new_sym = by_qname_new[qn]
        if new_sym.status == "deprecated" and old_sym.status != "deprecated":
            deprecated.append(new_sym)
        else:
            stable.append((old_sym, new_sym))

    replaced: list[tuple[str, str]] = []
    replaced_old_qnames: set[str] = set()
    for s in symbols_old:
        if (
            s.replacement_qname
            and s.qualified_name in only_old
            and s.replacement_qname in by_qname_new
        ):
            replaced.append((s.qualified_name, s.replacement_qname))
            replaced_old_qnames.add(s.qualified_name)

    # Removed = old symbols not in new and NOT successfully replaced.
    removed = [by_qname_old[qn] for qn in only_old if qn not in replaced_old_qnames]

    return DiffResult(
        added=added,
        removed=removed,
        deprecated=deprecated,
        stable=stable,
        replaced=replaced,
    )
