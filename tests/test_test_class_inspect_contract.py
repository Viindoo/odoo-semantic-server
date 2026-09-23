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
  L4   xfail(strict): name-only INHERITS_TEST resolution links a child to a
       same-named helper of ANOTHER module (known limitation, follow-up).
  FU   GUARD: the null-map pattern in ``orm_queries._ancestor_tagged_prologue``
       stays harmless (no phantom owner model, exact own-field set).

The graph is seeded through the real indexer writers (``write_test_results``,
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
           repo: str = "odoo", profiles: list[str] | None = None) -> None:
    profiles = [SHARED] if profiles is None else profiles
    with driver.session() as s:
        s.run(
            "MERGE (m:Module {name: $n, odoo_version: $v}) "
            "SET m.profile = [x IN coalesce(m.profile, []) WHERE NOT x IN $p] + $p",
            n=module, v=V, p=profiles,
        )
    mod = ModuleInfo(name=module, odoo_version=V, repo=repo,
                     path=f"/{repo}/{module}", depends=[])
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

    Run 1 (full index): edges resolve to the TestClass nodes, then
    finalize_is_helper promotes the three helpers and MERGEs TestHelper twins.
    Run 2 (incremental, adds sale_stock): reconcile resolves "TestHelper first",
    so every child ALSO gets an edge to the twin and the new child ONLY has the
    twin edge - the live TestSaleCommon@17.0 state.
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
    ])
    _index_pass(writer)
    if second_run:
        _write(writer, driver, "sale_stock", [
            _cls("TestSaleStock", "sale_stock", ["TestSaleCommon"],
                 tests=[("test_00_sale_stock_invoice", 40)]),
        ])
        _index_pass(writer)


def test_twin_topology_is_the_one_under_test(writer, clean_neo4j):
    """Positive control for R04: the writers really produced the split edges
    (child on the twin only, child on both) - otherwise R04 proves nothing."""
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
        "TestSaleStock": ["TestHelper"],
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


def test_children_right_after_promotion_resolve_through_the_testclass(writer, clean_neo4j):
    """R04 (FIX): after the first full index the only edge points at the
    TestClass node (twin just created) - the child is still listed once."""
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
    for module, name in reversed(expected):
        _write(writer, clean_neo4j, module, [
            _cls(name, module, ["AccountTestInvoicingCommon"], tests=[("test_it", 5)]),
        ])
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
    """Shared CE helper sale/TestSaleCommon subclassed by:
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
    ], repo="acme_addons", profiles=[OWN])
    _write(writer, driver, "rival_sale", [
        _cls("TestRivalSecretPricing", "rival_sale", ["TestSaleCommon"],
             tests=[("test_rival_margin", 22)]),
    ], repo="rival_addons", profiles=[FOREIGN, SHARED])
    _write(writer, driver, "acme_sale", [
        _cls("TestAcmeDiscount", "acme_sale", ["TestSaleCommon"],
             file_path="acme_sale/tests/test_discount.py",
             tests=[("test_rival_private_case", 16)]),
    ], repo="rival_addons", profiles=[FOREIGN, SHARED])
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
# Known limitation pin (L4) and follow-up GUARD (FU)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known limitation (review L4 of #373, follow-up issue to be filed): "
        "reconcile_test_inherits resolves a base by NAME only, so a child of "
        "sale_coupon's TestSaleCouponCommon is also linked to loyalty's "
        "same-named class. Flip to a plain test when resolution uses the "
        "child's import/module context."
    ),
)
def test_same_named_helper_in_another_module_does_not_gain_false_subclasses(
    writer, clean_neo4j,
):
    """L4: two modules each define ``TestSaleCouponCommon``; a child of the
    sale_coupon one must not appear under the loyalty one."""
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
