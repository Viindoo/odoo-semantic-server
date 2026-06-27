# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/version_registry.py — Shared version-dispatch registry (ADR-0032).
#
# Contract: a VersionRegistry is a sorted list of entries
#   (min_major: int, max_major: int | None, handler: T)
# where:
#   - min_major: first Odoo major for which this entry applies (inclusive).
#   - max_major: last Odoo major for which this entry applies (inclusive),
#     or None meaning "this version and all newer ones" (open-ended).
#   - handler: the callable or value associated with this range.
#
# Resolution:
#   - Entries are evaluated in ascending min_major order.
#   - First matching entry wins (no fall-through to a later entry).
#   - A version that matches no entry returns the default (None or caller-supplied).
#
# Adding v20 is a 1-line append:
#   entries.append((20, None, v20_handler))
#
# No plugin framework — this is intentionally minimal.


class VersionRegistry[T]:
    """Sorted (min_major, max_major|None, handler) registry.

    Entries are stored sorted by min_major ascending. First match wins.
    max_major=None means open-ended (applies to major and above).
    """

    def __init__(self, entries: list[tuple[int, int | None, T]]) -> None:
        # Sort by min_major ascending so iteration is deterministic.
        self._entries: list[tuple[int, int | None, T]] = sorted(
            entries, key=lambda e: e[0]
        )

    def resolve(self, major: int, default: T | None = None) -> T | None:
        """Return the handler for the given Odoo major version, or *default*."""
        for min_major, max_major, handler in self._entries:
            if major < min_major:
                continue
            if max_major is not None and major > max_major:
                continue
            return handler
        return default

    def resolve_version(self, odoo_version: str, default: T | None = None) -> T | None:
        """Parse *odoo_version* (e.g. ``"17.0"``) and delegate to :meth:`resolve`.

        Unparseable versions return *default* without raising.
        """
        try:
            major = int(str(odoo_version).split(".")[0])
        except (ValueError, IndexError, AttributeError):
            return default
        return self.resolve(major, default)


def make_version_registry[T](entries: list[tuple[int, int | None, T]]) -> VersionRegistry[T]:
    """Convenience constructor — mirrors ``VersionRegistry(entries)``."""
    return VersionRegistry(entries)


# --- Stylesheet era gate (osm-audit-views GAP-3) --------------------------
# Odoo's frontend stylesheet language migrated LESS -> SCSS at the v11/v12
# boundary (v11 = 155 LESS files, 1 SCSS; v12 = 0 LESS, 206 SCSS). LESS was
# introduced in v9 (v8 used plain CSS). Plain CSS is present in every era and is
# always parsed separately, so it is intentionally NOT gated here.
#
# A True handler means "run this parser for this version". A version that matches
# no entry resolves to the registry default (False) — i.e. do not run.
#   - LESS: active v9-v11.
#   - SCSS: active v12+ (open-ended).
# Both parsers no-op when their file glob finds nothing, so the gate is a
# correctness/documentation boundary, not a data-loss fix.
STYLESHEET_LESS_REGISTRY: VersionRegistry[bool] = VersionRegistry([
    (9, 11, True),  # LESS era: v9-v11
])
STYLESHEET_SCSS_REGISTRY: VersionRegistry[bool] = VersionRegistry([
    (12, None, True),  # SCSS era: v12+
])


def less_active(odoo_version: str) -> bool:
    """True when the LESS parser should run for *odoo_version* (v9-v11)."""
    return bool(STYLESHEET_LESS_REGISTRY.resolve_version(odoo_version, default=False))


def scss_active(odoo_version: str) -> bool:
    """True when the SCSS parser should run for *odoo_version* (v12+)."""
    return bool(STYLESHEET_SCSS_REGISTRY.resolve_version(odoo_version, default=False))


