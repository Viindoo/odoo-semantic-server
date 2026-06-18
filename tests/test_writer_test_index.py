# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for WI-1 test-surface writer (writer_neo4j.py).

Requires Neo4j (testcontainers or local bolt://localhost:7687).
Mark: pytest.mark.neo4j.

All data uses TEST_VERSION='99.0' and clean_neo4j fixture to avoid conflicts
with real indexed data.

ANTI-TAUTOLOGY: coverage assertions compare graph edges to a HUMAN-AUTHORED
expected-set literal from a handcrafted fixture — never feed parser output back
to itself as the oracle.
"""
import os

import pytest

from src.indexer.models import (
    ModuleInfo,
    TestClassInfo,
    TestHelperInfo,
    TestMethodInfo,
    TestParseResult,
)
from src.indexer.parser_test import seed_framework_helpers
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter connected to test DB."""
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _make_module(name: str, repo: str = "test_repo") -> ModuleInfo:
    return ModuleInfo(
        name=name,
        odoo_version=TEST_VERSION,
        repo=repo,
        path=f"/addons/{name}",
        depends=[],
    )


def _make_test_class(
    name: str,
    module: str,
    file_path: str = "tests/test_base.py",
    test_type: str = "transaction",
    base_classes_ordered: list[str] | None = None,
    tagged: list[str] | None = None,
    commit_allowed: bool = False,
    defines_no_test_methods: bool = False,
) -> TestClassInfo:
    return TestClassInfo(
        name=name,
        module=module,
        file_path=file_path,
        odoo_version=TEST_VERSION,
        test_type=test_type,
        base_classes_ordered=base_classes_ordered or ["TransactionCase"],
        tagged=tagged or [],
        commit_allowed=commit_allowed,
        defines_no_test_methods=defines_no_test_methods,
    )


def _make_test_method(
    name: str,
    test_class: str,
    module: str,
    file_path: str = "tests/test_base.py",
    model_refs: list[str] | None = None,
    field_refs: list[str] | None = None,
    method_refs: list[str] | None = None,
    via: str = "body",
) -> TestMethodInfo:
    return TestMethodInfo(
        name=name,
        test_class=test_class,
        module=module,
        file_path=file_path,
        odoo_version=TEST_VERSION,
        model_refs=model_refs or [],
        field_refs=field_refs or [],
        method_refs=method_refs or [],
        via=via,
    )


def _make_test_result(
    module_name: str,
    classes: list[TestClassInfo],
    repo: str = "test_repo",
) -> TestParseResult:
    module = _make_module(module_name, repo=repo)
    return TestParseResult(module=module, test_classes=classes)


# ---------------------------------------------------------------------------
# Test 1: basic node write
# ---------------------------------------------------------------------------

def test_write_creates_testclass_node(writer, neo4j_driver):
    """Business rule: write_test_results persists a TestClass node with all required properties."""
    tc = _make_test_class("TestSaleOrder", "sale", file_path="tests/test_sale_order.py")
    tc.methods = [
        _make_test_method("test_amount_total", "TestSaleOrder", "sale",
                          file_path="tests/test_sale_order.py",
                          model_refs=["sale.order"], field_refs=["amount_total"]),
    ]
    result = _make_test_result("sale", [tc])
    writer.write_test_results([result], profiles=["test_profile"])

    with neo4j_driver.session() as s:
        row = s.run(
            "MATCH (c:TestClass {name: $n, module: $m, odoo_version: $v}) RETURN c",
            n="TestSaleOrder", m="sale", v=TEST_VERSION,
        ).single()
    assert row is not None, "TestClass node must exist after write_test_results"
    node = row["c"]
    assert node["test_type"] == "transaction"
    assert node["commit_allowed"] is False
    assert "test_profile" in node["profile"]


def test_write_creates_testmethod_node(writer, neo4j_driver):
    """Business rule: write_test_results creates a TestMethod node linked via BELONGS_TO_TEST."""
    tc = _make_test_class("TestSaleCommon", "sale")
    tc.methods = [
        _make_test_method("test_basic", "TestSaleCommon", "sale",
                          model_refs=["sale.order"]),
    ]
    result = _make_test_result("sale", [tc])
    writer.write_test_results([result])

    with neo4j_driver.session() as s:
        row = s.run(
            "MATCH (m:TestMethod {name: $n, test_class: $c, odoo_version: $v})"
            "-[:BELONGS_TO_TEST]->(cls:TestClass) RETURN m, cls",
            n="test_basic", c="TestSaleCommon", v=TEST_VERSION,
        ).single()
    assert row is not None, "TestMethod node must exist and link to TestClass"
    m_node = row["m"]
    assert "sale.order" in m_node["model_refs"]


# ---------------------------------------------------------------------------
# Test 2: CRITICAL-1 - two same-name classes in one module stay TWO distinct nodes
# ---------------------------------------------------------------------------

def test_critical1_same_name_different_files_yield_two_distinct_nodes(writer, neo4j_driver):
    """Business rule (CRITICAL-1): same class name in two different files of the same module
    must produce TWO distinct TestClass nodes (MERGE key includes file_path).

    This is the key regression for the C1 schema fix: if file_path were not in the MERGE key,
    the second write would overwrite the first and only one node would remain.
    """
    # Two classes: same name, same module, different file_path
    tc_file1 = _make_test_class(
        "TestSaleCommon", "sale",
        file_path="tests/test_sale_order.py",  # file 1
    )
    tc_file2 = _make_test_class(
        "TestSaleCommon", "sale",
        file_path="tests/common.py",           # file 2 (same module, different file)
    )
    result = _make_test_result("sale", [tc_file1, tc_file2])
    writer.write_test_results([result])

    with neo4j_driver.session() as s:
        count = s.run(
            "MATCH (c:TestClass {name: $n, module: $m, odoo_version: $v}) RETURN count(c) AS cnt",
            n="TestSaleCommon", m="sale", v=TEST_VERSION,
        ).single()["cnt"]
    assert count == 2, (
        f"CRITICAL-1: expected 2 distinct TestClass nodes (different file_path), got {count}. "
        "Check that file_path is included in the MERGE key."
    )


# ---------------------------------------------------------------------------
# Test 3: idempotency - reindex produces single node (union profile)
# ---------------------------------------------------------------------------

def test_idempotent_reindex_single_node_union_profile(writer, neo4j_driver):
    """Business rule: writing the same TestClass twice (reindex) yields ONE node with union profile.

    ADR-0034 profile union-only: same entity re-indexed must not create duplicate nodes;
    profile[] must accumulate both profile names.
    """
    tc = _make_test_class("TestIdempotent", "sale")
    result = _make_test_result("sale", [tc])

    # Write twice with different profiles
    writer.write_test_results([result], profiles=["profile_a"])
    writer.write_test_results([result], profiles=["profile_b"])

    with neo4j_driver.session() as s:
        row = s.run(
            "MATCH (c:TestClass {name: $n, module: $m, odoo_version: $v}) RETURN c",
            n="TestIdempotent", m="sale", v=TEST_VERSION,
        ).single()
    assert row is not None
    node = row["c"]
    # Only one node should exist (idempotent MERGE)
    with neo4j_driver.session() as s:
        cnt = s.run(
            "MATCH (c:TestClass {name: $n, module: $m, odoo_version: $v}) RETURN count(c) AS cnt",
            n="TestIdempotent", m="sale", v=TEST_VERSION,
        ).single()["cnt"]
    assert cnt == 1, "Reindex must NOT create duplicate TestClass nodes"
    # Both profiles should be in the union
    assert "profile_a" in node["profile"], "profile_a must be in profile[] after union"
    assert "profile_b" in node["profile"], "profile_b must be in profile[] after union"


# ---------------------------------------------------------------------------
# Test 4: INHERITS_TEST reconcile - 3 children of one helper -> exactly 3 edges, not 9
# ---------------------------------------------------------------------------

def test_reconcile_inherits_test_no_k_squared_mesh(writer, neo4j_driver):
    """Business rule (ADR-0048): N children of one base -> exactly N INHERITS_TEST edges, not N².

    K×D: each child gets ONE edge to the shared parent (definition node).
    If the Cypher accidentally cross-joined children with each other, we'd get N² edges.
    """
    # Write framework helper (the shared parent)
    parent_helper = TestHelperInfo(
        name="SharedBase",
        module="@framework",
        odoo_version=TEST_VERSION,
        origin="framework",
        test_type="transaction",
        setup_summary=["auto-rollback per test"],
    )
    writer.write_framework_test_helpers([parent_helper])

    # Write 3 child classes all inheriting SharedBase
    children = []
    for i in range(1, 4):
        tc = _make_test_class(
            f"ChildTest{i}", "sale",
            file_path=f"tests/test_{i}.py",
            base_classes_ordered=["SharedBase"],
        )
        children.append(tc)
    result = _make_test_result("sale", children)
    writer.write_test_results([result])

    # Run reconcile post-pass
    writer.reconcile_test_inherits(TEST_VERSION)

    with neo4j_driver.session() as s:
        # Count INHERITS_TEST edges from each child to SharedBase
        count = s.run(
            """
            MATCH (child:TestClass {odoo_version: $v})
                  -[:INHERITS_TEST]->(parent {name: 'SharedBase', odoo_version: $v})
            WHERE child.module = 'sale'
            RETURN count(*) AS cnt
            """,
            v=TEST_VERSION,
        ).single()["cnt"]

    assert count == 3, (
        f"ADR-0048: expected exactly 3 INHERITS_TEST edges (K×D, not K²), got {count}. "
        "If you see 9, the Cypher has a cross-join bug."
    )


# ---------------------------------------------------------------------------
# Test 5: COVERS_MODEL / COVERS_FIELD edges (to is_definition=true only)
# ---------------------------------------------------------------------------

def test_reconcile_coverage_edges_target_is_definition_only(writer, neo4j_driver):
    """Business rule (ADR-0013): COVERS_MODEL/COVERS_FIELD edges -> is_definition=true only.

    A TestMethod that references sale.order must NOT create dangling edges when
    no Model node exists for that model; it MUST create edges when the model is present
    with is_definition=true.
    """
    # Seed a Model node with is_definition=true (what a normal model write produces)
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (m:Model {name: $name, module: $mod, odoo_version: $v})
            SET m.is_definition = true
            """,
            name="sale.order", mod="sale", v=TEST_VERSION,
        )

    # Write a TestClass + TestMethod with model_refs to sale.order
    tc = _make_test_class("TestCoverageCheck", "sale")
    tc.methods = [
        _make_test_method(
            "test_coverage", "TestCoverageCheck", "sale",
            model_refs=["sale.order", "nonexistent.model"],  # nonexistent.model has no node
            field_refs=["amount_total"],
        ),
    ]
    result = _make_test_result("sale", [tc])
    writer.write_test_results([result])
    writer.reconcile_test_coverage(TEST_VERSION)

    with neo4j_driver.session() as s:
        # Edge to is_definition=true Model must exist
        covered_models = s.run(
            """
            MATCH (:TestMethod {name: 'test_coverage', odoo_version: $v})
                  -[:COVERS_MODEL]->(m:Model {is_definition: true})
            RETURN collect(m.name) AS names
            """,
            v=TEST_VERSION,
        ).single()["names"]

    assert "sale.order" in covered_models, (
        "COVERS_MODEL edge to is_definition=true Model must exist"
    )
    assert "nonexistent.model" not in covered_models, (
        "No dangling COVERS_MODEL edge must be created for a model with no node"
    )


def test_reconcile_coverage_no_dangling_edges_for_unknown_ref(writer, neo4j_driver):
    """Business rule: unknown model reference -> zero COVERS_MODEL edges created (no dangling)."""
    tc = _make_test_class("TestNoDangling", "sale")
    tc.methods = [
        _make_test_method(
            "test_phantom", "TestNoDangling", "sale",
            model_refs=["phantom.model.that.does.not.exist"],
        ),
    ]
    result = _make_test_result("sale", [tc])
    writer.write_test_results([result])
    writer.reconcile_test_coverage(TEST_VERSION)

    with neo4j_driver.session() as s:
        count = s.run(
            """
            MATCH (:TestMethod {name: 'test_phantom', odoo_version: $v})-[:COVERS_MODEL]->()
            RETURN count(*) AS cnt
            """,
            v=TEST_VERSION,
        ).single()["cnt"]
    assert count == 0, f"No COVERS_MODEL edges for nonexistent model refs, got {count}"


# ---------------------------------------------------------------------------
# Test 6: framework TestHelper seeded per version
# ---------------------------------------------------------------------------

def test_framework_test_helpers_seeded_per_version(writer, neo4j_driver):
    """Business rule: framework TestHelper nodes (TransactionCase etc.) are seeded per version.

    Each Odoo version gets its own set of framework TestHelper nodes.
    They use module='@framework' (MED-3) and have no DEFINED_IN edge.
    """
    helpers = seed_framework_helpers(TEST_VERSION)
    writer.write_framework_test_helpers(helpers)

    with neo4j_driver.session() as s:
        rows = s.run(
            "MATCH (h:TestHelper {module: '@framework', odoo_version: $v}) "
            "RETURN collect(h.name) AS names",
            v=TEST_VERSION,
        ).single()
    names = rows["names"]
    assert "TransactionCase" in names
    assert "HttpCase" in names
    assert "SavepointCase" in names


def test_framework_test_helper_has_no_defined_in_edge(writer, neo4j_driver):
    """Business rule (MED-3): framework TestHelper nodes must NOT have DEFINED_IN edges."""
    helpers = seed_framework_helpers(TEST_VERSION)
    writer.write_framework_test_helpers(helpers)

    with neo4j_driver.session() as s:
        count = s.run(
            """
            MATCH (h:TestHelper {module: '@framework', odoo_version: $v})-[:DEFINED_IN]->()
            RETURN count(*) AS cnt
            """,
            v=TEST_VERSION,
        ).single()["cnt"]
    assert count == 0, (
        f"Framework TestHelper nodes must have NO DEFINED_IN edge (MED-3), got {count}"
    )


# ---------------------------------------------------------------------------
# Test 7: is_helper finalization (reconcile pass)
# ---------------------------------------------------------------------------

def test_finalize_is_helper_marks_classes_with_no_test_methods(writer, neo4j_driver):
    """Business rule: finalize_is_helper() marks is_helper=True on TestClass nodes
    that (a) define no test_ methods AND (b) are subclassed by at least one other TestClass.

    These become TestHelper projection nodes available for INHERITS_TEST resolution.
    A class must be subclassed to qualify: a standalone base with no children is NOT a helper.
    """
    # A class with no test_ methods (only setUp helpers), subclassed by tc_child
    # defines_no_test_methods=True is set by the parser; here we set it explicitly
    tc_helper = _make_test_class(
        "SaleTestCommon", "sale", test_type="transaction",
        defines_no_test_methods=True,
    )
    tc_helper.methods = [
        _make_test_method("setUpClass", "SaleTestCommon", "sale", via="setup"),
    ]  # no test_ method -> should be is_helper=True after finalize (once subclassed)

    # A class that inherits from SaleTestCommon (triggers the "subclassed" condition)
    tc_child = _make_test_class(
        "TestActualSale", "sale",
        file_path="tests/test_actual.py",
        base_classes_ordered=["SaleTestCommon"],
    )
    tc_child.methods = [
        _make_test_method("test_something", "TestActualSale", "sale",
                          file_path="tests/test_actual.py"),
    ]

    # A class with a test_ method that is NOT subclassed -> NOT is_helper
    tc_real = _make_test_class("TestStandaloneReal", "sale",
                               file_path="tests/test_standalone.py")
    tc_real.methods = [
        _make_test_method("test_something", "TestStandaloneReal", "sale",
                          file_path="tests/test_standalone.py"),
    ]

    result = _make_test_result("sale", [tc_helper, tc_child, tc_real])
    writer.write_test_results([result])
    # reconcile_test_inherits must run first so INHERITS_TEST edges exist for finalize_is_helper
    writer.reconcile_test_inherits(TEST_VERSION)
    writer.finalize_is_helper(TEST_VERSION)

    with neo4j_driver.session() as s:
        helper_row = s.run(
            "MATCH (c:TestClass {name: $n, odoo_version: $v}) RETURN c.is_helper AS is_helper",
            n="SaleTestCommon", v=TEST_VERSION,
        ).single()
        real_row = s.run(
            "MATCH (c:TestClass {name: $n, odoo_version: $v}) RETURN c.is_helper AS is_helper",
            n="TestStandaloneReal", v=TEST_VERSION,
        ).single()

    assert helper_row["is_helper"] is True, (
        "SaleTestCommon has no test_ methods and is subclassed "
        "-> finalize_is_helper must set is_helper=True"
    )
    assert not real_row["is_helper"], (
        "TestStandaloneReal has a test_ method -> is_helper must remain False"
    )


# ---------------------------------------------------------------------------
# Test 8: COVERS_* via property is set
# ---------------------------------------------------------------------------

def test_covers_model_edge_has_via_property(writer, neo4j_driver):
    """Business rule (HIGH-2): COVERS_MODEL edges carry a 'via' property.

    via is 'assert' for test_ methods, 'setup' for setUp, 'body' for others.
    """
    # Seed a Model node
    with neo4j_driver.session() as s:
        s.run(
            "MERGE (m:Model {name: $n, module: $mod, odoo_version: $v}) SET m.is_definition=true",
            n="res.partner", mod="base", v=TEST_VERSION,
        )

    tc = _make_test_class("TestViaCheck", "sale")
    tc.methods = [
        _make_test_method(
            "test_partner", "TestViaCheck", "sale",
            model_refs=["res.partner"],
            via="assert",
        ),
    ]
    result = _make_test_result("sale", [tc])
    writer.write_test_results([result])
    writer.reconcile_test_coverage(TEST_VERSION)

    with neo4j_driver.session() as s:
        row = s.run(
            """
            MATCH (:TestMethod {name: 'test_partner', odoo_version: $v})
                  -[r:COVERS_MODEL]->(:Model {name: 'res.partner', odoo_version: $v})
            RETURN r.via AS via
            """,
            v=TEST_VERSION,
        ).single()
    assert row is not None, "COVERS_MODEL edge must exist"
    # via property must be present on the edge
    assert row["via"] in ("assert", "setup", "body", None), (
        f"via must be a valid coverage category, got {row['via']!r}"
    )


# ---------------------------------------------------------------------------
# Test 9: gc_stale_test_nodes (--full reindex GC)
# ---------------------------------------------------------------------------

def test_gc_stale_test_nodes_removes_deleted_module(writer, neo4j_driver):
    """Business rule: gc_stale_test_nodes() removes TestClass/TestMethod nodes whose
    module name is not in the live module set (stale after --full reindex).

    live_module_names contains module NAMES (not filesystem paths) — implementation
    compares tc.module IN $live_modules.
    """
    # Write a test class for a module that will be 'gone' in the next full run
    tc_stale = _make_test_class("TestStaleClass", "gone_module",
                                file_path="tests/test_stale.py")
    tc_stale.methods = [
        _make_test_method("test_x", "TestStaleClass", "gone_module",
                          file_path="tests/test_stale.py"),
    ]
    # Write a test class for a module that stays live
    tc_live = _make_test_class("TestLiveClass", "live_module",
                               file_path="tests/test_live.py")

    writer.write_test_results([_make_test_result("gone_module", [tc_stale])])
    writer.write_test_results([_make_test_result("live_module", [tc_live])])

    # GC: only live_module is still present (pass module NAME, not filesystem path)
    writer.gc_stale_test_nodes(TEST_VERSION, live_module_names=["live_module"])

    with neo4j_driver.session() as s:
        live_cnt = s.run(
            "MATCH (c:TestClass {module: 'live_module', odoo_version: $v}) RETURN count(c) AS cnt",
            v=TEST_VERSION,
        ).single()["cnt"]
        stale_cnt = s.run(
            "MATCH (c:TestClass {module: 'gone_module', odoo_version: $v}) RETURN count(c) AS cnt",
            v=TEST_VERSION,
        ).single()["cnt"]

    assert live_cnt == 1, "Live module's TestClass must NOT be GC'd"
    assert stale_cnt == 0, "Stale module's TestClass must be removed by GC"


# ---------------------------------------------------------------------------
# Test 10 (Defect A): COVERS_METHOD edges are created by reconcile_test_coverage
# ---------------------------------------------------------------------------

def test_reconcile_coverage_covers_method_edges(writer, neo4j_driver):
    """Defect A fix: reconcile_test_coverage() must create COVERS_METHOD edges.

    RED-BEFORE-GREEN: before the fix, reconcile_test_coverage had no COVERS_METHOD
    block. A TestMethod with method_refs pointing at a real Method node on the
    is_definition Model produced ZERO COVERS_METHOD edges (always empty).

    Business rule: A TestMethod with method_refs=['action_confirm'] on a model
    'sale.order' must create COVERS_METHOD edges to the Method node
    (:Method {name='action_confirm', model='sale.order', is_definition=true}) ONLY
    (ADR-0048 K×D). Unknown method_refs must produce no dangling edges.

    Anti-tautology oracle: the expected edge targets are HUMAN-AUTHORED seed nodes
    (written directly, not fed from parser output), so any parser-to-writer
    tautology is broken.
    """
    # Seed the is_definition Model + two Method nodes for that model
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (m:Model {name: 'sale.order', module: 'sale', odoo_version: $v})
            SET m.is_definition = true
            MERGE (meth1:Method {name: 'action_confirm', model: 'sale.order',
                                 module: 'sale', odoo_version: $v})
            MERGE (meth2:Method {name: 'action_cancel', model: 'sale.order',
                                 module: 'sale', odoo_version: $v})
            """,
            v=TEST_VERSION,
        )

    # Write a TestMethod with method_refs targeting the seeded Method nodes
    tc = _make_test_class("TestCoverageMethod", "sale")
    tc.methods = [
        _make_test_method(
            "test_action_confirm",
            "TestCoverageMethod",
            "sale",
            model_refs=["sale.order"],   # establishes COVERS_MODEL link first
            method_refs=["action_confirm", "nonexistent_method"],
        ),
    ]
    result = _make_test_result("sale", [tc])
    writer.write_test_results([result])
    writer.reconcile_test_coverage(TEST_VERSION)

    with neo4j_driver.session() as s:
        covered_methods = s.run(
            """
            MATCH (:TestMethod {name: 'test_action_confirm', odoo_version: $v})
                  -[:COVERS_METHOD]->(meth:Method)
            RETURN collect(meth.name) AS names
            """,
            v=TEST_VERSION,
        ).single()["names"]
        dangling_cnt = s.run(
            """
            MATCH (:TestMethod {name: 'test_action_confirm', odoo_version: $v})
                  -[:COVERS_METHOD]->(meth:Method {name: 'nonexistent_method'})
            RETURN count(*) AS cnt
            """,
            v=TEST_VERSION,
        ).single()["cnt"]

    assert "action_confirm" in covered_methods, (
        "Defect A: COVERS_METHOD edge must be created for action_confirm -> "
        "reconcile_test_coverage was missing the COVERS_METHOD block"
    )
    assert "action_cancel" not in covered_methods, (
        "COVERS_METHOD must only link referenced methods, not all methods on the model"
    )
    assert dangling_cnt == 0, (
        "No COVERS_METHOD edge must be created for nonexistent_method (graceful-skip)"
    )


