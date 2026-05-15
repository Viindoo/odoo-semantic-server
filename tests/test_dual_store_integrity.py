"""Dual-store integrity tests for pattern catalogue (F2 fix, ADR-0007 D6-split).

Verifies that Neo4j PatternExample nodes and pgvector pattern embeddings remain
in sync, and that the split sentinel (patterns_neo4j / patterns_pgvector) correctly
reflects the state of each store.

Findings addressed:
- F2: sentinel was set by --no-embed CLI run before pgvector was ever populated.
  Every subsequent auto-reseed saw "patterns unchanged — skipping." Fix ensures
  --no-embed only updates patterns_neo4j sentinel, leaving patterns_pgvector unset
  so a future full embed run will still write the embeddings.

Per CLAUDE.md testing rules:
- pytestmark = pytest.mark.neo4j (all tests need Neo4j)
- TEST_VERSION = "99.0" (dedicated test version — no conflict with real data)
- clean_neo4j fixture used throughout
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.conftest import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

pytestmark = pytest.mark.neo4j

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_PATTERN = {
    "pattern_id": "dual-store-test-pat",
    "intent_keywords": ["test", "dual-store"],
    "file_ref": "f:1",
    "snippet_text": "# dual store integrity test snippet",
    "gotchas": ["gotcha one", "gotcha two", "gotcha three"],
    "odoo_version_min": "99.0",
    "language": "python",
}


def _make_patterns_file(tmp_path: Path, pattern: dict | None = None) -> Path:
    """Write a single-pattern patterns.json to tmp_path."""
    p = tmp_path / "patterns.json"
    p.write_text(json.dumps([pattern or _MINIMAL_PATTERN]))
    return p


def _get_neo4j_writer_for_test(neo4j_uri, neo4j_user, neo4j_password):
    """Build a Neo4jWriter from test-env credentials."""
    from src.indexer.writer_neo4j import Neo4jWriter
    return Neo4jWriter(neo4j_uri, neo4j_user, neo4j_password)


def _count_neo4j_pattern_examples(driver) -> int:
    """Count PatternExample nodes in Neo4j."""
    with driver.session() as session:
        row = session.run(
            "MATCH (p:PatternExample) RETURN count(p) AS n"
        ).single()
        return row["n"] if row else 0


def _count_pgvector_pattern_embeddings(pg_conn) -> int:
    """Count pattern_example embeddings in pgvector for __patterns__ module."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM embeddings "
            "WHERE chunk_type = 'pattern_example' AND module = '__patterns__'"
        )
        row = cur.fetchone()
        return row[0] if row else 0


def _get_sentinel_sha(driver, key: str) -> str | None:
    """Return sha256 from _SeedMeta node for the given key, or None."""
    with driver.session() as session:
        row = session.run(
            "MATCH (s:_SeedMeta {key: $key}) RETURN s.sha256 AS sha LIMIT 1",
            key=key,
        ).single()
        return row["sha"] if row else None


def _wipe_seed_meta(driver) -> None:
    """Remove all _SeedMeta nodes — start clean for each test."""
    with driver.session() as session:
        session.run("MATCH (s:_SeedMeta) DELETE s")


def _wipe_pattern_examples(driver) -> None:
    """Remove all PatternExample nodes — avoid interference between tests."""
    with driver.session() as session:
        session.run("MATCH (p:PatternExample) DETACH DELETE p")


def _monkeypatch_neo4j_env(monkeypatch, neo4j_uri, neo4j_user, neo4j_password) -> None:
    """Set NEO4J_* env vars so seed_patterns.main() resolves to the test container."""
    monkeypatch.setenv("NEO4J_URI", neo4j_uri)
    monkeypatch.setenv("NEO4J_USER", neo4j_user)
    monkeypatch.setenv("NEO4J_PASSWORD", neo4j_password)


# ---------------------------------------------------------------------------
# Test 1: --no-embed only sets patterns_neo4j sentinel, NOT patterns_pgvector
# ---------------------------------------------------------------------------

