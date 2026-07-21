# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test-base-class rendering helpers (split out of src/mcp/tools/test_tools.py).

Issue #362 (test_base_classes read-side rework) grew test_tools.py past the
tools/*.py god-file ceiling (tests/test_no_god_file.py TOOL_MODULE_MAX_LINES)
by adding a NOT-AVAILABLE absence branch, an out-of-catalogue note, and a
graph-enrichment overlay to the ``test_base_classes`` tool's render path. Per
the Phase 7 / A1 precedent (src/mcp/describe.py, src/mcp/listings.py), the fix
is not to raise the ceiling but to move the RENDERING/FORMATTING cluster out
to a non-tool helper module directly under src/mcp/ — leaving the `@mcp.tool`
body and its `_test_base_classes` impl entry point in test_tools.py.

Moved verbatim from test_tools.py:
  - ``_COMMIT_FORBIDDEN_MSG`` — the PP3 cursor-contract literal (contains a
    real em dash — never ASCII-ify it), rendered in both branches below and
    re-exported from ``src.mcp.tools.test_tools`` for existing test imports.
  - ``_SURVEYED_MAJORS``      — the surveyed-catalogue range consumed by
    ``_describe_absence``.
  - ``_resolve_major_or_none``— parses the leading major from a version
    string; shared by the era-prefix helper and the tree/absence renderers.
  - ``_era_prefix``           — era-correct test-package prefix for the
    header (issue #362 WI-3 item 7).
  - ``_enrich_with_graph_locations`` — the graph-enrichment overlay: adds
    parse-derived file_path/line onto the curated framework_bases facts
    without ever adding/removing/renaming a menu entry.
  - ``_format_base_classes``  — the ADR-0023 tree renderer for the full
    framework base-class menu.
  - ``_describe_absence``     — data-driven absence window/deprecation/
    replacement description for one class at one version.
  - ``_format_base_class_not_available`` — the NOT AVAILABLE absence branch
    (issue #362 WI-3 Defect 3 / item 3).

This is NOT a tool module: it declares no ``@mcp.tool`` and is not part of
the import-time tool-registration side effect. Unlike src/mcp/describe.py and
src/mcp/listings.py, this cluster does not touch the Neo4j driver/session hub
at all — every function here operates on already-fetched data
(``framework_bases()`` facts + graph rows), so there is no ``_srv`` server
reference to bind; ordinary top-level imports are enough.
"""

from dataclasses import replace

from src.constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR
from src.indexer.framework_bases import (
    KNOWN_FRAMEWORK_BASE_NAMES,
    framework_base,
    is_out_of_catalogue,
    removed_at,
)
from src.mcp.hints import format_next_step

_COMMIT_FORBIDDEN_MSG = "cr.commit() FORBIDDEN — isolation is savepoint rollback"

# Surveyed catalogue per api-contract.md / framework_bases.is_out_of_catalogue.
_SURVEYED_MAJORS = range(8, 20)  # 8..19


def _resolve_major_or_none(v: str) -> int | None:
    """Parse the leading major from *v* — never raises (mirrors
    ``framework_bases._resolve_major``, but that helper is module-private and
    the frozen API contract does not expose it)."""
    try:
        return int(str(v).split(".")[0])
    except (ValueError, AttributeError, IndexError):
        return None


def _era_prefix(v: str) -> str:
    """Era-correct test-package prefix for the header (issue #362 WI-3 item 7).

    Reuses the SAME boundary constant ``src.indexer.framework_bases`` itself
    dispatches on (``ODOO_NAMESPACE_LEGACY_MAX_MAJOR``) rather than a second
    hardcoded "9", so the header prefix can never silently drift from the
    menu's own era resolution.
    """
    major = _resolve_major_or_none(v)
    if major is not None and major <= ODOO_NAMESPACE_LEGACY_MAX_MAJOR:
        return "openerp"
    return "odoo"


def _enrich_with_graph_locations(facts: list, rows: list[dict]) -> list:
    """Overlay graph-sourced ``file_path``/``line`` onto the curated *facts*.

    Enrichment-only (issue #362 WI-3 / api-contract.md "Read-side contract"):
    the graph can add a real parse-derived line number when the index was
    built with an Odoo source root, but it can never add, remove, or rename a
    menu entry — that decision belongs to ``framework_bases()`` alone.
    """
    row_by_name = {r.get("name"): r for r in rows if r.get("name")}
    enriched = []
    for fact in facts:
        row = row_by_name.get(fact.name)
        if row:
            file_path = row.get("file_path") or fact.file_path
            line = row.get("line") if row.get("line") is not None else fact.line
            if file_path != fact.file_path or line != fact.line:
                fact = replace(fact, file_path=file_path, line=line)
        enriched.append(fact)
    return enriched


def _format_base_classes(facts: list, v: str) -> str:
    """Format ``FrameworkBaseFacts`` rows as an ADR-0023 tree (issue #362 WI-3).

    *facts* is per-version AUTHORITY data from ``src.indexer.framework_bases``
    — every row describes a class that genuinely exists at Odoo *v*. Class
    rows always use ``├─`` (never ``└─``): ``Cursor rule:`` and ``Next:``
    (and, conditionally, a version-caveat ``Note:``) always follow as further
    header siblings, so no class row is ever the tree's true last child
    (fixes the ADR-0023 §1.2 connector defect D1, phase4-solution.md §0).
    """
    header = f"Odoo {v} - Test framework base classes ({_era_prefix(v)}/tests/)"
    lines = [header]

    for fact in facts:
        commit_str = (
            _COMMIT_FORBIDDEN_MSG
            if not fact.commit_allowed
            else "cr.commit() allowed (@standalone only)"
        )
        lines.append(f"├─ {fact.name}     {fact.test_type} · {commit_str}")
        sub_lines: list[str] = []
        if fact.setup_summary:
            sub_lines.append(f"setup: {', '.join(fact.setup_summary[:3])}")
        if fact.file_path:
            # Null file_path (stdlib TestCase) OMITS this sub-line entirely
            # (issue #362 WI-3 item 9) — the row's remaining connectors above
            # already stay legal because sub_lines is built conditionally.
            loc = f"{fact.file_path}:{fact.line}" if fact.line else fact.file_path
            sub_lines.append(f"source: {loc}")
        for i, text in enumerate(sub_lines):
            conn = "└─" if i == len(sub_lines) - 1 else "├─"
            lines.append(f"│   {conn} {text}")

    # Menu-level removal line (issue #362 WI-4, phase5-review.md finding W2 /
    # required change 10): the FULL MENU — not just the name= drill-down — must
    # name what replaced a removed class. The menu is the path an agent actually
    # takes; the drill-down is rarely reached until the agent already suspects
    # the class is gone, and OSM's documented audience includes version-upgrade
    # work, so silent absence misses the reader upgrading FROM an era that had
    # it. Sourced from removed_at(v) — the same SSOT _describe_absence() already
    # uses for the drill-down — never a second hardcoded {old: new} literal. The
    # version named in the line is the queried `v` itself, not a hardcoded
    # "17.0", so the line stays correct if/when a later era removes a different
    # class.
    removed = removed_at(v)
    if removed:
        removed_str = "; ".join(f"{old} -> {new}" for old, new in removed)
        lines.append(f"├─ Removed as of Odoo {v}: {removed_str}")

    major = _resolve_major_or_none(v)
    if major in (8, 9):
        # Folded into the tree as a proper sibling BEFORE Cursor rule: (issue
        # #362 WI-3 item 5) — previously appended AFTER the Next: footer.
        lines.append(
            "├─ Note: v8/v9 era1 - addon-level class hierarchy is regex best-effort."
        )
    if is_out_of_catalogue(v):
        lines.append(
            f"├─ Note: Odoo {v} is outside the surveyed catalogue (v8-v19); "
            "showing the newest known menu (v17+)."
        )

    # Always append cursor rule (PP3 contract - must appear in output). D2 fix:
    # _COMMIT_FORBIDDEN_MSG already ends with "isolation is savepoint rollback" —
    # the old trailing "; isolation = savepoint rollback" duplicated the clause.
    lines.append(f"├─ Cursor rule:   {_COMMIT_FORBIDDEN_MSG}")

    next_line = format_next_step([
        f"suggest_pattern(intent='test computed field', odoo_version='{v}',"
        " category='test') for curated patterns",
        f"test_class_inspect(name='<ClassName>', odoo_version='{v}')"
        " to inspect one class",
    ])
    lines.append(next_line)
    return "\n".join(lines)


def _describe_absence(name: str, v: str) -> tuple[str, str | None]:
    """Data-driven (ETHOS #11) absence description for *name* at *v*.

    Scans ``framework_base()`` across the surveyed catalogue (8.0-19.0) — the
    window/deprecation/removal facts are DERIVED from the SSOT, never a
    second hardcoded per-class literal (the exact anti-pattern issue #362
    exists to remove). Returns ``(window_line, replacement_or_None)``; the
    replacement comes from ``removed_at(v)`` per the WI-3 contract.
    """
    queried_major = _resolve_major_or_none(v)
    history = [(major, framework_base(f"{major}.0", name)) for major in _SURVEYED_MAJORS]
    present = [(major, fact) for major, fact in history if fact is not None]
    replacement = dict(removed_at(v)).get(name)

    if not present:
        lo, hi = _SURVEYED_MAJORS[0], _SURVEYED_MAJORS[-1]
        return (f"not available at any surveyed version ({lo}.0-{hi}.0)", replacement)

    first_major, last_major = present[0][0], present[-1][0]
    deprecated_majors = [m for m, f in present if f.status == "deprecated"]

    bits = (
        [f"available {first_major}.0 only"]
        if first_major == last_major
        else [f"available {first_major}.0-{last_major}.0"]
    )
    if deprecated_majors:
        dep_major = deprecated_majors[0]
        bits.append(
            f"deprecated at {dep_major}.0 (merged into {replacement})"
            if replacement
            else f"deprecated at {dep_major}.0"
        )
    if queried_major is not None and queried_major < first_major:
        bits.append(f"not yet introduced at Odoo {v}")
    else:
        bits.append(f"removed at {last_major + 1}.0")

    return ("; ".join(bits), replacement)


def _format_base_class_not_available(v: str, name: str) -> str:
    """Absence branch (issue #362 WI-3 Defect 3 / item 3).

    NEVER falls back to rendering the whole menu. Distinguishes a genuinely
    UNKNOWN name (never part of ``KNOWN_FRAMEWORK_BASE_NAMES``) from a real
    Odoo framework base class that is simply absent at the resolved version.
    Both sub-branches still carry the PP3 literal and end with ``Next:``.
    """
    header = f"Odoo {v} - Test framework base classes ({_era_prefix(v)}/tests/)"
    lines = [header]
    replacement: str | None = None

    if name not in KNOWN_FRAMEWORK_BASE_NAMES:
        lines.append(f"├─ {name}: not a known Odoo framework base class at Odoo {v}")
    else:
        window_line, replacement = _describe_absence(name, v)
        lines.append(f"├─ {name}: NOT AVAILABLE at Odoo {v}")
        sub_lines = [window_line]
        if replacement:
            repl_fact = framework_base(v, replacement)
            if repl_fact and repl_fact.setup_summary:
                sub_lines.append(f"use: {replacement} - {repl_fact.setup_summary[0]}")
            else:
                sub_lines.append(f"use: {replacement} instead")
        for i, text in enumerate(sub_lines):
            conn = "└─" if i == len(sub_lines) - 1 else "├─"
            lines.append(f"│   {conn} {text}")

    major = _resolve_major_or_none(v)
    if major in (8, 9):
        lines.append(
            "├─ Note: v8/v9 era1 - addon-level class hierarchy is regex best-effort."
        )
    if is_out_of_catalogue(v):
        lines.append(
            f"├─ Note: Odoo {v} is outside the surveyed catalogue (v8-v19); "
            "showing the newest known menu (v17+)."
        )

    lines.append(f"├─ Cursor rule:   {_COMMIT_FORBIDDEN_MSG}")

    if replacement:
        next_line = format_next_step([
            f"test_base_classes(odoo_version='{v}') for the full menu",
            f"test_class_inspect(name='{replacement}', odoo_version='{v}')"
            " to inspect the replacement",
        ])
    else:
        next_line = format_next_step([
            f"test_base_classes(odoo_version='{v}') for the full menu",
            f"test_class_inspect(name='<ClassName>', odoo_version='{v}')"
            " to inspect one class",
        ])
    lines.append(next_line)
    return "\n".join(lines)
