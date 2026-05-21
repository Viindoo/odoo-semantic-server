# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module GC flag tests — ADR-0007 §D5 follow-up (M7 C4).

Tests cover:
- gc_stale_modules deletes renamed/removed module nodes (DETACH DELETE).
- Risk gate blocks GC when scanner returned 0 modules.
- Default gc=False leaves stale nodes intact.
"""
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.neo4j

TEST_VERSION = "99.0"
TEST_REPO = "repo_gc_test"  # m.repo value used in all GC tests


# ---------------------------------------------------------------------------
# Helper — create a Neo4j Module node directly
# ---------------------------------------------------------------------------

def _create_module_node(driver, name: str, path: str) -> None:
    """Directly create a Module node in Neo4j for testing."""
    with driver.session() as session:
        session.run(
            """
            MERGE (m:Module {name: $name, odoo_version: $v})
            SET m.repo = $repo, m.path = $path
            """,
            name=name, v=TEST_VERSION, repo=TEST_REPO, path=path,
        )


def _module_exists(driver, name: str) -> bool:
    """Return True if a Module node with given name+version exists."""
    with driver.session() as session:
        row = session.run(
            "MATCH (m:Module {name: $name, odoo_version: $v}) RETURN count(m) AS n",
            name=name, v=TEST_VERSION,
        ).single()
    return (row["n"] > 0) if row else False


# ---------------------------------------------------------------------------
# Test 1: gc_stale_modules deletes the renamed/removed module
# ---------------------------------------------------------------------------

class TestGcDeletesRenamedModule:
    """gc_stale_modules removes Module nodes whose path is absent from live_paths."""

    def test_gc_deletes_renamed_module(self, clean_neo4j):
        """Seed two Module nodes; GC with only one live path removes the stale one."""
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j

        # Seed two Module nodes — simulating pre-rename state
        _create_module_node(driver, "stock", path="/repo/addons/stock")
        _create_module_node(driver, "inventory", path="/repo/addons/inventory")

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        try:
            # Scanner only sees 'inventory' (stock was renamed → inventory)
            live_paths = {"/repo/addons/inventory"}
            deleted = writer.gc_stale_modules(TEST_REPO, TEST_VERSION, live_paths)
        finally:
            writer.close()

        # stock should be gone; inventory should remain
        assert deleted == 1, f"Expected 1 deleted, got {deleted}"
        assert not _module_exists(driver, "stock"), (
            "Module node 'stock' should have been DETACH DELETEd by gc_stale_modules"
        )
        assert _module_exists(driver, "inventory"), (
            "Module node 'inventory' (live) must NOT be deleted"
        )

    def test_gc_returns_zero_when_nothing_stale(self, clean_neo4j):
        """gc_stale_modules returns 0 when all indexed modules are in live_paths."""
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j
        _create_module_node(driver, "sale", path="/repo/addons/sale")
        _create_module_node(driver, "purchase", path="/repo/addons/purchase")

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        try:
            live_paths = {"/repo/addons/sale", "/repo/addons/purchase"}
            deleted = writer.gc_stale_modules(TEST_REPO, TEST_VERSION, live_paths)
        finally:
            writer.close()

        assert deleted == 0, f"Expected 0 deleted (all live), got {deleted}"
        assert _module_exists(driver, "sale")
        assert _module_exists(driver, "purchase")


# ---------------------------------------------------------------------------
# Test 2: risk gate blocks GC when scanner returned 0 modules
# ---------------------------------------------------------------------------

class TestGcRiskGateBlocksWhenScannerEmpty:
    """_index_repo with gc=True skips GC and logs warning when scanner finds 0 modules."""

    def test_gc_risk_gate_blocks_when_scanner_empty(
        self, clean_neo4j, tmp_path, caplog
    ):
        """Seed two Module nodes; scanner mock returns {}; both nodes must survive."""
        from src.indexer.pipeline import _index_repo
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j

        # Seed two Module nodes in Neo4j
        _create_module_node(driver, "mod_a", path=str(tmp_path / "mod_a"))
        _create_module_node(driver, "mod_b", path=str(tmp_path / "mod_b"))

        # Create a minimal directory so local_path exists (FileNotFoundError guard)
        local_path = str(tmp_path)

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()

        repo = {
            "id": 9901,
            "local_path": local_path,
            "odoo_version": TEST_VERSION,
            "url": "file://gc-test-repo",
        }

        # Mock build_registry to return empty (simulating scanner failure)
        # Mock incremental helpers to skip git operations
        with (
            patch("src.indexer.pipeline.build_registry", return_value={}),
            patch("src.indexer.pipeline._incremental.get_repo_head", return_value=None),
            caplog.at_level(logging.WARNING, logger="src.indexer.pipeline"),
        ):
            _index_repo(repo, writer, gc=True)

        writer.close()

        # Both nodes must still be present — risk gate prevented GC
        assert _module_exists(driver, "mod_a"), (
            "mod_a must NOT be deleted when scanner returned 0 modules (risk gate)"
        )
        assert _module_exists(driver, "mod_b"), (
            "mod_b must NOT be deleted when scanner returned 0 modules (risk gate)"
        )

        # Warning log must be emitted
        warning_lines = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING and "GC" in r.message
        ]
        assert any("skipping" in m.lower() or "0 modules" in m.lower() for m in warning_lines), (
            f"Expected a GC risk-gate warning log line; got: {warning_lines}"
        )


# ---------------------------------------------------------------------------
# Test 3: gc=False (default) leaves stale nodes intact
# ---------------------------------------------------------------------------

class TestGcDisabledNoOp:
    """When gc=False (default), _index_repo must NOT delete any Module nodes."""

    def test_gc_disabled_no_op(self, clean_neo4j, tmp_path):
        """Seed two Module nodes; scanner returns only one; gc=False → stale node survives."""
        from src.indexer.pipeline import _index_repo
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j

        stale_path = str(tmp_path / "stale_mod")
        live_path = str(tmp_path / "live_mod")

        _create_module_node(driver, "stale_mod", path=stale_path)
        _create_module_node(driver, "live_mod", path=live_path)

        local_path = str(tmp_path)

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()

        repo = {
            "id": 9902,
            "local_path": local_path,
            "odoo_version": TEST_VERSION,
            "url": "file://gc-noop-test-repo",
        }

        # Scanner returns only live_mod — if gc were enabled, stale_mod would be deleted
        live_module_info = MagicMock()
        live_module_info.name = "live_mod"
        live_module_info.odoo_version = TEST_VERSION
        live_module_info.path = live_path
        live_module_info.repo = tmp_path.name
        live_module_info.depends = []

        fake_registry = {TEST_VERSION: {"live_mod": live_module_info}}

        with (
            patch("src.indexer.pipeline.build_registry", return_value=fake_registry),
            patch("src.indexer.pipeline._incremental.get_repo_head", return_value=None),
            patch("src.indexer.pipeline.topological_sort", return_value=[]),
            patch("src.indexer.pipeline.parser_python.parse_module", return_value=MagicMock(
                module=live_module_info, models=[],
            )),
            patch("src.indexer.pipeline.parser_xml.parse_module", return_value=MagicMock(views=[])),
            patch("src.indexer.pipeline.parser_qweb.parse_module", return_value=MagicMock(qweb=[])),
            patch("src.indexer.pipeline.parser_js.parse_module_graph", return_value=MagicMock(
                patches=[], components=[],
            )),
        ):
            # gc defaults to False — stale node must survive
            _index_repo(repo, writer)

        writer.close()

        # stale_mod must still be present — gc was not requested
        assert _module_exists(driver, "stale_mod"), (
            "stale_mod must NOT be deleted when gc=False (default off)"
        )
        assert _module_exists(driver, "live_mod"), (
            "live_mod must still be present after indexing"
        )


# ---------------------------------------------------------------------------
# Test 4 (C4 finding #13): gc does NOT delete modules from other repos
# ---------------------------------------------------------------------------

class TestGcDoesNotDeleteOtherRepoModules:
    """gc_stale_modules must be scoped to the repo being GC'd.

    Note: Module composite key is (name, odoo_version) — two repos with the
    SAME module name at the SAME version share one Neo4j node. Tests use
    distinct module names per repo to properly isolate the scoping behavior.
    """

    def test_gc_does_not_delete_other_repo_modules(self, clean_neo4j):
        """GC for repo_a must NOT delete Module nodes belonging to repo_b.

        repo_a has 'gc_blast_mod_a' at path /repo_a/addons/gc_blast_mod_a.
        repo_b has 'gc_blast_mod_b' at path /repo_b/addons/gc_blast_mod_b.
        GC on repo_a with no live_paths deletes only repo_a's module.
        """
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j

        # Create distinct Module nodes for two different repos
        with driver.session() as session:
            session.run(
                """
                MERGE (m:Module {name: $name, odoo_version: $v})
                SET m.repo = $repo, m.path = $path
                """,
                name="gc_blast_mod_a", v=TEST_VERSION,
                repo="repo_a_gc_blast",
                path="/repo_a/addons/gc_blast_mod_a",
            )
            session.run(
                """
                MERGE (m:Module {name: $name, odoo_version: $v})
                SET m.repo = $repo, m.path = $path
                """,
                name="gc_blast_mod_b", v=TEST_VERSION,
                repo="repo_b_gc_blast",
                path="/repo_b/addons/gc_blast_mod_b",
            )

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        try:
            # Run GC for repo_a with empty live_paths (all of repo_a's modules removed)
            deleted = writer.gc_stale_modules("repo_a_gc_blast", TEST_VERSION, live_paths=set())
        finally:
            writer.close()

        # repo_a's gc_blast_mod_a should be deleted (not in live_paths for repo_a)
        with driver.session() as session:
            row_a = session.run(
                "MATCH (m:Module {name: $name, odoo_version: $v}) RETURN count(m) AS n",
                name="gc_blast_mod_a", v=TEST_VERSION,
            ).single()
            row_b = session.run(
                "MATCH (m:Module {name: $name, odoo_version: $v}) RETURN count(m) AS n",
                name="gc_blast_mod_b", v=TEST_VERSION,
            ).single()

        assert deleted == 1, f"Expected 1 deleted from repo_a, got {deleted}"
        assert row_a["n"] == 0, "repo_a Module{gc_blast_mod_a} should be deleted by GC"
        assert row_b["n"] == 1, (
            "repo_b Module{gc_blast_mod_b} must NOT be deleted — GC is scoped to repo_a only"
        )