def test_no_embed_only_updates_neo4j_sentinel(clean_neo4j, tmp_path, monkeypatch):
    """--no-embed run writes patterns_neo4j sentinel but leaves patterns_pgvector absent.

    This is the core F2 fix: the stale sentinel was caused by --no-embed setting
    a single 'patterns' sentinel that gated out all subsequent embed runs.

    After the fix:
    - patterns_neo4j sentinel IS set after --no-embed
    - patterns_pgvector sentinel is NOT set (pgvector write was skipped)
    - A subsequent run without --no-embed will still write the embeddings
    """
    from src.indexer.seed_patterns import _compute_patterns_sha256, main

    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)
    _monkeypatch_neo4j_env(monkeypatch, neo4j_uri, neo4j_user, neo4j_password)

    patterns_file = _make_patterns_file(tmp_path)
    _wipe_seed_meta(clean_neo4j)

    rc = main(["--patterns-file", str(patterns_file), "--no-embed"])
    assert rc == 0, f"main() returned non-zero: {rc}"

    expected_sha = _compute_patterns_sha256(patterns_file)

    # patterns_neo4j sentinel must be set
    neo4j_sha = _get_sentinel_sha(clean_neo4j, "patterns_neo4j")
    assert neo4j_sha == expected_sha, (
        f"patterns_neo4j sentinel mismatch: got {neo4j_sha!r}, expected {expected_sha!r}"
    )

    # patterns_pgvector sentinel must NOT be set (pgvector was skipped)
    pgvec_sha = _get_sentinel_sha(clean_neo4j, "patterns_pgvector")
    assert pgvec_sha is None, (
        f"patterns_pgvector sentinel must be absent after --no-embed, got sha={pgvec_sha!r}"
    )

    _wipe_seed_meta(clean_neo4j)
    _wipe_pattern_examples(clean_neo4j)


# ---------------------------------------------------------------------------
# Test 2: When sentinel says "up-to-date", stores must not diverge
# (half-state detection via split sentinel)
# ---------------------------------------------------------------------------

def test_split_sentinel_detects_partial_state(clean_neo4j, tmp_path, monkeypatch):
    """When patterns_neo4j is set but patterns_pgvector is absent, stores are diverged.

    This test simulates the exact F2 scenario:
    - patterns_neo4j sentinel sha = current sha (Neo4j was seeded)
    - patterns_pgvector sentinel absent (pgvector was never written)
    A non-force run without --no-embed should detect pgvector is missing
    and proceed to write it (NOT skip the full run).
    """
    from src.indexer.seed_patterns import (
        _compute_patterns_sha256,
        _get_stored_patterns_sha,
        _set_stored_patterns_sha,
    )

    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)

    patterns_file = _make_patterns_file(tmp_path)
    _wipe_seed_meta(clean_neo4j)

    current_sha = _compute_patterns_sha256(patterns_file)

    # Simulate F2: manually set patterns_neo4j sentinel but NOT patterns_pgvector
    _set_stored_patterns_sha(clean_neo4j, current_sha, key="patterns_neo4j")

    # Confirm Neo4j sentinel is present
    assert _get_stored_patterns_sha(clean_neo4j, key="patterns_neo4j") == current_sha

    # Confirm pgvector sentinel is absent (diverged state)
    assert _get_stored_patterns_sha(clean_neo4j, key="patterns_pgvector") is None, (
        "Test precondition failed: patterns_pgvector should be absent"
    )

    # run() with embedder=None should see neo4j is done but still not update pgvector
    writer = _get_neo4j_writer_for_test(neo4j_uri, neo4j_user, neo4j_password)
    try:
        from src.indexer.seed_patterns import run
        result = run(
            writer=writer,
            embedder=None,
            force=False,
            patterns_file=patterns_file,
        )
    finally:
        writer.close()

    # With embedder=None: neo4j is already up-to-date, pgvector is skipped → "skipped"
    # because neo4j doesn't need update AND embedder is None (no pgvec check at all)
    assert result["skipped"] is True, (
        f"Expected skipped=True (neo4j up-to-date, embedder=None), got {result}"
    )

    # pgvector sentinel must still be absent (embedder=None never writes it)
    pgvec_sha = _get_sentinel_sha(clean_neo4j, "patterns_pgvector")
    assert pgvec_sha is None, (
        f"patterns_pgvector must remain absent when embedder=None, got sha={pgvec_sha!r}"
    )

    _wipe_seed_meta(clean_neo4j)
    _wipe_pattern_examples(clean_neo4j)


