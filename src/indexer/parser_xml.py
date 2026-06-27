# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
from pathlib import Path

from lxml import etree as _lxml_etree

from ._xmlid import qualify_xmlid
from .models import (
    LintViolationInfo,
    ModuleInfo,
    ViewConditionInfo,
    ViewInfo,
    ViewParseResult,
    XPathInfo,
)
from .version_registry import VersionRegistry

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RelaxNG validation (v15+ only) — WI-E, M11
# ---------------------------------------------------------------------------
# RNG files are read directly from the indexed Odoo core source tree at index
# time: <core_repo_root>/odoo/addons/base/rng/ (v10+) or
# <core_repo_root>/openerp/addons/base/rng/ (v8/v9).
# This guarantees version-exact validation — no vendored copy required.

# VersionRegistry gate: validate only for Odoo v15+ where the schema is stable.
# v8-v14 had different view grammar; applying v15+ schema would produce
# false positives, so we return None for those majors.
_RELAXNG_GATE: VersionRegistry[bool] = VersionRegistry([
    (15, None, True),   # v15, v16, v17, v18, v19, … — validate
])

# Module-level cache: absolute rng file path (str) -> etree.RelaxNG or None.
# Keyed by resolved absolute path so different Odoo versions cache separately
# (e.g. /home/git/odoo17/odoo/addons/base/rng/tree_view.rng vs
#  /home/git/odoo18/odoo/addons/base/rng/list_view.rng).
_RELAXNG_CACHE: dict[str, "_lxml_etree.RelaxNG | None"] = {}


def _get_relaxng_validator(
    view_type: str,
    rng_root: Path | None,
) -> "_lxml_etree.RelaxNG | None":
    """Return cached RelaxNG validator for *view_type* from *rng_root*, or None.

    *rng_root* is the directory that contains ``<view_type>_view.rng`` for the
    indexed Odoo version (read from the actual source tree at index time —
    never from a vendored copy).

    Correctness is driven purely by file existence:
    - v15-v17 ship ``tree_view.rng`` (no ``list_view.rng``).
    - v18-v19 ship ``list_view.rng`` (no ``tree_view.rng``).
    - activity/calendar/graph/pivot/search exist across all supported versions.
    If the expected ``<view_type>_view.rng`` is absent in *rng_root*, or if
    *rng_root* is None, this function returns None (validation gracefully
    skipped — no false positives).

    Cache key is the resolved absolute rng file path so parallel profiles
    pointing at different Odoo versions never share a stale validator entry.
    """
    if rng_root is None:
        return None

    rng_path = rng_root / f"{view_type}_view.rng"
    if not rng_path.exists():
        return None

    cache_key = str(rng_path.resolve())
    if cache_key not in _RELAXNG_CACHE:
        try:
            rng_doc = _lxml_etree.parse(str(rng_path))
            _RELAXNG_CACHE[cache_key] = _lxml_etree.RelaxNG(rng_doc)
        except Exception:
            _logger.exception("Failed to load RelaxNG schema %r", rng_path)
            _RELAXNG_CACHE[cache_key] = None
    return _RELAXNG_CACHE[cache_key]


def _validate_arch_relaxng(
    view: ViewInfo,
    rng_root: Path | None = None,
) -> list[LintViolationInfo]:
    """Validate a view's arch XML against its RelaxNG schema (v15+ only).

    *rng_root* is the directory containing ``<view_type>_view.rng`` for the
    indexed Odoo version.  When None or when the relevant ``.rng`` file is
    absent, validation is silently skipped (no false positives).

    Returns a list of LintViolationInfo (empty = valid or schema not available).
    Version gate is checked by the caller via _RELAXNG_GATE.
    """
    if not view.arch or not view.file_path:
        return []
    validator = _get_relaxng_validator(view.view_type, rng_root)
    if validator is None:
        return []

    # Parse the arch XML via lxml to get proper line numbers.
    # view.arch is the serialized <field name="arch" type="xml">...</field> element.
    try:
        arch_el = _lxml_etree.fromstring(view.arch.encode())
    except _lxml_etree.XMLSyntaxError:
        return []

    # The arch element's children are the actual view root elements (e.g. <tree>/<list>).
    violations: list[LintViolationInfo] = []
    for view_root in arch_el:
        if view_root.tag != view.view_type:
            # Extension arch may wrap with <data>; skip foreign tags
            continue
        if validator.validate(view_root):
            continue
        rule_id = f"relaxng.{view.view_type}_view"
        for error in validator.error_log:
            violations.append(LintViolationInfo(
                file_path=view.file_path,
                line=error.line,
                rule=rule_id,
                message=error.message,
                view_xmlid=view.xmlid,
                odoo_version=view.odoo_version,
                severity="error",
                view_type=view.view_type,
            ))
    return violations

_VIEW_TYPES = {
    "form", "tree", "list", "kanban", "search",
    "pivot", "graph", "calendar", "gantt", "activity", "map",
    # EE-only view types (GAP-9). Without these, an EE arch whose root tag is one
    # of these would silently default to "form". `gantt`/`activity`/`map` are
    # already above; `hierarchy`/`cohort` are EE additions (v17+).
    "hierarchy", "cohort",
}

