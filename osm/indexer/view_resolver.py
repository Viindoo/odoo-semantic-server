"""DOM-level view inheritance resolver.

Pure-function module: given a primary view ``arch`` (bytes) and an ordered list
of extension views plus their XPath patches, produce the final merged XML.
Semantics mirror Odoo core's
``odoo/tools/template_inheritance.py::apply_inheritance_specs``.

No database access. The caller is responsible for fetching ``views`` +
``view_patches`` rows and sorting extensions by
``(priority ASC, load_order ASC)`` before invoking :func:`resolve_chain`.
"""

from __future__ import annotations

import copy
import functools
from dataclasses import dataclass, field

from lxml import etree

# Hardened parser used for every XML deserialization in this module: disables
# external entity resolution (XXE) and outbound network lookups. Patch content
# originates from indexed addon XML and is therefore in the same trust class
# as the file the indexer parsed; we still refuse to dereference DTDs.
_SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ViewRow:
    """Minimal view header needed by the resolver.

    Mirrors the columns in ``data-model/views.md`` that the resolver actually
    reads. ``xmlid`` is used for ``patch_log`` attribution; other fields may
    be populated by the caller but are not referenced here.
    """

    xmlid: str


@dataclass(frozen=True, slots=True)
class PatchRow:
    """Minimal patch row needed by the resolver.

    Mirrors the columns in ``data-model/views.md`` for ``view_patches``.
    ``content`` is the inner-XML fragment of the patch spec — serialized by
    the XML parser without the wrapper element.
    """

    ordinal: int
    expr: str
    position: str
    content: str


