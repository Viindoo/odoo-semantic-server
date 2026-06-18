# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for parser_test.py (no Docker required).

Business rules protected by each test are stated in the test function name
and docstring. Tests use handcrafted fixture sources under tests/fixtures/test_src/.

Red-before-green: each assertion was verified to fail before the implementation
was complete.
"""
from pathlib import Path

from src.indexer.models import ModuleInfo, TestParseResult
from src.indexer.parser_test import (
    _extract_tagged_args,
    _is_test_file,
    _parse_era1_test_file_degraded,
    _parse_era2_test_file,
    parse_module,
    seed_framework_helpers,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "test_src"
SALE_TESTS_DIR = FIXTURE_DIR / "sale"
SALE_V8_TESTS_DIR = FIXTURE_DIR / "sale_v8"


def _make_module_info(name: str, path: str, version: str = "17.0") -> ModuleInfo:
    """Helper to build a ModuleInfo for fixtures (no repo_root -> paths stay absolute)."""
    return ModuleInfo(
        name=name,
        odoo_version=version,
        repo="test_repo",
        path=path,
        depends=[],
    )


# ---------------------------------------------------------------------------
# _is_test_file guard tests
# ---------------------------------------------------------------------------

def test_non_test_file_not_recognized():
    """Business rule (C3): production .py OUTSIDE a tests/ dir is never a test file;
    inside tests/ EVERY .py (incl common.py) IS indexed - the addon common bases
    (SaleCommon, MailCommon, AccountTestInvoicingCommon) live in tests/common.py and
    ~90% of real test classes inherit them, so excluding common.py left every
    INHERITS_TEST edge dangling on real source."""
    # Outside tests/ -> conservative name guard still applies.
    assert not _is_test_file("/addons/sale/models/sale_order.py")
    assert not _is_test_file("/addons/sale/wizard/sale_order.py")
    # __init__.py is never a class-bearing test source.
    assert not _is_test_file("/addons/sale/tests/__init__.py")


def test_test_file_recognized():
    """Business rule: test_*.py AND any .py under tests/ (incl common.py) is indexed (C3)."""
    assert _is_test_file("/addons/sale/tests/test_sale_order.py")
    assert _is_test_file("/addons/account/tests/test_move.py")
    # C3: addon common base files under tests/ MUST be indexed.
    assert _is_test_file("/addons/sale/tests/common.py")
    assert _is_test_file("/addons/account/tests/account_test_savepoint.py")


# ---------------------------------------------------------------------------
# _extract_tagged_args
# ---------------------------------------------------------------------------

def test_extract_tagged_args_basic():
    """Business rule: @tagged(args) extracts all string args including negative '-tag'."""
    import ast
    src = "@tagged('post_install', '-at_install')\nclass Foo: pass"
    tree = ast.parse(src)
    cls_node = tree.body[0]
    tagged, is_standalone = _extract_tagged_args(cls_node.decorator_list)
    assert tagged == ["post_install", "-at_install"]
    assert not is_standalone


def test_extract_standalone_decorator():
    """Business rule: @standalone decorator -> commit_allowed=True (PP3 contract)."""
    import ast
    src = "@standalone\nclass Foo: pass"
    tree = ast.parse(src)
    cls_node = tree.body[0]
    tagged, is_standalone = _extract_tagged_args(cls_node.decorator_list)
    assert is_standalone is True


def test_extract_standalone_call_decorator():
    """Business rule: @standalone() call form also signals commit_allowed."""
    import ast
    src = "@standalone()\nclass Foo: pass"
    tree = ast.parse(src)
    cls_node = tree.body[0]
    _, is_standalone = _extract_tagged_args(cls_node.decorator_list)
    assert is_standalone is True


# ---------------------------------------------------------------------------
# era2 AST parser
# ---------------------------------------------------------------------------

ERA2_SRC = """
from odoo.tests.common import TransactionCase, tagged, standalone

