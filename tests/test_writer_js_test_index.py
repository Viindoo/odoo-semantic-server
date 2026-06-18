# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for WI-3 JS test writer (writer_neo4j.py _write_js_test_batch).

Requires Neo4j (testcontainers or local bolt://localhost:7687).
Mark: pytest.mark.neo4j.

All data uses TEST_VERSION='99.0' and clean_neo4j fixture to avoid conflict
with real indexed data.

MED-1 CONTRACT TESTS: explicitly assert that NO JsTestSuite-[:COVERS_MODEL]->Model
edge exists (the main design invariant for WI-3: mock models must not produce
real coverage edges).
"""
import os

import pytest

from src.indexer.models import JsTestSuiteInfo
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


# ---------------------------------------------------------------------------
# Fixtures
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


def _make_module(name: str, writer: Neo4jWriter) -> None:
    """Insert a Module node so JsTestSuite DEFINED_IN edge can be MERGE'd."""
    with writer.driver.session() as session:
        session.run(
            """
            MERGE (m:Module {name: $name, odoo_version: $ver})
            SET m.profile = ['test']
            """,
            name=name, ver=TEST_VERSION,
        )


def _make_suite(
    file_path: str,
    module: str,
    framework: str = "hoot",
    describe_blocks: list | None = None,
    test_names: list | None = None,
    tags: list | None = None,
    mounts: list | None = None,
    mock_models: list | None = None,
) -> JsTestSuiteInfo:
    return JsTestSuiteInfo(
        file_path=file_path,
        module=module,
        odoo_version=TEST_VERSION,
        framework=framework,
        describe_blocks=describe_blocks or [],
        test_names=test_names or [],
        tags=tags or [],
        mounts=mounts or [],
        mock_models=mock_models or [],
        line=1,
    )


# ---------------------------------------------------------------------------
# Basic persistence
# ---------------------------------------------------------------------------

def test_js_test_suite_node_created_file_grained(writer):
    """Business rule: write_js_test_results creates one JsTestSuite node per file."""
    _make_module("account", writer)
    suite = _make_suite(
        "account/static/tests/move.test.js",
        "account",
        framework="hoot",
        test_names=["renders move"],
    )
    writer.write_js_test_results([suite], profiles=["test"])

    with writer.driver.session() as session:
        row = session.run(
            "MATCH (js:JsTestSuite {file_path: $fp, odoo_version: $ver}) RETURN js",
            fp="account/static/tests/move.test.js", ver=TEST_VERSION,
        ).single()
    assert row is not None
    js = row["js"]
    assert js["framework"] == "hoot"
    assert "renders move" in js["test_names"]


def test_js_test_suite_framework_stored_correctly_hoot_vs_qunit(writer):
    """Business rule: framework='hoot' vs 'qunit' is stored and retrievable."""
    _make_module("sale", writer)
    hoot_suite = _make_suite("sale/static/tests/sale.test.js", "sale", framework="hoot")
    qunit_suite = _make_suite("sale/static/tests/sale_tests.js", "sale", framework="qunit")

    writer.write_js_test_results([hoot_suite, qunit_suite], profiles=["test"])

    with writer.driver.session() as session:
        rows = session.run(
            "MATCH (js:JsTestSuite {module: $mod, odoo_version: $ver}) "
            "RETURN js.framework AS fw ORDER BY fw",
            mod="sale", ver=TEST_VERSION,
        ).data()
    frameworks = [r["fw"] for r in rows]
    assert "hoot" in frameworks
    assert "qunit" in frameworks


# ---------------------------------------------------------------------------
# Idempotency / profile union
# ---------------------------------------------------------------------------

def test_idempotent_reindex_creates_one_node_with_union_profile(writer):
    """Business rule: indexing the same JsTestSuite twice -> one node, profile[] union-only."""
    _make_module("account", writer)
    suite = _make_suite("account/static/tests/foo.test.js", "account")

    writer.write_js_test_results([suite], profiles=["profile_a"])
    writer.write_js_test_results([suite], profiles=["profile_b"])

    with writer.driver.session() as session:
        rows = session.run(
            "MATCH (js:JsTestSuite {file_path: $fp, odoo_version: $ver}) RETURN js",
            fp="account/static/tests/foo.test.js", ver=TEST_VERSION,
        ).data()
    # Exactly one node
    assert len(rows) == 1
    profile = rows[0]["js"]["profile"]
    assert "profile_a" in profile
    assert "profile_b" in profile


def test_profile_union_does_not_duplicate_existing_entries(writer):
    """Business rule: re-indexing with the same profile does not duplicate profile entries."""
    _make_module("account", writer)
    suite = _make_suite("account/static/tests/bar.test.js", "account")

    writer.write_js_test_results([suite], profiles=["p1"])
    writer.write_js_test_results([suite], profiles=["p1"])

    with writer.driver.session() as session:
        rows = session.run(
            "MATCH (js:JsTestSuite {file_path: $fp, odoo_version: $ver}) RETURN js.profile AS p",
            fp="account/static/tests/bar.test.js", ver=TEST_VERSION,
        ).data()
    assert rows[0]["p"].count("p1") == 1


# ---------------------------------------------------------------------------
# MED-1 CONTRACT: NO JsTestSuite-[:COVERS_MODEL]->Model edge
# ---------------------------------------------------------------------------

def test_no_js_test_suite_to_model_covers_edge_exists(writer):
    """MED-1 contract: zero COVERS_MODEL edges from JsTestSuite to any Model node.

    This is the PRIMARY invariant of WI-3: mock_models in Hoot tests are
    hand-rolled test-doubles, not real Odoo models. The writer MUST NOT emit
    a COVERS_MODEL edge even when mock_models[] contains recognizable model names.
    """
    _make_module("account", writer)
    # Create a real Model node to verify no edge is created to it
    with writer.driver.session() as session:
        session.run(
            """
            MERGE (m:Model {name: 'account.account', module: 'account', odoo_version: $ver})
            SET m.is_definition = true, m.profile = ['test']
            """,
            ver=TEST_VERSION,
        )

    # Suite that mentions account.account as a mock model
    suite = _make_suite(
        "account/static/tests/char.test.js",
        "account",
        framework="hoot",
        mock_models=["account.account"],  # hand-rolled mock - should NOT produce edge
        mounts=["account.account"],       # real mountView call - also no edge (WI-3 decision)
    )
    writer.write_js_test_results([suite], profiles=["test"])

    # Assert: ZERO COVERS_MODEL edges from any JsTestSuite
    with writer.driver.session() as session:
        row = session.run(
            """
            MATCH (js:JsTestSuite {odoo_version: $ver})-[:COVERS_MODEL]->(m:Model)
            RETURN count(*) AS cnt
            """,
            ver=TEST_VERSION,
        ).single()
    assert row["cnt"] == 0, (
        "MED-1 violated: found COVERS_MODEL edges from JsTestSuite -> Model. "
        "JS mock models must NOT produce coverage edges."
    )


def test_no_covers_model_edge_even_with_mounts_populated(writer):
    """MED-1 extension: mounts[] in JsTestSuite also must NOT produce COVERS_MODEL edges.

    Decision from design §4.4 + debate MED-1: no JS->Model coverage edges at all
    (Hoot uses hand-rolled mock models; a real-model edge would be semantically false).
    """
    _make_module("sale", writer)
    # Create a Model node to verify no edge
    with writer.driver.session() as session:
        session.run(
            "MERGE (m:Model {name: 'sale.order', module: 'sale', odoo_version: $ver}) "
            "SET m.is_definition = true, m.profile = ['test']",
            ver=TEST_VERSION,
        )

    suite = _make_suite(
        "sale/static/tests/sale.test.js",
        "sale",
        framework="hoot",
        mounts=["sale.order"],
    )
    writer.write_js_test_results([suite], profiles=["test"])

    with writer.driver.session() as session:
        row = session.run(
            "MATCH (js:JsTestSuite)-[:COVERS_MODEL]->(m:Model {odoo_version: $ver}) "
            "RETURN count(*) AS cnt",
            ver=TEST_VERSION,
        ).single()
    assert row["cnt"] == 0, (
        "mounts[] in JsTestSuite must not produce COVERS_MODEL edges (WI-3 design §4.4)."
    )


# ---------------------------------------------------------------------------
# Data integrity: describe_blocks, test_names, tags, mock_models stored
# ---------------------------------------------------------------------------

def test_all_fields_persisted_correctly(writer):
    """Business rule: all JsTestSuiteInfo fields are stored without loss."""
    _make_module("account", writer)
    suite = _make_suite(
        "account/static/tests/x2many.test.js",
        "account",
        framework="hoot",
        describe_blocks=["X2many buttons"],
        test_names=["renders add line", "handles keyboard input"],
        tags=["desktop"],
        mounts=["account.move"],
        mock_models=["account.account"],
    )
    writer.write_js_test_results([suite], profiles=["test"])

    with writer.driver.session() as session:
        row = session.run(
            "MATCH (js:JsTestSuite {file_path: $fp, odoo_version: $ver}) RETURN js",
            fp="account/static/tests/x2many.test.js", ver=TEST_VERSION,
        ).single()
    js = row["js"]
    assert "X2many buttons" in js["describe_blocks"]
    assert "renders add line" in js["test_names"]
    assert "handles keyboard input" in js["test_names"]
    assert "desktop" in js["tags"]
    assert "account.move" in js["mounts"]
    assert "account.account" in js["mock_models"]


def test_tour_suite_stored_with_tour_framework(writer):
    """Business rule: tour JsTestSuiteInfo is stored with framework='tour'."""
    _make_module("website", writer)
    suite = _make_suite(
        "website/static/tests/tours/checkout.js",
        "website",
        framework="tour",
        test_names=["shop_checkout"],  # tour names stored in test_names
    )
    writer.write_js_test_results([suite], profiles=["test"])

    with writer.driver.session() as session:
        row = session.run(
            "MATCH (js:JsTestSuite {framework: 'tour', odoo_version: $ver}) RETURN js",
            ver=TEST_VERSION,
        ).single()
    assert row is not None
    assert row["js"]["framework"] == "tour"
    assert "shop_checkout" in row["js"]["test_names"]