# GAP-1 - the four conditional-visibility attributes that, in v17+, carry a
# direct Python-like expression (no `attrs` dict). `column_invisible` is the
# list/tree-column variant. We extract each as a ViewConditionInfo(legacy=False).
# Trivial constant values are kept (e.g. column_invisible="1") because they are
# meaningful state, but empty strings are skipped.
_DIRECT_COND_ATTRS = ("invisible", "required", "readonly", "column_invisible")

# GAP-1 - the keys that may appear inside a legacy `attrs="{...}"` dict (v8-v16).
# Same four semantic targets as the v17+ direct attrs.
_ATTRS_DICT_KEYS = ("invisible", "required", "readonly", "column_invisible")


def _extract_attrs_dict_conditions(
    element_tag: str, field_name: str | None, attrs_raw: str
) -> list[ViewConditionInfo]:
    """Parse a legacy ``attrs="{...}"`` value into ViewConditionInfo entries.

    The value is a Python-dict literal mapping condition keys
    (``invisible``/``required``/``readonly``/``column_invisible``) to Odoo domains,
    e.g. ``{'invisible': [('state', '=', 'draft')], 'required': [('x', '!=', False)]}``.

    We parse it with ``ast.literal_eval`` (safe - no code execution). On any parse
    failure (malformed, contains non-literal nodes), we fall back to emitting ONE
    entry with ``attr='attrs'`` and the raw string, so the data is never silently
    dropped - the raw expression is still captured for the AI agent.
    """
    import ast

    out: list[ViewConditionInfo] = []
    try:
        parsed = ast.literal_eval(attrs_raw)
    except (ValueError, SyntaxError):
        parsed = None
    if isinstance(parsed, dict):
        for key, domain in parsed.items():
            if key not in _ATTRS_DICT_KEYS:
                # Unknown attrs key (e.g. a custom widget key) - still record it
                # under its own name so nothing is lost.
                pass
            out.append(ViewConditionInfo(
                element=element_tag,
                attr=f"attrs.{key}",
                expr=repr(domain),
                field=field_name,
                legacy=True,
            ))
    else:
        # Could not parse to a dict - keep the raw string verbatim.
        out.append(ViewConditionInfo(
            element=element_tag,
            attr="attrs",
            expr=attrs_raw,
            field=field_name,
            legacy=True,
        ))
    return out


def _extract_conditions(arch_el: "_lxml_etree._Element") -> list[ViewConditionInfo]:
    """Walk an arch tree and extract all conditional-visibility expressions (GAP-1).

    Captures BOTH forms in one pass over every element in the arch:
      * legacy (v8-v16): ``attrs="{...}"`` (dict of domains) + ``states="a,b"``;
      * modern (v17+): direct ``invisible=``/``required=``/``readonly=``/
        ``column_invisible=`` expression attributes.

    Walks the FULL subtree (``arch_el.iter()``) - not just the root element - so
    that fields nested arbitrarily deep, and fields inserted via ``<xpath>`` in
    an extension view, are all captured. lxml Comment/PI nodes (non-str ``.tag``)
    are skipped. Returns entries in document order.
    """
    conditions: list[ViewConditionInfo] = []
    for el in arch_el.iter():
        tag = el.tag
        if not isinstance(tag, str):
            continue  # skip lxml Comment/ProcessingInstruction nodes
        fname = el.get("name") if tag == "field" else None

        # Legacy attrs="{...}" (v8-v16)
        attrs_raw = el.get("attrs")
        if attrs_raw and attrs_raw.strip():
            conditions.extend(
                _extract_attrs_dict_conditions(tag, fname, attrs_raw.strip())
            )

        # Legacy states="draft,sent" (v8-v16)
        states_raw = el.get("states")
        if states_raw and states_raw.strip():
            conditions.append(ViewConditionInfo(
                element=tag, attr="states", expr=states_raw.strip(),
                field=fname, legacy=True,
            ))

        # Modern direct-expression attrs (v17+): invisible/required/readonly/
        # column_invisible. These also exist pre-v17 on some elements (e.g.
        # column_invisible is v14+), so capturing them at all versions is correct.
        for attr in _DIRECT_COND_ATTRS:
            val = el.get(attr)
            if val is None:
                continue
            val = val.strip()
            if not val:
                continue
            conditions.append(ViewConditionInfo(
                element=tag, attr=attr, expr=val, field=fname, legacy=False,
            ))
    return conditions


