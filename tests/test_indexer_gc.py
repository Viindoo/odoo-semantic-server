# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module GC flag tests - ADR-0007 §D5 follow-up (M7 C4), ported to ADR-0056 B6.

Tests cover:
- retire_modules (the single retirement cascade that replaced gc_stale_modules)
  retires renamed/removed modules found by orphan_module_names.
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


def _writer():
    from src.indexer.writer_neo4j import Neo4jWriter

    return Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )


def _profile_module(driver, name: str, repo: str = TEST_REPO) -> None:
    """Give a seeded Module a profile: only indexed modules are retirement candidates."""
    with driver.session() as session:
        session.run(
            "MATCH (m:Module {name: $name, odoo_version: $v}) "
            "SET m.profile = ['gc_test_99'], m.repo = $repo",
            name=name, v=TEST_VERSION, repo=repo,
        )


def _stale_then_retire(writer, present_names, repo=TEST_REPO) -> dict:
    """The ported GC flow: stale = indexed modules of *repo* absent from the scan,
    then the single retirement cascade (ADR-0056 B6 - replaces gc_stale_modules)."""
    run_started_at = writer.server_now()
    stale = writer.orphan_module_names(TEST_VERSION, present_names, repo=repo)
    return writer.retire_modules(TEST_VERSION, stale, run_started_at=run_started_at)


# ---------------------------------------------------------------------------
# Test 1: a renamed/removed module is retired (ported from gc_stale_modules)
# ---------------------------------------------------------------------------

class TestGcDeletesRenamedModule:
    """Ported to retire_modules (ADR-0056 B6): gc_stale_modules was removed; the
    protection is unchanged - a module the scan no longer sees is retired, a live one
    is kept. Real shape: stock renamed to inventory."""

    def test_gc_deletes_renamed_module(self, clean_neo4j):
        """Seed two Module nodes; only 'inventory' is scanned, 'stock' is retired."""
        driver = clean_neo4j
        _create_module_node(driver, "stock", path="addons/stock")
        _create_module_node(driver, "inventory", path="addons/inventory")
        _profile_module(driver, "stock")
        _profile_module(driver, "inventory")

        writer = _writer()
        try:
            result = _stale_then_retire(writer, ["inventory"])
        finally:
            writer.close()

        assert result["modules"] == 1, f"Expected 1 retired, got {result}"
        assert result["retired"] == ["stock"]
        assert not _module_exists(driver, "stock"), "renamed-away 'stock' must be retired"
        assert _module_exists(driver, "inventory"), "live 'inventory' must NOT be deleted"

    def test_gc_returns_zero_when_nothing_stale(self, clean_neo4j):
        """Every indexed module is still scanned -> nothing is retired."""
        driver = clean_neo4j
        _create_module_node(driver, "sale", path="addons/sale")
        _create_module_node(driver, "purchase", path="addons/purchase")
        _profile_module(driver, "sale")
        _profile_module(driver, "purchase")

        writer = _writer()
        try:
            result = _stale_then_retire(writer, ["sale", "purchase"])
        finally:
            writer.close()

        assert result["modules"] == 0 and result["children"] == 0, result
        assert _module_exists(driver, "sale")
        assert _module_exists(driver, "purchase")


# ---------------------------------------------------------------------------
# Test 1b (ADR-0037): path-form drift can no longer mass-delete a repo
# ---------------------------------------------------------------------------