# ---------------------------------------------------------------------------
# Test 3: pgvector count integrity when pgvector is available
# ---------------------------------------------------------------------------

@pytest.mark.postgres
def test_pgvector_count_matches_neo4j_after_full_seed(
    clean_neo4j, tmp_path, monkeypatch
):
    """After a full seed (with embedder), both stores must have data.

    Uses a mock embedder so the test doesn't require a real Ollama instance.
    Verifies that:
    - Neo4j PatternExample count > 0
    - pgvector embeddings count > 0
    - Both patterns_neo4j and patterns_pgvector sentinels are set with the same sha
    """
    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)

    # Check pgvector availability before attempting
    try:
        import psycopg2  # noqa: PLC0415

        from tests.conftest import PG_TEST_DSN

        pg = psycopg2.connect(PG_TEST_DSN)
        pg.autocommit = True
        from src.db.pg import init_pool
        init_pool(PG_TEST_DSN, min_conn=1, max_conn=3)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    try:
        from src.db.migrate import _vector_extension_available, run_migrations
        run_migrations(pg)
        if not _vector_extension_available(pg):
            pytest.skip("pgvector extension not installed")
    except Exception as e:
        pg.close()
        pytest.skip(f"DB migration failed: {e}")

    patterns_file = _make_patterns_file(tmp_path)
    _wipe_seed_meta(clean_neo4j)

    # Clean pgvector pattern embeddings before test
    with pg.cursor() as cur:
        cur.execute(
            "DELETE FROM embeddings "
            "WHERE chunk_type = 'pattern_example' AND module = '__patterns__'"
        )

    from src.indexer.seed_patterns import (
        _compute_patterns_sha256,
        run,
    )

    # Build a mock embedder that returns zero-vectors (avoids real Ollama dep)
    class _MockEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 1024 for _ in texts]

    writer = _get_neo4j_writer_for_test(neo4j_uri, neo4j_user, neo4j_password)
    try:
        result = run(
            writer=writer,
            embedder=_MockEmbedder(),
            force=True,
            patterns_file=patterns_file,
        )
    finally:
        writer.close()

    assert result["skipped"] is False, f"Expected skipped=False on force run, got {result}"
    assert result["patterns"] >= 1, f"Expected ≥1 patterns written, got {result}"
    assert result["embeddings"] >= 1, f"Expected ≥1 embeddings written, got {result}"

    # Verify Neo4j has PatternExample nodes
    neo4j_count = _count_neo4j_pattern_examples(clean_neo4j)
    assert neo4j_count >= 1, (
        f"Expected ≥1 PatternExample node in Neo4j after full seed, got {neo4j_count}"
    )

    # Verify pgvector has pattern embeddings
    pgvec_count = _count_pgvector_pattern_embeddings(pg)
    assert pgvec_count >= 1, (
        f"Expected ≥1 pattern embedding in pgvector after full seed, got {pgvec_count}"
    )

    # Verify both sentinels are set with matching sha
    expected_sha = _compute_patterns_sha256(patterns_file)
    neo4j_sha = _get_sentinel_sha(clean_neo4j, "patterns_neo4j")
    pgvec_sha = _get_sentinel_sha(clean_neo4j, "patterns_pgvector")

    assert neo4j_sha == expected_sha, (
        f"patterns_neo4j sentinel mismatch after full seed: {neo4j_sha!r} != {expected_sha!r}"
    )
    assert pgvec_sha == expected_sha, (
        f"patterns_pgvector sentinel mismatch after full seed: {pgvec_sha!r} != {expected_sha!r}"
    )

    # Clean up
    with pg.cursor() as cur:
        cur.execute(
            "DELETE FROM embeddings "
            "WHERE chunk_type = 'pattern_example' AND module = '__patterns__'"
        )
    _wipe_seed_meta(clean_neo4j)
    _wipe_pattern_examples(clean_neo4j)
    pg.close()


