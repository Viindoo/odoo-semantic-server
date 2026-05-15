# tests/test_inherits_order_invariant.py
#
# Invariant: every INHERITS edge written by the indexer MUST have an `order`
# property.  A NULL order breaks mixin-injection order tracking (ADR-0013)
# and the ranking heuristic in resolve_model.
#
# F4 root cause: the old `WHERE NOT EXISTS` guard at writer_neo4j.py:85-93
# blocked backfilling `r.order` on existing edges — the guard has been
# replaced with MERGE + ON MATCH SET coalesce semantics.
#
# This file covers two scenarios:
#   1. Writer test — index two modules that both extend the same model; assert
#      both INHERITS edges have `order` set (multi-module extension case that
#      the old guard broke).
#   2. Invariant query — after indexing, assert zero INHERITS edges have NULL
#      order in the TEST_VERSION namespace.

import os

import pytest

from src.indexer.models import FieldInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter connected to the test Neo4j instance."""
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _make_result(module_name: str, model_name: str,
                 inherit: list[str] | None = None) -> ParseResult:
    """Build a minimal ParseResult for testing INHERITS edges."""
    module = ModuleInfo(
        name=module_name, odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo", path="/tmp",
        depends=[], version_raw="",
    )
    model = ModelInfo(
        name=model_name, module=module_name, odoo_version=TEST_VERSION,
        fields=[FieldInfo(name="id", ttype="integer")],
        methods=[],
        inherit=inherit or [],
    )
    return ParseResult(module=module, models=[model])


# ---------------------------------------------------------------------------
# Test 1: multi-module extension — both edges must have order set
# ---------------------------------------------------------------------------

def test_multi_module_inherits_both_edges_have_order(writer, neo4j_driver):
    """Two modules extending the same model → both INHERITS edges get order.

    The old guard `WHERE NOT EXISTS (...)-[:INHERITS]->(tip)` would create the
    first edge correctly but silently skip the second one (no edge, no order).
    The MERGE-based fix creates both edges idempotently and always sets order.
    """
    # Module A is the canonical definition of account.move
    result_def = _make_result("account", "account.move")
    writer.write_results([result_def])

    # Module B extends account.move (self-inherit pattern)
    result_b = _make_result("account_analytic", "account.move")
    result_b.models[0].inherit = ["account.move"]
    writer.write_results([result_b])

    # Module C also extends account.move (second extender — old guard would skip this)
    result_c = _make_result("account_budget", "account.move")
    result_c.models[0].inherit = ["account.move"]
    writer.write_results([result_c])

    with neo4j_driver.session() as session:
        rows = session.run(
            """
            MATCH (src:Model {odoo_version: $v})-[r:INHERITS]->(tgt:Model {odoo_version: $v})
            WHERE tgt.name = 'account.move'
            RETURN src.module AS src_mod, r.order AS order_val
            """,
            v=TEST_VERSION,
        ).data()

    assert len(rows) >= 2, (
        f"Expected at least 2 INHERITS edges to account.move, got {len(rows)}. "
        "Old guard would have blocked the second edge entirely."
    )
    null_order_rows = [r for r in rows if r["order_val"] is None]
    assert null_order_rows == [], (
        f"INHERITS edges with NULL order: {null_order_rows}. "
        "Every edge must have r.order set."
    )


# ---------------------------------------------------------------------------
# Test 2: cross-module inherit (non-self) — order must be set
# ---------------------------------------------------------------------------

def test_cross_module_inherits_order_set(writer, neo4j_driver):
    """Model inheriting from a different model name → r.order must be set."""
    # sale.order is the base
    result_base = _make_result("sale", "sale.order")
    writer.write_results([result_base])

    # sale_subscription extends sale.order
    result_ext = _make_result("sale_subscription", "sale.subscription.order")
    result_ext.models[0].inherit = ["sale.order"]
    writer.write_results([result_ext])

    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (src:Model {name: 'sale.subscription.order',
                              module: 'sale_subscription', odoo_version: $v})
                  -[r:INHERITS]->
                  (tgt:Model {name: 'sale.order', odoo_version: $v})
            RETURN r.order AS order_val
            """,
            v=TEST_VERSION,
        ).single()

    assert row is not None, "INHERITS edge not created"
    assert row["order_val"] is not None, (
        f"r.order is NULL on INHERITS edge. Got: {row['order_val']}"
    )
    assert row["order_val"] == 0, (
        f"Expected order=0 (first/only parent), got {row['order_val']}"
    )


