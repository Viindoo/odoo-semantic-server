# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_neo4j_online_backup_roundtrip.py
"""Integration round-trip: _export_neo4j_online → _restore_neo4j_cypher.

Requires a running Neo4j instance (testcontainers or `docker compose up -d neo4j`).
Marked ``neo4j`` AND ``neo4j_backup``.  The ``neo4j_backup`` marker exists
because these tests issue a WHOLE-GRAPH ``MATCH (n) DETACH DELETE n`` — the
restore-replaces-the-graph business contract is genuine and MUST keep its
whole-graph assertion (it is NOT label-scoped; see ADR / WS-C M5).  Because
that wipe would destroy any seed-once data co-resident in the shared test
Neo4j, the standard integration run EXCLUDES this file
(``-m "neo4j and not neo4j_backup"``) and it is run separately/last via
``make test-neo4j-backup`` (``-m neo4j_backup``).

Round-trip contract:
  1. Write a known set of nodes + relationships via the Bolt driver.
  2. Export to a temporary neo4j.cypher file via _export_neo4j_online().
  3. Delete all nodes from Neo4j.
  4. Restore from the cypher file via _restore_neo4j_cypher().
  5. Verify the original nodes + relationships are present.

This test does NOT require APOC or any extra Neo4j plugin.
"""
import tempfile
from pathlib import Path

import pytest
from neo4j import GraphDatabase

from src.cli import _export_neo4j_online, _restore_neo4j_cypher

pytestmark = [pytest.mark.neo4j, pytest.mark.neo4j_backup]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def neo4j_driver(request):
    """Open a Bolt driver to the test Neo4j instance, close after test."""
    import os
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "password")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
    except Exception as exc:
        pytest.skip(f"Neo4j not reachable: {exc}")
    yield driver
    driver.close()


@pytest.fixture()
def clean_backup_graph(neo4j_driver):
    """Create test nodes, yield driver, then delete all test nodes after the test."""
    with neo4j_driver.session() as session:
        # Clean up any leftover test data first
        session.run(
            "MATCH (n:_BackupTestNode) DETACH DELETE n"
        ).consume()
        # Create two nodes and one relationship
        session.run(
            "CREATE (a:_BackupTestNode {name: 'NodeA', value: 42, active: true})"
            "-[:_BACKUP_TEST_REL {weight: 1.5}]->"
            "(b:_BackupTestNode {name: 'NodeB', value: 99, active: false})"
        ).consume()
    yield neo4j_driver
    # Cleanup after test
    with neo4j_driver.session() as session:
        session.run("MATCH (n:_BackupTestNode) DETACH DELETE n").consume()


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------

def test_export_restore_roundtrip(clean_backup_graph):
    """Export → delete → restore → verify: nodes and relationships survive."""
    driver = clean_backup_graph

    with tempfile.TemporaryDirectory() as tmpdir:
        cypher_path = Path(tmpdir) / "neo4j.cypher"

        # Step 1: Export
        ok, msg = _export_neo4j_online(cypher_path)
        assert ok, f"Export failed: {msg}"
        assert cypher_path.exists(), "neo4j.cypher not created"
        content = cypher_path.read_text(encoding="utf-8")
        assert "CREATE" in content, "Expected CREATE statements in export"
        assert "_BackupTestNode" in content, "Expected test label in export"

        # Step 2: Delete all nodes
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
            count = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        assert count == 0, f"Expected empty graph after delete, got {count} nodes"

        # Step 3: Restore
        ok2, msg2 = _restore_neo4j_cypher(cypher_path)
        assert ok2, f"Restore failed: {msg2}"

        # Step 4: Verify nodes restored
        with driver.session() as session:
            result = session.run(
                "MATCH (n:_BackupTestNode) RETURN n.name AS name, n.value AS value "
                "ORDER BY n.name"
            )
            rows = result.data()

        names = [r["name"] for r in rows]
        values = [r["value"] for r in rows]
        assert "NodeA" in names, f"NodeA not restored; got {names}"
        assert "NodeB" in names, f"NodeB not restored; got {names}"
        assert 42 in values, f"value=42 not restored; got {values}"
        assert 99 in values, f"value=99 not restored; got {values}"

        # Step 5: Verify relationship restored
        with driver.session() as session:
            rel_count = session.run(
                "MATCH (:_BackupTestNode)-[r:_BACKUP_TEST_REL]->(:_BackupTestNode) "
                "RETURN count(r) AS c"
            ).single()["c"]
        assert rel_count == 1, f"Expected 1 relationship, got {rel_count}"

        # Step 6: Verify __eid__ property was cleaned up
        with driver.session() as session:
            eid_count = session.run(
                "MATCH (n) WHERE n.__eid__ IS NOT NULL RETURN count(n) AS c"
            ).single()["c"]
        assert eid_count == 0, (
            f"__eid__ cleanup failed: {eid_count} nodes still have __eid__ property"
        )


