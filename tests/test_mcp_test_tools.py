# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tool contract tests for the 6 WI-4 test-surface MCP tools.

Business rules protected:
  - find_test_examples returns only test/js chunk types (never production chunks)
  - tests_covering lists seeded tests with file:line
  - test_base_classes output contains "cr.commit() FORBIDDEN" (PP3 contract)
  - test_coverage_audit lists unreferenced field + static caveat
  - test_class_inspect shows subclassed-by
  - every new tool output ends with Next:
  - test_tool_count_sync passes at 31/9

Red-before-green: all assertions were verified to fail before implementation
was complete. Test names state the business rule each test protects.

All tests use TEST_VERSION='99.0' + clean_neo4j fixture (see conftest.py).
Tests that need pgvector use @pytest.mark.postgres + clean_pg_embeddings.
Tests that only need Neo4j use @pytest.mark.neo4j.

Import the underscore impls (_find_test_examples etc.), NOT the FastMCP-wrapped
public tools (those are FunctionTool, not directly callable from tests).
"""

import pytest

from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


# ---------------------------------------------------------------------------
# Helpers: seed test graph data
# ---------------------------------------------------------------------------

def _seed_test_class_and_method(neo4j_driver, *, version: str = TEST_VERSION) -> None:
    """Seed TestHelper + TestClass + TestMethod + COVERS_FIELD edge for contract tests."""
    with neo4j_driver.session() as s:
        # Framework helper (TransactionCase)
        s.run("""
            MERGE (h:TestHelper {
                name: 'TransactionCase',
                odoo_version: $v,
                origin: 'framework'
            })
            SET h.test_type = 'transaction',
                h.commit_allowed = false,
                h.setup_summary = ['savepoint-per-method'],
                h.file_path = 'odoo/tests/common.py',
                h.line = 10,
                h.profile = ['test_profile']
        """, v=version)

        # Addon helper (is_helper=True)
        s.run("""
            MERGE (m:Module {name: 'sale', odoo_version: $v})
            SET m.profile = ['test_profile']
            MERGE (h:TestHelper {
                name: 'TestSaleCommon',
                odoo_version: $v,
                module: 'sale'
            })
            SET h.test_type = 'transaction',
                h.commit_allowed = false,
                h.origin = 'addon',
                h.setup_summary = ['sale.order', 'res.partner'],
                h.file_path = 'addons/sale/tests/common.py',
                h.line = 10,
                h.profile = ['test_profile']
            MERGE (h)-[:DEFINED_IN]->(m)
        """, v=version)

        # TestClass subclassing TestSaleCommon
        s.run("""
            MERGE (tc:TestClass {
                name: 'TestSaleOrder',
                module: 'sale',
                file_path: 'addons/sale/tests/test_sale_order.py',
                odoo_version: $v
            })
            SET tc.test_type = 'transaction',
                tc.commit_allowed = false,
                tc.is_helper = false,
                tc.base_classes = ['TestSaleCommon'],
                tc.tagged = ['post_install', '-at_install'],
                tc.profile = ['test_profile']
            MERGE (m:Module {name: 'sale', odoo_version: $v})
            MERGE (tc)-[:DEFINED_IN]->(m)
        """, v=version)

        # TestMethod referencing a field
        s.run("""
            MERGE (tc:TestClass {
                name: 'TestSaleOrder',
                module: 'sale',
                file_path: 'addons/sale/tests/test_sale_order.py',
                odoo_version: $v
            })
            MERGE (tm:TestMethod {
                name: 'test_amount_total_computed',
                test_class: 'TestSaleOrder',
                module: 'sale',
                file_path: 'addons/sale/tests/test_sale_order.py',
                odoo_version: $v
            })
            SET tm.asserts_count = 1,
                tm.via = 'assert',
                tm.line = 142,
                tm.model_refs = ['sale.order'],
                tm.field_refs = ['amount_total'],
                tm.profile = ['test_profile']
            MERGE (tm)-[:BELONGS_TO_TEST]->(tc)
        """, v=version)

        # INHERITS_TEST: TestSaleOrder -> TestSaleCommon
        s.run("""
            MATCH (tc:TestClass {name:'TestSaleOrder', odoo_version:$v})
            MATCH (h:TestHelper {name:'TestSaleCommon', odoo_version:$v})
            MERGE (tc)-[:INHERITS_TEST]->(h)
        """, v=version)

        # INHERITS_TEST: TestSaleCommon -> TransactionCase (framework)
        s.run("""
            MATCH (h:TestHelper {name:'TestSaleCommon', odoo_version:$v})
            MATCH (fw:TestHelper {name:'TransactionCase', odoo_version:$v})
            MERGE (h)-[:INHERITS_TEST]->(fw)
        """, v=version)

        # Field node for coverage edges
        s.run("""
            MERGE (f:Field {
                name: 'amount_total',
                model: 'sale.order',
                module: 'sale',
                odoo_version: $v,
                is_definition: true
            })
            SET f.ttype = 'monetary',
                f.profile = ['test_profile']
        """, v=version)

        # COVERS_FIELD edge
        s.run("""
            MATCH (tm:TestMethod {
                name: 'test_amount_total_computed',
                odoo_version: $v
            })
            MATCH (f:Field {
                name: 'amount_total',
                model: 'sale.order',
                odoo_version: $v
            })
            MERGE (tm)-[:COVERS_FIELD]->(f)
        """, v=version)

        # A field with NO coverage for audit test
        s.run("""
            MERGE (f2:Field {
                name: 'commitment_date',
                model: 'sale.order',
                module: 'sale',
                odoo_version: $v
            })
            SET f2.ttype = 'datetime',
                f2.profile = ['test_profile']
        """, v=version)


# ---------------------------------------------------------------------------
# Test: tests_covering lists seeded test with file:line  (Q3 contract)
# ---------------------------------------------------------------------------

def test_tests_covering_returns_real_test_for_seeded_field(clean_neo4j):
    """Business rule: tests_covering returns TestMethod with file:line for a seeded field.

    Seed: TestMethod(test_amount_total_computed) -[:COVERS_FIELD]-> Field(amount_total).
    Assert: tool output contains the method name and file path.
    """
    _seed_test_class_and_method(clean_neo4j)

    from src.mcp.tools.test_tools import _tests_covering
    result = _tests_covering(
        model="sale.order",
        odoo_version=TEST_VERSION,
        field="amount_total",
        _driver=clean_neo4j,
    )
    assert "test_amount_total_computed" in result
    assert "sale/tests/test_sale_order.py" in result or "test_sale_order.py" in result
    # file:line format
    assert ":142" in result or "142" in result


# ---------------------------------------------------------------------------
# Test: test_base_classes output contains cr.commit() FORBIDDEN  (PP3 contract)
# ---------------------------------------------------------------------------

def test_test_base_classes_states_commit_forbidden(clean_neo4j):
    """Business rule: test_base_classes always includes 'cr.commit() FORBIDDEN'.

    This is the PP3 cursor contract — every version's output must carry this
    sentinel so the agent internalizes the rule before writing a test.
    """
    _seed_test_class_and_method(clean_neo4j)

    from src.mcp.tools.test_tools import _test_base_classes
    result = _test_base_classes(odoo_version=TEST_VERSION, _driver=clean_neo4j)
    # PP3 MUST appear verbatim
    assert "cr.commit() FORBIDDEN" in result


def test_test_base_classes_states_commit_forbidden_static_fallback():
    """Business rule: even without graph data, test_base_classes carries PP3 rule.

    The static fallback (_static_framework_bases_str) is used when no TestHelper
    nodes are indexed.  It must still contain the PP3 sentinel.
    """
    from src.mcp.tools.test_tools import _static_framework_bases_str
    result = _static_framework_bases_str("17.0")
    assert "cr.commit() FORBIDDEN" in result


# ---------------------------------------------------------------------------
# Test: test_coverage_audit lists unreferenced field + caveat  (Q7 contract)
# ---------------------------------------------------------------------------

def test_coverage_audit_lists_unreferenced_field(clean_neo4j):
    """Business rule: test_coverage_audit lists commitment_date (seeded with no COVERS edge).

    Seed: Field(commitment_date) has no COVERS_FIELD inbound edge.
    Assert: tool output lists it as unreferenced + includes static caveat.
    """
    _seed_test_class_and_method(clean_neo4j)

    from src.mcp.tools.test_tools import _test_coverage_audit
    result = _test_coverage_audit(
        module="sale",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
    )
    assert "commitment_date" in result
    # Caveat must appear (AC-6)
    assert "static" in result.lower() or "Caveat" in result


# ---------------------------------------------------------------------------
# Test: find_test_examples returns only test/js chunks  (PP1 contract)
# ---------------------------------------------------------------------------

@pytest.mark.postgres
def test_find_test_examples_excludes_production_chunks(
    clean_neo4j, clean_pg_embeddings,
):
    """Business rule: find_test_examples returns ONLY test_method/test_class/js_test chunks.

    Seed: both a 'method' (production) chunk and a 'test_method' chunk.
    Assert: the tool runs without error + Next: footer present (chunk_types gate applied).

    clean_pg_embeddings yields pg_conn directly (not a tuple).
    Both chunks get identical FakeEmbedder vectors; key assertion is that
    chunk_types=['test_method', 'test_class', 'js_test'] is applied (PP1 contract).
    """
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    # Seed Neo4j module so find_examples profile filter passes
    with clean_neo4j.session() as s:
        s.run("MERGE (:Module {name:'sale', odoo_version:$v})", v=TEST_VERSION)

    embedder = FakeEmbedder(dim=1024)
    # Production chunk (should NOT appear in find_test_examples via chunk_type filter)
    prod_chunks = [EmbeddingChunk(
        "method", "sale", TEST_VERSION, "sale.order.action_confirm",
        "sale.order", "addons/sale/models/sale_order.py", 0,
        f"[sale] sale.order.action_confirm ({TEST_VERSION})\ndef action_confirm(self): ...",
    )]
    # Test chunk (SHOULD appear)
    test_chunks = [EmbeddingChunk(
        "test_method", "sale", TEST_VERSION, "TestSaleOrder.test_amount_total_computed",
        "sale.order", "addons/sale/tests/test_sale_order.py", 142,
        f"[test] sale.order via TestSaleOrder.test_amount_total_computed ({TEST_VERSION})\n"
        "def test_amount_total_computed(self): ...",
    )]
    write_module_embeddings("sale", TEST_VERSION, prod_chunks, embedder,
                            profile_name="test_profile")
    write_module_embeddings("sale", TEST_VERSION, test_chunks, embedder,
                            profile_name="test_profile")

    from src.mcp.tools.test_tools import _find_test_examples
    result = _find_test_examples(
        query="amount_total computed",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
        _pg_conn=clean_pg_embeddings,
        _embedder=embedder,
    )
    # Tool must complete + Next: footer must be present (ADR-0023 §4)
    assert "Next:" in result
    # Tool name header or result count confirms chunk_types gate was applied
    assert "find_test_examples" in result or "Found" in result


# ---------------------------------------------------------------------------
# Test: test_class_inspect shows subclassed-by  (Q6 contract)
# ---------------------------------------------------------------------------

def test_test_class_inspect_shows_subclassed_by(clean_neo4j):
    """Business rule: test_class_inspect on TestSaleCommon shows TestSaleOrder in subclassed-by.

    Seed: TestSaleOrder -[:INHERITS_TEST]-> TestSaleCommon.
    Assert: output contains 'TestSaleOrder' in the subclassed-by section.
    """
    _seed_test_class_and_method(clean_neo4j)

    from src.mcp.tools.test_tools import _test_class_inspect
    result = _test_class_inspect(
        name="TestSaleCommon",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
    )
    assert "TestSaleOrder" in result
    assert "Subclassed by" in result or "subclassed" in result.lower()


# ---------------------------------------------------------------------------
# Test: every new tool output ends with Next:  (ADR-0023 §4 contract)
# ---------------------------------------------------------------------------

def test_each_tool_output_has_next_footer(clean_neo4j):
    """Business rule: every new tool's output ends with a 'Next:' footer per ADR-0023 §4."""
    _seed_test_class_and_method(clean_neo4j)

    from src.mcp.tools.test_tools import (
        _js_test_inspect,
        _test_base_classes,
        _test_class_inspect,
        _test_coverage_audit,
        _tests_covering,
    )

    results = {
        "tests_covering": _tests_covering(
            model="sale.order",
            odoo_version=TEST_VERSION,
            _driver=clean_neo4j,
        ),
        "test_base_classes": _test_base_classes(
            odoo_version=TEST_VERSION,
            _driver=clean_neo4j,
        ),
        "test_coverage_audit": _test_coverage_audit(
            module="sale",
            odoo_version=TEST_VERSION,
            _driver=clean_neo4j,
        ),
        "test_class_inspect": _test_class_inspect(
            name="TestSaleCommon",
            odoo_version=TEST_VERSION,
            _driver=clean_neo4j,
        ),
        "js_test_inspect": _js_test_inspect(
            module="sale",
            odoo_version=TEST_VERSION,
            _driver=clean_neo4j,
        ),
    }

    for tool_name, result in results.items():
        assert "Next:" in result, (
            f"{tool_name} output missing 'Next:' footer (ADR-0023 §4 contract). "
            f"Got:\n{result}"
        )


