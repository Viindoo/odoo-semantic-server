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
# Test 1b: unresolved flag cleared after real View/QWebTmpl write
#
# Residual gap from commit f4e9306: placeholder MERGE key converges with real
# node key {xmlid, odoo_version}, so the real write lands on the same node —
# but the real SET block never cleared `unresolved=true` left by the placeholder.
# Result: node ends up with module=<real> AND unresolved=true, which causes
# node-level filters (server.py ~986, ~1421, ~3986) to wrongly hide the view.
# ---------------------------------------------------------------------------

def _view_unresolved(driver, xmlid: str) -> bool | None:
    """Return the `unresolved` property of the single View node, or None if absent."""
    with driver.session() as session:
        row = session.run(
            "MATCH (v:View {xmlid: $xmlid, odoo_version: $v}) RETURN v.unresolved AS u",
            xmlid=xmlid, v=TEST_VERSION,
        ).single()
    return row["u"] if row else None


def _qweb_unresolved(driver, xmlid: str) -> bool | None:
    """Return the `unresolved` property of the single QWebTmpl node, or None if absent."""
    with driver.session() as session:
        row = session.run(
            "MATCH (t:QWebTmpl {xmlid: $xmlid, odoo_version: $v}) RETURN t.unresolved AS u",
            xmlid=xmlid, v=TEST_VERSION,
        ).single()
    return row["u"] if row else None


def _view_module(driver, xmlid: str) -> str | None:
    """Return module property of the View node."""
    with driver.session() as session:
        row = session.run(
            "MATCH (v:View {xmlid: $xmlid, odoo_version: $v}) RETURN v.module AS m",
            xmlid=xmlid, v=TEST_VERSION,
        ).single()
    return row["m"] if row else None


def _qweb_module(driver, xmlid: str) -> str | None:
    """Return module property of the QWebTmpl node."""
    with driver.session() as session:
        row = session.run(
            "MATCH (t:QWebTmpl {xmlid: $xmlid, odoo_version: $v}) RETURN t.module AS m",
            xmlid=xmlid, v=TEST_VERSION,
        ).single()
    return row["m"] if row else None


