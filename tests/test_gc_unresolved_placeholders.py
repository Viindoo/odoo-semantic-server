# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_gc_unresolved_placeholders.py
"""Regression tests for __unresolved__ placeholder GC and shadow-View prevention.

Covers ADR-0007 §D5 extension:
  (i)  Writer fix: indexing a View whose parent was previously unresolved does NOT
       leave a shadow node after the fix (placeholder key now converges with real key).
  (ii) gc_unresolved_placeholders deletes inert placeholder nodes scoped by version.
  (iii) gc_unresolved_placeholders preserves real (non-placeholder) nodes.
"""
import os

import pytest

from src.indexer.models import (
    ModuleInfo,
    QWebInfo,
    ViewInfo,
    ViewParseResult,
)
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


# ---------------------------------------------------------------------------
# Shared writer fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter connected to isolated test DB via TEST_VERSION."""
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_module(name: str = "sale") -> ModuleInfo:
    return ModuleInfo(
        name=name, odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp",
        depends=[], version_raw="",
    )


def _view_count(driver, xmlid: str) -> int:
    """Return how many View nodes exist for this xmlid+version."""
    with driver.session() as session:
        row = session.run(
            "MATCH (v:View {xmlid: $xmlid, odoo_version: $v}) RETURN count(v) AS n",
            xmlid=xmlid, v=TEST_VERSION,
        ).single()
    return row["n"] if row else 0


def _qweb_count(driver, xmlid: str) -> int:
    """Return how many QWebTmpl nodes exist for this xmlid+version."""
    with driver.session() as session:
        row = session.run(
            "MATCH (t:QWebTmpl {xmlid: $xmlid, odoo_version: $v}) RETURN count(t) AS n",
            xmlid=xmlid, v=TEST_VERSION,
        ).single()
    return row["n"] if row else 0


# ---------------------------------------------------------------------------
# Test 1: No shadow View after real View is indexed (writer fix regression)
# ---------------------------------------------------------------------------

class TestNoShadowViewAfterRealViewIndexed:
    """Indexing the child view (which references an unknown parent) then later
    indexing the parent must result in exactly ONE View node — not two."""

    def test_no_shadow_view(self, writer, clean_neo4j):
        """Step 1: index child that references unknown parent → placeholder created.
        Step 2: index the parent → must converge on the same node (no shadow).
        """
        driver = clean_neo4j
        child_xmlid = "sale.view_sale_order_form_ext"
        parent_xmlid = "sale.view_sale_order_form"

        # --- Step 1: index child view whose parent is not yet indexed ---
        child_module = _make_module("sale_ext")
        child_view = ViewInfo(
            xmlid=child_xmlid,
            name="Sale Order Form Extension",
            model="sale.order",
            module="sale_ext",
            odoo_version=TEST_VERSION,
            view_type="form",
            mode="extension",
            inherit_xmlid=parent_xmlid,
        )
        writer.write_view_results(
            [ViewParseResult(module=child_module, views=[child_view])],
            profiles=["test_profile"],
        )

        # After step 1: the placeholder for parent_xmlid must exist
        assert _view_count(driver, parent_xmlid) == 1, (
            "Step 1: placeholder for parent_xmlid must exist"
        )

        # --- Step 2: index the parent view ---
        parent_module = _make_module("sale")
        parent_view = ViewInfo(
            xmlid=parent_xmlid,
            name="Sale Order Form",
            model="sale.order",
            module="sale",
            odoo_version=TEST_VERSION,
            view_type="form",
            mode="primary",
            inherit_xmlid=None,
        )
        writer.write_view_results(
            [ViewParseResult(module=parent_module, views=[parent_view])],
            profiles=["test_profile"],
        )

        # After step 2: must be exactly ONE View node for parent_xmlid (no shadow)
        count = _view_count(driver, parent_xmlid)
        assert count == 1, (
            f"Expected exactly 1 View node for {parent_xmlid} after real view indexed, "
            f"got {count}. Shadow detected — MERGE key divergence is the likely cause."
        )

    def test_no_shadow_qweb(self, writer, clean_neo4j):
        """Same no-shadow contract for QWebTmpl nodes."""
        driver = clean_neo4j
        child_xmlid = "sale.qweb_child"
        parent_xmlid = "sale.qweb_parent"

        child_module = _make_module("sale_ext")
        child_qweb = QWebInfo(
            xmlid=child_xmlid,
            module="sale_ext",
            odoo_version=TEST_VERSION,
            inherit_xmlid=parent_xmlid,
        )
        writer.write_view_results(
            [ViewParseResult(module=child_module, qweb=[child_qweb])],
            profiles=["test_profile"],
        )

        # placeholder must exist
        assert _qweb_count(driver, parent_xmlid) == 1

        parent_module = _make_module("sale")
        parent_qweb = QWebInfo(
            xmlid=parent_xmlid,
            module="sale",
            odoo_version=TEST_VERSION,
            inherit_xmlid=None,
        )
        writer.write_view_results(
            [ViewParseResult(module=parent_module, qweb=[parent_qweb])],
            profiles=["test_profile"],
        )

        count = _qweb_count(driver, parent_xmlid)
        assert count == 1, (
            f"Expected exactly 1 QWebTmpl node for {parent_xmlid} after real template "
            f"indexed, got {count}. Shadow detected."
        )


