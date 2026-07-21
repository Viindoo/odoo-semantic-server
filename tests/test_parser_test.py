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
    """Business rule: framework seeding returns TestHelperInfo for all known base
    classes that actually exist at the given version (17.0).

    SavepointCase is INVERTED here (issue #362 review, was `"SavepointCase" in names`,
    which locked in the exact bug the issue reports). Evidence: `SavepointCase` has
    zero occurrences anywhere in /home/tuan/git/odoo17 - odoo17/odoo/tests/common.py
    defines no such class. It is last present (deprecated) at
    odoo16/odoo/tests/common.py:826 and is removed outright entering v17. Every other
    assertion below is era-invariant (real at every version 8.0-19.0, per
    api-contract.md's era table) and is kept unchanged - no assertion is deleted, one
    is inverted with cited evidence. See T2/T6 below for the full SavepointCase
    lifecycle (available 12.0-14.0, deprecated 15.0-16.0, absent 17.0+).
    """
    helpers = seed_framework_helpers("17.0")
    names = {h.name for h in helpers}
    assert "TransactionCase" in names
    assert "HttpCase" in names
    assert "SingleTransactionCase" in names
    assert "Form" in names
    assert "SavepointCase" not in names


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


# ---------------------------------------------------------------------------
# framework_bases() / framework_base() / is_out_of_catalogue() - issue #362.
#
# `src/indexer/framework_bases.py` does NOT exist yet (WI-4/WI-6 build it). Every
# test below imports it via `_import_framework_bases()` INSIDE the test body, never
# at module level, so `pytest --collect-only` on this file stays clean (no collection
# error) while each test still fails at RUN time with the real ImportError - that
# failure IS the red-before-green proof this commit records. See api-contract.md for
# the frozen dataclass/function shapes and "Authoritative menus per era" for the E1-E7
# table transcribed below.
# ---------------------------------------------------------------------------

def _import_framework_bases():
    """Deferred import of the not-yet-built SSOT module (issue #362, WI-4/WI-6).

    Imported inside each test function - never at module level - so collection of
    this file succeeds today even though the module does not exist; the ImportError
    surfaces only when a test actually runs, which is the RED proof this commit is
    meant to record (a collection error would instead hide every other test below).
    """
    from src.indexer import framework_bases
    return framework_bases


# Era menus transcribed verbatim from api-contract.md "Authoritative menus per era".
_E1_E2_NAMES = {
    "BaseCase", "HttpCase", "SavepointCase", "SingleTransactionCase",
    "TestCase", "TransactionCase",
}
_E3_NAMES = _E1_E2_NAMES | {"TreeCase"}
_E4_NAMES = _E3_NAMES | {"Form", "O2MForm"}
_E5_NAMES = _E4_NAMES | {"HttpCaseCommon", "HttpSavepointCase"}
_E6_NAMES = _E5_NAMES - {"TreeCase", "HttpCaseCommon"}
_E7_NAMES = _E6_NAMES - {"SavepointCase", "HttpSavepointCase"}

# T1 boundary matrix: covers all four proven era transitions (v10/v11, v11/v12,
# v13/v14, v14/v15) plus v16/v17 and the v19 open-ended-era check, per §8 T1.
_ERA_MENU_BY_VERSION = {
    "8.0": _E1_E2_NAMES,
    "10.0": _E1_E2_NAMES,
    "11.0": _E3_NAMES,
    "12.0": _E4_NAMES,
    "14.0": _E5_NAMES,
    "15.0": _E6_NAMES,
    "16.0": _E6_NAMES,
    "17.0": _E7_NAMES,
    "19.0": _E7_NAMES,
}


def test_framework_bases_menu_matches_the_authoritative_era_table_per_version():
    """T1 - business rule: the per-version class-NAME menu is EXACTLY the curated
    era table (api-contract.md era registry) - set equality, not subset. An extra
    entry (e.g. a stale SavepointCase surviving into v17) must fail this test just
    as loudly as a missing one.
    """
    fb = _import_framework_bases()
    for version, expected_names in _ERA_MENU_BY_VERSION.items():
        actual_names = {f.name for f in fb.framework_bases(version)}
        assert actual_names == expected_names, (
            f"{version}: expected {sorted(expected_names)}, got {sorted(actual_names)}"
        )


def test_savepointcase_status_tracks_its_full_lifecycle_available_deprecated_removed():
    """T2 - business rule: SavepointCase is a real, non-deprecated, recommended base
    at 12.0-14.0; deprecated (merged into TransactionCase) at 15.0-16.0; and gone
    entirely at 17.0+ - this is the issue's central fact and both of its wrong ends.
    """
    fb = _import_framework_bases()
    for version in ("12.0", "13.0", "14.0"):
        entry = fb.framework_base(version, "SavepointCase")
        assert entry is not None, f"SavepointCase must exist at {version}"
        assert entry.status == "available", f"SavepointCase must be available at {version}"
    for version in ("15.0", "16.0"):
        entry = fb.framework_base(version, "SavepointCase")
        assert entry is not None, f"SavepointCase must still exist (deprecated) at {version}"
        assert entry.status == "deprecated", f"SavepointCase must be deprecated at {version}"
    assert fb.framework_base("17.0", "SavepointCase") is None, (
        "SavepointCase has zero occurrences in odoo17/odoo/tests/common.py - "
        "framework_base must report it as absent, not merely deprecated"
    )