def _parse_record(
    record: "_lxml_etree._Element", module: ModuleInfo, file_path: str | None = None
) -> ViewInfo | None:
    """Parse a <record> element as an ir.ui.view.

    *record* is an lxml element so that `.sourceline` is available for A3
    provenance (1-based line of the <record> tag).  stdlib ET elements are not
    accepted here — `parse_file` uses lxml.etree.parse() to produce lxml trees.
    """
    if record.get("model") != "ir.ui.view":
        return None

    xml_id = record.get("id", "").strip()
    if not xml_id:
        return None

    name = ""
    model = ""
    inherit_xmlid = None
    view_type = "form"
    mode = "primary"
    xpaths: list[XPathInfo] = []
    arch: str | None = None
    conditions: list[ViewConditionInfo] = []

    for child in record:
        tag = child.tag
        if not isinstance(tag, str) or tag != "field":
            continue
        fname = child.get("name", "")
        if fname == "name":
            name = (child.text or "").strip()
        elif fname == "model":
            model = (child.text or "").strip()
        elif fname == "inherit_id":
            ref = child.get("ref", "").strip()
            if ref:
                inherit_xmlid = qualify_xmlid(ref, module.name)
                mode = "extension"
        elif fname == "arch":
            arch = _lxml_etree.tostring(child, encoding="unicode")
            # F-5: lxml Comment/PI nodes have non-str .tag (callable) — skip them
            # so a leading comment does not shadow the real view-type element.
            _first_real = next(
                (c for c in child if isinstance(c.tag, str)), None
            )
            if _first_real is not None:
                first_tag = _first_real.tag
                # Unwrap <data> container used by many extension views
                if first_tag == "data":
                    _first_data_real = next(
                        (c for c in _first_real if isinstance(c.tag, str)), None
                    )
                    if _first_data_real is not None and _first_data_real.tag in _VIEW_TYPES:
                        view_type = _first_data_real.tag
                elif first_tag in _VIEW_TYPES:
                    view_type = first_tag
            for xpath_el in child.iter("xpath"):
                expr = xpath_el.get("expr", "").strip()
                position = xpath_el.get("position", "inside").strip()
                if expr:
                    xpaths.append(XPathInfo(expr=expr, position=position))
            # GAP-1 - conditional-visibility extraction over the whole arch
            # subtree (catches nested + xpath-inserted fields, both legacy
            # attrs=/states= and v17+ direct invisible=/required=/readonly=/
            # column_invisible= forms).
            conditions = _extract_conditions(child)

    if not model:
        return None

    # A3 — best-effort source line from lxml .sourceline attribute (always int on lxml
    # elements; wrap with getattr for defensive safety against hypothetical future callers).
    src_line: int | None = getattr(record, "sourceline", None) or None

    # T2 — arch_snippet: first ≤30 lines (≤2000 chars) of arch for BASE views only.
    # Gives AI agents a quick structural overview without the full arch body.
    # Extension/inherit-only views carry None (their arch is typically just xpaths).
    _arch_snippet: str | None = None
    if arch and inherit_xmlid is None:
        _lines = arch.splitlines()[:30]
        _candidate = "\n".join(_lines)
        _arch_snippet = _candidate[:2000]

    return ViewInfo(
        xmlid=qualify_xmlid(xml_id, module.name),
        name=name,
        model=model,
        module=module.name,
        odoo_version=module.odoo_version,
        view_type=view_type,
        mode=mode,
        inherit_xmlid=inherit_xmlid,
        xpaths=xpaths,
        arch=arch,
        file_path=file_path,
        line=src_line,
        arch_snippet=_arch_snippet,
        conditions=conditions,
    )


def parse_file(filepath: str, module: ModuleInfo) -> list[ViewInfo]:
    """Parse an XML file, return list of ViewInfo found.

    Uses lxml.etree.parse() so that elements carry .sourceline for A3 provenance.
    Falls back to an empty list on any parse error.
    """
    try:
        tree = _lxml_etree.parse(filepath)
    except (_lxml_etree.XMLSyntaxError, OSError):
        return []
    root = tree.getroot()
    views = []
    for record in root.iter("record"):
        view = _parse_record(record, module, file_path=filepath)
        if view:
            views.append(view)
    return views


def parse_module(
    module_info: ModuleInfo,
    rng_root: Path | None = None,
) -> ViewParseResult:
    """Parse all XML files in a module directory.

    For Odoo v15+, each parsed view is validated against the version-exact
    RelaxNG schema read from *rng_root* (the ``rng/`` directory inside the
    indexed Odoo core source tree).  Violations are collected into
    ``result.lint_violations``.

    When *rng_root* is None, or when the relevant ``.rng`` file is absent in
    that directory (e.g. ``tree_view.rng`` is absent on v18+), validation is
    silently skipped — no false positives.

    v8-v14 views are always skipped regardless of *rng_root* (different grammar
    — the v15+ gate is enforced via VersionRegistry before any file lookup).
    """
    result = ViewParseResult(module=module_info)
    module_path = Path(module_info.path)
    SKIP_DIRS = {".git", "static", "tests", "__pycache__"}
    for xml_file in sorted(module_path.rglob("*.xml")):
        if SKIP_DIRS & set(xml_file.parts):
            continue
        result.views.extend(parse_file(str(xml_file), module_info))

    # RelaxNG validation — v15+ gate via VersionRegistry
    should_validate = _RELAXNG_GATE.resolve_version(module_info.odoo_version, default=False)
    if should_validate:
        for view in result.views:
            violations = _validate_arch_relaxng(view, rng_root)
            result.lint_violations.extend(violations)

    return result