# ---------------------------------------------------------------------------
# Test 2: gc_unresolved_placeholders removes inert placeholder nodes
# ---------------------------------------------------------------------------

class TestGcUnresolvedRemovesPlaceholders:
    """gc_unresolved_placeholders must delete placeholder nodes and leave
    real nodes untouched."""

    def test_gc_removes_model_placeholders(self, writer, clean_neo4j):
        """Manually seed a placeholder Model node; gc must remove it."""
        driver = clean_neo4j

        with driver.session() as session:
            session.run("""
                MERGE (n:Model {name: $name, module: '__unresolved__', odoo_version: $v})
                SET n.unresolved = true, n.is_definition = false
            """, name="nonexistent.parent", v=TEST_VERSION)

        counts = writer.gc_unresolved_placeholders(TEST_VERSION)

        assert counts.get("Model", 0) >= 1, (
            "gc_unresolved_placeholders must delete placeholder Model node"
        )
        with driver.session() as session:
            row = session.run(
                "MATCH (n:Model {name: $name, odoo_version: $v}) RETURN count(n) AS c",
                name="nonexistent.parent", v=TEST_VERSION,
            ).single()
        assert row["c"] == 0, "Placeholder Model node must be gone after gc"

    def test_gc_removes_view_placeholders(self, writer, clean_neo4j):
        """Manually seed a placeholder View node; gc must remove it."""
        driver = clean_neo4j

        with driver.session() as session:
            session.run("""
                MERGE (n:View {xmlid: $xmlid, odoo_version: $v})
                SET n.module = '__unresolved__', n.unresolved = true
            """, xmlid="base.view_ghost_form", v=TEST_VERSION)

        counts = writer.gc_unresolved_placeholders(TEST_VERSION)

        assert counts.get("View", 0) >= 1, (
            "gc_unresolved_placeholders must delete placeholder View node"
        )
        with driver.session() as session:
            row = session.run(
                "MATCH (n:View {xmlid: $xmlid, odoo_version: $v}) RETURN count(n) AS c",
                xmlid="base.view_ghost_form", v=TEST_VERSION,
            ).single()
        assert row["c"] == 0

    def test_gc_preserves_real_nodes(self, writer, clean_neo4j):
        """gc_unresolved_placeholders must NOT delete real (non-placeholder) nodes."""
        driver = clean_neo4j

        with driver.session() as session:
            # Real View node: module is 'sale', unresolved is absent
            session.run("""
                MERGE (n:View {xmlid: $xmlid, odoo_version: $v})
                SET n.module = 'sale', n.name = 'Sale Form'
            """, xmlid="sale.view_real_form", v=TEST_VERSION)

        counts = writer.gc_unresolved_placeholders(TEST_VERSION)

        # gc must delete 0 nodes (no placeholders present)
        assert counts.get("View", 0) == 0, (
            "gc_unresolved_placeholders must NOT delete real View nodes"
        )
        with driver.session() as session:
            row = session.run(
                "MATCH (n:View {xmlid: $xmlid, odoo_version: $v}) RETURN count(n) AS c",
                xmlid="sale.view_real_form", v=TEST_VERSION,
            ).single()
        assert row["c"] == 1, "Real View node must survive gc"

    def test_gc_is_idempotent(self, writer, clean_neo4j):
        """Running gc_unresolved_placeholders twice returns 0 on the second run."""
        driver = clean_neo4j

        with driver.session() as session:
            session.run("""
                MERGE (n:Model {name: $name, module: '__unresolved__', odoo_version: $v})
                SET n.unresolved = true, n.is_definition = false
            """, name="idempotent.target", v=TEST_VERSION)

        first = writer.gc_unresolved_placeholders(TEST_VERSION)
        second = writer.gc_unresolved_placeholders(TEST_VERSION)

        assert first.get("Model", 0) >= 1, "First run must delete placeholder"
        assert sum(second.values()) == 0, "Second run must delete 0 (idempotent)"

    def test_gc_scoped_by_version(self, writer, clean_neo4j, neo4j_driver):
        """gc_unresolved_placeholders must NOT touch placeholders from other versions."""
        other_version = "98.0"  # also a test-sentinel; clean_neo4j only clears TEST_VERSION

        # Seed a placeholder at OTHER version — must survive gc on TEST_VERSION
        with neo4j_driver.session() as session:
            session.run("""
                MERGE (n:Model {name: $name, module: '__unresolved__', odoo_version: $v})
                SET n.unresolved = true, n.is_definition = false
            """, name="other.version.model", v=other_version)

        try:
            counts = writer.gc_unresolved_placeholders(TEST_VERSION)
            assert counts.get("Model", 0) == 0, (
                "gc scoped to TEST_VERSION must not touch other-version placeholders"
            )
            with neo4j_driver.session() as session:
                row = session.run(
                    "MATCH (n:Model {name: $name, odoo_version: $v}) RETURN count(n) AS c",
                    name="other.version.model", v=other_version,
                ).single()
            assert row["c"] == 1, (
                "Placeholder at other_version must survive gc scoped to TEST_VERSION"
            )
        finally:
            # Cleanup the other-version placeholder to avoid leaking across tests
            with neo4j_driver.session() as session:
                session.run(
                    "MATCH (n {odoo_version: $v}) DETACH DELETE n",
                    v=other_version,
                )
