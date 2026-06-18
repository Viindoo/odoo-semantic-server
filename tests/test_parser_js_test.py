# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for parser_js_test.py (no Docker required).

Business rules protected by each test are stated in the test function name.
Fixture JS sources are handcrafted inline strings — NOT read from real Odoo
source — so the oracle is author-independent of the parser under test
(anti-tautology principle, ETHOS #10 + design §8.4).

Red-before-green: all assertions target behaviors that the implementation
must explicitly implement (they fail without the code).
"""
import textwrap

from src.indexer.models import ModuleInfo
from src.indexer.parser_js_test import (
    _detect_framework,
    _extract_describe_blocks,
    _extract_mock_models,
    _extract_mounts,
    _extract_tags,
    _extract_test_names,
    _extract_tour_names,
    _is_js_test_file,
    parse_js_test_file,
    parse_module_js_tests,
)

# ---------------------------------------------------------------------------
# _is_js_test_file guard
# ---------------------------------------------------------------------------

def test_hoot_test_file_recognized():
    """Business rule: *.test.js under static/tests/ is a Hoot test file."""
    assert _is_js_test_file("/addons/account/static/tests/account_move.test.js")


def test_qunit_test_file_recognized():
    """Business rule: *_tests.js under static/tests/ is a QUnit test file."""
    assert _is_js_test_file("/addons/account/static/tests/account_move_tests.js")


def test_tour_file_recognized():
    """Business rule: *.js under static/tests/tours/ is a tour file."""
    assert _is_js_test_file("/addons/account/static/tests/tours/account_tour.js")


def test_non_test_js_file_not_recognized():
    """Business rule: JS files outside static/tests/ are NOT test files."""
    assert not _is_js_test_file("/addons/sale/static/src/js/sale_order.js")
    assert not _is_js_test_file("/addons/sale/models/sale_order.py")


def test_static_but_non_tests_dir_not_recognized():
    """Business rule: JS files in static/src/ (not tests) are not test files."""
    assert not _is_js_test_file("/addons/sale/static/src/components/test_widget.js")


# ---------------------------------------------------------------------------
# _detect_framework
# ---------------------------------------------------------------------------

HOOT_SOURCE = textwrap.dedent("""
    import { test } from "@odoo/hoot";
    import { mountView } from "@web/../tests/web_test_helpers";

    test("My test", async () => {
        await mountView({ type: "form", resModel: "sale.order" });
    });
""")

QUNIT_SOURCE = textwrap.dedent("""
    /* @odoo-module */
    QUnit.module("Sale", function () {
        QUnit.test("basic test", function (assert) {
            assert.ok(true);
        });
    });
""")

TOUR_SOURCE = textwrap.dedent("""
    /** @odoo-module **/
    import { registry } from "@web/core/registry";

    registry.category("web_tour.tours").add("my_tour", {
        url: "/odoo",
        steps: () => [],
    });
""")

TOUR_LEGACY_SOURCE = textwrap.dedent("""
    import web_tour from "web_tour.tour";

    web_tour.tour.register("legacy_tour", {
        url: "/odoo",
        steps: [],
    });
""")


def test_detects_hoot_from_import():
    """Business rule: '@odoo/hoot' import -> framework='hoot'."""
    assert _detect_framework(HOOT_SOURCE) == "hoot"


def test_detects_qunit_from_usage():
    """Business rule: QUnit. usage without hoot import -> framework='qunit'."""
    assert _detect_framework(QUNIT_SOURCE) == "qunit"


def test_detects_tour_from_registry_category():
    """Business rule: registry.category('web_tour.tours') -> framework='tour'."""
    assert _detect_framework(TOUR_SOURCE) == "tour"


def test_hoot_takes_priority_over_qunit():
    """Business rule: hoot import present even alongside QUnit usage -> 'hoot' wins."""
    mixed = HOOT_SOURCE + "\n" + QUNIT_SOURCE
    assert _detect_framework(mixed) == "hoot"


# ---------------------------------------------------------------------------
# _extract_describe_blocks
# ---------------------------------------------------------------------------

HOOT_DESCRIBE_SOURCE = textwrap.dedent("""
    import { describe, test } from "@odoo/hoot";

    describe("X2many buttons", () => {
        describe("nested group", () => {
            test("renders", async () => {});
        });
    });
""")

QUNIT_MODULE_SOURCE = textwrap.dedent("""
    QUnit.module("Views", {}, function () {
        QUnit.module("MoveFormView");
        QUnit.test("switches tabs", async (assert) => {});
    });
""")


def test_extract_hoot_describe_blocks():
    """Business rule: describe('title') in Hoot source -> describe_blocks list."""
    blocks = _extract_describe_blocks(HOOT_DESCRIBE_SOURCE, "hoot")
    assert "X2many buttons" in blocks
    assert "nested group" in blocks


def test_extract_qunit_module_blocks():
    """Business rule: QUnit.module('title') -> describe_blocks list."""
    blocks = _extract_describe_blocks(QUNIT_MODULE_SOURCE, "qunit")
    assert "Views" in blocks
    assert "MoveFormView" in blocks


# ---------------------------------------------------------------------------
# _extract_test_names
# ---------------------------------------------------------------------------

def test_extract_hoot_test_names():
    """Business rule: test('title', ...) in Hoot source -> test_names list."""
    source = textwrap.dedent("""
        import { test } from "@odoo/hoot";
        test("renders add line", async () => {});
        test("handles keyboard input", async () => {});
    """)
    names = _extract_test_names(source, "hoot")
    assert "renders add line" in names
    assert "handles keyboard input" in names


def test_extract_qunit_test_names():
    """Business rule: QUnit.test('title', ...) -> test_names list."""
    source = textwrap.dedent("""
        QUnit.test("When I switch tabs, it saves", async (assert) => {});
        QUnit.test("validation shows error", async (assert) => {});
    """)
    names = _extract_test_names(source, "qunit")
    assert "When I switch tabs, it saves" in names
    assert "validation shows error" in names


# ---------------------------------------------------------------------------
# _extract_tags
# ---------------------------------------------------------------------------

def test_extract_hoot_tags_from_describe_current():
    """Business rule: describe.current.tags('tag') -> tags list."""
    source = textwrap.dedent("""
        import { describe } from "@odoo/hoot";
        describe.current.tags("desktop", "mobile");
        test("my test", () => {});
    """)
    tags = _extract_tags(source)
    assert "desktop" in tags
    assert "mobile" in tags


def test_extract_tags_from_test_tags():
    """Business rule: test.tags('tag') on a test -> tags list."""
    source = textwrap.dedent("""
        import { test } from "@odoo/hoot";
        test.tags("desktop")("renders", async () => {});
    """)
    tags = _extract_tags(source)
    assert "desktop" in tags


# ---------------------------------------------------------------------------
# _extract_mounts
# ---------------------------------------------------------------------------

def test_extract_mounts_from_mountview():
    """Business rule: resModel in mountView({resModel: '...'}) -> mounts list."""
    source = textwrap.dedent("""
        await mountView({
            type: "form",
            resModel: "sale.order",
        });
        await mountView({ type: "list", resModel: "account.move" });
    """)
    mounts = _extract_mounts(source)
    assert "sale.order" in mounts
    assert "account.move" in mounts


def test_extract_mounts_deduplicates():
    """Business rule: duplicate resModel values appear once in mounts."""
    source = textwrap.dedent("""
        await mountView({ resModel: "sale.order" });
        await mountView({ resModel: "sale.order" });
    """)
    mounts = _extract_mounts(source)
    assert mounts.count("sale.order") == 1


# ---------------------------------------------------------------------------
# _extract_mock_models (MED-1)
# ---------------------------------------------------------------------------

HOOT_MOCK_MODELS_SOURCE = textwrap.dedent("""
    import { defineModels, models, fields } from "@web/../tests/web_test_helpers";

    class Account extends models.Model {
        _name = "account.account";
        code = fields.Char();
    }

    class Partner extends models.Model {
        _name = "res.partner";
    }

    defineModels([Account, Partner]);
""")


def test_extract_mock_models_from_class_body():
    """Business rule: class X extends models.Model { _name='model.name' } -> mock_models."""
    mock_models = _extract_mock_models(HOOT_MOCK_MODELS_SOURCE)
    assert "account.account" in mock_models
    assert "res.partner" in mock_models


def test_mock_model_hoot_file_puts_in_mock_not_real_model():
    """MED-1 contract: a Hoot file defining mock account.account stores it in
    mock_models, NOT as a real model reference. This test validates the data field
    assignment — the writer test checks that NO COVERS_MODEL edge is emitted.
    """
    mock_models = _extract_mock_models(HOOT_MOCK_MODELS_SOURCE)
    # 'account.account' is a mock (hand-rolled), must be in mock_models
    assert "account.account" in mock_models
    # mounts is the channel for real model refs - verify no leak into mounts
    mounts = _extract_mounts(HOOT_MOCK_MODELS_SOURCE)
    assert "account.account" not in mounts


# ---------------------------------------------------------------------------
# _extract_tour_names
# ---------------------------------------------------------------------------

def test_extract_tour_names_from_registry_add():
    """Business rule: registry.category('web_tour.tours').add('name', ...) -> tour names."""
    names = _extract_tour_names(TOUR_SOURCE)
    assert "my_tour" in names


# ---------------------------------------------------------------------------
# parse_js_test_file - integration of all sub-extractors
# ---------------------------------------------------------------------------

def test_parse_hoot_file_returns_correct_framework(tmp_path):
    """Business rule: a *.test.js with @odoo/hoot import -> framework='hoot' in JsTestSuiteInfo."""
    f = tmp_path / "static" / "tests" / "sale.test.js"
    f.parent.mkdir(parents=True)
    f.write_text(HOOT_SOURCE, encoding="utf-8")

    suite = parse_js_test_file(str(f), module="sale", odoo_version="18.0")
    assert suite is not None
    assert suite.framework == "hoot"
    assert suite.module == "sale"
    assert suite.odoo_version == "18.0"


def test_parse_qunit_file_returns_correct_framework(tmp_path):
    """Business rule: a *_tests.js with QUnit usage -> framework='qunit'."""
    f = tmp_path / "static" / "tests" / "sale_tests.js"
    f.parent.mkdir(parents=True)
    f.write_text(QUNIT_SOURCE, encoding="utf-8")

    suite = parse_js_test_file(str(f), module="sale", odoo_version="17.0")
    assert suite is not None
    assert suite.framework == "qunit"


def test_parse_tour_file_returns_framework_tour(tmp_path):
    """Business rule: a .js file in static/tests/tours/ -> framework='tour'."""
    f = tmp_path / "static" / "tests" / "tours" / "my_tour.js"
    f.parent.mkdir(parents=True)
    f.write_text(TOUR_SOURCE, encoding="utf-8")

    suite = parse_js_test_file(str(f), module="sale", odoo_version="17.0")
    assert suite is not None
    assert suite.framework == "tour"


def test_parse_non_test_file_returns_none(tmp_path):
    """Business rule: a JS file outside static/tests/ -> parse_js_test_file returns None."""
    f = tmp_path / "static" / "src" / "sale_order.js"
    f.parent.mkdir(parents=True)
    f.write_text("console.log('hello');", encoding="utf-8")

    result = parse_js_test_file(str(f), module="sale", odoo_version="17.0")
    assert result is None


def test_parse_hoot_file_with_mock_model_populates_mock_models_not_mounts(tmp_path):
    """MED-1 contract: mock models from defineModels go to mock_models, not mounts.

    This is the critical contract test: if a Hoot test hand-rolls account.account
    with defineModels/extends models.Model, that must NOT flow into mounts (which
    signals real model coverage). The writer must NOT emit COVERS_MODEL from mock_models.
    """
    source = textwrap.dedent("""
        import { defineModels, models, fields, mountView } from "@web/../tests/web_test_helpers";
        import { test } from "@odoo/hoot";

        class Account extends models.Model {
            _name = "account.account";
            code = fields.Char();
        }

        defineModels([Account]);

        test("renders account", async () => {
            await mountView({ type: "list", resModel: "account.account" });
        });
    """)
    f = tmp_path / "static" / "tests" / "char_field.test.js"
    f.parent.mkdir(parents=True)
    f.write_text(source, encoding="utf-8")

    suite = parse_js_test_file(str(f), module="account", odoo_version="18.0")
    assert suite is not None
    assert "account.account" in suite.mock_models
    assert "account.account" in suite.mounts  # mountView ALSO captured
    # But framework is hoot
    assert suite.framework == "hoot"


def test_parse_module_js_tests_scans_static_tests_dir(tmp_path):
    """Business rule: parse_module_js_tests returns JsTestSuiteInfo for each test file found."""
    # Create a fake module with static/tests/ structure
    module_path = tmp_path / "sale"
    (module_path / "static" / "tests").mkdir(parents=True)
    (module_path / "static" / "tests" / "sale.test.js").write_text(HOOT_SOURCE, encoding="utf-8")
    (module_path / "static" / "tests" / "tours").mkdir(parents=True)
    (module_path / "static" / "tests" / "tours" / "tour.js").write_text(
        TOUR_SOURCE, encoding="utf-8"
    )

    info = ModuleInfo(
        name="sale",
        odoo_version="18.0",
        repo="odoo",
        path=str(module_path),
        depends=[],
    )
    suites = parse_module_js_tests(info)
    assert len(suites) == 2
    frameworks = {s.framework for s in suites}
    assert "hoot" in frameworks
    assert "tour" in frameworks


def test_parse_module_returns_empty_when_no_static_tests(tmp_path):
    """Business rule: modules without static/tests/ return empty list (no crash)."""
    module_path = tmp_path / "base"
    module_path.mkdir()

    info = ModuleInfo(
        name="base",
        odoo_version="18.0",
        repo="odoo",
        path=str(module_path),
        depends=[],
    )
    suites = parse_module_js_tests(info)
    assert suites == []