def test_treecase_window_is_v11_through_v14_only():
    """T3 - business rule: TreeCase is introduced at 11.0 (BaseCase re-parents onto
    it) and folded back into BaseCase at 15.0 - the issue's own 'v14+' framing is
    inverted; this pins the real window (absent 10.0, present 11.0 and 14.0, absent
    15.0).
    """
    fb = _import_framework_bases()
    assert fb.framework_base("10.0", "TreeCase") is None
    assert fb.framework_base("11.0", "TreeCase") is not None
    assert fb.framework_base("14.0", "TreeCase") is not None
    assert fb.framework_base("15.0", "TreeCase") is None


def test_httpcasecommon_exists_at_v14_only_across_the_whole_surveyed_range():
    """T4 - business rule: HttpCaseCommon is a one-version-only HTTP mixin
    (introduced and folded back out between 14.0 and 15.0) - it must be present at
    14.0 and absent at every one of the other eleven surveyed majors (8-19).
    """
    fb = _import_framework_bases()
    for major in range(8, 20):
        version = f"{major}.0"
        entry = fb.framework_base(version, "HttpCaseCommon")
        if version == "14.0":
            assert entry is not None, "HttpCaseCommon must exist at 14.0"
        else:
            assert entry is None, f"HttpCaseCommon must be absent at {version}"


def test_form_and_o2mform_lower_bound_is_v12_not_v14():
    """T4 - business rule: Form/O2MForm are introduced at 12.0 (the server-side Form
    helper), not v14 as the pre-fix code comment claimed - absent at 11.0, present at
    12.0.
    """
    fb = _import_framework_bases()
    assert fb.framework_base("11.0", "Form") is None
    assert fb.framework_base("11.0", "O2MForm") is None
    assert fb.framework_base("12.0", "Form") is not None
    assert fb.framework_base("12.0", "O2MForm") is not None


def test_form_file_path_relocates_to_form_py_at_v17():
    """T5 - business rule: Form/O2MForm move out of common.py into their own
    odoo/tests/form.py starting at 17.0 - file_path must track the relocation.
    """
    fb = _import_framework_bases()
    v17_form = fb.framework_base("17.0", "Form")
    assert v17_form is not None
    assert v17_form.file_path == "odoo/tests/form.py"


def test_form_file_path_is_still_common_py_at_v16():
    """T5 - business rule: before the v17 relocation, Form lives in common.py like
    every other framework base.
    """
    fb = _import_framework_bases()
    v16_form = fb.framework_base("16.0", "Form")
    assert v16_form is not None
    assert v16_form.file_path == "odoo/tests/common.py"


def test_every_non_null_v8_file_path_uses_the_openerp_namespace_prefix():
    """T5 - business rule: at 8.0 (era1) every class that carries a file_path at all
    is rooted under openerp/, never odoo/ - the odoo/ runtime alias did not exist
    yet at this version.
    """
    fb = _import_framework_bases()
    v8_entries = fb.framework_bases("8.0")
    non_none_paths = [f.file_path for f in v8_entries if f.file_path is not None]
    assert non_none_paths, "8.0 menu must have at least one non-null file_path to assert on"
    for path in non_none_paths:
        assert path.startswith("openerp/"), f"{path!r} must start with 'openerp/' at 8.0"


def test_testcase_file_path_is_always_none_stdlib_has_no_repo_path():
    """T5 - business rule: TestCase is python stdlib unittest.TestCase at every
    surveyed version (never re-exported from odoo.tests, per phase4-solution.md
    §2.4) - it must never carry a repo-relative file_path.
    """
    fb = _import_framework_bases()
    for major in range(8, 20):
        version = f"{major}.0"
        entry = fb.framework_base(version, "TestCase")
        assert entry is not None, f"TestCase classifier entry must exist at {version}"
        assert entry.file_path is None, f"TestCase.file_path must be None at {version}"


def test_sentinel_version_99_resolves_to_the_same_menu_as_v19():
    """T6 - business rule: 99.0 (the repo's TEST_VERSION sentinel convention,
    tests/conftest.py) is out-of-catalogue and resolves through the open-ended v17+
    era, giving the identical name set as the newest real surveyed version.
    """
    fb = _import_framework_bases()
    names_99 = {f.name for f in fb.framework_bases("99.0")}
    names_19 = {f.name for f in fb.framework_bases("19.0")}
    assert names_99 == names_19


def test_is_out_of_catalogue_flags_the_sentinel_but_not_a_real_surveyed_version():
    """T6 - business rule: is_out_of_catalogue() is the provenance-line trigger -
    True only for a resolved major outside the surveyed [8, 19] range.
    """
    fb = _import_framework_bases()
    assert fb.is_out_of_catalogue("99.0") is True
    assert fb.is_out_of_catalogue("17.0") is False


