"""lxml-based parser for Odoo view XML: ``<record model="ir.ui.view">`` elements.

Mirrors the shape of ``python_parser.py``:
- Frozen dataclasses ``ParsedView`` / ``ParsedPatch`` as public types
- ``FileParseResult`` NamedTuple returned by ``parse_view_file``
- Warnings returned as a tuple of strings on the result (never raised for
  recoverable parse errors)

Scope (WP-14): produce rows for the ``views`` and ``view_patches`` tables.
Patch *application* (``apply_inheritance_specs``) is WP-15 and lives in a
separate module.  This parser captures the raw patch shape only:
``(ordinal, expr, position, content)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from typing import NamedTuple

from lxml import etree

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedPatch:
    """One XPath operation inside an extension view's ``<field name="arch">``.

    - ``expr``: XPath expression.  For explicit ``<xpath expr="...">`` elements
      this is the raw attribute.  For implicit-locator specs (``<field
      name="X">`` etc.) this is a synthesised XPath that mirrors Odoo's
      ``locate_node()`` semantics (see
      ``odoo/tools/template_inheritance.py::locate_node``).
    - ``position``: ``after`` / ``before`` / ``inside`` / ``replace`` /
      ``attributes``.  Default ``inside`` per Odoo.
    - ``content``: serialized children of the patch element (the *payload*
      to insert / attributes to set), NOT the wrapper itself.
    - ``ordinal``: 0-indexed position among non-whitespace element children
      of ``<field name="arch">``.
    """

    ordinal: int
    expr: str
    position: str
    content: str


@dataclass(frozen=True, slots=True)
class ParsedView:
    """One ``<record model="ir.ui.view">`` entry.

    ``inherit_xmlid`` is the raw ``ref="module.xmlid"`` string; FK resolution
    to a ``views.id`` bigint is deferred to WP-15's second pass (mirrors
    ``resolver.py``'s ``override_of`` approach).

    For primary views, ``arch_xml`` holds the serialized root child of
    ``<field name="arch">`` and ``arch_hash`` hashes those bytes.  For
    extension views, ``arch_xml`` is ``b''`` (extensions carry no standalone
    arch) and ``arch_hash`` hashes the concatenation of
    ``patch.content`` strings — so delta detection catches patch changes.
    """

    xmlid: str
    model: str
    view_type: str
    inherit_xmlid: str | None
    priority: int
    mode: str  # 'primary' | 'extension'
    arch_hash: str
    arch_xml: bytes
    patches: tuple[ParsedPatch, ...]
    file_path: str
    start_line: int
    end_line: int


class FileParseResult(NamedTuple):
    views: tuple[ParsedView, ...]
    warnings: tuple[str, ...]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MANIFEST_FILENAMES = ("__manifest__.py", "__openerp__.py")


def _find_module_name(path: Path) -> str | None:
    """Walk upward until a manifest file is found; return its parent dir name."""
    for parent in path.parents:
        for fn in _MANIFEST_FILENAMES:
            if (parent / fn).is_file():
                return parent.name
    return None


def _arch_hash(arch_bytes: bytes) -> str:
    return blake2b(arch_bytes, digest_size=16).hexdigest()


def _find_field(record: etree._Element, name: str) -> etree._Element | None:
    for child in record:
        if child.tag == "field" and child.get("name") == name:
            return child
    return None


def _read_ref(elem: etree._Element) -> str | None:
    ref = elem.get("ref")
    if ref:
        return str(ref)
    return None


def _sourceline(elem: etree._Element) -> int:
    """Return sourceline as int, 0 when unknown.

    lxml's ``sourceline`` attribute is typed as a class-level sentinel in
    ``lxml-stubs`` but is really an ``int | None`` at runtime; access via
    ``getattr`` to bypass the stub's incorrect narrowing.
    """
    sl = getattr(elem, "sourceline", None)
    if isinstance(sl, int):
        return sl
    return 0


def _record_end_line(record: etree._Element) -> int:
    """Approximate end-line by walking descendants and taking the max sourceline.

    lxml does not track closing-tag line numbers natively — we use the latest
    element sourceline within the record as a pragmatic proxy, good enough for
    surfacing back to humans via the MCP tool.
    """
    last = _sourceline(record)
    for descendant in record.iter():
        if isinstance(descendant, etree._Element):
            sl = _sourceline(descendant)
            if sl > last:
                last = sl
    return last


def _synth_expr(spec: etree._Element) -> str | None:
    """Mirror ``odoo/tools/template_inheritance.py::locate_node`` as an XPath.

    - ``<xpath expr="...">`` → the raw expr
    - ``<field name="X">`` → ``//field[@name='X']`` (Odoo matches by name only)
    - other tag with attributes → ``//<tag>[@a='b'][@c='d']`` (all attrs
      except ``position``)
    - other tag with no attributes → ``//<tag>`` (matches first element of tag;
      Odoo's ``locate_node`` iterates ``spec.attrib`` so an empty filter set
      reduces to ``all([])`` == ``True`` — first-of-tag wins)
    - fallback → ``None`` only when tag is unusable (non-str, e.g. comments)
    """
    tag_raw: object = spec.tag
    if not isinstance(tag_raw, str):
        return None
    tag = tag_raw
    if tag == "xpath":
        expr = spec.get("expr")
        return str(expr) if expr else None
    if tag == "field":
        name = spec.get("name")
        if name:
            return f"//field[@name={_xpath_quote(str(name))}]"
        return None
    attrs = [
        (str(k), str(v))
        for k, v in spec.attrib.items()
        if str(k) != "position"
    ]
    if not attrs:
        return f"//{tag}"
    predicates = "".join(f"[@{k}={_xpath_quote(v)}]" for k, v in attrs)
    return f"//{tag}{predicates}"


def _xpath_quote(value: str) -> str:
    """Quote a literal for inclusion in an XPath expression."""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    # Both quote types present — build with concat()
    parts = value.split("'")
    joined = ",\"'\",".join(f"'{p}'" for p in parts)
    return f"concat({joined})"


def _serialize_children(elem: etree._Element) -> str:
    """Serialize the inner XML of ``elem`` (children + text), sans the wrapper."""
    out: list[str] = []
    if elem.text:
        out.append(elem.text)
    for child in elem:
        out.append(etree.tostring(child, encoding="unicode", with_tail=True))
    return "".join(out)


# ---------------------------------------------------------------------------
# Core per-record parser
# ---------------------------------------------------------------------------


def _parse_record(
    record: etree._Element,
    module_name: str,
    file_path: str,
    warnings: list[str],
) -> ParsedView | None:
    model_attr = record.get("model")
    if model_attr != "ir.ui.view":
        # Silently skip non-view records (act_window, ir.rule, ir.actions.*, ...).
        return None

    start_line = _sourceline(record)
    record_id = record.get("id") or f"line_{start_line}"
    xmlid = f"{module_name}.{record_id}"
    end_line = _record_end_line(record)

    model_field = _find_field(record, "model")
    if model_field is None or not (model_field.text and model_field.text.strip()):
        warnings.append(f"missing_model:{xmlid}")
        return None
    model = model_field.text.strip()

    arch_field = _find_field(record, "arch")
    if arch_field is None:
        warnings.append(f"missing_arch:{xmlid}")
        return None

    # inherit_id handling — read raw ref; warn on non-ref forms but still
    # mark as extension so downstream knows this was an inheritance attempt.
    inherit_xmlid: str | None = None
    inherit_field = _find_field(record, "inherit_id")
    if inherit_field is not None:
        ref = _read_ref(inherit_field)
        if ref is not None:
            inherit_xmlid = ref
        else:
            # e.g. <field name="inherit_id" eval="..."/> — we cannot resolve
            # but still want the caller to treat this as an extension view.
            warnings.append(f"inherit_id_non_ref:{xmlid}")

    # Priority — default 16 per Odoo.
    priority = 16
    prio_field = _find_field(record, "priority")
    if prio_field is not None and prio_field.text:
        try:
            priority = int(prio_field.text.strip())
        except ValueError:
            warnings.append(f"priority_non_int:{xmlid}")

    # Mode — explicit <field name="mode"> wins (Odoo supports ``mode='primary'``
    # together with ``inherit_id`` to mean "clone parent arch, then apply my
    # patches, but register as a standalone primary view"). Otherwise inferred
    # from ``inherit_id`` presence.
    mode_field = _find_field(record, "mode")
    declared_mode: str | None = None
    if mode_field is not None and mode_field.text:
        stripped = mode_field.text.strip()
        if stripped in ("primary", "extension"):
            declared_mode = stripped
    if declared_mode is not None:
        mode = declared_mode
    else:
        is_ext = inherit_xmlid is not None or inherit_field is not None
        mode = "extension" if is_ext else "primary"

    # view_type — read explicit <field name="type"> if present; else infer
    # from the arch's root element tag (Odoo also infers this way).
    view_type: str
    type_field = _find_field(record, "type")
    if type_field is not None and type_field.text and type_field.text.strip():
        view_type = type_field.text.strip()
    else:
        view_type = _infer_view_type(arch_field)

    if mode == "primary":
        arch_xml, patches = _extract_primary_arch(arch_field)
        arch_hash = _arch_hash(arch_xml)
    else:
        arch_xml = b""
        patches = _extract_patches(arch_field, xmlid, warnings)
        concat = "".join(p.content for p in patches).encode("utf-8")
        arch_hash = _arch_hash(concat)

    return ParsedView(
        xmlid=xmlid,
        model=model,
        view_type=view_type,
        inherit_xmlid=inherit_xmlid,
        priority=priority,
        mode=mode,
        arch_hash=arch_hash,
        arch_xml=arch_xml,
        patches=patches,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
    )


def _infer_view_type(arch_field: etree._Element) -> str:
    """Return the tag of the first element child of ``<field name="arch">``."""
    for child in arch_field:
        tag: object = child.tag
        if isinstance(tag, str):
            return tag
    return "unknown"


def _extract_primary_arch(arch_field: etree._Element) -> tuple[bytes, tuple[ParsedPatch, ...]]:
    """Serialize the primary arch root element.

    Odoo primary views carry a single root child inside ``<field name="arch">``
    (``<form>``, ``<tree>``, ``<kanban>``, ``<search>``, ...).  We serialize
    that child verbatim; primary views have no patches.
    """
    for child in arch_field:
        tag: object = child.tag
        if isinstance(tag, str):
            return bytes(etree.tostring(child, encoding="utf-8")), ()
    return b"", ()


def _extract_patches(
    arch_field: etree._Element,
    xmlid: str,
    warnings: list[str],
) -> tuple[ParsedPatch, ...]:
    """Walk direct element children of ``<field name="arch">`` as patch specs.

    Odoo's ``apply_inheritance_specs`` treats each top-level child as a spec;
    a ``<data>`` wrapper has its children flattened.  We mirror that shape.
    """
    patches: list[ParsedPatch] = []
    ordinal = 0
    for child in arch_field:
        child_tag: object = child.tag
        if not isinstance(child_tag, str):
            continue  # skip comments/PIs (runtime-only; stub says tag is str)
        if child_tag == "data":
            for grandchild in child:
                grandchild_tag: object = grandchild.tag
                if not isinstance(grandchild_tag, str):
                    continue
                patch = _build_patch(grandchild, ordinal, xmlid, warnings)
                if patch is not None:
                    patches.append(patch)
                    ordinal += 1
            continue
        patch = _build_patch(child, ordinal, xmlid, warnings)
        if patch is not None:
            patches.append(patch)
            ordinal += 1
    return tuple(patches)


def _build_patch(
    spec: etree._Element,
    ordinal: int,
    xmlid: str,
    warnings: list[str],
) -> ParsedPatch | None:
    expr = _synth_expr(spec)
    if not expr:
        warnings.append(f"unparseable_patch_target:{xmlid}:ord={ordinal}")
        return None
    position = spec.get("position", "inside")
    content = _serialize_children(spec)
    return ParsedPatch(ordinal=ordinal, expr=expr, position=position, content=content)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_view_file(path: Path) -> FileParseResult:
    """Parse every ``<record model="ir.ui.view">`` in *path*.

    Returns ``FileParseResult(views, warnings)`` — never raises for
    recoverable errors (malformed records, missing fields, unparseable
    XPath targets).  IO errors from ``path.read_bytes()`` propagate to the
    caller.
    """
    module_name = _find_module_name(path)
    if module_name is None:
        return FileParseResult((), (f"module_name_undetermined:{path}",))

    try:
        source = path.read_bytes()
    except OSError as exc:
        _logger.warning("parse_view_file: cannot read %s: %s", path, exc)
        return FileParseResult((), (f"io_error:{path}:{exc}",))

    try:
        tree = etree.fromstring(source)
    except etree.XMLSyntaxError as exc:
        _logger.warning("parse_view_file: xml syntax error in %s: %s", path, exc)
        return FileParseResult((), (f"xml_syntax_error:{path}:{exc}",))

    warnings: list[str] = []
    views: list[ParsedView] = []
    for record in tree.iter("record"):
        parsed = _parse_record(record, module_name, str(path), warnings)
        if parsed is not None:
            views.append(parsed)

    return FileParseResult(tuple(views), tuple(warnings))