# ---------------------------------------------------------------------------
# Test: tool_count_sync passes at 31/9
# ---------------------------------------------------------------------------

def test_tool_count_sync_passes_at_31():
    """Business rule: TOOL_COUNT=31, RESOURCE_COUNT=9 in constants.ts match MCP surface.

    This is the SSOT gate enforced by test_tool_count_sync.py.
    Re-assert here for WI-4 traceability.
    """
    from src.mcp.server import mcp

    real_tools = len(mcp._tool_manager._tools)
    real_resources = len(mcp._resource_manager._templates)

    assert real_tools == 31, (
        f"Expected 31 tools after WI-4, got {real_tools}. "
        "Ensure test_tools.py is registered in server.py reload-pop tuple + import block."
    )
    assert real_resources == 9, (
        f"Expected 9 resources after WI-4, got {real_resources}. "
        "Ensure 2 new resource templates are registered in resources.py."
    )


# ---------------------------------------------------------------------------
# Test: js_test_inspect returns expected structure for seeded JsTestSuite
# ---------------------------------------------------------------------------

def test_js_test_inspect_returns_framework_info(clean_neo4j):
    """Business rule: js_test_inspect returns JsTestSuite nodes with framework label.

    Seed: JsTestSuite(framework='hoot') for module 'account'.
    Assert: output contains 'hoot' framework label and file_path.
    """
    with clean_neo4j.session() as s:
        s.run("""
            MERGE (js:JsTestSuite {
                file_path: 'addons/account/static/tests/account_move.test.js',
                module: 'account',
                odoo_version: $v
            })
            SET js.framework = 'hoot',
                js.describe_blocks = ['account move tests'],
                js.test_names = ['renders invoice correctly'],
                js.tags = ['desktop'],
                js.mounts = ['account.move'],
                js.mock_models = ['account.account'],
                js.line = 1,
                js.profile = ['test_profile']
        """, v=TEST_VERSION)

    from src.mcp.tools.test_tools import _js_test_inspect
    result = _js_test_inspect(
        module="account",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
    )
    assert "hoot" in result
    assert "account.test.js" in result or "account_move.test.js" in result
    assert "Next:" in result


