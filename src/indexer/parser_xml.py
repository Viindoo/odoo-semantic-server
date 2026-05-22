# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from lxml import etree as _lxml_etree

from .models import LintViolationInfo, ModuleInfo, ViewInfo, ViewParseResult, XPathInfo
from .version_registry import VersionRegistry

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RelaxNG validation (v15+ only) — WI-E, M11
# ---------------------------------------------------------------------------

# Schemas directory (vendored from Odoo SA, LGPL-3.0 — see NOTICE file).
_SCHEMA_DIR = Path(__file__).parent / "schemas" / "odoo_xml"

# VersionRegistry gate: validate only for Odoo v15+ where the schema is stable.
# v8-v14 had different view grammar; applying v15+ schema would produce
# false positives, so we return None for those majors.
_RELAXNG_GATE: VersionRegistry[bool] = VersionRegistry([
    (15, None, True),   # v15, v16, v17, v18, v19, … — validate
])

# View types that have a vendored RNG file, by version era.
# v15-v17: <tree> root validated by tree_view.rng
# v18+:    <list> root validated by list_view.rng (Odoo 18 renamed tree→list)
# Other types (activity, calendar, graph, pivot, search) are version-stable.
_RNG_SUPPORTED_VIEW_TYPES_PRE18 = frozenset({
    "activity", "calendar", "graph", "pivot", "search", "tree",
})
_RNG_SUPPORTED_VIEW_TYPES_V18PLUS = frozenset({
    "activity", "calendar", "graph", "pivot", "search", "list",
})

# Module-level cache: rng_filename_stem -> etree.RelaxNG or None (if load failed).
# Key is the schema filename stem (e.g. "tree_view", "list_view") so both tree
# and list schemas can be cached independently.
_RELAXNG_CACHE: dict[str, "_lxml_etree.RelaxNG | None"] = {}


def _odoo_major(odoo_version: str) -> int:
    """Return the major version integer from an Odoo version string (e.g. '18.0' -> 18)."""
    try:
        return int(odoo_version.split(".")[0])
    except (ValueError, IndexError):
        return 0


def _get_relaxng_validator(view_type: str, odoo_version: str) -> "_lxml_etree.RelaxNG | None":
    """Return cached RelaxNG validator for *view_type* at *odoo_version*, or None.

    Version-aware routing:
      - v18+: 'list' -> list_view.rng; 'tree' is not a valid root (skip).
      - v15-v17: 'tree' -> tree_view.rng; 'list' is not a valid root (skip).
      - Other types (activity, calendar, graph, pivot, search) unchanged.
    """
    major = _odoo_major(odoo_version)
    if major >= 18:
        supported = _RNG_SUPPORTED_VIEW_TYPES_V18PLUS
        # For list views on v18+ use list_view.rng; other types keep {type}_view.rng
        schema_stem = "list_view" if view_type == "list" else f"{view_type}_view"
    else:
        supported = _RNG_SUPPORTED_VIEW_TYPES_PRE18
        schema_stem = f"{view_type}_view"

    if view_type not in supported:
        return None

    if schema_stem not in _RELAXNG_CACHE:
        rng_path = _SCHEMA_DIR / f"{schema_stem}.rng"
        try:
            rng_doc = _lxml_etree.parse(str(rng_path))
            _RELAXNG_CACHE[schema_stem] = _lxml_etree.RelaxNG(rng_doc)
        except Exception:
            _logger.exception("Failed to load RelaxNG schema %r", rng_path.name)
            _RELAXNG_CACHE[schema_stem] = None
    return _RELAXNG_CACHE[schema_stem]


def _validate_arch_relaxng(
    view: ViewInfo,
) -> list[LintViolationInfo]:
    """Validate a view's arch XML against its RelaxNG schema (v15+ only).

    Returns a list of LintViolationInfo (empty = valid or schema not available).
    Version gate is checked by the caller via _RELAXNG_GATE.
    """
    if not view.arch or not view.file_path:
        return []
    validator = _get_relaxng_validator(view.view_type, view.odoo_version)
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
}


def _parse_record(
    record: ET.Element, module: ModuleInfo, file_path: str | None = None
) -> ViewInfo | None:
    """Parse a <record> element as an ir.ui.view."""
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

    for child in record:
        if child.tag != "field":
            continue
        fname = child.get("name", "")
        if fname == "name":
            name = (child.text or "").strip()
        elif fname == "model":
            model = (child.text or "").strip()
        elif fname == "inherit_id":
            ref = child.get("ref", "").strip()
            if ref:
                inherit_xmlid = ref
                mode = "extension"
        elif fname == "arch":
            arch = ET.tostring(child, encoding="unicode")
            arch_children = list(child)
            if arch_children:
                first = arch_children[0]
                # Unwrap <data> container used by many extension views
                if first.tag == "data":
                    data_children = list(first)
                    if data_children and data_children[0].tag in _VIEW_TYPES:
                        view_type = data_children[0].tag
                elif first.tag in _VIEW_TYPES:
                    view_type = first.tag
            for xpath_el in child.iter("xpath"):
                expr = xpath_el.get("expr", "").strip()
                position = xpath_el.get("position", "inside").strip()
                if expr:
                    xpaths.append(XPathInfo(expr=expr, position=position))

    if not model:
        return None

    return ViewInfo(
        xmlid=f"{module.name}.{xml_id}",
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
    )


def parse_file(filepath: str, module: ModuleInfo) -> list[ViewInfo]:
    """Parse an XML file, return list of ViewInfo found."""
    try:
        tree = ET.parse(filepath)
    except ET.ParseError:
        return []
    root = tree.getroot()
    views = []
    for record in root.iter("record"):
        view = _parse_record(record, module, file_path=filepath)
        if view:
            views.append(view)
    return views


def parse_module(module_info: ModuleInfo) -> ViewParseResult:
    """Parse all XML files in a module directory.

    For Odoo v15+, each parsed view whose type has a vendored RelaxNG schema
    is validated; violations are collected into ``result.lint_violations``.
    v8-v14 views are skipped (different grammar — would produce false positives).
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
            violations = _validate_arch_relaxng(view)
            result.lint_violations.extend(violations)

    return result