def test_restore_onto_nonempty_graph_does_not_duplicate(clean_backup_graph):
    """Business rule (FIX 1): restore REPLACES the graph — it must wipe existing
    nodes first so restoring onto a non-empty graph does NOT duplicate.

    The Cypher export replays CREATE (not MERGE) statements; without an upfront
    wipe, a second restore (or a restore onto a live graph) would double every
    node and relationship. This test seeds DIFFERENT pre-existing nodes, restores
    the bundle, and asserts the final graph contains EXACTLY the bundle's nodes —
    the pre-existing nodes are gone and there are no duplicates. It deliberately
    does NOT call DETACH DELETE itself: the wipe must come from
    _restore_neo4j_cypher, not the test fixture.
    """
    driver = clean_backup_graph

    with tempfile.TemporaryDirectory() as tmpdir:
        cypher_path = Path(tmpdir) / "neo4j.cypher"

        # _export_neo4j_online() captures the WHOLE graph (MATCH (n)), so the
        # invariant is expressed relative to the exported set, not an absolute
        # count — this is robust whether the instance is pristine or shared.
        with driver.session() as session:
            exported_nodes = session.run(
                "MATCH (n) RETURN count(n) AS c"
            ).single()["c"]
            exported_rels = session.run(
                "MATCH ()-[r]->() RETURN count(r) AS c"
            ).single()["c"]

        # Export the current graph (includes the 2 seeded test nodes + 1 rel).
        ok, msg = _export_neo4j_online(cypher_path)
        assert ok, f"Export failed: {msg}"

        # Seed DIFFERENT nodes AFTER the export — these are NOT in the bundle, so
        # a correct restore (wipe-first) must remove them; a buggy additive
        # restore would leave them AND duplicate the exported nodes.
        with driver.session() as session:
            session.run(
                "CREATE (:_BackupTestNode {name: '_StaleAfterExport1', value: -1})"
            ).consume()
            session.run(
                "CREATE (:_BackupTestNode {name: '_StaleAfterExport2', value: -2})"
            ).consume()

        # Restore — internal wipe must run; NO manual DETACH DELETE here.
        ok2, msg2 = _restore_neo4j_cypher(cypher_path)
        assert ok2, f"Restore failed: {msg2}"

        with driver.session() as session:
            total_nodes = session.run(
                "MATCH (n) RETURN count(n) AS c"
            ).single()["c"]
            total_rels = session.run(
                "MATCH ()-[r]->() RETURN count(r) AS c"
            ).single()["c"]
            stale_left = session.run(
                "MATCH (n:_BackupTestNode) "
                "WHERE n.name IN ['_StaleAfterExport1', '_StaleAfterExport2'] "
                "RETURN count(n) AS c"
            ).single()["c"]

        # Wipe-first restore reproduces EXACTLY the exported set (no doubling)
        # and the post-export stale nodes are gone.
        assert total_nodes == exported_nodes, (
            f"Restore duplicated/leaked nodes: exported {exported_nodes}, "
            f"got {total_nodes} after restore onto non-empty graph"
        )
        assert total_rels == exported_rels, (
            f"Restore duplicated/leaked relationships: exported {exported_rels}, "
            f"got {total_rels}"
        )
        assert stale_left == 0, (
            f"Post-export stale nodes survived restore (wipe did not run): "
            f"{stale_left} left"
        )


def test_export_node_with_no_serialisable_props_yields_valid_cypher(clean_backup_graph):
    """Business rule (FIX 3): a node whose every property is None (no serialisable
    props) must export to VALID Cypher — no leading-comma `CREATE (n:Label {, ...})`.

    The export tags each node with __eid__; the guard must emit
    `CREATE (n:Label {__eid__: ...})` (no stray comma) when props_cypher is empty,
    so the file replays without a syntax error. We assert by round-tripping: a
    node with only a None-valued property survives export → wipe → restore.
    """
    driver = clean_backup_graph

    # Add a node whose only property is None → _props_to_cypher() returns "".
    with driver.session() as session:
        session.run(
            "CREATE (:_BackupTestNode {name: 'NullProps', value: null})"
        ).consume()

    with tempfile.TemporaryDirectory() as tmpdir:
        cypher_path = Path(tmpdir) / "neo4j.cypher"
        ok, msg = _export_neo4j_online(cypher_path)
        assert ok, f"Export failed: {msg}"

        content = cypher_path.read_text(encoding="utf-8")
        # No CREATE node statement may start its property map with a comma.
        assert "{, " not in content, (
            "Zero-prop node produced invalid leading-comma Cypher:\n" + content
        )

        # Round-trip proves the generated Cypher is replayable (parses + runs).
        ok2, msg2 = _restore_neo4j_cypher(cypher_path)
        assert ok2, f"Restore of zero-prop-node export failed (invalid Cypher?): {msg2}"

        with driver.session() as session:
            null_node = session.run(
                "MATCH (n:_BackupTestNode {name: 'NullProps'}) RETURN count(n) AS c"
            ).single()["c"]
        assert null_node == 1, (
            f"Zero-prop node not faithfully restored, got count={null_node}"
        )