def test_reconcile_coverage_covers_method_idempotent(writer, neo4j_driver):
    """Defect A: COVERS_METHOD MERGE is idempotent — running reconcile twice yields one edge."""
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (m:Model {name: 'account.move', module: 'account', odoo_version: $v})
            SET m.is_definition = true
            MERGE (meth:Method {name: '_compute_amount', model: 'account.move',
                                module: 'account', odoo_version: $v})
            """,
            v=TEST_VERSION,
        )
    tc = _make_test_class("TestAccountMove", "account")
    tc.methods = [
        _make_test_method(
            "test_compute_amount",
            "TestAccountMove",
            "account",
            model_refs=["account.move"],
            method_refs=["_compute_amount"],
        ),
    ]
    writer.write_test_results([_make_test_result("account", [tc])])
    writer.reconcile_test_coverage(TEST_VERSION)
    writer.reconcile_test_coverage(TEST_VERSION)  # second run

    with neo4j_driver.session() as s:
        cnt = s.run(
            """
            MATCH (:TestMethod {name: 'test_compute_amount', odoo_version: $v})
                  -[:COVERS_METHOD]->(:Method {name: '_compute_amount', odoo_version: $v})
            RETURN count(*) AS cnt
            """,
            v=TEST_VERSION,
        ).single()["cnt"]
    assert cnt == 1, (
        f"Idempotent MERGE: expected exactly 1 COVERS_METHOD edge after two reconcile runs, "
        f"got {cnt}"
    )


# ---------------------------------------------------------------------------
# Test 11 (Defect I): incremental GC preserves unchanged module nodes
# ---------------------------------------------------------------------------

def test_gc_file_prune_preserves_unchanged_module(writer, neo4j_driver):
    """Defect I fix: file-level prune must NOT delete unchanged-module nodes on incremental.

    RED-BEFORE-GREEN: before the fix, live_module_names (full registry) was passed
    as the file-level prune scope. On an incremental run, unchanged modules never
    re-emit test files, so their file_paths are absent from live_test_files.
    The query `WHERE module IN $live_modules AND NOT file_path IN $live_files` would
    then delete ALL TestClass/TestMethod nodes for unchanged modules.

    Business rule: if module M2 was NOT re-parsed this run (not in test_results),
    its TestClass/TestMethod nodes must survive the GC call, even when M2 is in
    live_module_names. Only module M1 (which WAS re-parsed) should have its
    stale-file nodes pruned.
    """
    # Module M1: will be re-parsed (in test_results). M1 has two test files;
    # after the incremental run only one file is "alive".
    tc_m1_file1 = _make_test_class("TestM1A", "module_m1", file_path="tests/test_a.py")
    tc_m1_file1.methods = [
        _make_test_method("test_a", "TestM1A", "module_m1", file_path="tests/test_a.py"),
    ]
    tc_m1_file2 = _make_test_class("TestM1B", "module_m1", file_path="tests/test_b.py")
    tc_m1_file2.methods = [
        _make_test_method("test_b", "TestM1B", "module_m1", file_path="tests/test_b.py"),
    ]
    # Module M2: UNCHANGED — not re-parsed this run.
    tc_m2 = _make_test_class("TestM2", "module_m2", file_path="tests/test_m2.py")
    tc_m2.methods = [
        _make_test_method("test_m2", "TestM2", "module_m2", file_path="tests/test_m2.py"),
    ]

    writer.write_test_results([_make_test_result("module_m1", [tc_m1_file1, tc_m1_file2])])
    writer.write_test_results([_make_test_result("module_m2", [tc_m2])])

    # Simulate incremental GC: M1 was re-parsed (only test_a.py survived; test_b.py deleted).
    # M2 was NOT re-parsed at all.
    # - live_module_names: FULL registry (both M1 and M2 are still on disk).
    # - live_file_paths: only the files emitted by the re-parsed modules this run (only M1's).
    # - live_modules_for_file_gc: only M1 (the re-parsed subset).
    writer.gc_stale_test_nodes(
        TEST_VERSION,
        live_module_names=["module_m1", "module_m2"],  # full registry
        live_file_paths=["tests/test_a.py"],           # only M1's surviving file
        live_modules_for_file_gc=["module_m1"],        # only re-parsed module
    )

    with neo4j_driver.session() as s:
        # M1 stale file (test_b.py) must be pruned
        m1_file2_cnt = s.run(
            "MATCH (c:TestClass {name: 'TestM1B', odoo_version: $v}) RETURN count(c) AS cnt",
            v=TEST_VERSION,
        ).single()["cnt"]
        # M1 live file (test_a.py) must survive
        m1_file1_cnt = s.run(
            "MATCH (c:TestClass {name: 'TestM1A', odoo_version: $v}) RETURN count(c) AS cnt",
            v=TEST_VERSION,
        ).single()["cnt"]
        # M2 nodes must survive (M2 was not re-parsed — its files were never emitted)
        m2_cnt = s.run(
            "MATCH (c:TestClass {name: 'TestM2', odoo_version: $v}) RETURN count(c) AS cnt",
            v=TEST_VERSION,
        ).single()["cnt"]

    assert m1_file2_cnt == 0, (
        "Defect I: M1's deleted test file (test_b.py) must be pruned from GC"
    )
    assert m1_file1_cnt == 1, "M1's surviving test file (test_a.py) must NOT be pruned"
    assert m2_cnt == 1, (
        "Defect I: M2 was NOT re-parsed this run; its TestClass nodes must survive. "
        "Before the fix, the full live_module_names scope caused M2's nodes to be "
        "deleted because test_m2.py was absent from live_file_paths."
    )


# ---------------------------------------------------------------------------
# Test 12 (Defect H): repo-scoped GC does not delete another repo's nodes
# ---------------------------------------------------------------------------

def test_gc_repo_scoping_preserves_other_repo_nodes(writer, neo4j_driver):
    """Defect H fix: gc_stale_test_nodes() scoped to repo_A must NOT delete repo_B nodes.

    RED-BEFORE-GREEN: before the fix, gc_stale_test_nodes had no repo filter.
    In a multi-repo profile (e.g. odoo + enterprise both having 'sale' at 17.0),
    the per-repo GC call for repo_A with live_modules=['sale'] would match
    EVERY TestClass for 'sale' at that version regardless of which repo wrote them.
    If repo_B's 'sale' module happened to be absent from repo_A's live_modules
    (which is impossible here but the prune is repo-unscoped), any module unique
    to repo_B could be deleted.

    This test uses the module-level prune: repo_A has ['base'] as its live modules
    (repo_A's 'sale' was removed). Without repo-scoping, the GC for repo_A would
    also match repo_B's TestClass nodes (which have module='sale') because 'sale'
    is NOT in repo_A's live_modules. With repo-scoping, only repo_A's nodes for
    module='sale' are deleted; repo_B's are untouched.
    """
    # repo_A has 'sale' and 'base'; after GC only 'base' remains (sale removed).
    tc_a_sale = _make_test_class("TestSale", "sale", file_path="tests/test_sale.py")
    tc_a_base = _make_test_class("TestBase", "base", file_path="tests/test_base.py")
    # repo_B also has 'sale' (same module name, different repo — real multi-repo scenario).
    tc_b_sale = _make_test_class("TestSale", "sale", file_path="tests/test_sale.py")

    writer.write_test_results([_make_test_result("sale", [tc_a_sale], repo="repo_a")])
    writer.write_test_results([_make_test_result("base", [tc_a_base], repo="repo_a")])
    writer.write_test_results([_make_test_result("sale", [tc_b_sale], repo="repo_b")])

    # GC for repo_a: sale module removed from repo_a (only base remains).
    writer.gc_stale_test_nodes(
        TEST_VERSION,
        live_module_names=["base"],   # repo_a's remaining modules
        repo="repo_a",                # scope to repo_a only
    )

    with neo4j_driver.session() as s:
        # repo_a's sale TestClass must be deleted (not in live_modules AND repo=repo_a)
        a_sale_cnt = s.run(
            "MATCH (c:TestClass {name: 'TestSale', repo: 'repo_a', odoo_version: $v}) "
            "RETURN count(c) AS cnt",
            v=TEST_VERSION,
        ).single()["cnt"]
        # repo_a's base TestClass must survive (still live)
        a_base_cnt = s.run(
            "MATCH (c:TestClass {name: 'TestBase', repo: 'repo_a', odoo_version: $v}) "
            "RETURN count(c) AS cnt",
            v=TEST_VERSION,
        ).single()["cnt"]
        # repo_b's sale TestClass must survive (different repo, untouched)
        b_sale_cnt = s.run(
            "MATCH (c:TestClass {name: 'TestSale', repo: 'repo_b', odoo_version: $v}) "
            "RETURN count(c) AS cnt",
            v=TEST_VERSION,
        ).single()["cnt"]

    assert a_sale_cnt == 0, (
        "repo_a's removed 'sale' module TestClass must be GC'd"
    )
    assert a_base_cnt == 1, "repo_a's 'base' module TestClass must survive"
    assert b_sale_cnt == 1, (
        "Defect H: repo_b's 'sale' TestClass must NOT be deleted by repo_a's GC. "
        "Before the fix, the unscoped query matched all 'sale' nodes at the version, "
        "so repo_b's nodes would be deleted when 'sale' was not in repo_a's live_modules."
    )
