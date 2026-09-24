# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for ``test_class_inspect`` subclass/method lists (issue #373).

Business rules protected (plan A3 table):
  R01  A class with ZERO real subclasses shows no placeholder row and no
       inflated count - in summary mode, hierarchy mode and through the
       ``odoo://{version}/test/{module}/{class}`` resource.
  R02  Exactly one child -> "Subclassed by: 1 test class" and one closed row
       (odoo-mcp-client#177 reported "2" where the truth was "0"/"1").
  R03  More children than the cap -> exactly cap rows + a closed
       "... and N more (use ...)" disclosure row; the count is the true total.
       Same disclosure for the test-method preview.
  R04  Children split across a promoted TestClass and its TestHelper twin
       (``finalize_is_helper`` projection) are each listed once; the direct
       base list names each base once (live TestSaleCommon@17.0 shape).
  R05  Children are ordered by (module, name) regardless of insertion order.
  R06  GUARD: SavepointCaseWithUserDemo (a TransactionCase subclass) is never
       listed under SavepointCase (exact edges, not substring matching).
  R07  The resource body equals the tool body and has no placeholder.
  R20  A scoped tenant sees only its own + shared subclasses and methods;
       another tenant's private classes are neither listed nor counted (H6).
  L4   A child is never listed under a same-named helper of ANOTHER module
       it neither depends on nor imports from (F11: INHERITS_TEST resolves
       a base through the child's import / module / dependency closure).
  FU   GUARD: the null-map pattern in ``orm_queries._ancestor_tagged_prologue``
       stays harmless (no phantom owner model, exact own-field set).

The graph is seeded through the real indexer writers (``write_results`` for
the Module + DEPENDS_ON edges, ``write_test_results``,
``write_framework_test_helpers``, ``reconcile_test_inherits``,
``finalize_is_helper``) so the TestClass/TestHelper twin topology is the one
production builds, not a hand-drawn stand-in. Class names, bases and module
names mirror real Odoo source (paths cited per test).

All data at TEST_VERSION='99.0'; clean_neo4j wipes it before and after.
"""

import asyncio
import importlib
import os
import re
from unittest.mock import patch

import pytest

from src.constants import LIST_PREVIEW_MAX_ITEMS
from src.indexer.models import (
    ModuleInfo,
    ParseResult,
    TestClassInfo,
    TestHelperInfo,
    TestMethodInfo,
    TestParseResult,
)
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

V = TEST_VERSION
SHARED = "t373_shared_ce"      # shared/global profile (CE base)
OWN = "t373_tenant_a"          # the calling tenant's own profile
FOREIGN = "t373_tenant_b"      # another tenant's private profile
PIPE = "│   "                  # ADR-0023 1.3 sub-list prefix (4 chars)


# ---------------------------------------------------------------------------
# Seeding through the real writers
# ---------------------------------------------------------------------------

@pytest.fixture
def writer(clean_neo4j):
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _fw(name: str, file_path: str = "odoo/tests/common.py", line: int = 1,
        test_type: str = "transaction") -> TestHelperInfo:
    return TestHelperInfo(
        name=name, module="@framework", odoo_version=V, origin="framework",
        test_type=test_type, file_path=file_path, line=line,
    )


def _cls(name: str, module: str, bases: list[str], *, file_path: str | None = None,
         tests: list[tuple[str, int]] | None = None, line: int = 10) -> TestClassInfo:
    """A TestClass; ``tests`` = [(method_name, line)]. No test_ method -> helper-able."""
    fp = file_path or f"addons/{module}/tests/test_{name.lower()}.py"
    methods = [
        TestMethodInfo(name=m, test_class=name, module=module, file_path=fp,
                       odoo_version=V, line=ln, asserts_count=1)
        for m, ln in (tests or [])
    ]
    return TestClassInfo(
        name=name, module=module, file_path=fp, odoo_version=V,
        test_type="transaction", base_classes_ordered=bases,
        defines_no_test_methods=not any(m.name.startswith("test_") for m in methods),
        line=line, methods=methods,
    )


def _write(writer, driver, module: str, classes: list[TestClassInfo], *,
           repo: str = "odoo", profiles: list[str] | None = None,
           depends: list[str] | None = None) -> None:
    """Write *module* the way an index run does: its Module node with the
    manifest ``depends`` as DEPENDS_ON edges (``write_results``), then its test
    classes. A test base in another module resolves only through the child's
    dependency closure or its import source (F11/L4), so every fixture that
    links across modules declares the real manifest dependency."""
    profiles = [SHARED] if profiles is None else profiles
    with driver.session() as s:
        s.run(
            "MERGE (m:Module {name: $n, odoo_version: $v}) "
            "SET m.profile = [x IN coalesce(m.profile, []) WHERE NOT x IN $p] + $p",
            n=module, v=V, p=profiles,
        )
    mod = ModuleInfo(name=module, odoo_version=V, repo=repo,
                     path=f"/{repo}/{module}", depends=list(depends or []))
    if depends:
        writer.write_results([ParseResult(module=mod)], profiles=profiles)
    writer.write_test_results([TestParseResult(module=mod, test_classes=classes)],
                              profiles=profiles)


def _index_pass(writer) -> None:
    """The version-wide post-passes every index run executes, in pipeline order."""
    writer.reconcile_test_inherits(V)
    writer.finalize_is_helper(V)


# ---------------------------------------------------------------------------
# Calling + parsing helpers
# ---------------------------------------------------------------------------

def _inspect(driver, name: str, method: str = "summary", **kw) -> str:
    from src.mcp.tools.test_tools import _test_class_inspect
    return _test_class_inspect(name=name, odoo_version=V, method=method,
                               _driver=driver, **kw)


# Sub-rows are matched by structure (pipe, spaces, connector) rather than the
# exact indent, so a content rule is judged on content; the 4-char indent itself
# (ADR-0023 1.3) is asserted separately in the R02 test.
_SUBROW = re.compile(r"^│ +(?=[├└]─ )")


def _block(out: str, header_prefix: str) -> tuple[str | None, list[str]]:
    """Return (header line, sub-rows without the PIPE prefix) of one section."""
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(header_prefix):
            rows = []
            for nxt in lines[i + 1:]:
                m = _SUBROW.match(nxt)
                if not m:
                    break
                rows.append(nxt[m.end():])
            return ln, rows
    return None, []


def _subclass_block(out: str):
    return _block(out, "├─ Subclassed by:")


def _row_names(rows: list[str]) -> list[str]:
    """'├─ [mod] Name' -> '[mod] Name' (drops the connector)."""
    return [r[3:] for r in rows]


def _assert_no_placeholder(out: str) -> None:
    assert "[?]" not in out, f"placeholder '[?]' rendered:\n{out}"
    assert not re.search(r"(^|\s)\?(\s|$)", out), f"bare '?' placeholder rendered:\n{out}"


def _assert_closed(rows: list[str], out: str) -> None:
    assert rows, f"expected a non-empty sub-list:\n{out}"
    assert all(r.startswith("├─ ") for r in rows[:-1]), f"inner rows must use ├─:\n{out}"
    assert rows[-1].startswith("└─ "), f"last row must close the list with └─:\n{out}"


def _run_builder(driver, name: str, **kw) -> list[dict]:
    """Run the raw builder as an admin (own=None) and return rows as dicts."""
    from src.mcp.test_query import build_test_class_inspect_query
    cypher, params = build_test_class_inspect_query(name, V, **kw)
    params.update({"own": None, "shared": []})
    with driver.session() as s:
        return s.run(cypher, **params).data()


# ---------------------------------------------------------------------------
# Worlds mirroring real source
# ---------------------------------------------------------------------------

def _seed_v15_world(writer, driver) -> None:
    """Odoo 15.0 shape (odoo/tests/common.py, odoo/tests/form.py-era names).

    - SavepointCase exists but nothing subclasses it (04 section 6: true count 0).
    - Form / O2MForm are framework classes with no TestClass child.
    - website_sale/tests/test_website_sale_cart.py:
      ``class WebsiteSaleCart(TransactionCase)`` - an addon LEAF class.
    """
    writer.write_framework_test_helpers([
        _fw("TransactionCase", line=600),
        _fw("SavepointCase", line=869, test_type="savepoint"),
        _fw("Form", line=2000, test_type="form"),
        _fw("O2MForm", line=2491, test_type="form"),
    ], profiles=[SHARED])
    _write(writer, driver, "website_sale", [
        _cls("WebsiteSaleCart", "website_sale", ["TransactionCase"],
             file_path="addons/website_sale/tests/test_website_sale_cart.py",
             tests=[("test_add_cart_deleted_product", 18)]),
    ])
    _index_pass(writer)


def _seed_v14_world(writer, driver) -> None:
    """Odoo 14.0 shape: HttpCaseCommon exists (v14 only) with no child;
    ``class WebsiteSaleCart(SavepointCase)`` with three test_ methods."""
    writer.write_framework_test_helpers([
        _fw("TransactionCase", line=600),
        _fw("SavepointCase", line=700, test_type="savepoint"),
        _fw("HttpCaseCommon", line=1385, test_type="http"),
    ], profiles=[SHARED])
    _write(writer, driver, "website_sale", [
        _cls("WebsiteSaleCart", "website_sale", ["SavepointCase"],
             file_path="addons/website_sale/tests/test_website_sale_cart.py",
             tests=[("test_add_cart_deleted_product", 18),
                    ("test_add_cart_unpublished_product", 32),
                    ("test_update_pricelist_with_invalid_product", 51)]),
    ])
    _index_pass(writer)


_R01_CASES = [
    pytest.param(_seed_v15_world, "SavepointCase", "@framework", id="SavepointCase@15"),
    pytest.param(_seed_v15_world, "Form", "@framework", id="Form"),
    pytest.param(_seed_v15_world, "O2MForm", "@framework", id="O2MForm"),
    pytest.param(_seed_v15_world, "WebsiteSaleCart", "website_sale", id="WebsiteSaleCart@15"),
    pytest.param(_seed_v14_world, "HttpCaseCommon", "@framework", id="HttpCaseCommon@14"),
    pytest.param(_seed_v14_world, "WebsiteSaleCart", "website_sale", id="WebsiteSaleCart@14"),
]


# ---------------------------------------------------------------------------
# R01 - zero subclasses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("seed", "cls_name", "_module"), _R01_CASES)
def test_class_without_subclasses_summary_omits_subclass_section(
    writer, clean_neo4j, seed, cls_name, _module,
):
    """R01 (FIX, #373): a class nobody subclasses must not claim subclasses.

    Pre-fix the empty OPTIONAL MATCH hops each produced a {name:null} map, so
    every leaf rendered "Subclassed by: 2 test classes" + two "[?] ?" rows.
    """
    seed(writer, clean_neo4j)
    out = _inspect(clean_neo4j, cls_name)

    assert out.startswith(f"{cls_name} (Odoo {V})"), f"class must resolve:\n{out}"
    assert "Subclassed by" not in out, (
        f"summary must omit the section when there are no subclasses:\n{out}"
    )
    _assert_no_placeholder(out)


@pytest.mark.parametrize(("seed", "cls_name", "_module"), _R01_CASES)
def test_class_without_subclasses_hierarchy_states_zero_and_none(
    writer, clean_neo4j, seed, cls_name, _module,
):
    """R01 (FIX, #373 repro used method='hierarchy'): the count is 0 and the
    list says "(none)" - not "2 test classes" + "[?] ?" rows."""
    seed(writer, clean_neo4j)
    out = _inspect(clean_neo4j, cls_name, method="hierarchy")

    header, rows = _subclass_block(out)
    assert header == "├─ Subclassed by: 0 test classes", out
    assert rows == ["└─ (none)"], out
    _assert_no_placeholder(out)


def test_zero_subclass_negative_is_not_vacuous_real_children_do_render(writer, clean_neo4j):
    """Positive control for R01: in the same worlds, the base the leaf really
    inherits DOES list it - so "no subclasses" above is a real answer, not a
    broken seed."""
    _seed_v14_world(writer, clean_neo4j)
    out = _inspect(clean_neo4j, "SavepointCase", method="hierarchy")
    header, rows = _subclass_block(out)
    assert header == "├─ Subclassed by: 1 test class", out
    assert rows == ["└─ [website_sale] WebsiteSaleCart"], out


def test_builder_returns_empty_lists_not_null_maps_for_a_leaf(writer, clean_neo4j):
    """R01 (FIX) at the query contract: a leaf yields subclassed_by == [],
    subclassed_total == 0 and a methods list without null entries; a class with
    no methods at all yields methods == []."""
    _seed_v15_world(writer, clean_neo4j)

    leaf = _run_builder(clean_neo4j, "WebsiteSaleCart")
    assert len(leaf) == 1, leaf
    assert leaf[0]["subclassed_by"] == [], leaf[0]["subclassed_by"]
    assert leaf[0].get("subclassed_total") == 0, leaf[0]
    assert [m["name"] for m in leaf[0]["methods"]] == ["test_add_cart_deleted_product"]

    fw = _run_builder(clean_neo4j, "Form")
    assert len(fw) == 1, fw
    assert fw[0]["subclassed_by"] == [], fw[0]["subclassed_by"]
    assert fw[0].get("subclassed_total") == 0, fw[0]
    assert fw[0]["methods"] == [], fw[0]["methods"]


# ---------------------------------------------------------------------------
# R07 - the resource serves the same honest body
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("seed", "cls_name", "module"), _R01_CASES)
def test_test_class_resource_body_equals_tool_body_without_placeholder(
    writer, clean_neo4j, monkeypatch, seed, cls_name, module,
):
    """R07 (FIX): ``odoo://99.0/test/<module>/<class>`` renders exactly the tool
    body (summary), so the resource carries neither the phantom rows nor a
    different count."""
    from src.mcp import server as srv
    from src.mcp.resources import _render_test_class

    seed(writer, clean_neo4j)
    monkeypatch.setattr(srv, "_driver", clean_neo4j)

    body, _mime = _render_test_class(V, module, cls_name)
    assert body == _inspect(clean_neo4j, cls_name, module=module), body
    assert "Subclassed by" not in body, body
    _assert_no_placeholder(body)


def _read_resource(mcp, uri: str) -> str:
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(mcp.read_resource(uri))
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())
    contents = result.contents if hasattr(result, "contents") else result
    first = contents[0] if isinstance(contents, list | tuple) else contents
    return first.content if hasattr(first, "content") else str(first)


def test_test_class_resource_uri_route_serves_no_phantom_rows(
    writer, clean_neo4j, monkeypatch,
):
    """R07 (FIX) end to end through the registered FastMCP resource template:
    the leaf WebsiteSaleCart (the #373 addon repro) reads back without "[?]"."""
    from fastmcp import FastMCP

    from src.mcp import server as srv

    _seed_v14_world(writer, clean_neo4j)
    monkeypatch.setattr(srv, "_driver", clean_neo4j)
    import src.mcp.resources as resources_mod
    importlib.reload(resources_mod)  # empty resource cache
    mcp = FastMCP("t373-resources")
    resources_mod.register_resources(mcp)

    body = _read_resource(mcp, f"odoo://{V}/test/website_sale/WebsiteSaleCart")
    assert body.startswith(f"WebsiteSaleCart (Odoo {V})"), body
    assert "Subclassed by" not in body, body
    _assert_no_placeholder(body)


# ---------------------------------------------------------------------------
# R02 - exactly one child
# ---------------------------------------------------------------------------

def test_single_subclass_counts_one_and_closes_the_list(writer, clean_neo4j):
    """R02 (FIX, odoo-mcp-client#177 "2 vs 0" shape): one real child must read
    "1 test class" with one closed row - pre-fix the phantom made it 2.

    Shape: sale/tests/common.py (17.0) ``class TestSaleCommonBase(TransactionCase)``
    with exactly one subclass in the seeded module.
    """
    writer.write_framework_test_helpers([_fw("TransactionCase")], profiles=[SHARED])
    _write(writer, clean_neo4j, "sale", [
        _cls("TestSaleCommonBase", "sale", ["TransactionCase"],
             file_path="addons/sale/tests/common.py", line=50),
        _cls("TestSaleOrderLine", "sale", ["TestSaleCommonBase"],
             tests=[("test_compute_price_subtotal", 20)]),
    ])
    _index_pass(writer)

    for mode in ("summary", "hierarchy"):
        out = _inspect(clean_neo4j, "TestSaleCommonBase", method=mode)
        header, rows = _subclass_block(out)
        assert header == "├─ Subclassed by: 1 test class", f"[{mode}]\n{out}"
        assert rows == ["└─ [sale] TestSaleOrderLine"], f"[{mode}]\n{out}"
        assert f"{PIPE}└─ [sale] TestSaleOrderLine" in out.splitlines(), (
            f"[{mode}] sub-rows use the 4-char ADR-0023 1.3 prefix:\n{out}"
        )
        _assert_no_placeholder(out)


# ---------------------------------------------------------------------------
# R03 - more children than the cap
# ---------------------------------------------------------------------------

def _seed_common_with_children(writer, driver, n: int, base: str = "AccountTestInvoicingCommon"):
    """``account/tests/common.py`` helper subclassed by ``n`` test classes."""
    writer.write_framework_test_helpers([_fw("TransactionCase")], profiles=[SHARED])
    _write(writer, driver, "account", [
        _cls(base, "account", ["TransactionCase"],
             file_path="addons/account/tests/common.py", line=20),
    ] + [
        _cls(f"TestAccountCase{i:02d}", "account", [base],
             tests=[(f"test_case_{i:02d}", 10)])
        for i in range(n)
    ])
    _index_pass(writer)


def test_summary_caps_subclasses_at_six_with_closed_more_disclosure(writer, clean_neo4j):
    """R03 (FIX, ADR-0023 section 3): 8 children in summary -> 6 rows then a
    closing "... and 2 more (use test_class_inspect(... method='hierarchy') ...)"
    row; the count line states the true total (8)."""
    _seed_common_with_children(writer, clean_neo4j, 8)
    out = _inspect(clean_neo4j, "AccountTestInvoicingCommon")

    header, rows = _subclass_block(out)
    assert header == "├─ Subclassed by: 8 test classes", out
    assert len(rows) == 7, out
    _assert_closed(rows, out)
    assert _row_names(rows[:6]) == [f"[account] TestAccountCase{i:02d}" for i in range(6)], out
    assert rows[-1].startswith("└─ ... and 2 more (use test_class_inspect("), out
    assert "name='AccountTestInvoicingCommon'" in rows[-1], out
    assert "method='hierarchy'" in rows[-1], out


def test_hierarchy_lists_all_children_under_the_cap_without_disclosure(writer, clean_neo4j):
    """R03 (FIX): hierarchy mode shows all 8, closed, and no "... and" row."""
    _seed_common_with_children(writer, clean_neo4j, 8)
    out = _inspect(clean_neo4j, "AccountTestInvoicingCommon", method="hierarchy")

    header, rows = _subclass_block(out)
    assert header == "├─ Subclassed by: 8 test classes", out
    assert _row_names(rows) == [f"[account] TestAccountCase{i:02d}" for i in range(8)], out
    _assert_closed(rows, out)
    assert "... and" not in out, out


def test_hierarchy_over_list_cap_discloses_remainder_with_followup(writer, clean_neo4j):
    """R03 (FIX, TransactionCase@17.0 has 1795 live subclasses): past
    LIST_PREVIEW_MAX_ITEMS the hierarchy shows exactly the cap, then a closing
    "... and N more (use find_test_examples(...))" row; the count is the total."""
    n = LIST_PREVIEW_MAX_ITEMS + 3
    _seed_common_with_children(writer, clean_neo4j, n)
    out = _inspect(clean_neo4j, "AccountTestInvoicingCommon", method="hierarchy")

    header, rows = _subclass_block(out)
    assert header == f"├─ Subclassed by: {n} test classes", out
    assert len(rows) == LIST_PREVIEW_MAX_ITEMS + 1, out
    _assert_closed(rows, out)
    assert rows[-1].startswith("└─ ... and 3 more (use find_test_examples("), out
    assert "query='AccountTestInvoicingCommon'" in rows[-1], out


def test_builder_total_is_the_true_count_of_distinct_subclasses(writer, clean_neo4j):
    """R03 (FIX) at the query contract: with no cap the builder returns every
    child once and subclassed_total equals the distinct child count."""
    n = LIST_PREVIEW_MAX_ITEMS + 3
    _seed_common_with_children(writer, clean_neo4j, n)
    rows = _run_builder(clean_neo4j, "AccountTestInvoicingCommon")
    assert len(rows) == 1, rows
    names = [c["name"] for c in rows[0]["subclassed_by"]]
    assert None not in names, f"null-map phantom child returned: {rows[0]['subclassed_by']}"
    assert sorted(names) ==[f"TestAccountCase{i:02d}" for i in range(n)], names
    assert rows[0].get("subclassed_total") == n, rows[0].get("subclassed_total")


def _seed_class_with_methods(writer, driver, n_tests: int):
    tests = [(f"test_step_{i:02d}", 100 + 10 * i) for i in range(n_tests)]
    writer.write_framework_test_helpers([_fw("TransactionCase")], profiles=[SHARED])
    _write(writer, driver, "sale", [
        _cls("TestSaleOrder", "sale", ["TransactionCase"],
             file_path="addons/sale/tests/test_sale_order.py", tests=tests),
    ])
    _index_pass(writer)
    return tests


def test_summary_caps_test_methods_at_eight_with_closed_more_disclosure(writer, clean_neo4j):
    """R03 (FIX, 373-b): 11 test methods in summary -> 8 rows (source order)
    then a closing "... and 3 more (use ... method='methods' ...)" row."""
    tests = _seed_class_with_methods(writer, clean_neo4j, 11)
    out = _inspect(clean_neo4j, "TestSaleOrder")

    header, rows = _block(out, "├─ Test methods:")
    assert header == "├─ Test methods: 11", out
    assert len(rows) == 9, out
    _assert_closed(rows, out)
    for row, (name, line) in zip(rows[:8], tests[:8], strict=True):
        assert re.fullmatch(rf"├─ {name} \(asserts:1\) :{line}", row), (row, out)
    assert rows[-1].startswith("└─ ... and 3 more (use test_class_inspect("), out
    assert "method='methods'" in rows[-1], out


def test_methods_mode_lists_every_test_method_without_disclosure(writer, clean_neo4j):
    """R03 (FIX): method='methods' enumerates the whole class body, closed."""
    tests = _seed_class_with_methods(writer, clean_neo4j, 11)
    out = _inspect(clean_neo4j, "TestSaleOrder", method="methods")

    header, rows = _block(out, "├─ Test methods:")
    assert header == "├─ Test methods: 11", out
    assert [r[3:].split(" ")[0] for r in rows] == [n for n, _ in tests], out
    _assert_closed(rows, out)
    assert "... and" not in out, out


def test_methods_mode_on_a_class_without_tests_says_none(writer, clean_neo4j):
    """R01/R03 (FIX): a helper with no test_ methods reads "Test methods: 0" and
    "(none)" in methods mode - no placeholder row."""
    _seed_common_with_children(writer, clean_neo4j, 1)
    out = _inspect(clean_neo4j, "AccountTestInvoicingCommon", method="methods")
    header, rows = _block(out, "├─ Test methods:")
    assert header == "├─ Test methods: 0", out
    assert rows == ["└─ (none)"], out
    _assert_no_placeholder(out)


# ---------------------------------------------------------------------------
# R04 - TestClass / TestHelper twin (finalize_is_helper projection)
# ---------------------------------------------------------------------------

def _seed_test_sale_common_17(writer, driver, *, second_run: bool) -> None:
    """Odoo 17.0 sale/tests/common.py:
        class TestSaleCommonBase(TransactionCase)                        (line 50)
        class TestSaleCommon(AccountTestInvoicingCommon, TestSaleCommonBase)  (line 237)
    account/tests/common.py: class AccountTestInvoicingCommon(TransactionCase)

    sale reaches account through its manifest closure (17.0: sale ->
    account_payment -> account, shortened here to sale -> account) and
    sale_stock depends on sale (17.0 manifest: ['sale', 'stock_account']).

    Run 1 (full index): edges resolve to the TestClass nodes, then
    finalize_is_helper promotes the helpers, MERGEs their TestHelper twins and
    mirrors every child's edge onto the twin in the same run (F46).
    Run 2 (incremental, adds sale_stock): the new child gets the same pair of
    edges. Graphs written before F46 held the split (a child on the twin
    only); the reader still handles that shape - see
    test_child_whose_legacy_edge_reaches_only_the_twin_is_listed_once.
    """
    writer.write_framework_test_helpers([_fw("TransactionCase")], profiles=[SHARED])
    _write(writer, driver, "account", [
        _cls("AccountTestInvoicingCommon", "account", ["TransactionCase"],
             file_path="addons/account/tests/common.py", line=20),
    ])
    _write(writer, driver, "sale", [
        _cls("TestSaleCommonBase", "sale", ["TransactionCase"],
             file_path="addons/sale/tests/common.py", line=50),
        _cls("TestSaleCommon", "sale", ["AccountTestInvoicingCommon", "TestSaleCommonBase"],
             file_path="addons/sale/tests/common.py", line=237),
        _cls("TestSaleOrder", "sale", ["TestSaleCommon"],
             tests=[("test_sale_order", 30)]),
    ], depends=["account"])
    _index_pass(writer)
    if second_run:
        _write(writer, driver, "sale_stock", [
            _cls("TestSaleStock", "sale_stock", ["TestSaleCommon"],
                 tests=[("test_00_sale_stock_invoice", 40)]),
        ], depends=["sale"])
        _index_pass(writer)


def test_twin_topology_is_the_one_under_test(writer, clean_neo4j):
    """Positive control for R04, restated for F46: every child of the promoted
    TestSaleCommon reaches BOTH twin nodes - the TestClass and its TestHelper
    projection - whichever run added it, so the reader's twin union is really
    exercised (a child reachable only through one node would make R04 vacuous).

    Rewritten: this test used to pin the pre-F46 run-dependent split
    (TestSaleStock on the twin ONLY), i.e. the defect itself; rule F46 is that
    one run leaves the edges complete."""
    _seed_test_sale_common_17(writer, clean_neo4j, second_run=True)
    with clean_neo4j.session() as s:
        rows = s.run(
            """
            MATCH (c:TestClass {odoo_version: $v})-[:INHERITS_TEST]->(p {name: 'TestSaleCommon',
                                                                       odoo_version: $v})
            RETURN c.name AS child, collect(DISTINCT labels(p)[0]) AS targets
            """, v=V,
        ).data()
    targets = {r["child"]: sorted(r["targets"]) for r in rows}
    assert targets == {
        "TestSaleOrder": ["TestClass", "TestHelper"],
        "TestSaleStock": ["TestClass", "TestHelper"],
    }, targets


def test_children_on_the_helper_twin_are_listed_once(writer, clean_neo4j):
    """R04 (FIX, 373-d): a child whose edge targets only the TestHelper twin is
    listed, a child with edges to both is listed once, the total is distinct."""
    _seed_test_sale_common_17(writer, clean_neo4j, second_run=True)
    for mode in ("summary", "hierarchy"):
        out = _inspect(clean_neo4j, "TestSaleCommon", method=mode)
        header, rows = _subclass_block(out)
        assert header == "├─ Subclassed by: 2 test classes", f"[{mode}]\n{out}"
        assert _row_names(rows) == ["[sale] TestSaleOrder", "[sale_stock] TestSaleStock"], (
            f"[{mode}]\n{out}"
        )
        _assert_closed(rows, out)


def test_child_whose_legacy_edge_reaches_only_the_twin_is_listed_once(writer, clean_neo4j):
    """R04 (reader side): a graph written before F46 can hold a child whose only
    edge goes to the TestHelper twin (the live TestSaleCommon@17.0 state that
    motivated R04). The writers no longer produce that shape, so it is recreated
    by removing the child's TestClass edge; the reader must still list both
    children once each."""
    # GUARD: pre-existing behaviour
    _seed_test_sale_common_17(writer, clean_neo4j, second_run=True)
    with clean_neo4j.session() as s:
        gone = s.run(
            """
            MATCH (:TestClass {name: 'TestSaleStock', odoo_version: $v})
                  -[r:INHERITS_TEST]->(:TestClass {name: 'TestSaleCommon', odoo_version: $v})
            DELETE r RETURN count(r) AS n
            """, v=V,
        ).single()["n"]
    assert gone == 1, "positive control: the TestClass edge existed before the split"
    out = _inspect(clean_neo4j, "TestSaleCommon", method="hierarchy")
    header, rows = _subclass_block(out)
    assert header == "├─ Subclassed by: 2 test classes", out
    assert _row_names(rows) == ["[sale] TestSaleOrder", "[sale_stock] TestSaleStock"], out


def test_children_right_after_promotion_resolve_through_the_testclass(writer, clean_neo4j):
    """R04 (FIX): right after the first full index (twin just created, the
    child's edge mirrored onto it in the same run) the child is listed once."""
    _seed_test_sale_common_17(writer, clean_neo4j, second_run=False)
    out = _inspect(clean_neo4j, "TestSaleCommon", method="hierarchy")
    header, rows = _subclass_block(out)
    assert header == "├─ Subclassed by: 1 test class", out
    assert rows == ["└─ [sale] TestSaleOrder"], out


def test_direct_bases_listed_once_each_in_declaration_order(writer, clean_neo4j):
    """R04 (FIX, live TestSaleCommon@17.0 printed each base twice joined by
    ' -> '): "Inherits (direct):" names each base once, in the class
    statement's order (AccountTestInvoicingCommon, TestSaleCommonBase)."""
    _seed_test_sale_common_17(writer, clean_neo4j, second_run=True)
    for mode in ("summary", "hierarchy"):
        out = _inspect(clean_neo4j, "TestSaleCommon", method=mode)
        assert "├─ Inherits (direct): AccountTestInvoicingCommon, TestSaleCommonBase" in (
            out.splitlines()
        ), f"[{mode}]\n{out}"
        assert " -> " not in out, f"[{mode}] direct bases are not an ancestor chain:\n{out}"


def test_builder_all_bases_deduped_and_ordered(writer, clean_neo4j):
    """R04 (FIX) at the query contract: all_bases has each direct base once,
    ordered by position in base_classes_ordered."""
    _seed_test_sale_common_17(writer, clean_neo4j, second_run=True)
    rows = _run_builder(clean_neo4j, "TestSaleCommon")
    assert len(rows) == 1, rows
    assert rows[0]["all_bases"] == ["AccountTestInvoicingCommon", "TestSaleCommonBase"], rows[0]
    assert rows[0].get("subclassed_total") == 2, rows[0]


# ---------------------------------------------------------------------------
# R05 - deterministic order
# ---------------------------------------------------------------------------

def test_subclasses_are_ordered_by_module_then_name_not_insertion(writer, clean_neo4j):
    """R05 (FIX, 373-c): children written in REVERSE (module, name) order still
    render sorted by module, then name - the 6-row preview is not arbitrary."""
    writer.write_framework_test_helpers([_fw("TransactionCase")], profiles=[SHARED])
    _write(writer, clean_neo4j, "account", [
        _cls("AccountTestInvoicingCommon", "account", ["TransactionCase"],
             file_path="addons/account/tests/common.py"),
    ])
    expected = [
        ("account", "TestAccountMove"), ("account", "TestAccountPayment"),
        ("purchase", "TestPurchaseToInvoice"), ("sale", "TestSaleToInvoice"),
        ("stock_account", "TestStockValuation"), ("stock_account", "TestStockValuationLayer"),
        ("website_sale", "TestWebsiteSaleInvoice"),
    ]
    # Every child module's manifest closure reaches account (purchase and
    # stock_account list it directly; sale / website_sale through
    # account_payment / sale), declared here as a direct dependency.
    for module, name in reversed(expected):
        _write(writer, clean_neo4j, module, [
            _cls(name, module, ["AccountTestInvoicingCommon"], tests=[("test_it", 5)]),
        ], depends=[] if module == "account" else ["account"])
    _index_pass(writer)

    want = [f"[{m}] {n}" for m, n in expected]
    out = _inspect(clean_neo4j, "AccountTestInvoicingCommon", method="hierarchy")
    _header, rows = _subclass_block(out)
    assert _row_names(rows) == want, out

    out = _inspect(clean_neo4j, "AccountTestInvoicingCommon")
    _header, rows = _subclass_block(out)
    assert _row_names(rows[:6]) == want[:6], out

    b = _run_builder(clean_neo4j, "AccountTestInvoicingCommon")
    assert [(c["module"], c["name"]) for c in b[0]["subclassed_by"]] == expected, b


# ---------------------------------------------------------------------------
# R06 - exact edges, no substring false friends
# ---------------------------------------------------------------------------

def test_savepointcase_with_user_demo_is_not_a_savepointcase_subclass(writer, clean_neo4j):
    """R06: at 15.0 ``class SavepointCaseWithUserDemo(TransactionCase)``
    (odoo/addons/base/tests/common.py:61) and its subclasses (TestAPI,
    TestExpression, calendar TestCalendar) must NOT appear under SavepointCase,
    whose true subclass count is 0 (04 section 6, odoo-mcp-client#177)."""
    # GUARD: pre-existing behaviour
    writer.write_framework_test_helpers([
        _fw("TransactionCase"), _fw("SavepointCase", line=869, test_type="savepoint"),
    ], profiles=[SHARED])
    _write(writer, clean_neo4j, "base", [
        _cls("SavepointCaseWithUserDemo", "base", ["TransactionCase"],
             file_path="odoo/addons/base/tests/common.py", line=61),
        _cls("TestAPI", "base", ["SavepointCaseWithUserDemo"],
             file_path="odoo/addons/base/tests/test_api.py", tests=[("test_00_query", 12)]),
        _cls("TestExpression", "base", ["SavepointCaseWithUserDemo"],
             file_path="odoo/addons/base/tests/test_expression.py",
             tests=[("test_00_in_not_in_m2m", 20)]),
    ])
    _write(writer, clean_neo4j, "calendar", [
        _cls("TestCalendar", "calendar", ["SavepointCaseWithUserDemo"],
             tests=[("test_event_order", 30)]),
    ])
    _index_pass(writer)

    out = _inspect(clean_neo4j, "SavepointCase", method="hierarchy")
    _header, rows = _subclass_block(out)
    listed = " ".join(rows)
    for false_friend in ("SavepointCaseWithUserDemo", "TestAPI", "TestExpression", "TestCalendar"):
        assert false_friend not in listed, f"{false_friend} is not a SavepointCase child:\n{out}"

    # Positive control: the false friend IS visible where it really belongs.
    tc_out = _inspect(clean_neo4j, "TransactionCase", method="hierarchy")
    assert "[base] SavepointCaseWithUserDemo" in tc_out, tc_out
    demo_out = _inspect(clean_neo4j, "SavepointCaseWithUserDemo", method="hierarchy")
    assert "[calendar] TestCalendar" in demo_out and "[base] TestAPI" in demo_out, demo_out


# ---------------------------------------------------------------------------
# R20 - tenant isolation (H6)
# ---------------------------------------------------------------------------

def _seed_tenant_world(writer, driver) -> None:
    """Shared CE helper sale/TestSaleCommon subclassed by (both acme_sale and
    rival_sale declare ``depends: ['sale']``):
      - the tenant's own class   (repo acme_addons,  profile [OWN])
      - a shared CE class        (repo odoo,         profile [SHARED])
      - another tenant's class   (repo rival_addons, profile [FOREIGN, SHARED])
    plus the tenant's own class ``acme_sale/TestAcmeDiscount`` whose same-named
    twin in the rival repo (same module + class name) carries a private method.
    A framework helper child (origin='framework', profile outside the tenant's
    scope) must stay visible - framework source is public.
    """
    writer.write_framework_test_helpers([_fw("TransactionCase")], profiles=[SHARED])
    _write(writer, driver, "sale", [
        _cls("TestSaleCommon", "sale", ["TransactionCase"],
             file_path="addons/sale/tests/common.py", line=237),
        _cls("TestSaleOrder", "sale", ["TestSaleCommon"], tests=[("test_sale_order", 30)]),
    ], repo="odoo", profiles=[SHARED])
    _write(writer, driver, "acme_sale", [
        _cls("TestAcmeDiscount", "acme_sale", ["TestSaleCommon"],
             file_path="acme_sale/tests/test_discount.py",
             tests=[("test_discount_capped", 15)]),
    ], repo="acme_addons", profiles=[OWN], depends=["sale"])
    _write(writer, driver, "rival_sale", [
        _cls("TestRivalSecretPricing", "rival_sale", ["TestSaleCommon"],
             tests=[("test_rival_margin", 22)]),
    ], repo="rival_addons", profiles=[FOREIGN, SHARED], depends=["sale"])
    _write(writer, driver, "acme_sale", [
        _cls("TestAcmeDiscount", "acme_sale", ["TestSaleCommon"],
             file_path="acme_sale/tests/test_discount.py",
             tests=[("test_rival_private_case", 16)]),
    ], repo="rival_addons", profiles=[FOREIGN, SHARED], depends=["sale"])
    _index_pass(writer)
    # Synthetic: the writers never emit an edge FROM a framework helper, so the
    # framework-child branch of the contract is seeded directly.
    writer.write_framework_test_helpers([_fw("SaleFrameworkProbeCase")],
                                        profiles=["t373_unrelated_core"])
    with driver.session() as s:
        s.run(
            "MATCH (f:TestHelper {name: 'SaleFrameworkProbeCase', odoo_version: $v}) "
            "MATCH (h:TestHelper {name: 'TestSaleCommon', module: 'sale', odoo_version: $v}) "
            "MERGE (f)-[:INHERITS_TEST]->(h)", v=V,
        )


def _as_tenant(fn, *args, **kwargs):
    with patch("src.mcp.server._get_tenant_id", return_value=373), \
         patch("src.mcp.session.resolve_tenant_scope", return_value=([OWN], [SHARED])):
        return fn(*args, **kwargs)


def test_scoped_tenant_never_sees_or_counts_foreign_private_subclasses(writer, clean_neo4j):
    """R20 (FIX, H6): the tenant sees its own + shared children and the
    framework child; the rival tenant's private class is neither listed (name
    or module) nor counted."""
    _seed_tenant_world(writer, clean_neo4j)
    for mode in ("summary", "hierarchy"):
        out = _as_tenant(_inspect, clean_neo4j, "TestSaleCommon", method=mode)
        header, rows = _subclass_block(out)
        assert header == "├─ Subclassed by: 3 test classes", f"[{mode}]\n{out}"
        assert _row_names(rows) == [
            "[@framework] SaleFrameworkProbeCase",
            "[acme_sale] TestAcmeDiscount",
            "[sale] TestSaleOrder",
        ], f"[{mode}]\n{out}"
        assert "TestRivalSecretPricing" not in out, f"[{mode}] foreign name leaked:\n{out}"
        assert "rival_sale" not in out, f"[{mode}] foreign module leaked:\n{out}"


def test_admin_sees_every_subclass_including_other_tenants(writer, clean_neo4j):
    """R20 control: the unscoped admin view lists all four children, proving
    the tenant view above filtered real data rather than finding none."""
    _seed_tenant_world(writer, clean_neo4j)
    out = _inspect(clean_neo4j, "TestSaleCommon", method="hierarchy")
    header, rows = _subclass_block(out)
    assert header == "├─ Subclassed by: 4 test classes", out
    assert "[rival_sale] TestRivalSecretPricing" in _row_names(rows), out


def test_scoped_tenant_never_sees_foreign_private_test_methods(writer, clean_neo4j):
    """R20 (FIX, H6 on the method hop): methods are matched by class name +
    module, so the rival repo's same-named acme_sale/TestAcmeDiscount leaked
    its private test method. The tenant sees and counts only its own."""
    _seed_tenant_world(writer, clean_neo4j)
    out = _as_tenant(_inspect, clean_neo4j, "TestAcmeDiscount", method="methods")
    header, rows = _block(out, "├─ Test methods:")
    assert header == "├─ Test methods: 1", out
    assert [r[3:].split(" ")[0] for r in rows] == ["test_discount_capped"], out
    assert "test_rival_private_case" not in out, out


# ---------------------------------------------------------------------------
# L4 (F11) and follow-up GUARD (FU)
# ---------------------------------------------------------------------------

def test_same_named_helper_in_another_module_does_not_gain_false_subclasses(
    writer, clean_neo4j,
):
    """L4 (FIX, F11 - was a strict xfail pin of the known limitation): two
    modules each define ``TestSaleCouponCommon``; a child of the sale_coupon one
    must not appear under the loyalty one, because sale_coupon neither depends
    on nor imports from loyalty - Python resolves the base to sale_coupon's own
    class. Both helpers keep their real child."""
    writer.write_framework_test_helpers([_fw("TransactionCase")], profiles=[SHARED])
    _write(writer, clean_neo4j, "loyalty", [
        _cls("TestSaleCouponCommon", "loyalty", ["TransactionCase"],
             file_path="addons/loyalty/tests/common.py"),
        _cls("TestLoyaltyProgram", "loyalty", ["TestSaleCouponCommon"],
             tests=[("test_program_rules", 10)]),
    ])
    _write(writer, clean_neo4j, "sale_coupon", [
        _cls("TestSaleCouponCommon", "sale_coupon", ["TransactionCase"],
             file_path="addons/sale_coupon/tests/common.py"),
        _cls("TestSaleCouponProgramRules", "sale_coupon", ["TestSaleCouponCommon"],
             tests=[("test_program_rules_minimum_purchased_amount", 10)]),
    ])
    _index_pass(writer)

    out = _inspect(clean_neo4j, "TestSaleCouponCommon", module="loyalty", method="hierarchy")
    assert "[loyalty] TestLoyaltyProgram" in out, out
    assert "TestSaleCouponProgramRules" not in out, out
    own = _inspect(clean_neo4j, "TestSaleCouponCommon", module="sale_coupon",
                   method="hierarchy")
    header, rows = _subclass_block(own)
    assert header == "├─ Subclassed by: 1 test class", own
    assert rows == ["└─ [sale_coupon] TestSaleCouponProgramRules"], own


def test_ancestor_prologue_null_map_adds_no_phantom_owner_or_field(clean_neo4j):
    """FU: ``orm_queries._ancestor_tagged_prologue`` collects hop maps after an
    OPTIONAL MATCH (the same shape as #373). It must stay harmless: a model with
    no parent owns only itself, a one-hop model owns exactly itself + its mixin,
    and field listing/count return exactly the declared fields."""
    # GUARD: pre-existing behaviour
    from src.mcp.orm_queries import (
        _ancestor_owner_names,
        _count_fields_with_inherited,
        _list_fields_with_inherited,
    )
    with clean_neo4j.session() as s:
        s.run(
            """
            MERGE (mx:Model {name: 'mail.thread', module: 'mail', odoo_version: $v})
            SET mx.is_definition = true
            MERGE (c:Model {name: 'res.partner.t373', module: 'base', odoo_version: $v})
            SET c.is_definition = true
            MERGE (c)-[:INHERITS {order: 0}]->(mx)
            WITH mx, c
            UNWIND [['message_ids', 'mail.thread', 'mail'],
                    ['name', 'res.partner.t373', 'base']] AS f
            MERGE (fld:Field {name: f[0], model: f[1], module: f[2], odoo_version: $v})
            SET fld.ttype = 'char', fld.profile = []
            """, v=V,
        )
        assert sorted(_ancestor_owner_names("mail.thread", V, s)) == ["mail.thread"]
        assert sorted(_ancestor_owner_names("res.partner.t373", V, s)) == [
            "mail.thread", "res.partner.t373",
        ]
        leaf_fields = _list_fields_with_inherited("mail.thread", V, s)
        assert [f["name"] for f in leaf_fields] == ["message_ids"], leaf_fields
        assert all(f["owner_model"] for f in leaf_fields), leaf_fields
        child_fields = _list_fields_with_inherited("res.partner.t373", V, s)
        assert sorted((f["name"], f["owner_model"]) for f in child_fields) == [
            ("message_ids", "mail.thread"), ("name", "res.partner.t373"),
        ], child_fields
        assert _count_fields_with_inherited("res.partner.t373", V, s) == 2
        assert _count_fields_with_inherited("mail.thread", V, s) == 1


# ---------------------------------------------------------------------------
# lane-mcpfix defect 3 - module= / file_path= narrow the TestHelper fallback,
# and the setUpClass preview discloses its cap.
# ---------------------------------------------------------------------------

# sale/tests/common.py (17.0) - the helper every sale test builds on. Its
# setUpClass creates records of these models (8, more than the preview cap).
_SALE_COMMON_FIXTURES = [
    "res.partner", "product.product", "product.pricelist", "account.journal",
    "account.account", "account.tax", "res.users", "sale.order",
]


def _seed_sale_helper(writer, driver, setup_summary=None) -> None:
    """``TestSaleCommon`` as an addon TestHelper of ``sale`` only (no TestClass
    twin), plus the framework ``TransactionCase`` / ``Form`` helpers."""
    with driver.session() as s:
        s.run("MERGE (m:Module {name: 'sale', odoo_version: $v}) SET m.profile = $p",
              v=V, p=[SHARED])
    writer.write_framework_test_helpers([
        _fw("TransactionCase", line=600),
        _fw("Form", file_path="odoo/tests/form.py", line=20, test_type="form"),
        TestHelperInfo(
            name="TestSaleCommon", module="sale", odoo_version=V, origin="addon",
            test_type="transaction", file_path="addons/sale/tests/common.py", line=12,
            setup_summary=list(_SALE_COMMON_FIXTURES if setup_summary is None
                               else setup_summary),
        ),
    ], profiles=[SHARED])


def _is_not_found(out: str) -> bool:
    return "├─ Not found." in out


def test_helper_is_not_found_under_a_module_that_does_not_define_it(writer, clean_neo4j):
    """FIX: module='account' must not answer with sale's TestSaleCommon."""
    _seed_sale_helper(writer, clean_neo4j)
    out = _inspect(clean_neo4j, "TestSaleCommon", module="account")
    assert _is_not_found(out), out
    assert "addons/sale/tests/common.py" not in out, out


def test_helper_is_found_under_the_module_that_defines_it(writer, clean_neo4j):
    """GUARD: the positive control for the module filter."""
    # GUARD: pre-existing behaviour
    _seed_sale_helper(writer, clean_neo4j)
    out = _inspect(clean_neo4j, "TestSaleCommon", module="sale")
    assert out.startswith(f"TestSaleCommon (Odoo {V})"), out
    assert "addons/sale/tests/common.py" in out, out


def test_helper_is_not_found_under_a_file_that_does_not_define_it(writer, clean_neo4j):
    """FIX: file_path narrows the helper fallback like it narrows TestClass."""
    _seed_sale_helper(writer, clean_neo4j)
    out = _inspect(clean_neo4j, "TestSaleCommon",
                   file_path="addons/sale/tests/test_sale_order.py")
    assert _is_not_found(out), out
    found = _inspect(clean_neo4j, "TestSaleCommon", file_path="addons/sale/tests/common.py")
    assert found.startswith(f"TestSaleCommon (Odoo {V})"), found


def test_framework_helper_is_found_only_under_the_framework_module(writer, clean_neo4j):
    """FIX: TransactionCase lives in module '@framework'; module='sale' is Not found."""
    _seed_sale_helper(writer, clean_neo4j)
    assert _inspect(clean_neo4j, "TransactionCase", module="@framework").startswith(
        f"TransactionCase (Odoo {V})")
    assert _is_not_found(_inspect(clean_neo4j, "TransactionCase", module="sale"))


def test_framework_helper_file_path_filter(writer, clean_neo4j):
    """FIX: Form is in odoo/tests/form.py (17.0 layout), not odoo/tests/common.py."""
    _seed_sale_helper(writer, clean_neo4j)
    assert _is_not_found(_inspect(clean_neo4j, "Form", file_path="odoo/tests/common.py"))
    assert _inspect(clean_neo4j, "Form", file_path="odoo/tests/form.py").startswith(
        f"Form (Odoo {V})")


def test_test_resource_naming_the_wrong_module_is_not_found(
    writer, clean_neo4j, monkeypatch,
):
    """FIX: odoo://V/test/sale/TransactionCase must not serve the framework helper."""
    from src.mcp import server as srv
    from src.mcp.resources import _render_test_class

    _seed_sale_helper(writer, clean_neo4j)
    monkeypatch.setattr(srv, "_driver", clean_neo4j)
    body, _mime = _render_test_class(V, "sale", "TransactionCase")
    assert _is_not_found(body), body
    body, _mime = _render_test_class(V, "@framework", "TransactionCase")
    assert body.startswith(f"TransactionCase (Odoo {V})"), body


def _setup_line(out: str) -> str:
    found = [ln for ln in out.splitlines() if ln.startswith("├─ setUpClass:")]
    assert len(found) == 1, out
    return found[0]


def test_summary_setup_preview_discloses_how_many_fixtures_it_hides(writer, clean_neo4j):
    """FIX (ADR-0023 §3): 8 fixtures -> the first 6 named, then "... and 2 more"
    with a follow-up that lists them all. Pre-fix the last 2 vanished silently."""
    _seed_sale_helper(writer, clean_neo4j)
    line = _setup_line(_inspect(clean_neo4j, "TestSaleCommon", module="sale"))
    for model in _SALE_COMMON_FIXTURES[:6]:
        assert model in line, line
    for model in _SALE_COMMON_FIXTURES[6:]:
        assert model not in line, line
    assert "... and 2 more" in line, line
    assert "method='setup'" in line, line


def test_setup_mode_lists_every_fixture(writer, clean_neo4j):
    """FIX: method='setup' is the follow-up the preview points to - all 8, no cap."""
    _seed_sale_helper(writer, clean_neo4j)
    line = _setup_line(_inspect(clean_neo4j, "TestSaleCommon", "setup", module="sale"))
    for model in _SALE_COMMON_FIXTURES:
        assert model in line, line
    assert "more" not in line, line


def test_summary_setup_preview_at_the_cap_has_no_disclosure(writer, clean_neo4j):
    """GUARD: 6 fixtures fit the preview - nothing is hidden, nothing disclosed."""
    # GUARD: pre-existing behaviour
    _seed_sale_helper(writer, clean_neo4j, setup_summary=_SALE_COMMON_FIXTURES[:6])
    line = _setup_line(_inspect(clean_neo4j, "TestSaleCommon", module="sale"))
    for model in _SALE_COMMON_FIXTURES[:6]:
        assert model in line, line
    assert "more" not in line, line


# ---------------------------------------------------------------------------
# lane-mcpfix defect 2 - module_inspect(method='tests') closes its list
# ---------------------------------------------------------------------------

# sale/tests/*.py (17.0) test classes - 12, above the 10-row preview.
_SALE_TEST_CLASSES = [
    "TestAccessRights", "TestOnchangeProductId", "TestSaleFlow", "TestSaleOrder",
    "TestSaleOrderCancel", "TestSaleOrderDiscount", "TestSaleOrderDownPayment",
    "TestSalePrices", "TestSaleProductAttributeValueConfig", "TestSaleRefund",
    "TestSaleReport", "TestSaleToInvoice",
]


def _module_tests(driver, monkeypatch, module: str) -> str:
    from src.mcp import server as srv
    from src.mcp.inspect import _module_inspect

    monkeypatch.setattr(srv, "_driver", driver)
    return _module_inspect(module, "tests", V)


def _seed_sale_tests(writer, driver, names) -> None:
    _write(writer, driver, "sale", [
        _cls(n, "sale", ["TransactionCase"], tests=[("test_a", 10)]) for n in names
    ])


def test_module_tests_over_the_preview_closes_with_the_hidden_count(
    writer, clean_neo4j, monkeypatch,
):
    """FIX (ADR-0023 §3): 12 classes -> 10 rows, then a closing "... and 2 more"
    row that says how to reach one class. Pre-fix row 10 stayed '├─' and the
    remainder was a connector-less "+2 more" line with no follow-up."""
    _seed_sale_tests(writer, clean_neo4j, _SALE_TEST_CLASSES)
    out = _module_tests(clean_neo4j, monkeypatch, "sale")
    assert "├─ Test classes: 12" in out.splitlines(), out
    _hdr, rows = _block(out, "├─ Test classes:")
    assert len(rows) == 11, out
    assert all(r.startswith("├─ ") for r in rows[:10]), out
    shown = [r[3:].split()[0] for r in rows[:10]]
    assert len(set(shown)) == 10 and set(shown) <= set(_SALE_TEST_CLASSES), shown
    assert rows[10].startswith("└─ ... and 2 more"), out
    assert "test_class_inspect(" in rows[10] and "module='sale'" in rows[10], out
    lines = out.splitlines()
    assert lines[-1].startswith("└─ Next:"), out
    assert sum(ln.startswith("└─ ") for ln in lines) == 1, out


def test_module_tests_under_the_preview_closes_on_its_last_row(
    writer, clean_neo4j, monkeypatch,
):
    """GUARD: 3 classes -> 3 rows, the last one closes the list, no disclosure."""
    # GUARD: pre-existing behaviour
    _seed_sale_tests(writer, clean_neo4j, _SALE_TEST_CLASSES[:3])
    out = _module_tests(clean_neo4j, monkeypatch, "sale")
    _hdr, rows = _block(out, "├─ Test classes:")
    assert len(rows) == 3 and rows[-1].startswith("└─ ") and "more" not in out, out


def test_module_tests_pass_the_adr0023_tree_validator(writer, clean_neo4j, monkeypatch):
    """The full ADR-0023 validator (tests/test_mcp_module_lifecycle_read.py).

    Round 2 (F1, 79914c5): the round-1 xfail(strict) mark is dropped - the class
    rows and the closing disclosure indent with pipe + 3 spaces (ADR-0023 §1.3).
    """
    from tests.test_mcp_module_lifecycle_read import _assert_adr0023_tree

    _seed_sale_tests(writer, clean_neo4j, _SALE_TEST_CLASSES)
    _assert_adr0023_tree(_module_tests(clean_neo4j, monkeypatch, "sale"))


# ---------------------------------------------------------------------------
# lane-mcpfix round 2 C1 (bc79675) - module_inspect(method='tests') tells a
# failed query from an empty module, and counts every class
# ---------------------------------------------------------------------------


class _FailingTestClassQuery:
    """Driver stand-in: every query runs on the real driver except the
    TestClass read, which raises a non-timeout driver error (a dropped
    connection mid-read). The double sits BELOW the tool's error handling, at
    the driver session, so the handling itself is what the test observes."""

    def __init__(self, real):
        self._real = real

    def session(self, *a, **kw):
        outer = self

        class _Session:
            def __init__(self):
                self._s = outer._real.session(*a, **kw)

            def __enter__(self):
                self._s.__enter__()
                return self

            def __exit__(self, *exc):
                return self._s.__exit__(*exc)

            def run(self, query, *args, **params):
                text = getattr(query, "text", query)
                if "TestClass" in str(text):
                    from neo4j.exceptions import ServiceUnavailable
                    raise ServiceUnavailable("connection lost while reading TestClass")
                return self._s.run(query, *args, **params)

            def __getattr__(self, name):
                return getattr(self._s, name)

        return _Session()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_module_tests_query_failure_is_reported_unavailable_not_empty(
    writer, clean_neo4j, monkeypatch,
):
    """C1 FIX: sale has 3 indexed test classes; the TestClass read fails with a
    driver error. The answer must say the list is unavailable and that this is
    not evidence of no tests - never "No test classes indexed", which an agent
    would take as a fact and act on (e.g. "sale has no tests, write them all")."""
    _seed_sale_tests(writer, clean_neo4j, _SALE_TEST_CLASSES[:3])
    out = _module_tests(_FailingTestClassQuery(clean_neo4j), monkeypatch, "sale")
    assert "No test classes indexed" not in out, out
    line = [ln for ln in out.splitlines() if "Test classes:" in ln]
    assert len(line) == 1 and "unavailable" in line[0], out
    assert "not evidence" in line[0] and "[sale]" in line[0], out
    from tests.test_mcp_module_lifecycle_read import _assert_adr0023_tree
    _assert_adr0023_tree(out)


def test_module_tests_failure_is_not_cached_and_the_next_read_heals(
    writer, clean_neo4j, monkeypatch,
):
    """C1 FIX: a body rendered from a failed read is not stored by the resource
    cache (degraded), so the next read with the graph back serves the real
    list. Pre-fix the failed read rendered a normal-looking "No test classes"
    body that the cache kept for its whole TTL."""
    from src.mcp.resources import ResourceCache

    _seed_sale_tests(writer, clean_neo4j, _SALE_TEST_CLASSES[:3])
    cache = ResourceCache(ttl=300.0)
    key = f"{V}:module-tests:sale"

    def render(driver):
        return lambda: (_module_tests(driver, monkeypatch, "sale"), "text/markdown")

    first, _ = cache.get_or_compute(key, render(_FailingTestClassQuery(clean_neo4j)))
    second, _ = cache.get_or_compute(key, render(clean_neo4j))
    # The healing read is checked first: it is the rule this test owns (the
    # wording of the failed body is owned by the test above).
    assert second != first, f"the failed body was served again from the cache:\n{second}"
    assert "├─ Test classes: 3" in second.splitlines(), second
    for cls in _SALE_TEST_CLASSES[:3]:
        assert cls in second, second
    assert "unavailable" in first, first


def test_module_tests_healthy_body_is_still_cached(writer, clean_neo4j, monkeypatch):
    """C1 GUARD: a healthy answer is cached as before (a later outage within the
    TTL is invisible) - the degraded rule only skips failed renders."""
    # GUARD: pre-existing behaviour
    from src.mcp.resources import ResourceCache

    _seed_sale_tests(writer, clean_neo4j, _SALE_TEST_CLASSES[:3])
    cache = ResourceCache(ttl=300.0)
    key = f"{V}:module-tests:sale"
    healthy, _ = cache.get_or_compute(
        key, lambda: (_module_tests(clean_neo4j, monkeypatch, "sale"), "text/markdown"))
    again, _ = cache.get_or_compute(
        key, lambda: (_module_tests(_FailingTestClassQuery(clean_neo4j), monkeypatch,
                                    "sale"), "text/markdown"))
    assert again == healthy, again


def test_module_tests_count_is_exact_above_the_old_200_row_limit(
    writer, clean_neo4j, monkeypatch,
):
    """C1 FIX: the header is the true class count. Real case: odoo/addons/base/tests
    declares 219 classes at 17.0 (240 at 18.0, 252 at 19.0; counted in the local
    checkouts 2026-09-24) - above the old LIMIT 200, which made the header read
    200. Seeded: 250 classes -> header 250, the 10-row preview, then a closing
    "... and 240 more"."""
    names = [f"TestBaseCase{i:03d}" for i in range(250)]
    _write(writer, clean_neo4j, "base", [
        _cls(n, "base", ["TransactionCase"], tests=[("test_a", 10)]) for n in names
    ])
    out = _module_tests(clean_neo4j, monkeypatch, "base")
    assert "├─ Test classes: 250" in out.splitlines(), out
    _hdr, rows = _block(out, "├─ Test classes:")
    assert len(rows) == 11, out
    assert rows[10].startswith("└─ ... and 240 more"), out
