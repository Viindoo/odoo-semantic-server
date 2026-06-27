# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

from lxml import etree as _lxml_etree

from ._xmlid import qualify_xmlid
from .models import ModuleInfo, QWebInfo, ViewParseResult


def _parse_template(
    elem: "_lxml_etree._Element", module: ModuleInfo, file_path: str | None = None
) -> QWebInfo | None:
    """Extract QWebInfo from a <template> element.

    *elem* is an lxml element so that `.sourceline` is available for A3
    provenance.  Returns None if template has no id attribute.
    """
    template_id = elem.get("id", "").strip()
    if not template_id:
        return None

    inherit_xmlid = qualify_xmlid(elem.get("inherit_id"), module.name)

    # GAP-11 - website QWeb `key=` (canonical public xmlid for multi-website
    # dispatch / inheritance). GAP-12 - `mode=` ("primary" forks a new primary
    # view from the inherited one; default "extension" patches in place). Both are
    # plain attributes on <template>; None when absent.
    key = (elem.get("key") or "").strip() or None
    mode = (elem.get("mode") or "").strip() or None

    # A3 — best-effort source line from lxml .sourceline (always int on lxml
    # elements; defensive getattr in case of future callers with non-lxml elements).
    src_line: int | None = getattr(elem, "sourceline", None) or None

    return QWebInfo(
        xmlid=qualify_xmlid(template_id, module.name),
        module=module.name,
        odoo_version=module.odoo_version,
        inherit_xmlid=inherit_xmlid,
        content=_lxml_etree.tostring(elem, encoding="unicode"),
        file_path=file_path,
        line=src_line,
        key=key,
        mode=mode,
    )


def _parse_qweb_record(
    record: "_lxml_etree._Element", module: ModuleInfo, file_path: str | None = None
) -> QWebInfo | None:
    """Extract QWebInfo from a ``<record model="ir.ui.view">`` QWeb-type view.

    In Odoo v8-v14 (and test_website through v19), website-style QWeb templates
    are declared as ``ir.ui.view`` records carrying ``<field name="type">qweb</field>``
    plus a ``<field name="key">module.name</field>`` (the public xmlid) and a
    ``<field name="arch">`` body — NOT as a top-level ``<template>`` element and
    WITHOUT a ``<field name="model">`` child. ``parser_xml._parse_record`` drops
    these (no model field), and they are never matched by ``root.iter("template")``.

    This routes such records to the SAME QWebTmpl shape that ``_parse_template``
    emits, so the EXTENDS_TMPL resolver in ``writer_neo4j_ui`` can find them as a
    base. The QWebTmpl xmlid is keyed on the ``key`` field (the canonical public
    xmlid extenders inherit by), falling back to the record ``id`` when ``key`` is
    absent. ``inherit_id`` (if present) becomes ``inherit_xmlid``.

    Returns None when the record is not an ``ir.ui.view`` of type ``qweb``, or
    when no usable xmlid (key/id) can be derived.
    """
    if record.get("model") != "ir.ui.view":
        return None

    view_type = ""
    key = ""
    inherit_ref = ""
    has_arch = False
    for child in record:
        tag = child.tag
        if not isinstance(tag, str) or tag != "field":
            continue
        fname = child.get("name", "")
        if fname == "type":
            view_type = (child.text or "").strip()
        elif fname == "key":
            key = (child.text or "").strip()
        elif fname == "inherit_id":
            inherit_ref = child.get("ref", "").strip()
        elif fname == "arch":
            has_arch = True

    if view_type != "qweb":
        return None

    # Prefer the public `key` (what extenders inherit by); fall back to record id.
    raw_xmlid = key or record.get("id", "").strip()
    if not raw_xmlid:
        return None

    inherit_xmlid = (
        qualify_xmlid(inherit_ref, module.name) if inherit_ref else None
    )

    src_line: int | None = getattr(record, "sourceline", None) or None

    return QWebInfo(
        # `key` is already a fully-qualified "module.name"; qualify_xmlid is a
        # no-op on an already-dotted id, so this is safe for both key and bare id.
        xmlid=qualify_xmlid(raw_xmlid, module.name),
        module=module.name,
        odoo_version=module.odoo_version,
        inherit_xmlid=inherit_xmlid,
        content=_lxml_etree.tostring(record, encoding="unicode") if has_arch else None,
        file_path=file_path,
        line=src_line,
        # GAP-11 - preserve the website `key` field value (already captured above
        # for the xmlid). None when this record had no <field name="key">.
        key=(key or None),
    )


def parse_file(filepath: str, module: ModuleInfo) -> list[QWebInfo]:
    """Parse a single XML file and extract all QWeb templates.

    Two declaration syntaxes are recognised, both yielding QWebInfo:
      1. top-level ``<template id="...">`` elements (the modern v8+ form);
      2. ``<record model="ir.ui.view">`` records carrying
         ``<field name="type">qweb</field>`` (the v8-v14 website / test_website
         form that has a ``key`` xmlid + ``arch`` body but no ``model`` field —
         see ``_parse_qweb_record``).

    Uses lxml.etree.parse() so that elements carry .sourceline for A3 provenance.
    Returns empty list if XML is malformed.
    """
    try:
        tree = _lxml_etree.parse(filepath)
    except (_lxml_etree.XMLSyntaxError, OSError):
        return []

    root = tree.getroot()
    qweb = []
    for tmpl in root.iter("template"):
        q = _parse_template(tmpl, module, file_path=filepath)
        if q:
            qweb.append(q)
    for record in root.iter("record"):
        q = _parse_qweb_record(record, module, file_path=filepath)
        if q:
            qweb.append(q)
    return qweb


def parse_module(module_info: ModuleInfo) -> ViewParseResult:
    """Parse all XML files in a module directory.

    Scans the module directory recursively, skipping common non-content directories.
    Returns a ViewParseResult with all QWeb templates found.
    """
    result = ViewParseResult(module=module_info)
    module_path = Path(module_info.path)

    # Directories to skip when scanning
    SKIP_DIRS = {".git", "static", "tests", "__pycache__"}

    for xml_file in sorted(module_path.rglob("*.xml")):
        # Skip files in excluded directories
        if SKIP_DIRS & set(xml_file.parts):
            continue
        result.qweb.extend(parse_file(str(xml_file), module_info))

    return result
