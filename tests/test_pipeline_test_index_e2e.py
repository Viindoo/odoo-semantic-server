# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pipeline-level END-TO-END proof for the test-surface index (WI-1..WI-4).

This is the test the integration review demanded: the per-method writer/parser
suites are green in ISOLATION but proved NOTHING about whether the pipeline
actually WIRES them together. Here we drive the REAL ``_index_repo`` path over a
REAL on-disk Odoo module (parser, registry, writer) and then the REAL
``reconcile_test_surface`` post-pass (the exact production function
``index_profile`` calls), and assert that:

  * INHERITS_TEST edges exist   (C1 - reconcile wired)
  * COVERS_MODEL / COVERS_FIELD edges exist  (C1 - coverage reconcile wired)
  * framework TestHelper nodes exist  (C1 - seeding wired)
  * is_helper was finalized        (C1 - finalize wired)
  * the addon common base in tests/common.py got a node  (C3 - _is_test_file)
  * test_method / test_class embedding chunks are produced  (C2 - chunk makers wired)
  * a SECOND tenant's private test class is NOT visible to the first tenant's
    scope  (H1 - ADR-0034 choke threaded through the read tools)

RED-BEFORE-GREEN: on the pre-fix state these assertions FAIL -
  * C1: the reconcile/seed/finalize passes were never called -> ZERO edges/helpers.
  * C2: make_test_chunks was never appended to the embed list -> ZERO test chunks.
  * C3: _is_test_file excluded common.py -> the SaleCommon base had no node and the
    INHERITS_TEST edge from TestSaleOrder dangled.

Requires Neo4j (testcontainers or local bolt). Mark: pytest.mark.neo4j.
No Postgres: embeddings are intercepted by monkeypatching write_module_embeddings,
so pgvector is never touched (runs under -m "not postgres").
"""
import os
import subprocess

import pytest

from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

# ---------------------------------------------------------------------------
# WIRING GUARD (no Neo4j) - red-before-green proof for the C1/C2 PIPELINE wiring.
# The behavioral tests below call reconcile_test_surface directly + drive _index_repo
# for C2/C3; these source-level guards additionally lock that index_profile ACTUALLY
# calls the reconcile/seed post-pass and that the embed block ACTUALLY appends the
# test chunk makers. On the pre-fix orphaned state (reconcile never called, chunk
# makers never appended) THESE guards fail - which is exactly the DOA-while-green bug.
# ---------------------------------------------------------------------------

def test_index_profile_wires_reconcile_test_surface():
    """C1: index_profile must invoke the test-surface reconcile post-pass.

    Red-before-green: pre-fix, index_profile only called reconcile_same_name_inherits
    -> ZERO INHERITS_TEST/COVERS_* edges on a real run (DOA while CI green).
    """
    import inspect as _inspect

    from src.indexer import pipeline

    src = _inspect.getsource(pipeline.index_profile)
    # Strip comment lines so a stray mention in a comment can't satisfy the guard -
    # we require an ACTUAL call (the bug was the call being absent, not the name).
    code_only = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "reconcile_test_surface(" in code_only, (
        "C1 WIRING: index_profile must CALL reconcile_test_surface() in its per-version "
        "post-pass (next to reconcile_same_name_inherits)."
    )
    # reconcile_test_surface itself must run all four passes in the right order.
    rts = _inspect.getsource(pipeline.reconcile_test_surface)
    for fn in (
        "write_framework_test_helpers",
        "reconcile_test_inherits",
        "finalize_is_helper",
        "reconcile_test_coverage",
    ):
        assert fn in rts, f"C1: reconcile_test_surface must call writer.{fn}()"
    # index_core seeds framework helpers too (standalone core path).
    core_src = _inspect.getsource(pipeline.index_core)
    assert "write_framework_test_helpers" in core_src, (
        "C1: index_core must seed framework TestHelper nodes for the standalone core path."
    )


def test_embed_block_wires_test_chunk_makers():
    """C2: the pipeline embed block must append make_test_chunks + make_js_test_chunks.

    Red-before-green: pre-fix, the chunk makers existed but had ZERO callers, so
    find_test_examples (AC5) returned nothing.
    """
    import inspect as _inspect

    from src.indexer import pipeline_repo

    src = _inspect.getsource(pipeline_repo._index_repo)
    assert "make_test_chunks(" in src, (
        "C2 WIRING: _index_repo embed block must call make_test_chunks() so test "
        "chunks reach pgvector."
    )
    assert "make_js_test_chunks(" in src, (
        "C2 WIRING: _index_repo embed block must call make_js_test_chunks()."
    )


# ---------------------------------------------------------------------------
# Behavioral e2e (Neo4j via testcontainers). Marked per-test so the two source
# wiring guards above stay in the no-Docker unit tier.
# ---------------------------------------------------------------------------


# --- A real, minimal sale-like module on disk (real parser input) ----------

_MANIFEST = (
    "{{'name': 'sale', 'version': '{major}.1.0', 'depends': ['base'], "
    "'installable': True}}\n"
).format(major=TEST_VERSION.split(".")[0])

_COMMON_PY = '''\
"""Addon common base lives in tests/common.py (NOT a test_ file) - C3."""
from odoo.tests.common import TransactionCase


class SaleCommon(TransactionCase):
    """Reusable base - defines NO test_ methods, is subclassed (is_helper)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.order = cls.env['sale.order'].create({'name': 'SO1'})
