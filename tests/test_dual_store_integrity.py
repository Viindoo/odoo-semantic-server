# SPDX-License-Identifier: AGPL-3.0-or-later
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
    from src.indexer.seed_patterns import compute_patterns_canonical_sha, main

    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)
    _monkeypatch_neo4j_env(monkeypatch, neo4j_uri, neo4j_user, neo4j_password)

    patterns_file = _make_patterns_file(tmp_path)
    _wipe_seed_meta(clean_neo4j)

    rc = main(["--patterns-file", str(patterns_file), "--no-embed"])
    assert rc == 0, f"main() returned non-zero: {rc}"

    # WI-RV F-D: sentinel stores canonical (parsed-list) SHA, not file-bytes.
    expected_sha = compute_patterns_canonical_sha(patterns_file=patterns_file)

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
        _get_stored_patterns_sha,
        _set_stored_patterns_sha,
        compute_patterns_canonical_sha,
    )

    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)

    patterns_file = _make_patterns_file(tmp_path)
    _wipe_seed_meta(clean_neo4j)

    # WI-RV F-D: use canonical SHA so the manually-stamped sentinel matches
    # the value run() will compute on the next call.
    current_sha = compute_patterns_canonical_sha(patterns_file=patterns_file)

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
    clean_neo4j, clean_pg_embeddings, tmp_path, monkeypatch
):
    """After a full seed (with embedder), both stores must have data.

    Uses a mock embedder so the test doesn't require a real Ollama instance.
    Verifies that:
    - Neo4j PatternExample count > 0
    - pgvector embeddings count > 0
    - Both patterns_neo4j and patterns_pgvector sentinels are set with the same sha

    ``clean_pg_embeddings`` provides the ephemeral pgvector-enabled connection
    (skips automatically when pgvector extension is not installed).
    """
    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)

    # clean_pg_embeddings already ran run_migrations + pgvector availability check.
    # Use the connection it provides rather than opening a new one.
    pg = clean_pg_embeddings

    patterns_file = _make_patterns_file(tmp_path)
    _wipe_seed_meta(clean_neo4j)

    # Clean pgvector pattern embeddings before test
    with pg.cursor() as cur:
        cur.execute(
            "DELETE FROM embeddings "
            "WHERE chunk_type = 'pattern_example' AND module = '__patterns__'"
        )

    from src.indexer.seed_patterns import (
        compute_patterns_canonical_sha,
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
    # WI-RV F-D: canonical SHA matches what run() now stamps.
    expected_sha = compute_patterns_canonical_sha(patterns_file=patterns_file)
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
        _get_stored_patterns_sha,
        compute_patterns_canonical_sha,
    )

    patterns_file = _make_patterns_file(tmp_path)
    _wipe_seed_meta(clean_neo4j)

    # Legacy-fallback test: the legacy 'patterns' sentinel may have been
    # stamped with EITHER file-bytes or canonical SHA depending on the
    # original deployment vintage.  WI-RV F-D ships the canonical SHA path
    # going forward; the fallback _get_stored_patterns_sha() just returns
    # whatever value lives on the legacy node, so we use the canonical SHA
    # here and verify byte-for-byte equality.
    current_sha = compute_patterns_canonical_sha(patterns_file=patterns_file)

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
    from src.indexer.seed_patterns import compute_patterns_canonical_sha, run

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

    expected_sha = compute_patterns_canonical_sha(patterns_file=patterns_file)

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


# ---------------------------------------------------------------------------
# Test 6: an incomplete pattern embed keeps the rows and is retried (2026-10-21)
# ---------------------------------------------------------------------------

_REFUSED = "refused_by_the_embedder"


class _HealthyPatternEmbedder:
    """1024-dim zero vectors (no ``.dim``: the dim guard is skipped, as for the
    _MockEmbedder above)."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1024 for _ in texts]


class _FlakyPatternEmbedder(_HealthyPatternEmbedder):
    """Refuses (a 504) every request holding a _REFUSED text, or every request
    with *fail_all*."""

    def __init__(self, *, fail_all: bool = False) -> None:
        self.fail_all = fail_all

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail_all or any(_REFUSED in t for t in texts):
            raise RuntimeError("504 Gateway Time-out")
        return super().embed(texts)


def _pattern_dict(pid: str, snippet: str) -> dict:
    return {**_MINIMAL_PATTERN, "pattern_id": pid, "file_ref": f"{pid}.py:1",
            "snippet_text": snippet}


def _write_catalogue(tmp_path: Path, *patterns: dict) -> Path:
    p = tmp_path / "patterns.json"
    p.write_text(json.dumps(list(patterns)))
    return p


def _pattern_rows(pg_conn) -> dict[str, str]:
    """entity_name -> content of the pattern rows."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT entity_name, content FROM embeddings "
            "WHERE chunk_type = 'pattern_example' AND module = '__patterns__'"
        )
        return dict(cur.fetchall())


