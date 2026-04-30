"""Unit tests for osm.indexer.xml_parser against synthetic + CE-subset fixtures.

Style matches tests/indexer/test_python_parser.py:
- one TestClass per fixture module (8 cv_* classes)
- explicit invariant tests (primary/extension shape, multi-record, malformed)
- smoke coverage over a handful of real CE views — parser must not warn on them
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

import pytest

from osm.indexer.xml_parser import FileParseResult, ParsedView, parse_view_file

FIXTURES_CUSTOM = Path(__file__).parent.parent / "fixtures" / "custom_addons"
FIXTURES_CE = Path(__file__).parent.parent / "fixtures" / "odoo_ce_subset"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _only_views_xml(module_dir: Path) -> Path:
    """Return the sole .xml file under <module>/views/."""
    files = sorted((module_dir / "views").glob("*.xml"))
    assert len(files) == 1, f"expected one view file, got {files!r}"
    return files[0]


def _parse_cv(module_name: str) -> FileParseResult:
    return parse_view_file(_only_views_xml(FIXTURES_CUSTOM / module_name))


def _write_manifest_and_view(tmp: Path, module_name: str, view_xml: str) -> Path:
    """Lay out a minimal <tmp>/<module>/__manifest__.py + views/v.xml tree."""
    mod = tmp / module_name
    (mod / "views").mkdir(parents=True)
    (mod / "__manifest__.py").write_text(
        "{'name':'x','version':'0.1.0','depends':['base'],'installable':True}"
    )
    path = mod / "views" / "v.xml"
    path.write_text(view_xml)
    return path


# ---------------------------------------------------------------------------
# cv_basic_form — primary baseline
# ---------------------------------------------------------------------------


class TestCvBasicForm:
    def test_one_view_parsed(self) -> None:
        r = _parse_cv("cv_basic_form")
        assert len(r.views) == 1
        assert r.warnings == ()

    def test_is_primary(self) -> None:
        v = _parse_cv("cv_basic_form").views[0]
        assert v.mode == "primary"
        assert v.inherit_xmlid is None
        assert v.patches == ()

    def test_view_type_and_model(self) -> None:
        v = _parse_cv("cv_basic_form").views[0]
        assert v.model == "res.partner"
        assert v.view_type == "form"

    def test_xmlid(self) -> None:
        v = _parse_cv("cv_basic_form").views[0]
        assert v.xmlid == "cv_basic_form.cv_basic_partner_form"

    def test_arch_xml_non_empty(self) -> None:
        v = _parse_cv("cv_basic_form").views[0]
        assert v.arch_xml.startswith(b"<form")
        assert b"<field name=\"name\"" in v.arch_xml


# ---------------------------------------------------------------------------
# cv_simple_ext — extension with position="after"
# ---------------------------------------------------------------------------


class TestCvSimpleExt:
    def test_is_extension(self) -> None:
        v = _parse_cv("cv_simple_ext").views[0]
        assert v.mode == "extension"
        assert v.inherit_xmlid == "cv_basic_form.cv_basic_partner_form"
        assert v.arch_xml == b""

    def test_one_patch(self) -> None:
        v = _parse_cv("cv_simple_ext").views[0]
        assert len(v.patches) == 1
        p = v.patches[0]
        assert p.ordinal == 0
        assert p.position == "after"
        assert p.expr == "//field[@name='email']"
        assert "<field" in p.content and "phone" in p.content


# ---------------------------------------------------------------------------
# cv_replace_and_sibling
# ---------------------------------------------------------------------------


class TestCvReplaceAndSibling:
    def test_two_extension_views(self) -> None:
        r = _parse_cv("cv_replace_and_sibling")
        assert len(r.views) == 2
        assert all(v.mode == "extension" for v in r.views)

    def test_replace_patch(self) -> None:
        r = _parse_cv("cv_replace_and_sibling")
        replace_view = next(v for v in r.views if v.xmlid.endswith(".cv_replace_email"))
        assert replace_view.priority == 10
        assert replace_view.patches[0].position == "replace"
        assert replace_view.patches[0].expr == "//field[@name='email']"

    def test_sibling_patch(self) -> None:
        r = _parse_cv("cv_replace_and_sibling")
        sibling_view = next(v for v in r.views if v.xmlid.endswith(".cv_sibling_name"))
        assert sibling_view.priority == 20
        assert sibling_view.patches[0].position == "after"
        assert sibling_view.patches[0].expr == "//field[@name='name']"


# ---------------------------------------------------------------------------
# cv_replace_orphan
# ---------------------------------------------------------------------------


class TestCvReplaceOrphan:
    def test_two_extension_views(self) -> None:
        r = _parse_cv("cv_replace_orphan")
        assert len(r.views) == 2
        assert all(v.mode == "extension" for v in r.views)

    def test_replace_on_group(self) -> None:
        r = _parse_cv("cv_replace_orphan")
        repl = next(v for v in r.views if v.xmlid.endswith(".cv_orphan_replace_group"))
        assert repl.patches[0].position == "replace"
        assert repl.patches[0].expr == "//group"

    def test_descendant_patch_target(self) -> None:
        r = _parse_cv("cv_replace_orphan")
        desc = next(v for v in r.views if v.xmlid.endswith(".cv_orphan_descendant"))
        # The parser just records the target — the resolver decides applied=false.
        assert desc.patches[0].expr == "//field[@name='email']"
        assert desc.patches[0].position == "after"


# ---------------------------------------------------------------------------
# cv_multi_ext_same_target
# ---------------------------------------------------------------------------


class TestCvMultiExtSameTarget:
    def test_three_views(self) -> None:
        r = _parse_cv("cv_multi_ext_same_target")
        assert len(r.views) == 3

    def test_distinct_priorities(self) -> None:
        r = _parse_cv("cv_multi_ext_same_target")
        priorities = sorted(v.priority for v in r.views)
        assert priorities == [10, 20, 30]

    def test_all_target_email(self) -> None:
        r = _parse_cv("cv_multi_ext_same_target")
        for v in r.views:
            assert v.patches[0].expr == "//field[@name='email']"
            assert v.patches[0].position == "after"


# ---------------------------------------------------------------------------
# cv_xpath_no_match
# ---------------------------------------------------------------------------


class TestCvXpathNoMatch:
    def test_parser_does_not_warn(self) -> None:
        # Parser does not evaluate XPath — the warning belongs to the resolver.
        r = _parse_cv("cv_xpath_no_match")
        assert r.warnings == ()

    def test_expr_preserved(self) -> None:
        v = _parse_cv("cv_xpath_no_match").views[0]
        assert v.patches[0].expr == "//field[@name='this_field_does_not_exist']"


# ---------------------------------------------------------------------------
# cv_priority_tie
# ---------------------------------------------------------------------------


class TestCvPriorityTie:
    def test_equal_priorities(self) -> None:
        r = _parse_cv("cv_priority_tie")
        assert len(r.views) == 2
        assert {v.priority for v in r.views} == {16}

    def test_both_extensions(self) -> None:
        r = _parse_cv("cv_priority_tie")
        assert all(v.mode == "extension" for v in r.views)
        assert all(v.inherit_xmlid == "cv_basic_form.cv_basic_partner_form" for v in r.views)


# ---------------------------------------------------------------------------
# cv_attributes_op
# ---------------------------------------------------------------------------


class TestCvAttributesOp:
    def test_attributes_position(self) -> None:
        v = _parse_cv("cv_attributes_op").views[0]
        assert v.patches[0].position == "attributes"
        assert v.patches[0].expr == "//field[@name='email']"

    def test_content_has_attribute_children(self) -> None:
        v = _parse_cv("cv_attributes_op").views[0]
        assert "<attribute name=\"readonly\">1</attribute>" in v.patches[0].content
        assert "<attribute name=\"required\">1</attribute>" in v.patches[0].content


# ---------------------------------------------------------------------------
# parse_view_file invariants (inline fixtures)
# ---------------------------------------------------------------------------


class TestPrimaryReturnsInheritNone:
    def test_primary_inherit_none(self) -> None:
        v = _parse_cv("cv_basic_form").views[0]
        assert v.inherit_xmlid is None
        assert v.mode == "primary"
        assert v.patches == ()


class TestParseMultiRecordFile:
    XML = """<?xml version="1.0" encoding="UTF-8"?>
