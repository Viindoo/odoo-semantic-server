"""Unit tests for ``osm.indexer.view_resolver``.

Feeds primary arch bytes + ``(ViewRow, [PatchRow])`` lists directly into
:func:`resolve_chain`. Upstream (WP-16) will load real rows from Postgres;
these tests stay in memory and reuse the WP-14 XML parser to turn the
committed ``tests/fixtures/custom_addons/cv_*`` corpus into resolver inputs.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from osm.indexer.view_resolver import (
    PatchLogEntry,
    PatchRow,
    ResolvedView,
    ViewRow,
    resolve_chain,
)
from osm.indexer.xml_parser import ParsedPatch, ParsedView, parse_view_file

FIXTURES_CUSTOM = Path(__file__).parent.parent / "fixtures" / "custom_addons"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_module(module_name: str) -> tuple[ParsedView, ...]:
    """Parse the single XML file inside ``<fixture>/views/`` and return views."""
    files = sorted((FIXTURES_CUSTOM / module_name / "views").glob("*.xml"))
    assert len(files) == 1, f"expected one view file in {module_name!r}"
    result = parse_view_file(files[0])
    assert result.warnings == (), result.warnings
    return result.views


def _primary_arch(module_name: str) -> bytes:
    """Return the primary view arch from a single-primary fixture module."""
    views = _parse_module(module_name)
    primaries = [v for v in views if v.mode == "primary"]
    assert len(primaries) == 1, f"expected one primary in {module_name!r}"
    return primaries[0].arch_xml


def _patches_as_rows(parsed_patches: tuple[ParsedPatch, ...]) -> list[PatchRow]:
    return [
        PatchRow(ordinal=p.ordinal, expr=p.expr, position=p.position, content=p.content)
        for p in parsed_patches
    ]


def _ext(module_name: str, xmlid_suffix: str) -> tuple[ViewRow, list[PatchRow]]:
    """Pick one extension view by xmlid suffix from a fixture module."""
    views = _parse_module(module_name)
    matches = [v for v in views if v.xmlid.endswith("." + xmlid_suffix)]
    assert len(matches) == 1, f"xmlid suffix {xmlid_suffix!r} not unique in {module_name!r}"
    v = matches[0]
    return ViewRow(xmlid=v.xmlid), _patches_as_rows(v.patches)


def _count_in_final(xml: bytes, xpath: str) -> int:
    return len(etree.fromstring(xml).xpath(xpath))


# ---------------------------------------------------------------------------
# Tests — one scenario per WP-15 plan line item
# ---------------------------------------------------------------------------


def test_no_extensions() -> None:
    primary = _primary_arch("cv_basic_form")
    resolved = resolve_chain(primary, extensions=[])
    assert isinstance(resolved, ResolvedView)
    assert resolved.patch_log == []
    assert resolved.warnings == []
    # Final XML has the same element shape as primary (may differ in trivial
    # whitespace after lxml round-trip, so compare structurally).
    assert etree.fromstring(resolved.final_xml).tag == "form"
    assert _count_in_final(resolved.final_xml, "//field[@name='email']") == 1


def test_after_insert() -> None:
    primary = _primary_arch("cv_basic_form")
    resolved = resolve_chain(
        primary,
        extensions=[_ext("cv_simple_ext", "cv_simple_partner_ext")],
    )
    assert len(resolved.patch_log) == 1
    entry = resolved.patch_log[0]
    assert entry.applied is True
    assert entry.position == "after"
    assert entry.reason is None
    # phone field should now exist, positioned after email.
    final_tree = etree.fromstring(resolved.final_xml)
    fields = final_tree.xpath("//field[@name]")
    names = [f.get("name") for f in fields]
    assert "phone" in names
    assert names.index("email") + 1 == names.index("phone")


def test_before_insert() -> None:
    # cv_basic_form has no committed ``before`` fixture — synthesise inline.
    primary = _primary_arch("cv_basic_form")
    ext = (
        ViewRow(xmlid="test.before_ext"),
        [
            PatchRow(
                ordinal=0,
                expr="//field[@name='email']",
                position="before",
                content='<field name="phone"/>',
            )
        ],
    )
    resolved = resolve_chain(primary, extensions=[ext])
    assert resolved.patch_log[0].applied is True
    names = [
        f.get("name")
        for f in etree.fromstring(resolved.final_xml).xpath("//field[@name]")
    ]
    assert names.index("phone") + 1 == names.index("email")


def test_inside_insert() -> None:
    primary = _primary_arch("cv_basic_form")
    ext = (
        ViewRow(xmlid="test.inside_ext"),
        [
            PatchRow(
                ordinal=0,
                expr="//group",
                position="inside",
                content='<field name="phone"/>',
            )
        ],
    )
    resolved = resolve_chain(primary, extensions=[ext])
    assert resolved.patch_log[0].applied is True
    group = etree.fromstring(resolved.final_xml).find(".//group")
    assert group is not None
    # Last child of <group> is the inserted <field name="phone"/>.
    assert group[-1].tag == "field"
    assert group[-1].get("name") == "phone"


def test_replace_then_sibling() -> None:
    """cv_replace_and_sibling: replace <email>, then patch <name> sibling.

    Both patches should apply — the sibling lives in the same parent group
    and is not a descendant of the replaced node.
    """
    primary = _primary_arch("cv_basic_form")
    views = _parse_module("cv_replace_and_sibling")
    views_sorted = sorted(views, key=lambda v: (v.priority, v.xmlid))
    extensions = [
        (ViewRow(xmlid=v.xmlid), _patches_as_rows(v.patches)) for v in views_sorted
    ]
    resolved = resolve_chain(primary, extensions=extensions)
    assert len(resolved.patch_log) == 2
    assert all(e.applied for e in resolved.patch_log), resolved.warnings
    names = [
        f.get("name")
        for f in etree.fromstring(resolved.final_xml).xpath("//field[@name]")
    ]
    assert "email" not in names  # replaced
    assert "email_normalized" in names  # replacement content
    assert "display_name" in names  # sibling patch added
    assert names.index("name") + 1 == names.index("display_name")


def test_replace_then_descendant_orphan() -> None:
    """cv_replace_orphan: replace <group>, then patch <email> (descendant).

    The second patch targets a node that was a child of the replaced group.
    After the replace, that node no longer exists in the tree — the patch
    must be recorded as applied=False with reason='replaced_ancestor'.
    """
    primary = _primary_arch("cv_basic_form")
    views = _parse_module("cv_replace_orphan")
    views_sorted = sorted(views, key=lambda v: (v.priority, v.xmlid))
    extensions = [
        (ViewRow(xmlid=v.xmlid), _patches_as_rows(v.patches)) for v in views_sorted
    ]
    resolved = resolve_chain(primary, extensions=extensions)
    applied = [e for e in resolved.patch_log if e.applied]
    unapplied = [e for e in resolved.patch_log if not e.applied]
    assert len(applied) == 1, f"expected 1 applied, got {resolved.patch_log}"
    assert len(unapplied) == 1
    assert unapplied[0].reason == "replaced_ancestor"
    assert unapplied[0].position == "after"
    assert any("replaced_ancestor" in w for w in resolved.warnings)
    # Replacement content present; orphan patch did not land.
    names = [
        f.get("name")
        for f in etree.fromstring(resolved.final_xml).xpath("//field[@name]")
    ]
    assert "vat" in names
    assert "mobile" not in names


def test_multi_ext_order() -> None:
    """cv_multi_ext_same_target: three extensions, ascending priority.

    All three patches insert a field after <email>. Application order must
    follow priority ASC, and the resulting sibling sequence is the reverse
    of insertion — each ``addnext`` places the new node directly after
    <email>, pushing previously-inserted fields further down. Expected
    after-email order: field_c, field_b, field_a.
    """
    primary = _primary_arch("cv_basic_form")
    views = _parse_module("cv_multi_ext_same_target")
    views_sorted = sorted(views, key=lambda v: (v.priority, v.xmlid))
    extensions = [
        (ViewRow(xmlid=v.xmlid), _patches_as_rows(v.patches)) for v in views_sorted
    ]
    resolved = resolve_chain(primary, extensions=extensions)
    assert len(resolved.patch_log) == 3
    assert all(e.applied for e in resolved.patch_log)
    # Extensions were consumed priority-ascending.
    assert [e.from_xmlid for e in resolved.patch_log] == [
        v.xmlid for v in views_sorted
    ]
    names = [
        f.get("name")
        for f in etree.fromstring(resolved.final_xml).xpath("//field[@name]")
    ]
    email_idx = names.index("email")
    # addnext pushes earlier inserts further from <email>.
    assert names[email_idx + 1 : email_idx + 4] == ["field_c", "field_b", "field_a"]


def test_xpath_no_match() -> None:
    primary = _primary_arch("cv_basic_form")
    ext = _ext("cv_xpath_no_match", "cv_nomatch_ext")
    resolved = resolve_chain(primary, extensions=[ext])
    assert len(resolved.patch_log) == 1
    entry = resolved.patch_log[0]
    assert entry.applied is False
    assert entry.reason == "xpath_no_match"
    assert resolved.warnings  # non-empty
    assert any("xpath_no_match" in w for w in resolved.warnings)


def test_malformed_xpath() -> None:
    primary = _primary_arch("cv_basic_form")
    ext = (
        ViewRow(xmlid="test.malformed"),
        [
            PatchRow(
                ordinal=0,
                expr="//field[@name=",  # deliberately broken
                position="after",
                content='<field name="phone"/>',
            )
        ],
    )
    resolved = resolve_chain(primary, extensions=[ext])
    assert len(resolved.patch_log) == 1
    entry = resolved.patch_log[0]
    assert entry.applied is False
    assert entry.reason == "malformed_expr"
    assert any("malformed_xpath" in w for w in resolved.warnings)


def test_attributes_op() -> None:
    primary = _primary_arch("cv_basic_form")
    ext = _ext("cv_attributes_op", "cv_attr_ext")
    resolved = resolve_chain(primary, extensions=[ext])
    assert len(resolved.patch_log) == 1
    assert resolved.patch_log[0].applied is True
    email = etree.fromstring(resolved.final_xml).find(".//field[@name='email']")
    assert email is not None
    assert email.get("readonly") == "1"
    assert email.get("required") == "1"


def test_priority_tie_load_order() -> None:
    """cv_priority_tie: both extensions have priority=16.

    The caller orders by (priority, load_order); we simulate that ordering by
    feeding the list in two permutations and checking the applied sequence
    reflects the provided list order exactly.
    """
    primary = _primary_arch("cv_basic_form")
    views = _parse_module("cv_priority_tie")
    alpha = next(v for v in views if v.xmlid.endswith(".cv_tie_alpha"))
    beta = next(v for v in views if v.xmlid.endswith(".cv_tie_beta"))

    ext_alpha_first: list[tuple[ViewRow, list[PatchRow]]] = [
        (ViewRow(xmlid=alpha.xmlid), _patches_as_rows(alpha.patches)),
        (ViewRow(xmlid=beta.xmlid), _patches_as_rows(beta.patches)),
    ]
    ext_beta_first: list[tuple[ViewRow, list[PatchRow]]] = [
        (ViewRow(xmlid=beta.xmlid), _patches_as_rows(beta.patches)),
        (ViewRow(xmlid=alpha.xmlid), _patches_as_rows(alpha.patches)),
    ]

    resolved_a = resolve_chain(primary, extensions=ext_alpha_first)
    resolved_b = resolve_chain(primary, extensions=ext_beta_first)

    # Caller-provided order is preserved in patch_log.
    assert [e.from_xmlid for e in resolved_a.patch_log] == [alpha.xmlid, beta.xmlid]
    assert [e.from_xmlid for e in resolved_b.patch_log] == [beta.xmlid, alpha.xmlid]
    # And the resulting DOMs differ in the sibling order of the markers.
    names_a = [
        f.get("name")
        for f in etree.fromstring(resolved_a.final_xml).xpath("//field[@name]")
    ]
    names_b = [
        f.get("name")
        for f in etree.fromstring(resolved_b.final_xml).xpath("//field[@name]")
    ]
    email_a = names_a.index("email")
    email_b = names_b.index("email")
    # addnext places later-applied patch *closer* to <email>.
    assert names_a[email_a + 1 : email_a + 3] == ["beta_marker", "alpha_marker"]
    assert names_b[email_b + 1 : email_b + 3] == ["alpha_marker", "beta_marker"]


# ---------------------------------------------------------------------------
# Extra sanity — frozen dataclass shape
# ---------------------------------------------------------------------------


def test_patch_log_entry_is_frozen() -> None:
    entry = PatchLogEntry(
        from_xmlid="x.y", ordinal=0, expr="//form", position="inside",
        applied=True, reason=None,
    )
    try:
        entry.applied = False  # type: ignore[misc]
    except (AttributeError, TypeError):
        return
    raise AssertionError("PatchLogEntry must be frozen")