def _seed(creds, patterns_file: Path, embedder, *, force: bool = False) -> dict:
    from src.indexer.seed_patterns import run

    writer = _get_neo4j_writer_for_test(*creds)
    try:
        return run(writer=writer, embedder=embedder, force=force, patterns_file=patterns_file)
    finally:
        writer.close()


@pytest.fixture
def pattern_store(clean_neo4j, clean_pg_embeddings, monkeypatch):
    """Neo4j credentials + the pgvector connection; the catalogue is read from
    the test's patterns.json (never the DB ``patterns`` table); pattern rows,
    PatternExample nodes and sentinels wiped before and after."""
    import src.indexer.seed_patterns as sp

    monkeypatch.setattr(sp, "_load_patterns_from_db", lambda version_filter=None: None)
    creds = (
        os.getenv("NEO4J_TEST_URI", NEO4J_URI),
        os.getenv("NEO4J_TEST_USER", NEO4J_USER),
        os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD),
    )

    def wipe():
        with clean_pg_embeddings.cursor() as cur:
            cur.execute(
                "DELETE FROM embeddings "
                "WHERE chunk_type = 'pattern_example' AND module = '__patterns__'"
            )
        _wipe_seed_meta(clean_neo4j)
        _wipe_pattern_examples(clean_neo4j)

    wipe()
    yield creds, clean_pg_embeddings
    wipe()


@pytest.mark.postgres
def test_a_refused_pattern_chunk_keeps_its_row_and_the_next_reseed_retries(
    pattern_store, clean_neo4j, tmp_path,
):
    """A changed pattern the embedder refuses keeps its previous row, the other
    one is updated, and patterns_pgvector is not stamped; the next auto-reseed
    (no --force, same catalogue) re-runs only the pgvector write, embeds it and
    only then stamps the sentinel."""
    from src.indexer.seed_patterns import compute_patterns_canonical_sha

    creds, pg = pattern_store
    first = _write_catalogue(
        tmp_path, _pattern_dict("pat-a", "a v1"), _pattern_dict("pat-b", "b v1"),
    )
    _seed(creds, first, _HealthyPatternEmbedder(), force=True)
    assert set(_pattern_rows(pg)) == {"python__pat-a", "python__pat-b"}, "precondition"

    changed = _write_catalogue(
        tmp_path, _pattern_dict("pat-a", f"a v2 {_REFUSED}"), _pattern_dict("pat-b", "b v2"),
    )
    sha = compute_patterns_canonical_sha(patterns_file=changed)
    result = _seed(creds, changed, _FlakyPatternEmbedder())

    rows = _pattern_rows(pg)
    assert "a v1" in rows["python__pat-a"], "the refused chunk must keep its existing row"
    assert "b v2" in rows["python__pat-b"]
    assert result["embed_failed"] == 1
    assert _get_sentinel_sha(clean_neo4j, "patterns_neo4j") == sha
    assert _get_sentinel_sha(clean_neo4j, "patterns_pgvector") != sha, (
        "an incomplete embed must not stamp the pgvector sentinel"
    )

    retry = _seed(creds, changed, _HealthyPatternEmbedder())

    assert retry["skipped"] is False, "the unstamped sentinel must trigger a retry"
    assert retry["patterns"] == 0, "Neo4j is already current: only pgvector re-runs"
    assert retry["embed_failed"] == 0
    assert "a v2" in _pattern_rows(pg)["python__pat-a"]
    assert _get_sentinel_sha(clean_neo4j, "patterns_pgvector") == sha


@pytest.mark.postgres
def test_a_pattern_embed_with_no_vector_writes_nothing_and_is_retried(
    pattern_store, clean_neo4j, tmp_path,
):
    creds, pg = pattern_store
    catalogue = _write_catalogue(tmp_path, _pattern_dict("pat-a", "a"))

    down = _seed(creds, catalogue, _FlakyPatternEmbedder(fail_all=True))

    assert down["embed_failed"] == 1 and _pattern_rows(pg) == {}
    assert _get_sentinel_sha(clean_neo4j, "patterns_pgvector") is None

    _seed(creds, catalogue, _HealthyPatternEmbedder())

    assert set(_pattern_rows(pg)) == {"python__pat-a"}
    assert _get_sentinel_sha(clean_neo4j, "patterns_pgvector") is not None


@pytest.mark.postgres
def test_a_pattern_removed_from_the_catalogue_loses_its_rows(pattern_store, tmp_path):
    creds, pg = pattern_store
    both = _write_catalogue(tmp_path, _pattern_dict("pat-a", "a"), _pattern_dict("pat-b", "b"))
    _seed(creds, both, _HealthyPatternEmbedder(), force=True)
    assert set(_pattern_rows(pg)) == {"python__pat-a", "python__pat-b"}, "precondition"

    only_a = _write_catalogue(tmp_path, _pattern_dict("pat-a", "a"))
    _seed(creds, only_a, _HealthyPatternEmbedder())

    assert set(_pattern_rows(pg)) == {"python__pat-a"}