<odoo>
  <record id="va" model="ir.ui.view">
    <field name="name">va</field>
    <field name="model">res.partner</field>
    <field name="arch" type="xml">
      <form><field name="name"/></form>
    </field>
  </record>
  <record id="vb" model="ir.ui.view">
    <field name="name">vb</field>
    <field name="model">res.partner</field>
    <field name="inherit_id" ref="mymod.va"/>
    <field name="arch" type="xml">
      <field name="name" position="after"><field name="email"/></field>
    </field>
  </record>
</odoo>
"""

    def test_two_views_returned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_manifest_and_view(Path(td), "mymod", self.XML)
            r = parse_view_file(path)
            assert len(r.views) == 2
            assert r.warnings == ()
            modes = {v.mode for v in r.views}
            assert modes == {"primary", "extension"}


class TestParseMissingModelWarnsAndSkips:
    XML = """<?xml version="1.0"?>
<odoo>
  <record id="broken" model="ir.ui.view">
    <field name="name">broken</field>
    <field name="arch" type="xml"><form/></field>
  </record>
</odoo>
"""

    def test_missing_model(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_manifest_and_view(Path(td), "mymod", self.XML)
            r = parse_view_file(path)
            assert r.views == ()
            assert any(w.startswith("missing_model:") for w in r.warnings)


class TestParseMissingArchWarnsAndSkips:
    XML = """<?xml version="1.0"?>
