# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for RelaxNG XML validation at parser level — no Neo4j required (WI-E, M11).

Coverage:
  - Valid tree view yields no violations.
  - Invalid tree view (deliberate schema error) yields LintViolationInfo with correct fields.
  - v13 fixture (below v15 gate) yields NO violations from parse_module().
  - v15 and v17 fixtures (at/above gate) yield violations from parse_module().
  - LintViolationInfo dataclass fields are all populated correctly.
"""
import textwrap
from pathlib import Path

from src.indexer.models import ModuleInfo
from src.indexer.parser_xml import _validate_arch_relaxng, parse_file, parse_module

# No pytestmark = neo4j here — these tests run without Docker.

# ---------------------------------------------------------------------------
# Shared XML fixtures
# ---------------------------------------------------------------------------

_VALID_TREE_XML = """\
<?xml version="1.0"?>
<odoo>
    <record id="view_order_tree" model="ir.ui.view">
        <field name="name">sale.order.tree</field>
        <field name="model">sale.order</field>
        <field name="arch" type="xml">
            <tree>
                <field name="name"/>
                <field name="partner_id"/>
            </tree>
        </field>
    </record>
</odoo>
"""

# <badtag> is not allowed inside <tree> per Odoo's RNG schema.
_INVALID_TREE_XML = """\
<?xml version="1.0"?>
<odoo>
    <record id="view_order_tree_bad" model="ir.ui.view">
        <field name="name">sale.order.tree.bad</field>
        <field name="model">sale.order</field>
        <field name="arch" type="xml">
            <tree>
                <badtag foo="bar"/>
            </tree>
        </field>
    </record>
</odoo>
"""


def _make_module(name: str, version: str, path: str) -> ModuleInfo:
    return ModuleInfo(
        name=name, odoo_version=version, repo=f"{name}_repo",
        path=path, depends=[], version_raw="",
    )


def _write_xml(directory: Path, filename: str, content: str) -> str:
    p = directory / filename
    p.write_text(textwrap.dedent(content).strip())
    return str(p)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_valid_tree_view_no_violations(tmp_path):
    """A well-formed tree view produces zero violations."""
    module = _make_module("sale", "17.0", str(tmp_path))
    fp = _write_xml(tmp_path, "valid.xml", _VALID_TREE_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    violations = _validate_arch_relaxng(views[0])
    assert violations == [], f"unexpected violations: {violations}"


def test_invalid_tree_view_produces_violations(tmp_path):
    """An invalid tree view (bad element) produces at least 1 LintViolationInfo."""
    module = _make_module("sale", "17.0", str(tmp_path))
    fp = _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    violations = _validate_arch_relaxng(views[0])
    assert len(violations) >= 1, "expected violations for invalid tree view"
    v = violations[0]
    assert v.rule == "relaxng.tree_view"
    assert v.severity == "error"
    assert v.view_xmlid == "sale.view_order_tree_bad"
    assert v.odoo_version == "17.0"
    assert v.view_type == "tree"
    assert "badtag" in v.message


def test_lint_violation_info_fields_populated(tmp_path):
    """All LintViolationInfo fields are set correctly."""
    module = _make_module("sale", "17.0", str(tmp_path))
    fp = _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    views = parse_file(fp, module)
    violations = _validate_arch_relaxng(views[0])
    assert violations, "precondition: must have violations to check fields"
    v = violations[0]
    assert v.file_path == fp
    assert isinstance(v.line, int)
    assert v.rule.startswith("relaxng.")
    assert v.message
    assert v.view_xmlid
    assert v.odoo_version == "17.0"
    assert v.severity == "error"
    assert v.view_type == "tree"


def test_v13_parse_module_no_violations(tmp_path):
    """v13 is below the v15 gate — parse_module must produce zero violations."""
    module = _make_module("sale", "13.0", str(tmp_path))
    _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    result = parse_module(module)
    assert result.lint_violations == [], (
        f"v13 must produce no violations (gate=v15+); got {result.lint_violations}"
    )


def test_v14_parse_module_no_violations(tmp_path):
    """v14 is just below the gate — parse_module must produce zero violations."""
    module = _make_module("sale", "14.0", str(tmp_path))
    _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    result = parse_module(module)
    assert result.lint_violations == [], (
        f"v14 must produce no violations (gate=v15+); got {result.lint_violations}"
    )


def test_v15_parse_module_produces_violations(tmp_path):
    """v15 is the gate boundary (inclusive) — must validate."""
    module = _make_module("sale", "15.0", str(tmp_path))
    _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    result = parse_module(module)
    assert len(result.lint_violations) >= 1, (
        "v15 with invalid tree view must produce lint violations"
    )


def test_v17_parse_module_produces_violations(tmp_path):
    """v17 is above the gate — must validate."""
    module = _make_module("sale", "17.0", str(tmp_path))
    _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    result = parse_module(module)
    assert len(result.lint_violations) >= 1, (
        "v17 with invalid tree view must produce lint violations"
    )


def test_v19_parse_module_produces_violations(tmp_path):
    """v19 is above the gate (open-ended) — must validate.

    v19 >= 18 uses <list> root (tree→list rename), so we use the invalid <list>
    XML fixture.  An invalid <tree>-root view on v19 produces no violations
    because <tree> is not a valid v18+ root tag (not false-positiveable).
    """
    module = _make_module("sale", "19.0", str(tmp_path))
    # v19 uses <list> root (v18+ era); use the invalid <list> fixture.
    _write_xml(tmp_path, "invalid.xml", """\