@dataclass(frozen=True, slots=True)
class PatchLogEntry:
    """One attempted XPath op against the primary tree.

    ``reason`` is populated iff ``applied`` is ``False``; values are
    ``'replaced_ancestor'``, ``'xpath_no_match'``, or ``'malformed_expr'``.
    """

    from_xmlid: str
    ordinal: int
    expr: str
    position: str
    applied: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class ResolvedView:
    final_xml: bytes
    patch_log: list[PatchLogEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_VALID_POSITIONS = frozenset({"after", "before", "inside", "replace", "attributes"})


def _parse_patch_spec(content: str, position: str) -> etree._Element:
    """Wrap ``content`` in a synthetic ``<spec>`` element for uniform iteration.

    The parser stores the *inner* XML of each patch element (its children +
    any text) — to apply it we need an element whose children we can iterate.
    We deliberately pick a tag that cannot collide with any Odoo view element
    so accidental self-iteration via ``getiterator`` is obvious.
    """
    wrapped = f'<_osm_patch_spec position="{position}">{content}</_osm_patch_spec>'
    return etree.fromstring(wrapped.encode("utf-8"), _SAFE_PARSER)


@functools.lru_cache(maxsize=256)
def _compile_xpath(expr: str) -> etree.XPath | None:
    """Compile ``expr`` into a reusable lxml ``ETXPath`` object.

    Returns ``None`` when lxml rejects the expression. Cached so a view with
    N extensions does not pay the compile cost N times; sentinel-cached for
    bad exprs too so repeated malformed patches don't thrash.
    """
    try:
        return etree.ETXPath(expr)
    except etree.XPathSyntaxError:
        return None


def _locate(root: etree._Element, expr: str) -> tuple[etree._Element | None, str | None]:
    """Run ``expr`` against ``root``; return ``(node, error_reason)``.

    ``error_reason`` is ``'malformed_expr'`` when lxml rejects the expression,
    else ``None``. When the expression is valid but matches nothing the tuple
    is ``(None, None)``.
    """
    xpath = _compile_xpath(expr)
    if xpath is None:
        return None, "malformed_expr"
    try:
        matches = xpath(root)
    except etree.XPathEvalError:
        return None, "malformed_expr"
    # ETXPath return type is a broad union (bool / float / str / list); for
    # a non-predicate location step lxml always returns a list — narrow.
    if not isinstance(matches, list) or not matches:
        return None, None
    first = matches[0]
    if not isinstance(first, etree._Element):
        return None, None
    return first, None


def _apply_after(node: etree._Element, spec: etree._Element) -> None:
    """Insert ``spec``'s children as siblings after ``node``.

    We walk children in reverse and call ``addnext`` so the final order
    matches source order — ``addnext`` always inserts directly after ``node``.
    """
    for child in reversed(list(spec)):
        node.addnext(copy.deepcopy(child))


def _apply_before(node: etree._Element, spec: etree._Element) -> None:
    """Insert ``spec``'s children as siblings before ``node``."""
    for child in list(spec):
        node.addprevious(copy.deepcopy(child))


def _apply_inside(node: etree._Element, spec: etree._Element) -> None:
    """Append ``spec``'s children as the last children of ``node``."""
    for child in list(spec):
        node.append(copy.deepcopy(child))


def _apply_replace(node: etree._Element, spec: etree._Element) -> bool:
    """Remove ``node`` and insert ``spec``'s children in its place.

    Returns ``True`` iff the replace targeted the document element — in that
    case the caller must swap its tracked root reference, since removing the
    root from its (absent) parent is a no-op.
    """
    parent = node.getparent()
    if parent is None:
        return True
    for child in list(spec):
        node.addprevious(copy.deepcopy(child))
    parent.remove(node)
    return False


def _apply_attributes(node: etree._Element, spec: etree._Element) -> None:
    """Set / unset attributes per ``<attribute name="X">value</attribute>``.

    Matches Odoo core's basic semantics: empty value removes the attribute,
    non-empty sets it. The full ``add``/``remove``/``separator`` flavour is
    intentionally out of scope — it's a corner-case used almost exclusively
    in studio flows and can be added when real traffic demands it.
    """
    for child in spec.iter("attribute"):
        attribute = child.get("name")
        if attribute is None:
            continue
        value = child.text or ""
        if value:
            node.set(attribute, value)
        elif attribute in node.attrib:
            del node.attrib[attribute]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_chain(
    primary_arch: bytes,
    extensions: list[tuple[ViewRow, list[PatchRow]]],
) -> ResolvedView:
    """Merge ``extensions`` into ``primary_arch`` following Odoo semantics.

    :param primary_arch: serialized primary view arch (the ``<form>`` /
        ``<tree>`` / etc. root, UTF-8 encoded).
    :param extensions: each entry is ``(view_row, patches)`` — patches are
        applied in ``ordinal`` order, extensions in the given list order
        (caller sorts by ``(priority ASC, load_order ASC)``).
    :returns: :class:`ResolvedView` with ``final_xml``, per-patch
        ``patch_log`` and a human-readable ``warnings`` list.
    """
    root = etree.fromstring(primary_arch, _SAFE_PARSER)
    patch_log: list[PatchLogEntry] = []
    warnings: list[str] = []
    # Canonical element paths (via ElementTree.getpath) of every subtree root
    # removed by a prior ``position="replace"`` op, resolved against the
    # immutable ``original_root`` snapshot. Subsequent xpath matches that
    # land inside any of these subtrees are classified ``replaced_ancestor``
    # rather than the generic ``xpath_no_match``.
    # lxml element *identity* via id() is not stable across traversal —
    # wrappers can be re-materialised — so we compare canonical path strings.
    replaced_paths: list[str] = []
    # Deep copy so later xpath lookups can resolve against the pre-patch DOM
    # shape. Odoo semantics only require this for replaced_ancestor detection.
    original_root = copy.deepcopy(root)
    original_tree = original_root.getroottree()

    for view_row, patches in extensions:
        for patch in patches:
            position = patch.position
            if position not in _VALID_POSITIONS:
                warnings.append(
                    f"unknown_position:{view_row.xmlid}:ord={patch.ordinal}:pos={position}"
                )
                patch_log.append(
                    PatchLogEntry(
                        from_xmlid=view_row.xmlid,
                        ordinal=patch.ordinal,
                        expr=patch.expr,
                        position=position,
                        applied=False,
                        reason="malformed_expr",
                    )
                )
                continue

            node, locate_err = _locate(root, patch.expr)
            if locate_err == "malformed_expr":
                warnings.append(
                    f"malformed_xpath:{view_row.xmlid}:ord={patch.ordinal}:{patch.expr}"
                )
                patch_log.append(
                    PatchLogEntry(
                        from_xmlid=view_row.xmlid,
                        ordinal=patch.ordinal,
                        expr=patch.expr,
                        position=position,
                        applied=False,
                        reason="malformed_expr",
                    )
                )
                continue

            if node is None:
                # Distinguish replaced_ancestor from generic xpath_no_match.
                reason = "xpath_no_match"
                if replaced_paths:
                    orig_node, _orig_err = _locate(original_root, patch.expr)
                    if orig_node is not None:
                        match_path = original_tree.getpath(orig_node)
                        for rp in replaced_paths:
                            if match_path == rp or match_path.startswith(rp + "/"):
                                reason = "replaced_ancestor"
                                break
                warnings.append(
                    f"{reason}:{view_row.xmlid}:ord={patch.ordinal}:{patch.expr}"
                )
                patch_log.append(
                    PatchLogEntry(
                        from_xmlid=view_row.xmlid,
                        ordinal=patch.ordinal,
                        expr=patch.expr,
                        position=position,
                        applied=False,
                        reason=reason,
                    )
                )
                continue

            try:
                spec = _parse_patch_spec(patch.content, position)
            except etree.XMLSyntaxError:
                warnings.append(
                    f"malformed_patch_content:{view_row.xmlid}:ord={patch.ordinal}"
                )
                patch_log.append(
                    PatchLogEntry(
                        from_xmlid=view_row.xmlid,
                        ordinal=patch.ordinal,
                        expr=patch.expr,
                        position=position,
                        applied=False,
                        reason="malformed_expr",
                    )
                )
                continue

            if position == "after":
                _apply_after(node, spec)
            elif position == "before":
                _apply_before(node, spec)
            elif position == "inside":
                _apply_inside(node, spec)
            elif position == "attributes":
                _apply_attributes(node, spec)
            else:  # replace
                # Resolve the same xpath on ``original_root`` first so we can
                # stash the canonical path of the replaced subtree before we
                # mutate the live tree — that path is what lets subsequent
                # patches distinguish replaced_ancestor from xpath_no_match.
                orig_match, _ = _locate(original_root, patch.expr)
                if orig_match is not None:
                    replaced_paths.append(original_tree.getpath(orig_match))
                was_root = _apply_replace(node, spec)
                if was_root:
                    # Replaced the document root — rebuild from the first
                    # spec child. Odoo only supports single-root replacement
                    # at the top level; extra spec children are ignored to
                    # match core (``source = copy.deepcopy(spec_content)``).
                    new_root = None
                    for child in spec:
                        new_root = copy.deepcopy(child)
                        break
                    if new_root is not None:
                        root = new_root
                        original_root = copy.deepcopy(root)
                        original_tree = original_root.getroottree()
                        # Root-level replace invalidates prior paths — the
                        # tree is fully rebuilt under a new document element.
                        replaced_paths.clear()

            patch_log.append(
                PatchLogEntry(
                    from_xmlid=view_row.xmlid,
                    ordinal=patch.ordinal,
                    expr=patch.expr,
                    position=position,
                    applied=True,
                    reason=None,
                )
            )

    final_xml = etree.tostring(root, encoding="unicode").encode()
    return ResolvedView(final_xml=final_xml, patch_log=patch_log, warnings=warnings)


__all__ = [
    "PatchLogEntry",
    "PatchRow",
    "ResolvedView",
    "ViewRow",
    "resolve_chain",
]
