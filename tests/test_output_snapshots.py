"""
MCP output schema guard — integration tests (require Neo4j).

Catches API drift: when _resolve_* functions change output format without
updating docs/thiet-ke-kien-truc.md §MCP Tools Interface.

Run: pytest tests/test_output_snapshots.py -m neo4j
When intentionally changing output format: update this test + architecture doc.
"""
import os

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

_SNAP_VERSION = "96.0"  # dedicated version — avoids conflict with 99.0 / 98.0 / 97.0 fixtures


@pytest.fixture(scope="module")
def snapshot_db(neo4j_driver):
    """Seed minimal account.move data + yield; teardown after module."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_SNAP_VERSION)

    mod = ModuleInfo("account", _SNAP_VERSION, "odoo_test", "/tmp", [], "")
    model = ModelInfo(
        name="account.move",
        module="account",
        odoo_version=_SNAP_VERSION,
        fields=[FieldInfo("name", "char", required=True)],
        methods=[MethodInfo("action_post", has_super_call=True)],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)

    yield

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_SNAP_VERSION)


def test_resolve_model_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move (Odoo 96.0)
        ├─ Defined in:     [odoo_test] account
        ├─ Fields:         1
        └─ Methods:        1

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_model

    result = _resolve_model("account.move", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move (Odoo {_SNAP_VERSION})", (
        "Line 0 must be '<model> (Odoo <version>)' — see architecture doc §6"
    )
    assert any("Defined in" in ln for ln in lines), (
        "Missing 'Defined in' line"
    )
    assert any("Fields:" in ln for ln in lines), "Missing field count"
    assert any("Methods:" in ln for ln in lines), "Missing method count"
    assert any(ln.startswith("├─") or ln.startswith("└─") for ln in lines), (
        "Missing tree connectors (Ship Wow Product requirement)"
    )


def test_resolve_field_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move.name (Odoo 96.0)
        ├─ Type:     char
        ├─ Computed: No
        ...
        └─ Declared in: ...

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_field

    result = _resolve_field("account.move", "name", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move.name (Odoo {_SNAP_VERSION})"
    assert any("Type:" in ln for ln in lines), "Missing field type line"
    assert any("Computed" in ln for ln in lines), "Missing computed indicator"
    assert any("Declared in" in ln for ln in lines), "Missing declaration source"
    assert any(ln.startswith("├─") or ln.startswith("└─") for ln in lines)


def test_resolve_method_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move.action_post() (Odoo 96.0)
        Override chain:
          [odoo_test] account — ✓ calls super() — decorators: —

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_method

    result = _resolve_method("account.move", "action_post", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move.action_post() (Odoo {_SNAP_VERSION})"
    assert any("Override chain" in ln for ln in lines), "Missing 'Override chain' header"
    assert any("super()" in ln for ln in lines), "Missing super() call indicator"
    assert any("decorators" in ln for ln in lines), "Missing decorators field"


def test_resolve_view_not_found_contract(snapshot_db):
    """
    resolve_view NOT_FOUND output contract — added in M2.
    Happy path contract is in test_mcp_server.py::test_resolve_view_found.

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_view

    result = _resolve_view("nonexistent.view.xmlid", _SNAP_VERSION)
    assert "not found" in result, (
        "NOT_FOUND response must contain 'not found'"
    )
    assert "nonexistent.view.xmlid" in result, (
        "NOT_FOUND response must echo the queried xmlid"
    )


class TestFindExamplesOutputSchema:
    """Lock find_examples output contract — format change breaks this test intentionally.

    Requires both Neo4j + PostgreSQL + pgvector extension.
    Skips gracefully when pgvector is not installed locally.
    """

    pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]

    @pytest.fixture(autouse=True)
    def _seed(self, request, neo4j_driver):
        from src.db.migrate import _vector_extension_available
        # Check if pg_conn fixture is available (reachable)
        pg_conn_fixture = (
            request.getfixturevalue("pg_conn") if "pg_conn" in request.fixturenames else None
        )
        if pg_conn_fixture is None:
            pytest.skip("PostgreSQL not reachable")
        if not _vector_extension_available(pg_conn_fixture):
            pytest.skip("pgvector extension not installed")

        from pgvector.psycopg2 import register_vector

        from src.db.migrate import run_migrations
        from src.indexer.embedder import FakeEmbedder
        from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

        run_migrations(pg_conn_fixture)
        register_vector(pg_conn_fixture)

        with pg_conn_fixture.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (_SNAP_VERSION,))

        with neo4j_driver.session() as s:
            s.run("MERGE (:Module {name:'snap_mod', odoo_version:$v})", v=_SNAP_VERSION)

        embedder = FakeEmbedder(dim=1024)
        chunks = [EmbeddingChunk(
            "method", "snap_mod", _SNAP_VERSION, "snap_mod.sale.order.action_confirm",
            "sale.order", "snap_mod/models/sale.py", 0,
            f"[snap_mod] sale.order.action_confirm ({_SNAP_VERSION})\n"
            "def action_confirm(self): ...",
        )]
        write_module_embeddings(pg_conn_fixture, "snap_mod", _SNAP_VERSION, chunks, embedder)

        self._pg = pg_conn_fixture
        self._neo4j = neo4j_driver

        yield

        with pg_conn_fixture.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (_SNAP_VERSION,))

    def test_find_examples_output_header_contract(self):
        """
        Contract: first non-empty line = 'find_examples: "<query>" (<version>)'
        Second line = 'Found N results'

        If output format changes: update this test + architecture doc §MCP Tools.
        """
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _find_examples

        result = _find_examples(
            "confirm order", odoo_version=_SNAP_VERSION,
            _driver=self._neo4j, _pg_conn=self._pg, _embedder=FakeEmbedder(dim=1024),
        )
        lines = result.splitlines()
        assert lines[0] == f'find_examples: "confirm order" ({_SNAP_VERSION})'
        assert lines[1].startswith("Found ")

    def test_find_examples_result_block_contract(self):
        """
        Each result block must contain:
        - '#N · score X.XX · <type> · [<module>] <entity>'
        - 'File: <path>'
        - '┌' box border
        - '│' content lines
        - '└' box border
        """
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _find_examples

        result = _find_examples(
            "confirm order", odoo_version=_SNAP_VERSION,
            _driver=self._neo4j, _pg_conn=self._pg, _embedder=FakeEmbedder(dim=1024),
        )
        assert "· score" in result
        assert "File:" in result
        assert "┌" in result
        assert "│" in result
        assert "└" in result