<?xml version="1.0"?>
<odoo>
    <record id="view_order_list_bad" model="ir.ui.view">
        <field name="name">sale.order.list.bad</field>
        <field name="model">sale.order</field>
        <field name="arch" type="xml">
            <list>
                <badtag foo="bar"/>
            </list>
        </field>
    </record>
</odoo>
""")
    result = parse_module(module)
    assert len(result.lint_violations) >= 1, (
        "v19 with invalid list view must produce lint violations"
    )


def test_valid_tree_parse_module_no_violations(tmp_path):
    """A valid tree view at v17 produces zero violations from parse_module."""
    module = _make_module("sale", "17.0", str(tmp_path))
    _write_xml(tmp_path, "valid.xml", _VALID_TREE_XML)
    result = parse_module(module)
    assert result.lint_violations == [], (
        f"valid tree view must produce no violations; got {result.lint_violations}"
    )


def test_validate_arch_relaxng_no_arch_returns_empty(tmp_path):
    """View with arch=None produces no violations (nothing to validate)."""
    from src.indexer.models import ViewInfo
    view = ViewInfo(
        xmlid="sale.view_no_arch", name="no arch",
        model="sale.order", module="sale",
        odoo_version="17.0", view_type="tree", mode="primary",
        inherit_xmlid=None, arch=None, file_path="/tmp/x.xml",
    )
    assert _validate_arch_relaxng(view) == []


def test_validate_arch_relaxng_no_file_path_returns_empty():
    """View with file_path=None produces no violations."""
    from src.indexer.models import ViewInfo
    view = ViewInfo(
        xmlid="sale.view_no_fp", name="no fp",
        model="sale.order", module="sale",
        odoo_version="17.0", view_type="tree", mode="primary",
        inherit_xmlid=None, arch="<field><tree/></field>", file_path=None,
    )
    assert _validate_arch_relaxng(view) == []


def test_validate_arch_relaxng_unsupported_view_type_returns_empty(tmp_path):
    """Form views (no RNG file) produce no violations."""
    module = _make_module("sale", "17.0", str(tmp_path))
    xml_content = """\
<?xml version="1.0"?>
<odoo>
    <record id="view_form" model="ir.ui.view">
        <field name="name">sale.order.form</field>
        <field name="model">sale.order</field>
        <field name="arch" type="xml">
            <form>
                <field name="name"/>
            </form>
        </field>
    </record>
</odoo>
"""
    fp = _write_xml(tmp_path, "form.xml", xml_content)
    views = parse_file(fp, module)
    # Form views have no RNG schema — no violations produced
    assert views, "expected at least 1 view parsed"
    violations = _validate_arch_relaxng(views[0])
    assert violations == [], f"form views must not produce violations; got {violations}"


# ---------------------------------------------------------------------------
# v18 list-view tests (MED-1: tree→list rename)
# ---------------------------------------------------------------------------

_VALID_LIST_XML = """\
<?xml version="1.0"?>
<odoo>
    <record id="view_order_list" model="ir.ui.view">
        <field name="name">sale.order.list</field>
        <field name="model">sale.order</field>
        <field name="arch" type="xml">
            <list>
                <field name="name"/>
                <field name="partner_id"/>
            </list>
        </field>
    </record>
