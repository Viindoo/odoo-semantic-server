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
    """Result of compute_diff. All fields default to empty lists."""
    added: list[CoreSymbolInfo] = field(default_factory=list)
    removed: list[CoreSymbolInfo] = field(default_factory=list)
    stable: list[tuple[CoreSymbolInfo, CoreSymbolInfo]] = field(default_factory=list)
    replaced: list[tuple[str, str]] = field(default_factory=list)
    """Each entry: (old_qualified_name, new_qualified_name)."""


def compute_diff(
    symbols_old: list[CoreSymbolInfo],
    symbols_new: list[CoreSymbolInfo],
) -> DiffResult:
    """Diff two CoreSymbol lists (typically from two consecutive Odoo versions).

    REPLACED_BY edge is created ONLY when:
    1. The old symbol carries a `replacement_qname` (set by the curator/parser)
    2. The replacement_qname exists as a real symbol in `symbols_new`

    A removed symbol with a dangling replacement_qname (target not indexed) is
    treated as plain removed — we never create ghost replacement nodes (mirrors
    the project-wide `:INHERITS {unresolved}` policy).
    """
    by_qname_old = {s.qualified_name: s for s in symbols_old}
    by_qname_new = {s.qualified_name: s for s in symbols_new}

    common = by_qname_old.keys() & by_qname_new.keys()
    only_old = by_qname_old.keys() - by_qname_new.keys()
    only_new = by_qname_new.keys() - by_qname_old.keys()

    added = [by_qname_new[qn] for qn in only_new]
    stable = [(by_qname_old[qn], by_qname_new[qn]) for qn in common]

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
        stable=stable,
        replaced=replaced,
    )