'''

_TEST_SALE_PY = '''\
"""era2 test file that inherits the tests/common.py base (C3 chain)."""
from odoo.tests.common import tagged

from .common import SaleCommon


@tagged('post_install', '-at_install')
class TestSaleOrder(SaleCommon):
    """Tests for sale.order - inherits SaleCommon (-> INHERITS_TEST)."""

    def setUp(self):
        super().setUp()
        # def-use: self.so -> sale.order so self.so.amount_total resolves to a
        # COVERS_FIELD on sale.order.amount_total (HIGH-2 def-use within the class).
        self.so = self.env['sale.order'].create({'name': 'SO2'})

    def test_amount_total(self):
        """amount_total must be a number."""
        self.assertEqual(self.so.amount_total, 0.0)
        total = self.so.amount_total
        self.assertGreaterEqual(total, 0)
'''


def _write_sale_module(root):
    """Create a real on-disk `sale` module with tests/common.py + a test file."""
    mod = root / "sale"
    (mod / "tests").mkdir(parents=True)
    (mod / "__init__.py").write_text("")
    (mod / "__manifest__.py").write_text(_MANIFEST)
    (mod / "tests" / "__init__.py").write_text("")
    (mod / "tests" / "common.py").write_text(_COMMON_PY)
    (mod / "tests" / "test_sale_order.py").write_text(_TEST_SALE_PY)
    return mod


def _seed_definition_model_and_field(driver):
    """Seed the is_definition Model + Field that COVERS_* must resolve to.

    reconcile_test_coverage only links to is_definition=true nodes (ADR-0013/0048),
    so we hand-author the target sale.order / amount_total nodes. This is the
    ANTI-TAUTOLOGY oracle: the expected edge targets are independent fixtures, not
    parser output fed back into itself.
    """
    with driver.session() as s:
        s.run(
            """
            MERGE (m:Model {name: 'sale.order', module: 'sale', odoo_version: $v})
            SET m.is_definition = true, m.profile = ['odoo_99']
            MERGE (f:Field {name: 'amount_total', model: 'sale.order',
                            module: 'sale', odoo_version: $v})
            SET f.is_definition = true, f.profile = ['odoo_99'], f.ttype = 'monetary'
            """,
            v=TEST_VERSION,
        )


@pytest.fixture
def e2e_writer(clean_neo4j):
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _run_real_pipeline(tmp_path, e2e_writer, monkeypatch, *, profile="odoo_99"):
    """Drive the REAL _index_repo + reconcile_test_surface over the on-disk module.

    Returns the captured embedding chunks (so C2 can be asserted) - pgvector is
    never touched (write_module_embeddings is monkeypatched).
    """
    from src.indexer.pipeline import _index_repo, reconcile_test_surface

    repo_dir = tmp_path / profile
    repo_dir.mkdir()
    _write_sale_module(repo_dir)
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "checkout", "-b", TEST_VERSION],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "init"],
        check=True, capture_output=True,
    )

    # Capture chunks fed to pgvector (C2) without needing Postgres. The embed block
    # only runs when _vector_extension_available is True and a (fake) pg_conn+embedder
    # are passed, so we stub both.
    captured_chunks = []

    def _fake_write_module_embeddings(mod_name, version, chunks, embedder, **_kw):
        captured_chunks.extend(chunks)
        return len(chunks)

    monkeypatch.setattr(
        "src.indexer.writer_pgvector.write_module_embeddings",
        _fake_write_module_embeddings,
    )
    monkeypatch.setattr("src.db.migrate._vector_extension_available", lambda _c: True)
    # parser_js.parse_module returns embedding chunks for JS production code; stub to []
    monkeypatch.setattr("src.indexer.pipeline_repo.parser_js.parse_module", lambda _i: [])

    # _index_repo advances repos.head_sha via repo_store() at the end (needs Postgres).
    # With a (fake) pg_conn supplied for the embed path, that call would hit PG; stub
    # repo_store so the Neo4j-only e2e never touches Postgres (runs under -m not postgres).
    class _StubRepoStore:
        def get_repo_head_sha(self, _id):
            return None

        def update_repo_head_sha(self, _id, _sha):
            return None

        def get_repo_ids_by_local_path_basenames(self, *_a, **_k):
            return []

        def reset_head_sha(self, *_a, **_k):
            return 0

    monkeypatch.setattr("src.indexer.pipeline.repo_store", lambda: _StubRepoStore())

    repo = {
        "id": 7701,
        "local_path": str(repo_dir),
        "odoo_version": TEST_VERSION,
        "url": "file://e2e-test-repo",
    }
    _index_repo(
        repo, e2e_writer, pg_conn=object(), embedder=object(),
        full_reindex=True, profile_name=profile,
    )
    # The REAL post-pass that index_profile runs (extracted to a shared function).
    reconcile_test_surface(e2e_writer, [TEST_VERSION], framework_profiles=[profile])
    return captured_chunks


@pytest.mark.neo4j
def test_pipeline_wires_edges_helpers_and_chunks(tmp_path, e2e_writer, monkeypatch):
    """C1+C2+C3: a real wired index run produces INHERITS_TEST + COVERS_* edges,
    framework TestHelper nodes, finalized is_helper, the common.py base node, AND
    embedded test chunks. FAILS on the orphaned-reconcile / un-wired-chunks state.
    """
    _seed_definition_model_and_field(e2e_writer.driver)
    chunks = _run_real_pipeline(tmp_path, e2e_writer, monkeypatch)
    driver = e2e_writer.driver

    with driver.session() as s:
        # C3: tests/common.py was parsed -> SaleCommon got a TestClass node.
        common_cnt = s.run(
            "MATCH (tc:TestClass {name: 'SaleCommon', odoo_version: $v}) RETURN count(tc) AS n",
            v=TEST_VERSION,
        ).single()["n"]
        # C1: INHERITS_TEST edge TestSaleOrder -> SaleCommon (the common.py base).
        inherits_common = s.run(
            """
            MATCH (child:TestClass {name: 'TestSaleOrder', odoo_version: $v})
                  -[:INHERITS_TEST]->(base:TestClass {name: 'SaleCommon', odoo_version: $v})
            RETURN count(*) AS n
            """,
            v=TEST_VERSION,
        ).single()["n"]
        # C1: framework TestHelper seeded (TransactionCase) + INHERITS_TEST to it.
        framework_cnt = s.run(
            """
            MATCH (th:TestHelper {name: 'TransactionCase', odoo_version: $v,
                                  origin: 'framework'})
            RETURN count(th) AS n
            """,
            v=TEST_VERSION,
        ).single()["n"]
        inherits_framework = s.run(
            """
            MATCH (:TestClass {name: 'SaleCommon', odoo_version: $v})
                  -[:INHERITS_TEST]->(:TestHelper {name: 'TransactionCase', odoo_version: $v})
            RETURN count(*) AS n
            """,
            v=TEST_VERSION,
        ).single()["n"]
        # C1: finalize_is_helper promoted SaleCommon (subclassed + no test_ methods).
        is_helper = s.run(
            "MATCH (tc:TestClass {name: 'SaleCommon', odoo_version: $v}) RETURN tc.is_helper AS h",
            v=TEST_VERSION,
        ).single()["h"]
        # C1: COVERS_MODEL edge to sale.order (definition node).
        covers_model = s.run(
            """
            MATCH (:TestMethod {odoo_version: $v})-[:COVERS_MODEL]->
                  (:Model {name: 'sale.order', odoo_version: $v, is_definition: true})
            RETURN count(*) AS n
            """,
            v=TEST_VERSION,
        ).single()["n"]
        # C1: COVERS_FIELD edge to amount_total (def-use resolved field).
        covers_field = s.run(
            """
            MATCH (:TestMethod {odoo_version: $v})-[:COVERS_FIELD]->
                  (:Field {name: 'amount_total', model: 'sale.order', odoo_version: $v})
            RETURN count(*) AS n
            """,
            v=TEST_VERSION,
        ).single()["n"]

    assert common_cnt == 1, "C3: tests/common.py SaleCommon base must get a TestClass node"
    assert inherits_common >= 1, "C1: INHERITS_TEST to the common.py base must exist (C3 chain)"
    assert framework_cnt == 1, "C1: framework TransactionCase TestHelper must be seeded"
    assert inherits_framework >= 1, "C1: SaleCommon -> TransactionCase INHERITS_TEST must exist"
    assert is_helper is True, "C1: finalize_is_helper must promote the subclassed no-test base"
    assert covers_model >= 1, "C1: COVERS_MODEL edge to sale.order must be reconciled"
    assert covers_field >= 1, "C1: COVERS_FIELD edge to amount_total must be reconciled (def-use)"

    # C2: test chunks were appended to the embed list (find_test_examples / AC5).
    test_chunk_types = {
        c.chunk_type for c in chunks if c.chunk_type in ("test_method", "test_class")
    }
    assert "test_method" in test_chunk_types, (
        "C2: make_test_chunks must be wired into the embed path (test_method chunk present). "
        f"Got chunk types: {sorted({c.chunk_type for c in chunks})}"
    )
    assert "test_class" in test_chunk_types, "C2: test_class chunk must be embedded too"


@pytest.mark.neo4j
def test_tenant_scope_isolates_private_test_class(tmp_path, e2e_writer, monkeypatch):
    """H1 (ADR-0034): tenant B's read scope must NOT see tenant A's private TestClass.

    Index the same module under profile 'tenant_a_99'; then query tests_covering /
    test_class_inspect with a scope bound to a DIFFERENT profile and assert the
    private TestSaleOrder is invisible (fail-closed), while admin (own=None) sees it.
    """
    from src.mcp.test_query import build_test_class_inspect_query

    _seed_definition_model_and_field(e2e_writer.driver)
    # Make the seeded Model/Field private to tenant_a too (so scope is consistent).
    with e2e_writer.driver.session() as s:
        s.run(
            """
            MATCH (n {odoo_version: $v}) WHERE n:Model OR n:Field
            SET n.profile = ['tenant_a_99']
            """,
            v=TEST_VERSION,
        )
    _run_real_pipeline(tmp_path, e2e_writer, monkeypatch, profile="tenant_a_99")
    driver = e2e_writer.driver

    def _scope_pred(alias):
        return (
            f"($own IS NULL OR (size({alias}.profile) > 0 AND "
            f"all(__p IN {alias}.profile WHERE __p IN $own OR __p IN $shared)))"
        )

    cypher, params = build_test_class_inspect_query(
        "TestSaleOrder", TEST_VERSION, scope_pred=_scope_pred,
    )

    with driver.session() as s:
        # Tenant B (own=['tenant_b_99'], no shared) must NOT see tenant A's class.
        rows_b = s.run(cypher, **{**params, "own": ["tenant_b_99"], "shared": []}).data()
        # Tenant A (owner) sees it.
        rows_a = s.run(cypher, **{**params, "own": ["tenant_a_99"], "shared": []}).data()
        # Admin (own=None) sees it.
        rows_admin = s.run(cypher, **{**params, "own": None, "shared": []}).data()

    visible_b = [r for r in rows_b if r.get("name") == "TestSaleOrder"]
    visible_a = [r for r in rows_a if r.get("name") == "TestSaleOrder"]
    visible_admin = [r for r in rows_admin if r.get("name") == "TestSaleOrder"]

    assert not visible_b, "H1: tenant B must NOT see tenant A's private TestSaleOrder (data leak)"
    assert visible_a, "H1: tenant A (owner) must see its own TestSaleOrder"
    assert visible_admin, "H1: admin (own=None) must see all test classes"