class TestUnresolvedFlagClearedAfterRealWrite:
    """Regression tests for the residual gap in commit f4e9306.

    After the MERGE key convergence fix, a real View/QWebTmpl write lands on
    the same node as the placeholder (correct — no shadow). But the real SET
    block must ALSO clear the `unresolved` flag so the node is not hidden by
    server.py node-level filters.
    """

    def test_view_unresolved_cleared_after_real_write(self, writer, clean_neo4j):
        """After indexing the real parent View, the converged node must have
        coalesce(v.unresolved, false) = false AND module = <real module>."""
        driver = clean_neo4j
        child_xmlid = "sale.view_order_form_ext2"
        parent_xmlid = "sale.view_order_form2"

        # Step 1: index child → creates placeholder for parent with unresolved=true
        child_module = _make_module("sale_ext2")
        child_view = ViewInfo(
            xmlid=child_xmlid,
            name="Sale Order Form Extension 2",
            model="sale.order",
            module="sale_ext2",
            odoo_version=TEST_VERSION,
            view_type="form",
            mode="extension",
            inherit_xmlid=parent_xmlid,
        )
        writer.write_view_results(
            [ViewParseResult(module=child_module, views=[child_view])],
            profiles=["test_profile"],
        )

        # Confirm placeholder was created with unresolved=true
        assert _view_unresolved(driver, parent_xmlid) is True, (
            "Placeholder must have unresolved=true after child indexed"
        )
        assert _view_module(driver, parent_xmlid) == "__unresolved__", (
            "Placeholder must have module='__unresolved__'"
        )

        # Step 2: index the real parent view
        parent_module = _make_module("sale")
        parent_view = ViewInfo(
            xmlid=parent_xmlid,
            name="Sale Order Form 2",
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

        # After real write: unresolved must be false (or absent), module must be real
        unresolv = _view_unresolved(driver, parent_xmlid)
        assert not unresolv, (
            f"View node must have unresolved=false after real write, got unresolved={unresolv!r}. "
            "Real SET block does not clear the placeholder flag — node-level filters in "
            "server.py (~986, ~1421, ~3986) will wrongly hide this view."
        )
        assert _view_module(driver, parent_xmlid) == "sale", (
            "View node must have module='sale' after real write"
        )

    def test_qweb_unresolved_cleared_after_real_write(self, writer, clean_neo4j):
        """After indexing the real parent QWebTmpl, the converged node must have
        coalesce(t.unresolved, false) = false AND module = <real module>."""
        driver = clean_neo4j
        child_xmlid = "sale.qweb_child2"
        parent_xmlid = "sale.qweb_parent2"

        # Step 1: index child → creates placeholder for parent with unresolved=true
        child_module = _make_module("sale_ext2")
        child_qweb = QWebInfo(
            xmlid=child_xmlid,
            module="sale_ext2",
            odoo_version=TEST_VERSION,
            inherit_xmlid=parent_xmlid,
        )
        writer.write_view_results(
            [ViewParseResult(module=child_module, qweb=[child_qweb])],
            profiles=["test_profile"],
        )

        # Confirm placeholder was created with unresolved=true
        assert _qweb_unresolved(driver, parent_xmlid) is True, (
            "Placeholder must have unresolved=true after child indexed"
        )
        assert _qweb_module(driver, parent_xmlid) == "__unresolved__", (
            "Placeholder must have module='__unresolved__'"
        )

        # Step 2: index the real parent template
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

        # After real write: unresolved must be false (or absent), module must be real
        unresolv = _qweb_unresolved(driver, parent_xmlid)
        assert not unresolv, (
            f"QWebTmpl node must have unresolved=false after real write, "
            f"got unresolved={unresolv!r}. "
            "Real SET block does not clear the placeholder flag — node-level filters in "
            "server.py (~986, ~3986) will wrongly hide this template."
        )
        assert _qweb_module(driver, parent_xmlid) == "sale", (
            "QWebTmpl node must have module='sale' after real write"
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


# ---------------------------------------------------------------------------
# Test 3: heal_resolved_unresolved_flags — stale flags on already-resolved nodes
#
# Covers the "Residual 2" scenario documented in ADR-0007 §D5:
# a View/QWebTmpl node whose module was rewritten to a real value by an old
# real-write pass but whose unresolved=true was never cleared.  These nodes
# (and their incident edges) are visible in prod today (153 nodes / 326 edges).
# ---------------------------------------------------------------------------


def _edge_unresolved(driver, src_xmlid: str, rel_type: str, tgt_xmlid: str) -> bool | None:
    """Return the `unresolved` property of the single named relationship."""
    with driver.session() as session:
        row = session.run(
            f"""
            MATCH (s {{xmlid: $src, odoo_version: $v}})
                  -[r:{rel_type}]->
                  (t {{xmlid: $tgt, odoo_version: $v}})
            RETURN r.unresolved AS u
            """,
            src=src_xmlid, tgt=tgt_xmlid, v=TEST_VERSION,
        ).single()
    return row["u"] if row else None


class TestHealResolvedUnresolvedFlags:
    """heal_resolved_unresolved_flags must clear stale unresolved=true on real nodes/edges.

    Scenario: simulate the pre-PR-#194 residual where a real write updated module=<real>
    but left unresolved=true in place.  The heal method must clear the flag on both
    the node and its incident edges.
    """

    def test_heal_view_node_and_edge(self, writer, clean_neo4j):
        """A View node (module='sale', unresolved=true) + incoming INHERITS_VIEW edge
        {unresolved:true} must both be cleared by heal_resolved_unresolved_flags."""
        driver = clean_neo4j
        parent_xmlid = "sale.heal_view_parent"
        child_xmlid = "sale.heal_view_child"

        with driver.session() as session:
            # Parent: real module, but stale unresolved=true  (the residual-2 scenario)
            session.run("""
                MERGE (parent:View {xmlid: $parent_xmlid, odoo_version: $v})
                SET parent.module = 'sale', parent.unresolved = true, parent.name = 'Heal Parent'
            """, parent_xmlid=parent_xmlid, v=TEST_VERSION)
            # Child with a stale {unresolved:true} edge pointing to the parent
            session.run("""
                MERGE (child:View {xmlid: $child_xmlid, odoo_version: $v})
                SET child.module = 'sale_ext', child.name = 'Heal Child'
                WITH child
                MATCH (parent:View {xmlid: $parent_xmlid, odoo_version: $v})
                MERGE (child)-[r:INHERITS_VIEW {unresolved: true}]->(parent)
            """, child_xmlid=child_xmlid, parent_xmlid=parent_xmlid, v=TEST_VERSION)

        # Confirm preconditions
        assert _view_unresolved(driver, parent_xmlid) is True, (
            "Setup: node must start with unresolved=true"
        )
        assert _edge_unresolved(driver, child_xmlid, "INHERITS_VIEW", parent_xmlid) is True, (
            "Setup: edge must start with unresolved=true"
        )

        result = writer.heal_resolved_unresolved_flags(TEST_VERSION)

        assert result["nodes"] >= 1, "heal must report at least 1 node healed"
        assert result["edges"] >= 1, "heal must report at least 1 edge healed"
        # Node flag cleared
        assert not _view_unresolved(driver, parent_xmlid), (
            "View node must have unresolved=false after heal"
        )
        # Edge flag cleared
        assert not _edge_unresolved(driver, child_xmlid, "INHERITS_VIEW", parent_xmlid), (
            "INHERITS_VIEW edge must have unresolved=false after heal"
        )

    def test_heal_qweb_node_and_edge(self, writer, clean_neo4j):
        """A QWebTmpl node (module='sale', unresolved=true) + incoming EXTENDS_TMPL edge
        {unresolved:true} must both be cleared by heal_resolved_unresolved_flags."""
        driver = clean_neo4j
        parent_xmlid = "sale.heal_qweb_parent"
        child_xmlid = "sale.heal_qweb_child"

        with driver.session() as session:
            session.run("""
                MERGE (parent:QWebTmpl {xmlid: $parent_xmlid, odoo_version: $v})
                SET parent.module = 'sale', parent.unresolved = true
            """, parent_xmlid=parent_xmlid, v=TEST_VERSION)
            session.run("""
                MERGE (child:QWebTmpl {xmlid: $child_xmlid, odoo_version: $v})
                SET child.module = 'sale_ext'
                WITH child
                MATCH (parent:QWebTmpl {xmlid: $parent_xmlid, odoo_version: $v})
                MERGE (child)-[r:EXTENDS_TMPL {unresolved: true}]->(parent)
            """, child_xmlid=child_xmlid, parent_xmlid=parent_xmlid, v=TEST_VERSION)

        assert _qweb_unresolved(driver, parent_xmlid) is True
        assert _edge_unresolved(driver, child_xmlid, "EXTENDS_TMPL", parent_xmlid) is True

        result = writer.heal_resolved_unresolved_flags(TEST_VERSION)

        assert result["nodes"] >= 1
        assert result["edges"] >= 1
        assert not _qweb_unresolved(driver, parent_xmlid), (
            "QWebTmpl node must have unresolved=false after heal"
        )
        assert not _edge_unresolved(driver, child_xmlid, "EXTENDS_TMPL", parent_xmlid), (
            "EXTENDS_TMPL edge must have unresolved=false after heal"
        )

    def test_heal_does_not_delete_genuine_placeholder(self, writer, clean_neo4j):
        """A genuine placeholder (module='__unresolved__', unresolved=true) must NOT
        be modified by heal_resolved_unresolved_flags — it is still a true placeholder
        and must remain for gc_unresolved_placeholders to delete."""
        driver = clean_neo4j
        placeholder_xmlid = "sale.genuine_placeholder"

        with driver.session() as session:
            session.run("""
                MERGE (n:View {xmlid: $xmlid, odoo_version: $v})
                SET n.module = '__unresolved__', n.unresolved = true
            """, xmlid=placeholder_xmlid, v=TEST_VERSION)

        result = writer.heal_resolved_unresolved_flags(TEST_VERSION)

        # Heal must count 0 nodes (the placeholder is excluded by the module filter)
        assert result["nodes"] == 0, (
            "heal must not touch genuine placeholders (module='__unresolved__')"
        )
        # Node must still exist with unresolved=true and module='__unresolved__'
        assert _view_unresolved(driver, placeholder_xmlid) is True, (
            "Genuine placeholder must still have unresolved=true after heal"
        )
        assert _view_module(driver, placeholder_xmlid) == "__unresolved__", (
            "Genuine placeholder module must be unchanged after heal"
        )

    def test_heal_does_not_clear_edge_to_genuine_placeholder(self, writer, clean_neo4j):
        """The edge-heal predicate guards against touching edges whose target is a
        genuine placeholder (module='__unresolved__').  This test exercises that guard.

        Scenario: a real child View has an INHERITS_VIEW {unresolved:true} edge pointing
        to a genuine placeholder View (module='__unresolved__', unresolved=true).
        heal_resolved_unresolved_flags must report edges==0 AND the edge's unresolved
        property must still be true — the target-module guard
        ``coalesce(t.module,'')<>'__unresolved__'`` must protect it.
        """
        driver = clean_neo4j
        child_xmlid = "sale.edge_guard_child"
        placeholder_xmlid = "sale.edge_guard_placeholder"

        with driver.session() as session:
            # Real child (already resolved — module is a real value)
            session.run("""
                MERGE (child:View {xmlid: $c, odoo_version: $v})
                SET child.module = 'sale_ext', child.name = 'Edge Guard Child'
            """, c=child_xmlid, v=TEST_VERSION)
            # Genuine placeholder: module='__unresolved__', unresolved=true
            session.run("""
                MERGE (ph:View {xmlid: $ph, odoo_version: $v})
                SET ph.module = '__unresolved__', ph.unresolved = true
                WITH ph
                MATCH (child:View {xmlid: $c, odoo_version: $v})
                MERGE (child)-[r:INHERITS_VIEW {unresolved: true}]->(ph)
            """, ph=placeholder_xmlid, c=child_xmlid, v=TEST_VERSION)

        result = writer.heal_resolved_unresolved_flags(TEST_VERSION)

        # Node heal: the placeholder node is excluded (module='__unresolved__'); child
        # has no unresolved flag set — so nodes healed must be 0.
        assert result["nodes"] == 0, (
            "heal must not touch the genuine placeholder node (module='__unresolved__')"
        )
        # Edge heal: the target-module guard must prevent clearing this edge.
        assert result["edges"] == 0, (
            "heal must not clear the edge whose target is a genuine placeholder "
            "(module='__unresolved__').  The target-module guard is broken."
        )
        # The edge's unresolved property must still be true
        edge_u = _edge_unresolved(driver, child_xmlid, "INHERITS_VIEW", placeholder_xmlid)
        assert edge_u is True, (
            "INHERITS_VIEW edge to genuine placeholder must still have unresolved=true "
            "after heal — the target-module guard coalesce(t.module,'')<>'__unresolved__' "
            "must protect it."
        )

    def test_heal_version_scoped(self, writer, clean_neo4j, neo4j_driver):
        """heal_resolved_unresolved_flags must heal TEST_VERSION nodes/edges and leave
        identical stale nodes/edges at another version completely untouched.

        Previous version of this test only planted data at other_version and expected
        result["nodes"]==0 — a total no-op would also pass.  This rewrite plants
        healable data at BOTH versions so version-scoping is actually exercised.
        """
        other_version = "98.0"
        cur_parent_xmlid = "sale.ver_scope_parent_cur"
        cur_child_xmlid = "sale.ver_scope_child_cur"
        oth_parent_xmlid = "sale.ver_scope_parent_oth"
        oth_child_xmlid = "sale.ver_scope_child_oth"

        # --- plant stale data at TEST_VERSION (should be healed) ---
        with neo4j_driver.session() as session:
            session.run("""
                MERGE (parent:View {xmlid: $p, odoo_version: $v})
                SET parent.module = 'sale', parent.unresolved = true
                WITH parent
                MERGE (child:View {xmlid: $c, odoo_version: $v})
                SET child.module = 'sale_ext'
                WITH child, parent
                MERGE (child)-[r:INHERITS_VIEW {unresolved: true}]->(parent)
            """, p=cur_parent_xmlid, c=cur_child_xmlid, v=TEST_VERSION)

        # --- plant identical stale data at other_version (must NOT be touched) ---
        try:
            with neo4j_driver.session() as session:
                session.run("""
                    MERGE (parent:View {xmlid: $p, odoo_version: $v})
                    SET parent.module = 'sale', parent.unresolved = true
                    WITH parent
                    MERGE (child:View {xmlid: $c, odoo_version: $v})
                    SET child.module = 'sale_ext'
                    WITH child, parent
                    MERGE (child)-[r:INHERITS_VIEW {unresolved: true}]->(parent)
                """, p=oth_parent_xmlid, c=oth_child_xmlid, v=other_version)

            result = writer.heal_resolved_unresolved_flags(TEST_VERSION)

            # TEST_VERSION data must be healed
            assert result["nodes"] >= 1, (
                "heal must report at least 1 node healed at TEST_VERSION"
            )
            assert result["edges"] >= 1, (
                "heal must report at least 1 edge healed at TEST_VERSION"
            )
            with neo4j_driver.session() as session:
                row = session.run(
                    "MATCH (n:View {xmlid: $xmlid, odoo_version: $v}) RETURN n.unresolved AS u",
                    xmlid=cur_parent_xmlid, v=TEST_VERSION,
                ).single()
            assert not row["u"], (
                "TEST_VERSION node must have unresolved=false after heal"
            )
            with neo4j_driver.session() as session:
                row = session.run(
                    """
                    MATCH (s:View {xmlid: $c, odoo_version: $v})
                          -[r:INHERITS_VIEW]->
                          (t:View {xmlid: $p, odoo_version: $v})
                    RETURN r.unresolved AS u
                    """,
                    c=cur_child_xmlid, p=cur_parent_xmlid, v=TEST_VERSION,
                ).single()
            assert not row["u"], (
                "TEST_VERSION edge must have unresolved=false after heal"
            )

            # other_version data must be UNTOUCHED (still stale)
            with neo4j_driver.session() as session:
                row = session.run(
                    "MATCH (n:View {xmlid: $xmlid, odoo_version: $v}) RETURN n.unresolved AS u",
                    xmlid=oth_parent_xmlid, v=other_version,
                ).single()
            assert row["u"] is True, (
                "Node at other_version must not be touched by heal scoped to TEST_VERSION"
            )
            with neo4j_driver.session() as session:
                row = session.run(
                    """
                    MATCH (s:View {xmlid: $c, odoo_version: $v})
                          -[r:INHERITS_VIEW]->
                          (t:View {xmlid: $p, odoo_version: $v})
                    RETURN r.unresolved AS u
                    """,
                    c=oth_child_xmlid, p=oth_parent_xmlid, v=other_version,
                ).single()
            assert row["u"] is True, (
                "Edge at other_version must not be touched by heal scoped to TEST_VERSION"
            )
        finally:
            with neo4j_driver.session() as session:
                session.run(
                    "MATCH (n {odoo_version: $v}) DETACH DELETE n",
                    v=other_version,
                )

    def test_heal_idempotent(self, writer, clean_neo4j):
        """Running heal_resolved_unresolved_flags twice returns 0 on the second run."""
        driver = clean_neo4j
        parent_xmlid = "sale.heal_idem_view"

        with driver.session() as session:
            session.run("""
                MERGE (n:View {xmlid: $xmlid, odoo_version: $v})
                SET n.module = 'sale', n.unresolved = true
            """, xmlid=parent_xmlid, v=TEST_VERSION)

        first = writer.heal_resolved_unresolved_flags(TEST_VERSION)
        second = writer.heal_resolved_unresolved_flags(TEST_VERSION)

        assert first["nodes"] >= 1, "First run must heal at least 1 node"
        assert second["nodes"] == 0, "Second run must heal 0 (idempotent)"
        assert second["edges"] == 0, "Second run must heal 0 edges (idempotent)"

    def test_gc_calls_heal_automatically(self, writer, clean_neo4j):
        """gc_unresolved_placeholders must automatically heal stale flags on real nodes
        as a defense-in-depth step (heal is wired at end of gc)."""
        driver = clean_neo4j
        stale_xmlid = "sale.gc_auto_heal_view"

        with driver.session() as session:
            # Stale real node: module set to real value, but unresolved=true
            session.run("""
                MERGE (n:View {xmlid: $xmlid, odoo_version: $v})
                SET n.module = 'sale', n.unresolved = true
            """, xmlid=stale_xmlid, v=TEST_VERSION)

        # Run gc (which calls heal internally)
        writer.gc_unresolved_placeholders(TEST_VERSION)

        # Stale flag must be cleared by gc's automatic heal call
        assert not _view_unresolved(driver, stale_xmlid), (
            "gc_unresolved_placeholders must clear stale unresolved=true on real nodes "
            "via its internal heal_resolved_unresolved_flags call"
        )