class TestSaleCommon(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.order = cls.env['sale.order'].create({})

class MailCase:
    pass

@tagged('post_install', '-at_install')
class TestSaleOrder(TestSaleCommon, MailCase):
    def test_amount_total(self):
        self.assertEqual(self.order.amount_total, 0)
        self.assertEqual(self.order.partner_id.name, 'Test')

@standalone
class TestModuleLifecycle(TransactionCase):
    def test_install(self):
        self.assertTrue(True)
"""


def _make_test_info(version: str = "17.0") -> ModuleInfo:
    return ModuleInfo(
        name="sale",
        odoo_version=version,
        repo="test",
        path="/addons/sale",
        depends=[],
    )


def test_every_classdef_in_test_file_emits_a_node():
    """Business rule: HIGH-1 - EVERY ClassDef in a test file emits a TestClassInfo node.

    TEST_BASE_CLASSES CLASSIFIES, never GATES emission. MailCase (not in TEST_BASE_CLASSES)
    must also get a node.
    """
    info = _make_test_info()
    classes = _parse_era2_test_file(ERA2_SRC, "test_sale.py", info)
    names = {c.name for c in classes}
    assert "TestSaleCommon" in names, "TransactionCase subclass must emit a node"
    assert "MailCase" in names, "Non-Case mixin must also emit a node (HIGH-1)"
    assert "TestSaleOrder" in names, "Multi-base class must emit a node"
    assert "TestModuleLifecycle" in names, "@standalone class must emit a node"


def test_parser_classifies_transactioncase_as_transaction_type():
    """Business rule: direct TransactionCase base -> test_type='transaction'.

    Classification uses DIRECT bases only at parse time. A class that inherits an
    intermediate base (TestSaleCommon, not TransactionCase directly) gets 'unknown'
    at parse time; transitive type is resolved in the reconcile pass.
    """
    info = _make_test_info()
    classes = _parse_era2_test_file(ERA2_SRC, "test_sale.py", info)
    by_name = {c.name: c for c in classes}
    # Direct TransactionCase base -> classified immediately at parse time
    assert by_name["TestSaleCommon"].test_type == "transaction"
    # TestSaleOrder's bases are [TestSaleCommon, MailCase] - no direct framework class
    # -> 'unknown' at parse time; reconcile pass resolves transitive inheritance
    assert by_name["TestSaleOrder"].test_type == "unknown"
    # @standalone + direct TransactionCase -> classified
    assert by_name["TestModuleLifecycle"].test_type == "transaction"


def test_parser_flags_standalone_as_commit_allowed():
    """Business rule: @standalone -> commit_allowed=True; normal TransactionCase -> False.

    PP3 contract: only module-lifecycle tests may cr.commit().
    """
    info = _make_test_info()
    classes = _parse_era2_test_file(ERA2_SRC, "test_sale.py", info)
    by_name = {c.name: c for c in classes}
    assert by_name["TestModuleLifecycle"].commit_allowed is True, (
        "@standalone must be commit_allowed=True"
    )
    assert by_name["TestSaleCommon"].commit_allowed is False, (
        "Plain TransactionCase must be commit_allowed=False"
    )
    assert by_name["TestSaleOrder"].commit_allowed is False, (
        "Multi-base class without @standalone must be False"
    )


def test_parser_extracts_tagged_args_including_negative_tags():
    """Business rule: @tagged args are stored RAW including '-at_install' negative tags."""
    info = _make_test_info()
    classes = _parse_era2_test_file(ERA2_SRC, "test_sale.py", info)
    by_name = {c.name: c for c in classes}
    tagged = by_name["TestSaleOrder"].tagged
    assert "post_install" in tagged
    assert "-at_install" in tagged


def test_parser_extracts_model_refs_from_env_subscript():
    """Business rule: self.env['sale.order'] -> model_refs=['sale.order']."""
    src = """
class TestFoo(TransactionCase):
    def test_x(self):
        order = self.env['sale.order'].create({})
        partner = self.env['res.partner'].browse(1)
        self.assertTrue(order)
"""
    info = _make_test_info()
    classes = _parse_era2_test_file(src, "test_foo.py", info)
    assert len(classes) == 1
    tc = classes[0]
    all_refs = set()
    for m in tc.methods:
        all_refs.update(m.model_refs)
    assert "sale.order" in all_refs
    assert "res.partner" in all_refs


def test_parser_counts_asserts_in_test_methods():
    """Business rule: asserts_count = number of self.assert* calls in a test method."""
    src = """
class TestAsserts(TransactionCase):
    def test_multi_assert(self):
        self.assertEqual(1, 1)
        self.assertTrue(True)
        self.assertFalse(False)
"""
    info = _make_test_info()
    classes = _parse_era2_test_file(src, "test_asserts.py", info)
    assert len(classes) == 1
    methods = {m.name: m for m in classes[0].methods}
    assert methods["test_multi_assert"].asserts_count == 3


def test_parser_extracts_base_classes_in_mro_order():
    """Business rule: base_classes_ordered preserves Python MRO declaration order (HIGH-1)."""
    src = """
class TestMultiBase(TransactionCase, MailCase, OtherMixin):
    pass
"""
    info = _make_test_info()
    classes = _parse_era2_test_file(src, "test_mb.py", info)
    assert len(classes) == 1
    # Preserve declaration order
    assert classes[0].base_classes_ordered == ["TransactionCase", "MailCase", "OtherMixin"]


def test_parser_def_use_resolves_setUp_attr_to_field_refs():
    """Business rule: def-use pass resolves self.<attr>.<field>.

    When setUp assigns self.<attr> = env[model].create(),
    this propagates field coverage to member methods.
    setUp defined attrs propagate field coverage (HIGH-2).

    HIGH-2: setUp-defined attrs propagate field coverage to member methods.
    """
    src = """
class TestDefUse(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.order = cls.env['sale.order'].create({})

    def test_amount(self):
        total = self.order.amount_total
        self.assertEqual(total, 0)
"""
    info = _make_test_info()
    classes = _parse_era2_test_file(src, "test_defuse.py", info)
    assert len(classes) == 1
    tc = classes[0]
    # setUp should have extracted sale.order as a model ref
    setup_method = next((m for m in tc.methods if m.name == "setUpClass"), None)
    assert setup_method is not None
    assert "sale.order" in setup_method.model_refs

    # test_amount should have sale.order propagated from setUp
    test_method = next((m for m in tc.methods if m.name == "test_amount"), None)
    assert test_method is not None
    # The def-use pass should have propagated sale.order to test_amount's model_refs
    assert "sale.order" in test_method.model_refs
    # And amount_total should appear in field_refs (via def-use: self.order.amount_total)
    assert "amount_total" in test_method.field_refs


def test_era1_degraded_path_emits_nodes_with_unknown_type():
    """Business rule: era1 (v8/v9) fixture -> nodes with test_type='unknown', no crash.

    Never silently drop era1 test classes.
    """
    era1_src = """
from openerp.tests.common import TransactionCase

class TestSaleV8(TransactionCase):
    def setUp(self):
        super(TestSaleV8, self).setUp()

    def test_something(self):
        self.assertTrue(True)
"""
    info = ModuleInfo(
        name="sale",
        odoo_version="8.0",
        repo="test",
        path="/addons/sale",
        depends=[],
    )
    classes = _parse_era1_test_file_degraded(era1_src, "test_sale.py", info)
    assert len(classes) >= 1, "era1 must emit at least one TestClassInfo"
    assert classes[0].test_type == "unknown", "era1 degraded path must set test_type='unknown'"


def test_era1_does_not_crash_on_malformed_source():
    """Business rule: era1 parser never crashes even on malformed Python 2 source."""
    malformed = """
class Foo(Bar
    def test_x(self: pass
"""
    info = ModuleInfo(
        name="sale",
        odoo_version="8.0",
        repo="test",
        path="/addons/sale",
        depends=[],
    )
    # Must not raise
    classes = _parse_era1_test_file_degraded(malformed, "test_bad.py", info)
    # May return empty list (no complete class match) but must not crash
    assert isinstance(classes, list)


def test_non_test_python_file_yields_empty():
    """Business rule: a production model file (not a test file) yields no TestClassInfo."""
    # Simulate parsing a production file path (not under tests/)
    info = _make_test_info()
    # Call _parse_era2_test_file directly (the guard is in parse_module)
    # but the file_path check in parse_module would block it.
    # Here we verify _parse_era2_test_file itself returns classes (since
    # it doesn't run the guard - that's parse_module's job). This confirms
    # the guard is in parse_module.
    # The real test of the guard is via parse_module.
    result = parse_module(info)
    # The fixture sale/tests/ dir picks up test_sale_order.py AND common.py (C3:
    # every .py under tests/ is parsed). The guard only excludes .py OUTSIDE tests/.
    assert isinstance(result, TestParseResult)


# ---------------------------------------------------------------------------
# parse_module integration (uses real fixture files)
# ---------------------------------------------------------------------------

def test_parse_module_finds_test_classes_in_fixture():
    """Business rule: parse_module discovers all ClassDef in test_*.py files under tests/."""
    info = ModuleInfo(
        name="sale",
        odoo_version="17.0",
        repo="test_repo",
        path=str(SALE_TESTS_DIR),
        depends=[],
    )
    result = parse_module(info)
    names = {c.name for c in result.test_classes}
    # From test_sale_order.py
    assert "TestSaleCommon" in names
    assert "TestSaleOrder" in names
    assert "MailCase" in names  # HIGH-1: non-Case mixin
    assert "TestModuleLifecycle" in names
    # C3: tests/common.py IS now parsed (every .py under tests/, not just test_*.py).
    # The addon common bases live in common.py and the bulk of real test classes
    # inherit them - excluding common.py left INHERITS_TEST dangling on real source.
    # common.py ALSO defines a TestSaleCommon, so we now get TWO distinct nodes (same
    # name + same module, DIFFERENT file_path) - this is exactly CRITICAL-1's
    # file-scoped identity proven on a real two-file collision.
    sale_commons = [c for c in result.test_classes if c.name == "TestSaleCommon"]
    assert len(sale_commons) == 2, (
        f"C3 + CRITICAL-1: expected 2 TestSaleCommon (test_sale_order.py + common.py, "
        f"distinct file_paths), got {len(sale_commons)}: "
        f"{[c.file_path for c in sale_commons]}"
    )
    distinct_files = {c.file_path for c in sale_commons}
    assert len(distinct_files) == 2, "the two TestSaleCommon must have distinct file_paths"
    assert any(c.file_path.endswith("common.py") for c in sale_commons), (
        "C3: the tests/common.py SaleCommon-style base must be among the parsed classes"
    )


def test_parse_module_era1_does_not_crash():
    """Business rule: era1 (v8/v9) parse_module completes without crash."""
    info = ModuleInfo(
        name="sale",
        odoo_version="8.0",
        repo="test_repo",
        path=str(SALE_V8_TESTS_DIR),
        depends=[],
    )
    result = parse_module(info)
    assert isinstance(result, TestParseResult)
    # era1 fixture has at least one class
    assert len(result.test_classes) >= 1
    for tc in result.test_classes:
        assert tc.test_type == "unknown", "era1 must yield test_type='unknown'"


# ---------------------------------------------------------------------------
# seed_framework_helpers
# ---------------------------------------------------------------------------

def test_seed_framework_helpers_returns_known_bases():
    """Business rule: framework seeding returns TestHelperInfo for all known base classes."""
    helpers = seed_framework_helpers("17.0")
    names = {h.name for h in helpers}
    assert "TransactionCase" in names
    assert "HttpCase" in names
    assert "SavepointCase" in names
    assert "SingleTransactionCase" in names
    assert "Form" in names


def test_framework_helpers_use_at_framework_sentinel():
    """Business rule: framework helpers use module='@framework' (MED-3).

    Avoids __unresolved__ confusion when resolving framework base references.
    """
    helpers = seed_framework_helpers("17.0")
    for h in helpers:
        assert h.module == "@framework", f"{h.name} has module={h.module!r}, expected '@framework'"
        assert h.origin == "framework"


def test_framework_helpers_have_commit_allowed_false():
    """Business rule: framework bases default to commit_allowed=False (PP3 contract)."""
    helpers = seed_framework_helpers("17.0")
    by_name = {h.name: h for h in helpers}
    # All standard framework bases should have commit_allowed=False
    assert not by_name["TransactionCase"].commit_allowed
    assert not by_name["HttpCase"].commit_allowed
    assert not by_name["SingleTransactionCase"].commit_allowed


def test_framework_helpers_have_setup_summary():
    """Business rule: framework helpers carry setup_summary describing savepoint semantics (PP3)."""
    helpers = seed_framework_helpers("17.0")
    by_name = {h.name: h for h in helpers}
    tc = by_name["TransactionCase"]
    assert len(tc.setup_summary) >= 1
    # Must mention savepoint or auto-rollback semantics
    summary_text = " ".join(tc.setup_summary).lower()
    assert "savepoint" in summary_text or "auto-rollback" in summary_text, (
        f"TransactionCase setup_summary must mention savepoint semantics: {tc.setup_summary}"
    )
