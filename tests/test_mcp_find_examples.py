"""Integration tests for find_examples MCP tool — requires Neo4j + PostgreSQL + pgvector."""
import pytest

from tests.conftest import PG_EMBED_VERSION as TEST_VERSION

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]


@pytest.fixture
def seeded(clean_pg_embeddings, clean_neo4j):
    """Seed Neo4j modules + PostgreSQL embeddings for find_examples tests."""
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    # Neo4j: sale depends on base
    with clean_neo4j.session() as s:
        s.run("MERGE (:Module {name:'sale', odoo_version:$v})", v=TEST_VERSION)
        s.run("MERGE (:Module {name:'base', odoo_version:$v})", v=TEST_VERSION)
        s.run("""
            MATCH (a:Module {name:'sale', odoo_version:$v})
            MATCH (b:Module {name:'base', odoo_version:$v})
            MERGE (a)-[:DEPENDS_ON]->(b)
        """, v=TEST_VERSION)

    embedder = FakeEmbedder(dim=1024)
    sale_chunks = [EmbeddingChunk(
        "method", "sale", TEST_VERSION, "sale.order.action_confirm",
        "sale.order", "sale/models/sale.py", 0,
        f"[sale] sale.order.action_confirm ({TEST_VERSION})\ndef action_confirm(self): ...",
    )]
    base_chunks = [EmbeddingChunk(
        "field", "base", TEST_VERSION, "res.partner.name",
        "res.partner", "base/models/partner.py", 0,
        "[base] res.partner: name (char)\nname = fields.Char(...)",
    )]
    write_module_embeddings(clean_pg_embeddings, "sale", TEST_VERSION, sale_chunks, embedder)
    write_module_embeddings(clean_pg_embeddings, "base", TEST_VERSION, base_chunks, embedder)

    return clean_pg_embeddings, clean_neo4j


def test_find_examples_returns_header(seeded):
    pg, neo4j_driver = seeded
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _find_examples

    result = _find_examples(
        "confirm sale", odoo_version=TEST_VERSION,
        _driver=neo4j_driver, _pg_conn=pg, _embedder=FakeEmbedder(dim=1024),
    )
    assert 'find_examples: "confirm sale"' in result
    assert TEST_VERSION in result


def test_find_examples_found_results(seeded):
    pg, neo4j_driver = seeded
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _find_examples

    result = _find_examples(
        "confirm sale", odoo_version=TEST_VERSION, limit=2,
        _driver=neo4j_driver, _pg_conn=pg, _embedder=FakeEmbedder(dim=1024),
    )
    assert "Found" in result


def test_find_examples_output_has_score_and_file(seeded):
    pg, neo4j_driver = seeded
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _find_examples

    result = _find_examples(
        "partner name field", odoo_version=TEST_VERSION,
        _driver=neo4j_driver, _pg_conn=pg, _embedder=FakeEmbedder(dim=1024),
    )
    assert "· score" in result
    assert "File:" in result


def test_find_examples_chunk_type_filter(seeded):
    pg, neo4j_driver = seeded
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _find_examples

    result = _find_examples(
        "any query", odoo_version=TEST_VERSION, chunk_types=["field"],
        _driver=neo4j_driver, _pg_conn=pg, _embedder=FakeEmbedder(dim=1024),
    )
    assert "· field ·" in result
    assert "· method ·" not in result


def test_find_examples_empty_db_returns_zero(clean_pg_embeddings, clean_neo4j):
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _find_examples

    result = _find_examples(
        "anything", odoo_version=TEST_VERSION,
        _driver=clean_neo4j, _pg_conn=clean_pg_embeddings, _embedder=FakeEmbedder(dim=1024),
    )
    assert "Found 0" in result