# ---------------------------------------------------------------------------
# Test 3: ON MATCH backfill — existing edge with NULL order gets patched
# ---------------------------------------------------------------------------

def test_on_match_backfills_null_order(writer, neo4j_driver):
    """Existing edge without order gets backfilled on re-index (ON MATCH coalesce).

    Simulates the production scenario: stale edge created without order,
    then re-indexed via the fixed writer.
    """
    # Pre-seed a stale INHERITS edge with NULL order (mimics pre-ADR-0013 data)
    with neo4j_driver.session() as session:
        session.run(
            """
            MERGE (src:Model {name: 'res.partner', module: 'contacts_ext',
                               odoo_version: $v})
            MERGE (tgt:Model {name: 'res.partner', module: 'base',
                               odoo_version: $v})
            MERGE (src)-[r:INHERITS]->(tgt)
            """,
            v=TEST_VERSION,
        )
        # Confirm order is NULL before re-index
        row = session.run(
            """
            MATCH (src:Model {name: 'res.partner', module: 'contacts_ext',
                               odoo_version: $v})
                  -[r:INHERITS]->
                  (tgt:Model {name: 'res.partner', odoo_version: $v})
            RETURN r.order AS order_val
            """,
            v=TEST_VERSION,
        ).single()
        assert row is not None
        assert row["order_val"] is None, "Pre-condition: order should be NULL before backfill"

    # Now re-index contacts_ext (writer should backfill order via ON MATCH coalesce)
    result_ext = _make_result("contacts_ext", "res.partner")
    result_ext.models[0].inherit = ["res.partner"]
    writer.write_results([result_ext])

    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (src:Model {name: 'res.partner', module: 'contacts_ext',
                               odoo_version: $v})
                  -[r:INHERITS]->
                  (tgt:Model {name: 'res.partner', odoo_version: $v})
            RETURN r.order AS order_val
            """,
            v=TEST_VERSION,
        ).single()

    assert row is not None, "INHERITS edge disappeared after re-index"
    assert row["order_val"] is not None, (
        "ON MATCH coalesce failed: order still NULL after re-indexing stale edge. "
        "Expected r.order to be backfilled to 0."
    )


# ---------------------------------------------------------------------------
# Test 4: invariant query — zero INHERITS edges with NULL order after indexing
# ---------------------------------------------------------------------------

def test_no_null_order_inherits_edges_after_index(writer, neo4j_driver):
    """After a complete indexing pass, MATCH ()-[r:INHERITS]->() WHERE r.order IS NULL
    RETURN count(r) must be 0.

    This is the key invariant from CRIT-2B / F4: the writer must ALWAYS set
    r.order — whether creating a new edge (ON CREATE) or encountering an
    existing one (ON MATCH coalesce).
    """
    # Index several modules with various inherit patterns
    results = [
        _make_result("base", "res.partner"),
        _make_result("mail", "mail.thread"),
    ]
    # mail.thread.blacklist extends mail.thread (cross-module non-self)
    ext_result = _make_result("mass_mailing", "mail.thread.blacklist")
    ext_result.models[0].inherit = ["mail.thread"]
    results.append(ext_result)

    # Module extending res.partner (self-inherit)
    rp_ext = _make_result("contacts", "res.partner")
    rp_ext.models[0].inherit = ["res.partner"]
    results.append(rp_ext)

    for r in results:
        writer.write_results([r])

    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH ()-[r:INHERITS]->()
            WHERE r.order IS NULL
              AND EXISTS { MATCH ()-[r]->(:Model {odoo_version: $v}) }
            RETURN count(r) AS null_count
            """,
            v=TEST_VERSION,
        ).single()

    null_count = row["null_count"] if row else 0
    assert null_count == 0, (
        f"Found {null_count} INHERITS edge(s) with NULL order after indexing. "
        "All edges must have r.order set (ADR-0013 / CRIT-2B invariant)."
    )