# --- Report-type era gate (issue #345 follow-up; ADR-0052) ----------------
# An `ir.actions.report` action's `report_type` decides whether a report binds a
# QWeb template (USES_TEMPLATE) at all. The DEFAULT report_type when the XML omits
# it differs by era (Odoo's own model `default=`):
#   - v8-v10:  default "pdf"  -> RML/legacy reports; NON-qweb. The selection also
#              admits sxw/webkit/controller (all non-qweb). report_name is a
#              LocalService name, NOT a clean template xmlid (eraA survey §5).
#   - v11+:    default "qweb-pdf" -> qweb. RML and the non-qweb report_type values
#              were removed at v11; report_name == QWeb template xmlid uniformly.
# The rename `ir.actions.report.xml` -> `ir.actions.report` and the RML removal
# both land at v11 (one jump). A genuine qweb report is `report_type` empty/absent
# (-> era default) or starting with "qweb-", AND carrying no legacy file marker
# (rml/xml/xsl/sxw/parser attr-or-field, or auto="False").
_REPORT_TYPE_DEFAULT_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8, 10, "pdf"),       # legacy RML era: absent report_type means RML (non-qweb)
    (11, None, "qweb-pdf"),  # modern era: absent report_type means qweb-pdf
])

# WARN-on-unresolved USES_TEMPLATE is gated to v11+ qweb reports: even genuine
# v8-v10 qweb reports carry a report_name that is frequently a LocalService name,
# NOT the indexed template xmlid (eraA survey §5: the v10 mrp qweb-pdf trio
# report_name != template xmlid; DB ground-truth confirms 0 recoverable binds).
# So a v8-v10 qweb USES_TEMPLATE miss is an expected gap -> DEBUG, never WARNING.
# REPORTS_ON WARN keys on `is_qweb_report`, which is ITSELF version-aware via the era
# default (a v8-v10 report with no report_type defaults to "pdf" -> non-qweb -> DEBUG).
# This registry is a SEPARATE explicit version gate for the ADDITIONAL v11+
# USES_TEMPLATE template-warn only.
_REPORT_TEMPLATE_WARN_REGISTRY: VersionRegistry[bool] = VersionRegistry([
    (11, None, True),  # only v11+ qweb template misses are real coverage gaps
])


def report_default_type(odoo_version: str) -> str:
    """Return the default `report_type` when the report XML omits it.

    v8-v10 -> "pdf" (RML, non-qweb); v11+ -> "qweb-pdf". Unparseable versions are
    treated as modern ("qweb-pdf") so a stray version never misclassifies a real
    qweb report as legacy.
    """
    return _REPORT_TYPE_DEFAULT_REGISTRY.resolve_version(
        odoo_version, default="qweb-pdf"
    )  # type: ignore[return-value]


def is_qweb_report(report_info, odoo_version: str) -> bool:
    """True when *report_info* is a genuine QWeb-template + business-model report.

    Mirrors Odoo's own `_lookup_report` runtime gate (v10 ir_actions.py:187): a
    report is qweb iff its effective `report_type` starts with "qweb-" AND it
    carries no legacy file marker (rml/xml/xsl/sxw/parser or auto="False").

    The effective report_type falls back to the era default when the XML omitted
    it (`report_default_type`), so a v8-v10 RML shorthand with no `report_type=`
    attribute resolves to "pdf" (non-qweb), while a v11+ record with no
    report_type resolves to "qweb-pdf" (qweb).
    """
    if getattr(report_info, "has_legacy_marker", False):
        return False
    rt = (getattr(report_info, "report_type", "") or "").strip()
    if not rt:
        rt = report_default_type(odoo_version)
    return rt.startswith("qweb-")


def report_template_warn_active(odoo_version: str) -> bool:
    """True when an unresolved USES_TEMPLATE miss should WARN (v11+ only).

    v8-v10 qweb reports point report_name at a LocalService name, not the indexed
    template xmlid (eraA survey §5), so their misses are expected -> DEBUG.
    """
    return bool(_REPORT_TEMPLATE_WARN_REGISTRY.resolve_version(odoo_version, default=False))