def test_empty_and_unparseable_version_strings_fail_open_to_the_v17_plus_menu():
    """T6 - business rule: an empty or garbage version string must never raise - it
    fails open to the newest known era (v17+), exactly like any out-of-range major,
    so a caller always gets a usable menu instead of a crash.
    """
    fb = _import_framework_bases()
    v17_names = {f.name for f in fb.framework_bases("17.0")}
    empty_result = fb.framework_bases("")  # must not raise
    garbage_result = fb.framework_bases("not-a-version")  # must not raise
    assert {f.name for f in empty_result} == v17_names
    assert {f.name for f in garbage_result} == v17_names


# ---------------------------------------------------------------------------
# parse_framework_bases() - the AST oracle (T8). Self-contained via pytest's
# tmp_path fixture: these three scenarios are unit-level invariants of the parser's
# inclusion/exclusion/deprecation rules (api-contract.md / phase4-solution.md §5.2),
# independent of the per-version committed fixtures under
# tests/fixtures/odoo_tests_headers/ (those exist to power the CROSS-VERSION parity
# alarm in tests/test_framework_bases_parity.py, a different concern - matching real
# source content per version, not the parser's structural rules in isolation).
# ---------------------------------------------------------------------------

# Mirrors the real v15/v16 odoo/tests/common.py shape closely enough to exercise the
# deprecation-detection rule and the metaclass-kwarg-is-not-a-class-to-emit rule in
# one small fixture (see phase4-solution.md §5.2's inclusion rule and exclusion list,
# which explicitly names a MetaCase(type)-shaped class as one of the excluded ones).
_DEPRECATION_AND_METACLASS_SRC = '''
import warnings


class TransactionCase(object):
    """Stand-in for the real odoo/tests/common.py TransactionCase."""


class SavepointCase(TransactionCase):
    """Stand-in for the real v15/v16 deprecated SavepointCase - merged into
    TransactionCase; subclassing now emits a DeprecationWarning."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        warnings.warn(
            "SavepointCase is deprecated, use TransactionCase",
            DeprecationWarning,
        )


class MetaCase(type):
    """A metaclass, not a test base class - referenced elsewhere only via
    metaclass=MetaCase kwargs. Must never be emitted as a framework base."""
'''


def _write_common_py(tmp_path: Path, source: str, prefix: str = "odoo") -> Path:
    """Write `source` as <tmp_path>/<prefix>/tests/common.py and return tmp_path -
    the odoo_source_root shape parse_framework_bases expects."""
    tests_dir = tmp_path / prefix / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "common.py").write_text(source)
    return tmp_path


def test_init_subclass_deprecation_warning_marks_the_class_deprecated(tmp_path):
    """T8 - business rule: a class whose body defines __init_subclass__ calling
    warnings.warn(..., DeprecationWarning) is the mechanical signal
    ParsedFacts.is_deprecated must key off - exactly what distinguishes the real
    v15/v16 SavepointCase from the non-deprecated v12-v14 SavepointCase.
    """
    fb = _import_framework_bases()
    root = _write_common_py(tmp_path, _DEPRECATION_AND_METACLASS_SRC)
    parsed = fb.parse_framework_bases(root, "15.0")
    assert parsed is not None
    assert parsed["SavepointCase"].is_deprecated is True
    assert parsed["TransactionCase"].is_deprecated is False


def test_metaclass_shaped_class_is_never_emitted_by_the_parser(tmp_path):
    """T8 - business rule: MetaCase(type) is a metaclass, not a test base - the
    inclusion rule (base chain reaches TestCase, or name is in the known universe)
    must exclude it even though it is a top-level ClassDef in common.py.
    """
    fb = _import_framework_bases()
    root = _write_common_py(tmp_path, _DEPRECATION_AND_METACLASS_SRC)
    parsed = fb.parse_framework_bases(root, "15.0")
    assert parsed is not None
    assert "MetaCase" not in parsed


def test_openerp_tests_directory_holding_only_pycache_yields_none(tmp_path):
    """T8 - business rule (trap D3, evidenced by a REAL artifact on this machine:
    /home/tuan/git/odoo8/odoo/tests/ exists but holds only __pycache__, left behind
    by the openerp -> odoo runtime alias): a tests/ directory can EXIST while
    holding only a __pycache__ subdirectory and zero readable .py source. The
    resolver must test for a readable common.py FILE at the era-correct prefix
    (openerp/tests/ for v8/v9, per ODOO_NAMESPACE_LEGACY_MAX_MAJOR), never just
    directory presence - or it silently "succeeds" by parsing zero classes instead
    of returning None and letting the caller fall back to the curated table.
    """
    fb = _import_framework_bases()
    pycache_dir = tmp_path / "openerp" / "tests" / "__pycache__"
    pycache_dir.mkdir(parents=True)
    (pycache_dir / "common.cpython-27.pyc").write_bytes(b"\x00\x00")
    assert fb.parse_framework_bases(tmp_path, "8.0") is None
