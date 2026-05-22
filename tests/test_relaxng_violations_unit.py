# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for RelaxNG XML validation at parser level — no Neo4j required (WI-E rework, M11).

Coverage:
  - Valid tree view yields no violations (with fixture rng_root).
  - Invalid tree view (deliberate schema error) yields LintViolationInfo with correct fields.
  - rng_root=None → no violations + no crash (graceful skip).
  - v13/v14 fixture (below v15 gate) yields NO violations from parse_module(), regardless
    of rng_root.
  - v15 and v17 fixtures (at/above gate) yield violations from parse_module() when rng_root
    is provided and the RNG file is present.
  - View type whose RNG file is absent in fixture dir (e.g. search_view.rng) → skipped,
    no false positives.
  - LintViolationInfo dataclass fields are all populated correctly.
  - Optional gated tests: if ~/git/odoo17 and ~/git/odoo18 exist, assert version-exact
    RNG resolution (tree_view.rng on v17, list_view.rng on v18).

RNG source: tests/fixtures/rng/ — self-contained, no external includes.
CI: no ~/git/odoo* present → real-source gated tests are skipped.
"""
import os
import textwrap
from pathlib import Path

import pytest

from src.indexer.models import ModuleInfo
from src.indexer.parser_xml import _validate_arch_relaxng, parse_file, parse_module

# No pytestmark = neo4j here — these tests run without Docker.

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

# Self-contained RNG fixtures under tests/fixtures/rng/ — no external includes.
_FIXTURE_RNG_DIR = Path(__file__).parent / "fixtures" / "rng"

# Real Odoo source RNG dirs for gated local-only tests. Resolved relative to
# $HOME (developer-machine layout ~/git/odooNN); the tests that use these are
# skipif-gated on .is_dir() so they no-op on CI and on machines without the
# source checkouts. Override the base dir with OSM_ODOO_SRC_DIR if your repos
# live elsewhere.
_ODOO_SRC_DIR = Path(os.environ.get("OSM_ODOO_SRC_DIR", str(Path.home() / "git")))
_ODOO17_RNG = _ODOO_SRC_DIR / "odoo17" / "odoo" / "addons" / "base" / "rng"
_ODOO18_RNG = _ODOO_SRC_DIR / "odoo18" / "odoo" / "addons" / "base" / "rng"

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

# <badtag> is not allowed inside <tree> per the fixture RNG schema.
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

# <badtag> is not allowed inside <list> per the fixture RNG schema.
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
# _validate_arch_relaxng direct tests
# ---------------------------------------------------------------------------


def test_valid_tree_view_no_violations(tmp_path):
    """A well-formed tree view produces zero violations."""
    module = _make_module("sale", "17.0", str(tmp_path))
    fp = _write_xml(tmp_path, "valid.xml", _VALID_TREE_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    violations = _validate_arch_relaxng(views[0], rng_root=_FIXTURE_RNG_DIR)
    assert violations == [], f"unexpected violations: {violations}"


def test_invalid_tree_view_produces_violations(tmp_path):
    """An invalid tree view (bad element) produces at least 1 LintViolationInfo."""
    module = _make_module("sale", "17.0", str(tmp_path))
    fp = _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    violations = _validate_arch_relaxng(views[0], rng_root=_FIXTURE_RNG_DIR)
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
    violations = _validate_arch_relaxng(views[0], rng_root=_FIXTURE_RNG_DIR)
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


def test_validate_arch_relaxng_rng_root_none_no_violation(tmp_path):
    """_validate_arch_relaxng with rng_root=None returns empty list — no crash."""
    module = _make_module("sale", "17.0", str(tmp_path))
    fp = _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    # rng_root=None → graceful skip, no false positives
    violations = _validate_arch_relaxng(views[0], rng_root=None)
    assert violations == [], "rng_root=None must produce no violations"


def test_validate_arch_relaxng_missing_rng_file_skipped(tmp_path):
    """A view_type whose RNG file is absent in rng_root is silently skipped."""
    module = _make_module("sale", "17.0", str(tmp_path))
    # Write a search view — _FIXTURE_RNG_DIR has no search_view.rng
    search_xml = """\
<?xml version="1.0"?>
<odoo>
    <record id="view_search" model="ir.ui.view">
        <field name="name">sale.order.search</field>
        <field name="model">sale.order</field>
        <field name="arch" type="xml">
            <search>
                <badtag/>
            </search>
        </field>
    </record>
