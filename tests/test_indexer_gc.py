# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module GC flag tests - ADR-0007 §D5 follow-up (M7 C4), ported to ADR-0056 B6.

Tests cover:
- retire_modules (the single retirement cascade that replaced gc_stale_modules)
  retires renamed/removed modules found by orphan_module_names.
- A scan that sees none of a repo's modules retires nothing (total-wipe gate).
- T23: retirement runs on every run with no flag; --gc is a deprecated no-op;
  only --no-retire keeps a stale node (pending).
"""
import logging
import os

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
# Test 2: a scan that finds no module never retires anything
# ---------------------------------------------------------------------------

@pytest.fixture
def lifecycle_pg(clean_pg, clean_neo4j):
    from src.db.migrate import run_migrations

    run_migrations(clean_pg)
    return clean_pg


@pytest.mark.postgres
class TestGcRiskGateBlocksWhenScannerEmpty:
    """Ported (ADR-0056 B7-B9). The old test drove the removed per-repo ``--gc``
    shim and expected its "GC skipped, 0 modules" WARNING. The protection is the
    same and now holds on EVERY run: when the scanner sees none of the repo's
    modules (here every manifest became unparseable - the scanner-failure
    shape), the total-wipe gate trips, nothing is retired, and a
    ``lifecycle gate:`` WARNING names the reason."""

    def test_gc_risk_gate_blocks_when_scanner_empty(self, lifecycle_pg, neo4j_driver,
                                                    tmp_path, caplog):
        from tests._lifecycle_repo import GitRepo, module_node, register, run, write_module

        repo = GitRepo(tmp_path, "addons")
        write_module(repo, "mod_a")
        write_module(repo, "mod_b")
        repo.commit("add")
        register("gc_gate_99", repo)
        run(lifecycle_pg, "gc_gate_99", embedder=None)
        for name in ("mod_a", "mod_b"):
            (repo.path / name / "__manifest__.py").write_text("{'name': broken(\n")
        repo.commit("corrupt every manifest")

        with caplog.at_level(logging.WARNING, logger="src.indexer"):
            summary = run(lifecycle_pg, "gc_gate_99", embedder=None)

        assert module_node(neo4j_driver, "mod_a") is not None, (
            "mod_a must NOT be deleted when the scanner saw 0 modules (risk gate)"
        )
        assert module_node(neo4j_driver, "mod_b") is not None, (
            "mod_b must NOT be deleted when the scanner saw 0 modules (risk gate)"
        )
        assert (summary.get("lifecycle") or {}).get("needs_attention")
        gate_lines = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "lifecycle gate" in r.getMessage()
        ]
        assert any("2 of 2" in m for m in gate_lines), (
            f"Expected a lifecycle-gate WARNING naming the 2-of-2 drop; got: {gate_lines}"
        )


# ---------------------------------------------------------------------------
# Test 3 (T23): retirement needs no flag; --gc is a no-op; --no-retire holds
# ---------------------------------------------------------------------------

@pytest.mark.postgres
class TestRetirementNeedsNoFlag:
    """T23 - REWRITTEN from ``TestGcDisabledNoOp``.

    The old test asserted that with ``gc=False`` (the default) a module the scan
    no longer sees must SURVIVE. Default-off retirement is the #378 root cause:
    no timer ever passed ``--gc``, so renamed/removed modules answered "Yes"
    forever. The owner decision (08-approved-plan, decision 2) makes retirement
    part of every run; ``--gc`` is a deprecated no-op; the only legitimate way to
    keep a stale node is the explicit ``--no-retire`` escape hatch, which leaves
    it pending for the next plain run.
    """

    @staticmethod
    def _stale_repo(tmp_path, pg):
        from tests._lifecycle_repo import GitRepo, register, run, write_module

        repo = GitRepo(tmp_path, "addons")
        write_module(repo, "stale_mod")
        write_module(repo, "live_mod")
        repo.commit("add")
        (rid,) = register("t23_99", repo)
        run(pg, "t23_99", embedder=None)
        repo.rm("stale_mod")
        repo.commit("remove stale_mod")
        return rid

    @pytest.mark.parametrize("gc", [False, True], ids=["no_flag", "deprecated_gc"])
    def test_stale_module_is_retired_with_or_without_gc(
        self, lifecycle_pg, neo4j_driver, tmp_path, gc,
    ):
        from tests._lifecycle_repo import ledger, module_node, run

        rid = self._stale_repo(tmp_path, lifecycle_pg)

        run(lifecycle_pg, "t23_99", embedder=None, gc=gc)

        assert module_node(neo4j_driver, "stale_mod") is None, (
            "a module git no longer ships must be retired by a plain run"
        )
        assert module_node(neo4j_driver, "live_mod") is not None
        assert ledger(lifecycle_pg, rid, "stale_mod")["state"] == "retired"

    def test_no_retire_is_the_only_way_to_keep_a_stale_node(
        self, lifecycle_pg, neo4j_driver, tmp_path,
    ):
        from tests._lifecycle_repo import ledger, module_node, run

        rid = self._stale_repo(tmp_path, lifecycle_pg)

        run(lifecycle_pg, "t23_99", embedder=None, retire=False)
        assert module_node(neo4j_driver, "stale_mod") is not None
        assert ledger(lifecycle_pg, rid, "stale_mod")["retire_pending"] is True

        run(lifecycle_pg, "t23_99", embedder=None)
        assert module_node(neo4j_driver, "stale_mod") is None


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
