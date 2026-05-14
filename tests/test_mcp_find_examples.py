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
    write_module_embeddings("sale", TEST_VERSION, sale_chunks, embedder)
    write_module_embeddings("base", TEST_VERSION, base_chunks, embedder)

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


def test_find_examples_rerank_by_dependents(clean_pg_embeddings, clean_neo4j):
    """Modules with more dependents rank higher when cosine scores are tied.

    FakeEmbedder returns identical vectors for all texts (same seed resets per
    embed() call) so cosine scores are equal — rerank must break the tie via
    the dependents bonus.
    """
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
    from src.mcp.server import _find_examples

    # Seed Neo4j: popular_mod has 5 modules depending on it; isolated_mod has 0
    with clean_neo4j.session() as s:
        s.run("MERGE (:Module {name:'popular_mod', odoo_version:$v})", v=TEST_VERSION)
        s.run("MERGE (:Module {name:'isolated_mod', odoo_version:$v})", v=TEST_VERSION)
        for i in range(5):
            dep = f"dep_{i}"
            s.run("MERGE (:Module {name:$n, odoo_version:$v})", n=dep, v=TEST_VERSION)
            s.run("""
                MATCH (d:Module {name:$dep, odoo_version:$v})
                MATCH (p:Module {name:'popular_mod', odoo_version:$v})
                MERGE (d)-[:DEPENDS_ON]->(p)
            """, dep=dep, v=TEST_VERSION)

    # Same content → FakeEmbedder gives same cosine score to both chunks
    shared_content = f"[mod] example.action ({TEST_VERSION})\ndef action(self): pass"
    popular_chunk = EmbeddingChunk(
        "method", "popular_mod", TEST_VERSION,
        "popular_mod.example.action", "example.model",
        "popular_mod/models/m.py", 0, shared_content,
    )
    isolated_chunk = EmbeddingChunk(
        "method", "isolated_mod", TEST_VERSION,
        "isolated_mod.example.action", "example.model",
        "isolated_mod/models/m.py", 0, shared_content,
    )
    embedder = FakeEmbedder(dim=1024)
    write_module_embeddings("popular_mod", TEST_VERSION, [popular_chunk], embedder)
    write_module_embeddings("isolated_mod", TEST_VERSION, [isolated_chunk], embedder)

    result = _find_examples(
        "example action", odoo_version=TEST_VERSION, limit=5,
        _driver=clean_neo4j, _pg_conn=clean_pg_embeddings, _embedder=embedder,
    )

    popular_pos = result.find("popular_mod")
    isolated_pos = result.find("isolated_mod")
    assert popular_pos != -1, "popular_mod chunk must appear in results"
    assert isolated_pos != -1, "isolated_mod chunk must appear in results"
    assert popular_pos < isolated_pos, (
        "popular_mod (5 dependents) must rank above isolated_mod (0 dependents)"
    )


# --- Graceful degradation when embedder unavailable (I4) -------------------

class _BrokenEmbedder:
    """Simulates Ollama unreachable / model not loaded."""

    def embed(self, texts):
        raise ConnectionError("Connection refused: http://localhost:11434")


def test_find_examples_handles_embedder_call_failure(seeded):
    """When embedder.embed() raises, return actionable error message instead of 500."""
    pg, neo4j_driver = seeded
    from src.mcp.server import _find_examples

    result = _find_examples(
        "confirm sale", odoo_version=TEST_VERSION,
        _driver=neo4j_driver, _pg_conn=pg, _embedder=_BrokenEmbedder(),
    )
    assert "embedding query failed" in result.lower()
    assert "ollama" in result.lower() or "11434" in result
    assert "Found 0 results" in result


def test_find_examples_handles_embedder_construction_failure(seeded, monkeypatch):
    """When _get_embedder() raises (config missing, model unavailable), surface friendly error."""
    pg, neo4j_driver = seeded
    from src.mcp import server as srv

    # Force _get_embedder to raise — simulate misconfigured embedder section
    def _broken():
        raise RuntimeError("EMBEDDER_URL not set")

    monkeypatch.setattr(srv, "_get_embedder", _broken)

    result = srv._find_examples(
        "confirm sale", odoo_version=TEST_VERSION,
        _driver=neo4j_driver, _pg_conn=pg, _embedder=None,
    )
    assert "embedder unavailable" in result.lower()
    assert "Found 0 results" in result


# --- profile_name filter tests for find_examples ----------------------------


def test_find_examples_profile_none_backward_compat(seeded):
    """profile_name=None (default) returns results same as before — no regression."""
    pg, neo4j_driver = seeded
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _find_examples

    result = _find_examples(
        "confirm sale", odoo_version=TEST_VERSION, limit=2,
        profile_name=None,
        _driver=neo4j_driver, _pg_conn=pg, _embedder=FakeEmbedder(dim=1024),
    )
    # Should still return results — seeded modules have no profile array set,
    # so NULL filter passes through all nodes (backward compat).
    assert "find_examples:" in result
    assert TEST_VERSION in result


def test_find_examples_profile_filter_neo4j_rerank(seeded, clean_neo4j):
    """profile_name='profx' applied to Neo4j Module rerank — modules outside profx
    get zero dependents score (not boosted). The pgvector ANN step is unaffected.

    This test verifies the filter does not crash and returns a well-formed response.
    Since the seeded modules carry no .profile array, they score 0 dependents under
    any non-None profile filter — but the raw cosine results still surface.
    """
    pg, neo4j_driver = seeded
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _find_examples

    result = _find_examples(
        "confirm sale", odoo_version=TEST_VERSION, limit=2,
        profile_name="profx_nonexistent",
        _driver=neo4j_driver, _pg_conn=pg, _embedder=FakeEmbedder(dim=1024),
    )
    # Must return a valid header — not an error or exception
    assert "find_examples:" in result
    # pgvector path is unfiltered — results may still appear (limitation documented in ADR-0014 D6)
    assert "Found" in result