class TestGcMixedGraphGuard:
    """Ported (ADR-0056 B6). The ADR-0037 hazard was that path-keyed GC compared
    repo-RELATIVE live paths against legacy ABSOLUTE Module.path and so marked the
    whole repo stale; gc_stale_modules had to skip. Retirement is now decided by
    module NAME, so the same legacy graph must yield zero stale modules - the
    protection (no mass delete from path-form drift) is kept by construction rather
    than by a skip-and-warn guard.
    """

    def test_gc_skips_when_absolute_paths_present(self, clean_neo4j):
        """Legacy absolute Module.path + both names scanned -> nothing is retired;
        only the name the scan truly lost is stale."""
        driver = clean_neo4j
        _create_module_node(driver, "stock", path="/srv/clones/repo/addons/stock")
        _create_module_node(driver, "inventory", path="/srv/clones/repo/addons/inventory")
        _profile_module(driver, "stock")
        _profile_module(driver, "inventory")

        writer = _writer()
        try:
            result = _stale_then_retire(writer, ["stock", "inventory"])
            assert writer.orphan_module_names(
                TEST_VERSION, ["inventory"], repo=TEST_REPO,
            ) == ["stock"]
        finally:
            writer.close()

        assert result["modules"] == 0, f"path form must not make live modules stale: {result}"
        assert _module_exists(driver, "stock"), "absolute-path node must survive"
        assert _module_exists(driver, "inventory"), "absolute-path node must survive"


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
    """When gc=False (default), _index_repo must NOT delete any Module nodes.

    NOTE (ADR-0056): left untouched in B6 on purpose - plan B9 rewrites this as T23
    (retirement runs with no flag; --no-retire is the only way to keep a stale node).
    """

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
    """Ported to orphan_module_names(repo=) + retire_modules (ADR-0056 B6): a GC
    pass scoped to repo_a must never retire repo_b's modules, even though repo_a's
    scan does not list them."""

    def test_gc_does_not_delete_other_repo_modules(self, clean_neo4j):
        """repo_a ships nothing any more; only repo_a's module is retired."""
        driver = clean_neo4j
        _create_module_node(driver, "gc_blast_mod_a", path="addons/gc_blast_mod_a")
        _create_module_node(driver, "gc_blast_mod_b", path="addons/gc_blast_mod_b")
        _profile_module(driver, "gc_blast_mod_a", repo="repo_a_gc_blast")
        _profile_module(driver, "gc_blast_mod_b", repo="repo_b_gc_blast")

        writer = _writer()
        try:
            result = _stale_then_retire(writer, [], repo="repo_a_gc_blast")
        finally:
            writer.close()

        assert result["retired"] == ["gc_blast_mod_a"], result
        assert not _module_exists(driver, "gc_blast_mod_a")
        assert _module_exists(driver, "gc_blast_mod_b"), (
            "repo_b Module{gc_blast_mod_b} must NOT be retired - GC is scoped to repo_a"
        )


# ---------------------------------------------------------------------------
# Tests 5-10: gc_null_repo_dep_stubs — durable GC for repo_id-NULL dep-stubs
#             (FUFU-1 / ADR-0007 follow-up, PR #268)
# ---------------------------------------------------------------------------