<odoo>
  <record id="broken" model="ir.ui.view">
    <field name="name">broken</field>
    <field name="model">res.partner</field>
  </record>
</odoo>
"""

    def test_missing_arch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_manifest_and_view(Path(td), "mymod", self.XML)
            r = parse_view_file(path)
            assert r.views == ()
            assert any(w.startswith("missing_arch:") for w in r.warnings)


class TestParseNonViewRecordSilentlySkipped:
    XML = """<?xml version="1.0"?>
<odoo>
  <record id="act1" model="ir.actions.act_window">
    <field name="name">Some Action</field>
  </record>
  <record id="rule1" model="ir.rule">
    <field name="name">A rule</field>
  </record>
</odoo>
"""

    def test_silent_skip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_manifest_and_view(Path(td), "mymod", self.XML)
            r = parse_view_file(path)
            assert r.views == ()
            assert r.warnings == ()


# ---------------------------------------------------------------------------
# Smoke test — real CE fixtures parse without warnings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module,filename,min_primary,min_extension",
    [
        ("base", "res_partner_views.xml", 1, 0),
        ("product", "product_template_views.xml", 1, 0),
        ("account", "partner_view.xml", 0, 1),
        # Regression: product_document_views.xml has extensions with
        # <sheet position="inside"> / <search position="inside"> — spec
        # elements with no attributes beyond position. Ensures _synth_expr
        # returns //sheet and //search (not None → unparseable_patch_target).
        ("sale", "product_document_views.xml", 0, 1),
    ],
)
def test_ce_fixture_smoke(
    module: str, filename: str, min_primary: int, min_extension: int
) -> None:
    path = FIXTURES_CE / module / "views" / filename
    assert path.exists(), f"missing CE fixture: {path}"
    r = parse_view_file(path)
    # No recoverable parser warnings expected on hand-picked CE files.
    assert r.warnings == (), f"unexpected warnings: {r.warnings}"
    primaries = [v for v in r.views if v.mode == "primary"]
    extensions = [v for v in r.views if v.mode == "extension"]
    assert len(primaries) >= min_primary, (
        f"{module}/{filename}: wanted ≥{min_primary} primary, got {len(primaries)}"
    )
    assert len(extensions) >= min_extension, (
        f"{module}/{filename}: wanted ≥{min_extension} extension, got {len(extensions)}"
    )


def test_parsed_view_is_frozen() -> None:
    # Matches python_parser.ParsedModel posture — slots=True + frozen=True.
    v = _parse_cv("cv_basic_form").views[0]
    assert isinstance(v, ParsedView)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.xmlid = "other"  # type: ignore[misc]