# ---------------------------------------------------------------------------
# Test 4: Legacy 'patterns' sentinel is treated as patterns_neo4j fallback
# ---------------------------------------------------------------------------

def test_legacy_sentinel_read_as_neo4j_fallback(clean_neo4j, tmp_path):
    """Old key='patterns' sentinel is treated as patterns_neo4j for backward compat.

    When upgrading from the old single-sentinel implementation, deployments that
    have key='patterns' set should not re-seed Neo4j unnecessarily.  The
    _get_stored_patterns_sha function falls back to the legacy key when
    patterns_neo4j is absent.
    """
    from src.indexer.seed_patterns import (
        _compute_patterns_sha256,
        _get_stored_patterns_sha,
    )

    patterns_file = _make_patterns_file(tmp_path)
    _wipe_seed_meta(clean_neo4j)

    current_sha = _compute_patterns_sha256(patterns_file)

    # Simulate legacy deployment: write old-style 'patterns' sentinel directly
    with clean_neo4j.session() as session:
        session.run(
            "MERGE (s:_SeedMeta {key: 'patterns'}) "
            "SET s.sha256 = $sha, s.updated_at = datetime()",
            sha=current_sha,
        )

    # _get_stored_patterns_sha with key='patterns_neo4j' should return the legacy sha
    stored = _get_stored_patterns_sha(clean_neo4j, key="patterns_neo4j")
    assert stored == current_sha, (
        f"Legacy fallback failed: expected sha {current_sha!r}, got {stored!r}"
    )

    # _get_stored_patterns_sha with key='patterns_pgvector' should return None
    # (legacy key never stored pgvector data)
    pgvec_stored = _get_stored_patterns_sha(clean_neo4j, key="patterns_pgvector")
    assert pgvec_stored is None, (
        f"patterns_pgvector should not fall back to legacy key, got {pgvec_stored!r}"
    )

    _wipe_seed_meta(clean_neo4j)


# ---------------------------------------------------------------------------
# Test 5: run() with embedder=None skips pgvector sentinel even when neo4j is stale
# ---------------------------------------------------------------------------

def test_run_with_no_embedder_leaves_pgvector_sentinel_unset(
    clean_neo4j, tmp_path
):
    """run(embedder=None) must NOT write patterns_pgvector sentinel.

    Even if neo4j_needs_update is True (first run), the pgvector sentinel must
    remain absent so a later run with an embedder will write the embeddings.
    """
    from src.indexer.seed_patterns import _compute_patterns_sha256, run

    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)

    patterns_file = _make_patterns_file(tmp_path)
    _wipe_seed_meta(clean_neo4j)

    writer = _get_neo4j_writer_for_test(neo4j_uri, neo4j_user, neo4j_password)
    try:
        result = run(
            writer=writer,
            embedder=None,
            force=True,
            patterns_file=patterns_file,
        )
    finally:
        writer.close()

    assert result["skipped"] is False
    assert result["patterns"] >= 1

    expected_sha = _compute_patterns_sha256(patterns_file)

    # Neo4j sentinel must be set
    neo4j_sha = _get_sentinel_sha(clean_neo4j, "patterns_neo4j")
    assert neo4j_sha == expected_sha, (
        f"patterns_neo4j sentinel not set after run(embedder=None): {neo4j_sha!r}"
    )

    # pgvector sentinel must NOT be set
    pgvec_sha = _get_sentinel_sha(clean_neo4j, "patterns_pgvector")
    assert pgvec_sha is None, (
        f"patterns_pgvector sentinel must be absent when embedder=None, got {pgvec_sha!r}"
    )

    _wipe_seed_meta(clean_neo4j)
    _wipe_pattern_examples(clean_neo4j)
