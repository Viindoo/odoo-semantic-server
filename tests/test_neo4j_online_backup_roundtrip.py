# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_neo4j_online_backup_roundtrip.py
"""Integration round-trip: _export_neo4j_online → _restore_neo4j_cypher.

Requires a running Neo4j instance (testcontainers or `docker compose up -d neo4j`).
Marked ``neo4j`` so it only runs in the integration suite.

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

pytestmark = pytest.mark.neo4j


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
