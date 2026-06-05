# SPDX-License-Identifier: AGPL-3.0-or-later
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


# --- Graceful degradation when embedder unavailable (I4, #264) --------------
# WI-9: embedder failures now trigger lexical keyword fallback.
# Results are labelled match: lexical with a degraded banner so agents know
# quality is lower.  No exception (no RuntimeError) must escape.

class _BrokenEmbedder:
    """Simulates Ollama unreachable / model not loaded."""

    def embed(self, texts):
        raise ConnectionError("Connection refused: http://localhost:11434")


def test_find_examples_embedder_call_failure_lexical_fallback(seeded):
    """When embedder.embed() raises, lexical fallback returns match: lexical rows.

    Protects: acceptance #264 — degraded-but-useful, no RuntimeError.
    The seeded chunk entity_name 'sale.order.action_confirm' matches keyword
    'confirm' from the query 'confirm sale'.
    """
    pg, neo4j_driver = seeded
    from src.mcp.server import _find_examples

    result = _find_examples(
        "confirm sale", odoo_version=TEST_VERSION,
        _driver=neo4j_driver, _pg_conn=pg, _embedder=_BrokenEmbedder(),
    )
    # Must NOT raise — lexical fallback must produce a result
    assert "match: lexical" in result, (
        "Expected lexical fallback results with 'match: lexical' tag, got: " + result[:300]
    )
    assert "degraded" in result.lower(), (
        "Expected degraded banner in lexical fallback output"
    )
    # Must be a clean structured response, not an exception traceback
    assert "RuntimeError" not in result
    assert "Traceback" not in result


def test_find_examples_embedder_down_zero_hit_emits_exact_banner(seeded):
    """L7: embedder down AND lexical match finds nothing → exact zero-hit banner.

    The prior empty-corpus assertion (`"match: lexical" in r or "degraded" in r`)
    passed even if the zero-hit path was broken, because the OR short-circuited on
    a stray 'degraded' token elsewhere. This pins the SPECIFIC contract: when the
    embedder is unavailable and the lexical keyword search returns no rows, the
    output must be exactly "Found 0 results" + the
    "lexical search returned nothing" degraded banner — and crucially NOT the
    "lexical keyword match" banner (which only renders when rows were found).

    Fail-able: break the `if not lex_rows:` zero-hit branch (e.g. fall through to
    the found-rows banner) and this asserts the wrong banner.
    """
    pg, neo4j_driver = seeded
    from src.mcp.server import _find_examples

    # Tokens that cannot match the seeded 'sale.order.action_confirm' entity.
    result = _find_examples(
        "zxqwvb nonexistentkeyword gibberishtoken", odoo_version=TEST_VERSION,
        _driver=neo4j_driver, _pg_conn=pg, _embedder=_BrokenEmbedder(),
    )
    assert "Found 0 results" in result, (
        "Zero-hit lexical fallback must report 'Found 0 results'. Got:\n" + result[:300]
    )
    assert "lexical search returned nothing" in result, (
        "Zero-hit lexical fallback must emit the specific degraded-empty banner. "
        "Got:\n" + result[:300]
    )
    # The found-rows banner must NOT appear when there were no rows.
    assert "lexical keyword match" not in result, (
        "Zero-hit path must not render the found-rows 'lexical keyword match' banner. "
        "Got:\n" + result[:300]
    )
    assert "RuntimeError" not in result and "Traceback" not in result


def test_find_examples_embedder_construction_failure_lexical_fallback(seeded, monkeypatch):
    """When _get_embedder() raises, lexical fallback returns results.

    Protects: acceptance #264 — degraded-but-useful, no RuntimeError.
    """
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
    # Lexical fallback must kick in and return results or a clean "no results" message
    assert "match: lexical" in result or "degraded" in result.lower(), (
        "Expected lexical fallback or degraded banner, got: " + result[:300]
    )
    assert "RuntimeError" not in result


def test_find_examples_lexical_rls_scope(clean_pg_embeddings):
    """Lexical fallback respects tenant isolation — allowed=[] returns nothing.

    Protects: ADR-0034 tenant choke preserved in degraded path (WI-9, #264).
    Even with embedder down, a deny-all profile filter (allowed=[]) must cause
    lexical_example_lookup to return zero rows.  This test MUST FAIL if the
    profile_name = ANY(%s) guard is removed from example_lexical.py.

    Strategy: call lexical_example_lookup() directly (the same helper _find_examples
    invokes on the lexical path) with:
      - allowed=None  -> unrestricted; seeded row is visible (positive control)
      - allowed=[]    -> deny-all ANY('{}'); same row must be invisible (RLS guard)
    No auth stack or _effective_allowed detour needed — the choke lives entirely
    in the SQL WHERE clause parameterised by the allowed list.
    """
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
    from src.mcp.example_lexical import lexical_example_lookup
    from src.mcp.server import _rls_read_tx

    pg = clean_pg_embeddings

    # Seed one chunk whose entity_name will match the keyword 'action_confirm'.
    chunk = EmbeddingChunk(
        "method", "sale", TEST_VERSION, "sale.order.action_confirm",
        "sale.order", "sale/models/sale.py", 0,
        "def action_confirm(self): pass",
    )
    write_module_embeddings("sale", TEST_VERSION, [chunk], FakeEmbedder(dim=1024))

    # Positive control: allowed=None (admin/unrestricted) must find the seeded row.
    with _rls_read_tx(pg, None):
        with pg.cursor() as cur:
            rows_unrestricted = lexical_example_lookup(
                cur, "action_confirm", TEST_VERSION, allowed=None, limit=10,
                selected_types=[],
            )
    assert rows_unrestricted, (
        "Unrestricted lookup (allowed=None) must return the seeded row — positive control failed"
    )
    assert any(r["entity_name"] == "sale.order.action_confirm" for r in rows_unrestricted)

    # RLS guard: allowed=[] (deny-all) must return ZERO rows despite matching keyword.
    # ANY('{}') in SQL matches nothing — this is the ADR-0034 tenant choke.
    with _rls_read_tx(pg, []):
        with pg.cursor() as cur:
            rows_deny_all = lexical_example_lookup(
                cur, "action_confirm", TEST_VERSION, allowed=[], limit=10,
                selected_types=[],
            )
    assert rows_deny_all == [], (
        "Deny-all allowed=[] must return 0 rows — tenant isolation broken in lexical path. "
        f"Got {len(rows_deny_all)} row(s): {[r['entity_name'] for r in rows_deny_all]}"
    )


def test_find_examples_lexical_use_lexical_flag(seeded):
    """_use_lexical=True bypasses embed and returns lexical results directly.

    Protects: the internal _use_lexical flag works correctly (used by async wrapper).
    """
    pg, neo4j_driver = seeded
    from src.mcp.server import _find_examples

    # Explicit _use_lexical=True with profile_name=None and no embedder
    result = _find_examples(
        "confirm sale", odoo_version=TEST_VERSION,
        _driver=neo4j_driver, _pg_conn=pg, _use_lexical=True,
    )
    # entity_name 'sale.order.action_confirm' matches keyword 'confirm'
    assert "match: lexical" in result, (
        "Expected match: lexical tag in direct _use_lexical=True call"
    )
    assert "degraded" in result.lower()
    assert "RuntimeError" not in result


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
    # pgvector path is unfiltered — results may still appear (limitation documented in ADR-0016 D6)
    assert "Found" in result