class TestGcNullRepoDepStubs:
    """gc_null_repo_dep_stubs collects childless repo_id-NULL :Module stubs.

    These stubs are created by the dep-target MERGE in write_parse_result()
    for ``module.depends`` entries never indexed under their own profile.
    Their MERGE key is ``{name, odoo_version}`` only — no repo, no repo_id,
    no DEFINED_IN children.  Retirement (orphan_module_names) never considers
    them because they carry no profile and no repo_id.
    """

    def test_gc_deletes_childless_null_repo_stub(self, clean_neo4j):
        """A bare dep-stub (no repo_id, no DEFINED_IN children) is DETACH DELETEd."""
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j

        # Seed a dep-stub exactly as the dep-MERGE creates it:
        # {name, odoo_version} only — no repo, no repo_id, no children.
        with driver.session() as s:
            s.run(
                "MERGE (m:Module {name: $name, odoo_version: $v})",
                name="stub_dep_only", v=TEST_VERSION,
            )

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        try:
            deleted = writer.gc_null_repo_dep_stubs(TEST_VERSION)
        finally:
            writer.close()

        assert deleted == 1, f"Expected 1 deleted, got {deleted}"
        with driver.session() as s:
            row = s.run(
                "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN count(m) AS n",
                n="stub_dep_only", v=TEST_VERSION,
            ).single()
        assert row["n"] == 0, "childless null-repo stub must be deleted"

    def test_gc_does_not_delete_real_module(self, clean_neo4j):
        """A Module node with repo_id set is NOT deleted, even if it has no children."""
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j

        # Real module: has repo_id set (as _write_parse_result would do).
        with driver.session() as s:
            s.run(
                "MERGE (m:Module {name: $name, odoo_version: $v}) "
                "SET m.repo = $repo, m.repo_id = $repo_id",
                name="real_module", v=TEST_VERSION,
                repo="odoo_17.0", repo_id=42,
            )

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        try:
            deleted = writer.gc_null_repo_dep_stubs(TEST_VERSION)
        finally:
            writer.close()

        assert deleted == 0, f"Expected 0 deleted, got {deleted}"
        with driver.session() as s:
            row = s.run(
                "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN count(m) AS n",
                n="real_module", v=TEST_VERSION,
            ).single()
        assert row["n"] == 1, "real module (repo_id set) must survive GC"

    def test_gc_does_not_delete_null_repo_stub_with_defined_in_child(self, clean_neo4j):
        """A repo_id-NULL Module that has a DEFINED_IN child (partial-real) is NOT deleted."""
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j

        # A stub that was later partially promoted: it got a DEFINED_IN child
        # but never received repo_id (edge case / partial index).
        with driver.session() as s:
            s.run(
                """
                MERGE (m:Module {name: $name, odoo_version: $v})
                MERGE (model:Model {name: 'sale.order', module: $name, odoo_version: $v})
                MERGE (model)-[:DEFINED_IN]->(m)
                """,
                name="partial_real", v=TEST_VERSION,
            )

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        try:
            deleted = writer.gc_null_repo_dep_stubs(TEST_VERSION)
        finally:
            writer.close()

        assert deleted == 0, "stub with DEFINED_IN child must survive GC"
        with driver.session() as s:
            row = s.run(
                "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN count(m) AS n",
                n="partial_real", v=TEST_VERSION,
            ).single()
        assert row["n"] == 1, "partial-real module must survive GC"

    def test_gc_does_not_touch_other_version(self, clean_neo4j):
        """gc_null_repo_dep_stubs is scoped to odoo_version and must not touch other versions."""
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j

        # Stub for TEST_VERSION (should be deleted).
        # Stub for a different version (must survive).
        with driver.session() as s:
            s.run(
                "MERGE (m:Module {name: $name, odoo_version: $v})",
                name="stub_to_delete", v=TEST_VERSION,
            )
            s.run(
                "MERGE (m:Module {name: $name, odoo_version: $v})",
                name="stub_other_version", v="98.0",
            )

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        try:
            deleted = writer.gc_null_repo_dep_stubs(TEST_VERSION)
        finally:
            writer.close()

        assert deleted == 1, f"Expected 1 deleted (TEST_VERSION stub), got {deleted}"
        with driver.session() as s:
            row = s.run(
                "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN count(m) AS n",
                n="stub_other_version", v="98.0",
            ).single()
        assert row["n"] == 1, "stub from other version must not be touched"

    def test_gc_idempotent(self, clean_neo4j):
        """Running gc_null_repo_dep_stubs twice returns 0 on the second run."""
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j

        with driver.session() as s:
            s.run(
                "MERGE (m:Module {name: $name, odoo_version: $v})",
                name="stub_idem", v=TEST_VERSION,
            )

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        try:
            first = writer.gc_null_repo_dep_stubs(TEST_VERSION)
            second = writer.gc_null_repo_dep_stubs(TEST_VERSION)
        finally:
            writer.close()

        assert first == 1, f"Expected 1 on first run, got {first}"
        assert second == 0, "second run must be a no-op (idempotent)"

    def test_dep_merge_recreates_stub_after_gc(self, clean_neo4j):
        """After GC deletes a stub, the next dep-MERGE re-creates it for a still-declared dep."""
        from src.indexer.writer_neo4j import Neo4jWriter

        driver = clean_neo4j

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        try:
            # Step 1: simulate dep-MERGE creating the stub — source module has
            # repo_id set; dep target ('base') gets only {name, odoo_version}.
            with driver.session() as s:
                s.run(
                    """
                    MERGE (src:Module {name: 'sale_gc_test', odoo_version: $v})
                    SET src.repo = 'odoo_99.0', src.repo_id = 99
                    MERGE (dep:Module {name: 'base_gc_test', odoo_version: $v})
                    MERGE (src)-[:DEPENDS_ON]->(dep)
                    """,
                    v=TEST_VERSION,
                )

            # Step 2: GC deletes the childless repo_id-NULL stub 'base_gc_test'.
            deleted = writer.gc_null_repo_dep_stubs(TEST_VERSION)
            assert deleted == 1, f"dep stub for 'base_gc_test' should be deleted, got {deleted}"

            # Confirm it's gone.
            with driver.session() as s:
                row = s.run(
                    "MATCH (m:Module {name: 'base_gc_test', odoo_version: $v}) "
                    "RETURN count(m) AS n",
                    v=TEST_VERSION,
                ).single()
            assert row["n"] == 0, "stub must be absent after GC"

            # Step 3: simulate the next indexer run re-doing the dep-MERGE.
            with driver.session() as s:
                s.run(
                    """
                    MATCH (src:Module {name: 'sale_gc_test', odoo_version: $v})
                    MERGE (dep:Module {name: 'base_gc_test', odoo_version: $v})
                    MERGE (src)-[:DEPENDS_ON]->(dep)
                    """,
                    v=TEST_VERSION,
                )

            # 'base_gc_test' stub must exist again (cycle-safety).
            with driver.session() as s:
                row = s.run(
                    "MATCH (m:Module {name: 'base_gc_test', odoo_version: $v}) "
                    "RETURN count(m) AS n",
                    v=TEST_VERSION,
                ).single()
            assert row["n"] == 1, "dep-MERGE must re-create stub after GC deletion"
        finally:
            writer.close()