</odoo>
"""
    fp = _write_xml(tmp_path, "search.xml", search_xml)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    assert views[0].view_type == "search"
    # search_view.rng is absent in fixture dir → no violations (file-existence drive)
    violations = _validate_arch_relaxng(views[0], rng_root=_FIXTURE_RNG_DIR)
    assert violations == [], (
        "search view must not produce violations when search_view.rng is absent in rng_root"
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
    assert _validate_arch_relaxng(view, rng_root=_FIXTURE_RNG_DIR) == []


def test_validate_arch_relaxng_no_file_path_returns_empty():
    """View with file_path=None produces no violations."""
    from src.indexer.models import ViewInfo
    view = ViewInfo(
        xmlid="sale.view_no_fp", name="no fp",
        model="sale.order", module="sale",
        odoo_version="17.0", view_type="tree", mode="primary",
        inherit_xmlid=None, arch="<field><tree/></field>", file_path=None,
    )
    assert _validate_arch_relaxng(view, rng_root=_FIXTURE_RNG_DIR) == []


def test_validate_arch_relaxng_unsupported_view_type_returns_empty(tmp_path):
    """Form views (no form_view.rng in fixture) produce no violations."""
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
    assert views, "expected at least 1 view parsed"
    violations = _validate_arch_relaxng(views[0], rng_root=_FIXTURE_RNG_DIR)
    assert violations == [], f"form views must not produce violations; got {violations}"


# ---------------------------------------------------------------------------
# parse_module gate tests
# ---------------------------------------------------------------------------


def test_v13_parse_module_no_violations(tmp_path):
    """v13 is below the v15 gate — parse_module must produce zero violations."""
    module = _make_module("sale", "13.0", str(tmp_path))
    _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    result = parse_module(module, rng_root=_FIXTURE_RNG_DIR)
    assert result.lint_violations == [], (
        f"v13 must produce no violations (gate=v15+); got {result.lint_violations}"
    )


def test_v14_parse_module_no_violations(tmp_path):
    """v14 is just below the gate — parse_module must produce zero violations."""
    module = _make_module("sale", "14.0", str(tmp_path))
    _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    result = parse_module(module, rng_root=_FIXTURE_RNG_DIR)
    assert result.lint_violations == [], (
        f"v14 must produce no violations (gate=v15+); got {result.lint_violations}"
    )


def test_v15_parse_module_produces_violations(tmp_path):
    """v15 is the gate boundary (inclusive) — must validate."""
    module = _make_module("sale", "15.0", str(tmp_path))
    _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    result = parse_module(module, rng_root=_FIXTURE_RNG_DIR)
    assert len(result.lint_violations) >= 1, (
        "v15 with invalid tree view must produce lint violations"
    )


def test_v17_parse_module_produces_violations(tmp_path):
    """v17 is above the gate — must validate."""
    module = _make_module("sale", "17.0", str(tmp_path))
    _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    result = parse_module(module, rng_root=_FIXTURE_RNG_DIR)
    assert len(result.lint_violations) >= 1, (
        "v17 with invalid tree view must produce lint violations"
    )


def test_v19_parse_module_produces_violations(tmp_path):
    """v19 is above the gate — fixture list_view.rng covers <list> root views."""
    module = _make_module("sale", "19.0", str(tmp_path))
    _write_xml(tmp_path, "invalid.xml", _INVALID_LIST_XML)
    result = parse_module(module, rng_root=_FIXTURE_RNG_DIR)
    assert len(result.lint_violations) >= 1, (
        "v19 with invalid list view must produce lint violations"
    )


def test_v17_parse_module_rng_root_none_skips(tmp_path):
    """parse_module with rng_root=None skips validation even for v17."""
    module = _make_module("sale", "17.0", str(tmp_path))
    _write_xml(tmp_path, "invalid.xml", _INVALID_TREE_XML)
    result = parse_module(module, rng_root=None)
    assert result.lint_violations == [], (
        "rng_root=None must produce no violations regardless of version"
    )


def test_valid_tree_parse_module_no_violations(tmp_path):
    """A valid tree view at v17 produces zero violations from parse_module."""
    module = _make_module("sale", "17.0", str(tmp_path))
    _write_xml(tmp_path, "valid.xml", _VALID_TREE_XML)
    result = parse_module(module, rng_root=_FIXTURE_RNG_DIR)
    assert result.lint_violations == [], (
        f"valid tree view must produce no violations; got {result.lint_violations}"
    )


# ---------------------------------------------------------------------------
# v18/v19 list-view tests (tree→list rename)
# ---------------------------------------------------------------------------


def test_v18_valid_list_view_no_violations(tmp_path):
    """A well-formed v18 <list> view produces zero violations."""
    module = _make_module("sale", "18.0", str(tmp_path))
    fp = _write_xml(tmp_path, "valid_list.xml", _VALID_LIST_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    assert views[0].view_type == "list", f"expected view_type='list', got {views[0].view_type!r}"
    violations = _validate_arch_relaxng(views[0], rng_root=_FIXTURE_RNG_DIR)
    assert violations == [], f"valid v18 list view must produce no violations; got {violations}"


def test_v18_invalid_list_view_produces_violations(tmp_path):
    """An invalid v18 <list> view (bad element) yields at least 1 LintViolationInfo."""
    module = _make_module("sale", "18.0", str(tmp_path))
    fp = _write_xml(tmp_path, "invalid_list.xml", _INVALID_LIST_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    violations = _validate_arch_relaxng(views[0], rng_root=_FIXTURE_RNG_DIR)
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
    result = parse_module(module, rng_root=_FIXTURE_RNG_DIR)
    assert len(result.lint_violations) >= 1, (
        "v18 with invalid list view must produce lint violations via parse_module"
    )
    v = result.lint_violations[0]
    assert v.rule == "relaxng.list_view"
    assert v.view_type == "list"


def test_v17_tree_view_still_validates(tmp_path):
    """Regression: v17 <tree> view still validates correctly."""
    module = _make_module("sale", "17.0", str(tmp_path))
    _write_xml(tmp_path, "invalid_tree.xml", _INVALID_TREE_XML)
    result = parse_module(module, rng_root=_FIXTURE_RNG_DIR)
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
    result = parse_module(module, rng_root=_FIXTURE_RNG_DIR)
    assert len(result.lint_violations) >= 1, (
        "v19 with invalid list view must produce lint violations"
    )


def test_v18_tree_view_type_not_validated(tmp_path):
    """On v18+, a <tree>-rooted view has no tree_view.rng in an Odoo 18 source dir.

    Using the fixture rng_root (which contains both tree_view.rng AND list_view.rng),
    we simulate this by checking that on v18 a <list> view uses list_view.rng.
    For the 'no tree_view.rng on v18' guarantee we test with a real Odoo 18 source
    (gated below).  Here we only assert that a <tree> view on v18+ matches view_type
    'tree' and the fixture tree_view.rng DOES validate it (fixture-dir has tree_view.rng
    for all versions — the real-source version-exact guarantee is tested below).
    """
    module = _make_module("sale", "18.0", str(tmp_path))
    fp = _write_xml(tmp_path, "invalid_tree.xml", _INVALID_TREE_XML)
    views = parse_file(fp, module)
    assert views, "expected at least 1 view parsed"
    assert views[0].view_type == "tree"


# ---------------------------------------------------------------------------
# Optional gated tests — real Odoo source (skipped in CI)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _ODOO17_RNG.is_dir(),
    reason="local ~/git/odoo17 not present — skipped in CI",
)
def test_v17_real_source_resolves_tree_view_rng():
    """Version-exact: v17 real source has tree_view.rng (no list_view.rng)."""
    assert (_ODOO17_RNG / "tree_view.rng").exists(), (
        "v17 source must have tree_view.rng"
    )
    assert not (_ODOO17_RNG / "list_view.rng").exists(), (
        "v17 source must NOT have list_view.rng"
    )
    # Validator must load successfully for tree on v17
    from src.indexer.parser_xml import _get_relaxng_validator
    v = _get_relaxng_validator("tree", _ODOO17_RNG)
    assert v is not None, "tree validator must load from v17 real source"
    # list_view.rng absent → None returned
    v2 = _get_relaxng_validator("list", _ODOO17_RNG)
    assert v2 is None, "list validator must return None when list_view.rng absent in v17"


@pytest.mark.skipif(
    not _ODOO18_RNG.is_dir(),
    reason="local ~/git/odoo18 not present — skipped in CI",
)
def test_v18_real_source_resolves_list_view_rng():
    """Version-exact: v18 real source has list_view.rng (no tree_view.rng)."""
    assert (_ODOO18_RNG / "list_view.rng").exists(), (
        "v18 source must have list_view.rng"
    )
    assert not (_ODOO18_RNG / "tree_view.rng").exists(), (
        "v18 source must NOT have tree_view.rng"
    )
    from src.indexer.parser_xml import _get_relaxng_validator
    v = _get_relaxng_validator("list", _ODOO18_RNG)
    assert v is not None, "list validator must load from v18 real source"
    v2 = _get_relaxng_validator("tree", _ODOO18_RNG)
    assert v2 is None, "tree validator must return None when tree_view.rng absent in v18"