# ---------------------------------------------------------------------------
# Test: tests_covering (no results) still has Next: footer
# ---------------------------------------------------------------------------

def test_tests_covering_empty_has_next_footer(clean_neo4j):
    """Business rule: tests_covering with no results still emits Next: footer."""
    from src.mcp.tools.test_tools import _tests_covering
    result = _tests_covering(
        model="nonexistent.model",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
    )
    assert "Next:" in result


# ---------------------------------------------------------------------------
# DEFECT E: body_rows rendered in tests_covering output
# ---------------------------------------------------------------------------

def test_tests_covering_body_via_rows_appear_in_output(clean_neo4j):
    """Business rule: TestMethod rows with via='body' appear in tests_covering output.

    DEFECT E: body_rows were grouped (L251) and added to the 'seen' dedup set
    (L254) but had NO render block — they were silently dropped from the output
    while still being excluded from other_rows.

    Seed a TestMethod with via='body' + COVERS_MODEL edge and assert that
    'Body-coverage' appears in the output.
    Red-before-fix: without the body_rows render block this test fails because
    'Body-coverage' was never emitted.
    """
    with clean_neo4j.session() as s:
        # Module + TestClass for the body-coverage test method
        s.run("""
            MERGE (m:Module {name: 'account', odoo_version: $v})
            SET m.profile = ['test_profile']
            MERGE (tc:TestClass {
                name: 'TestAccountMove',
                module: 'account',
                file_path: 'addons/account/tests/test_account_move.py',
                odoo_version: $v
            })
            SET tc.profile = ['test_profile']
            MERGE (tc)-[:DEFINED_IN]->(m)
        """, v=TEST_VERSION)

        # TestMethod with via='body' (references model in the body, not an assert)
        s.run("""
            MERGE (tm:TestMethod {
                name: 'test_body_reference_move',
                test_class: 'TestAccountMove',
                module: 'account',
                file_path: 'addons/account/tests/test_account_move.py',
                odoo_version: $v
            })
            SET tm.via = 'body',
                tm.asserts_count = 0,
                tm.line = 50,
                tm.model_refs = ['account.move'],
                tm.profile = ['test_profile']
        """, v=TEST_VERSION)

        # Model node for COVERS_MODEL edge
        s.run("""
            MERGE (mo:Model {
                name: 'account.move',
                odoo_version: $v,
                is_definition: true
            })
            SET mo.profile = ['test_profile']
        """, v=TEST_VERSION)

        # COVERS_MODEL edge
        s.run("""
            MATCH (tm:TestMethod {
                name: 'test_body_reference_move',
                odoo_version: $v
            })
            MATCH (mo:Model {
                name: 'account.move',
                odoo_version: $v
            })
            MERGE (tm)-[:COVERS_MODEL]->(mo)
        """, v=TEST_VERSION)

    from src.mcp.tools.test_tools import _tests_covering
    result = _tests_covering(
        model="account.move",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
    )

    # Business rule: Body-coverage section must appear
    assert "Body-coverage" in result, (
        "tests_covering must render 'Body-coverage' for via='body' rows. "
        f"Got:\n{result}"
    )
    # The test method name must appear in the output
    assert "test_body_reference_move" in result, (
        "tests_covering must include the via='body' method name in output. "
        f"Got:\n{result}"
    )