</odoo>
"""

# <badtag> is not allowed inside <list> per Odoo 18's RNG schema.
_INVALID_LIST_XML = """\
<?xml version="1.0"?>
<odoo>
    <record id="view_order_list_bad" model="ir.ui.view">
        <field name="name">sale.order.list.bad</field>
        <field name="model">sale.order</field>
        <field name="arch" type="xml">
            <list>
                <badtag foo="bar"/>
            </list>
        </field>
    </record>
</odoo>
"""


def test_v18_valid_list_view_no_violations(tmp_path):
    """A well-formed v18 <list> view produces zero violations."""
    module = _make_module("sale", "18.0", str(tmp_path))
    fp = _write_xml(tmp_path, "valid_list.xml", _VALID_LIST_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    assert views[0].view_type == "list", f"expected view_type='list', got {views[0].view_type!r}"
    violations = _validate_arch_relaxng(views[0])
    assert violations == [], f"valid v18 list view must produce no violations; got {violations}"


def test_v18_invalid_list_view_produces_violations(tmp_path):
    """An invalid v18 <list> view (bad element) yields at least 1 LintViolationInfo."""
    module = _make_module("sale", "18.0", str(tmp_path))
    fp = _write_xml(tmp_path, "invalid_list.xml", _INVALID_LIST_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    violations = _validate_arch_relaxng(views[0])
    assert len(violations) >= 1, "expected violations for invalid v18 list view"
    v = violations[0]
    assert v.rule == "relaxng.list_view"
    assert v.severity == "error"
    assert v.view_xmlid == "sale.view_order_list_bad"
    assert v.odoo_version == "18.0"
    assert v.view_type == "list"
    assert "badtag" in v.message


def test_v18_parse_module_list_view_violations(tmp_path):
    """parse_module on v18 with invalid <list> view yields violations end-to-end."""
    module = _make_module("sale", "18.0", str(tmp_path))
    _write_xml(tmp_path, "invalid_list.xml", _INVALID_LIST_XML)
    result = parse_module(module)
    assert len(result.lint_violations) >= 1, (
        "v18 with invalid list view must produce lint violations via parse_module"
    )
    v = result.lint_violations[0]
    assert v.rule == "relaxng.list_view"
    assert v.view_type == "list"


def test_v17_tree_view_still_validates(tmp_path):
    """Regression: v17 <tree> view still validates correctly after v18 list support added."""
    module = _make_module("sale", "17.0", str(tmp_path))
    _write_xml(tmp_path, "invalid_tree.xml", _INVALID_TREE_XML)
    result = parse_module(module)
    assert len(result.lint_violations) >= 1, (
        "v17 with invalid tree view must still produce lint violations (regression check)"
    )
    v = result.lint_violations[0]
    assert v.rule == "relaxng.tree_view"
    assert v.view_type == "tree"


def test_v19_parse_module_list_view_violations(tmp_path):
    """v19 is also >= 18 — must use list_view.rng for <list> root views."""
    module = _make_module("sale", "19.0", str(tmp_path))
    _write_xml(tmp_path, "invalid_list.xml", _INVALID_LIST_XML)
    result = parse_module(module)
    assert len(result.lint_violations) >= 1, (
        "v19 with invalid list view must produce lint violations"
    )


def test_v18_tree_view_type_not_validated(tmp_path):
    """On v18+, a <tree>-rooted view_type='tree' is not a valid root — no RNG file.

    The parser sets view_type='tree' for a <tree> tag. On v18+, 'tree' is not
    in _RNG_SUPPORTED_VIEW_TYPES_V18PLUS, so no violations are produced
    (rather than false-positive errors from using the v17 tree_view.rng).
    """
    module = _make_module("sale", "18.0", str(tmp_path))
    fp = _write_xml(tmp_path, "invalid_tree.xml", _INVALID_TREE_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    # The parser sees <tree> tag -> view_type='tree'
    assert views[0].view_type == "tree"
    violations = _validate_arch_relaxng(views[0])
    assert violations == [], (
        "v18 <tree>-root view must not be validated "
        "(tree tag is not v18 root — avoid false positives)"
    )
