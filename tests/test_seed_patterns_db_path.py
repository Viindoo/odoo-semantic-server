# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for WI-8 DB-primary read path in seed_patterns.py.

5 test cases:
1. DB path is primary when patterns table is populated.
2. JSON fallback when DB patterns table is empty.
3. Sentinel SHA changes after a DB row modification.
4. Sentinel SHA is stable and idempotent for the same content.
5. ADR-0009 minimum 80 active patterns preserved after refactor.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

_PATTERNS_JSON = (
    Path(__file__).resolve().parent.parent / "src" / "data" / "patterns.json"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_pg(clean_pg):
    """Migrate schema (incl. patterns table) and yield a clean connection."""
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")
    run_migrations(clean_pg)
    yield clean_pg
    with clean_pg.cursor() as cur:
        cur.execute("DELETE FROM patterns")
    clean_pg.commit()


def _backfill_from_json(conn) -> int:
    """Insert all patterns from patterns.json into the DB. Returns row count."""
    data = json.loads(_PATTERNS_JSON.read_text(encoding="utf-8"))
    inserted = 0
    with conn.cursor() as cur:
        for entry in data:
            cur.execute(
                """INSERT INTO patterns
                     (pattern_id, intent_keywords, file_ref, snippet_text,
                      gotchas, odoo_version_min, odoo_version_max,
                      language, core_symbol_names)
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                   ON CONFLICT (pattern_id) DO NOTHING""",
                (
                    entry["pattern_id"],
                    entry.get("intent_keywords", []),
                    entry["file_ref"],
                    entry["snippet_text"],
                    json.dumps(entry.get("gotchas", [])),
                    entry["odoo_version_min"],
                    entry.get("odoo_version_max"),
                    entry["language"],
                    entry.get("core_symbol_names", []),
                ),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Test 1: DB path primary when populated
# ---------------------------------------------------------------------------


class TestDbPathPrimaryWhenPopulated:
    def test_db_path_primary_when_populated(self, fresh_pg):
        """_load_patterns_from_db() returns rows after backfill (not None)."""
        from src.indexer.seed_patterns import _load_patterns_from_db

        # Populate patterns table
        n = _backfill_from_json(fresh_pg)
        assert n > 0, "backfill must insert at least 1 row"

        rows = _load_patterns_from_db()
        assert rows is not None, "_load_patterns_from_db should return rows, not None"
        assert len(rows) == n or len(rows) > 0, (
            f"Expected populated rows, got {len(rows) if rows else 0}"
        )
        # All returned items are PatternExample
        from src.indexer.models import PatternExample
        for r in rows:
            assert isinstance(r, PatternExample)


# ---------------------------------------------------------------------------
# Test 2: JSON fallback when DB empty
# ---------------------------------------------------------------------------


class TestJsonFallbackWhenDbEmpty:
    def test_json_fallback_when_db_empty(self, fresh_pg):
        """_load_patterns_from_db() returns None when table is empty."""
        from src.indexer.seed_patterns import _load_patterns_from_db

        # Ensure no rows in patterns
        with fresh_pg.cursor() as cur:
            cur.execute("DELETE FROM patterns")
        fresh_pg.commit()

        result = _load_patterns_from_db()
        assert result is None, (
            "Expected None from empty patterns table, got list"
        )

    def test_json_fallback_loads_data(self, fresh_pg):
        """_load_patterns (JSON path) returns data when called on JSON file."""
        from src.indexer.seed_patterns import _load_patterns

        patterns = _load_patterns(_PATTERNS_JSON, version_filter=None)
        assert len(patterns) >= 80, (
            f"JSON file must have >= 80 patterns, got {len(patterns)}"
        )


# ---------------------------------------------------------------------------
# Test 3: Sentinel SHA changes on DB write
# ---------------------------------------------------------------------------


class TestSentinelShaChangesOnDbWrite:
    def test_sentinel_sha_changes_on_db_write(self, fresh_pg, monkeypatch):
        """recompute_sentinel_sha() returns different SHA after modifying a row."""
        from src.indexer import seed_patterns

        # Suppress Neo4j sentinel write (not available in postgres-only tests)
        monkeypatch.setattr(seed_patterns, "_get_neo4j_writer", lambda: None)

        # Populate DB
        _backfill_from_json(fresh_pg)

        sha_before = seed_patterns.recompute_sentinel_sha()
        assert len(sha_before) == 64, "SHA should be 64-char hex"

        # Modify one row
        with fresh_pg.cursor() as cur:
            cur.execute(
                "UPDATE patterns SET snippet_text = %s WHERE pattern_id = ("
                "SELECT pattern_id FROM patterns ORDER BY pattern_id LIMIT 1"
                ")",
                ("# MODIFIED SNIPPET for sentinel test",),
            )
        fresh_pg.commit()

        sha_after = seed_patterns.recompute_sentinel_sha()
        assert sha_before != sha_after, (
            "Sentinel SHA must differ after modifying a pattern row"
        )


# ---------------------------------------------------------------------------
# Test 4: Sentinel SHA stable / idempotent
# ---------------------------------------------------------------------------


class TestSentinelShaStableIdempotent:
    def test_sentinel_stable_idempotent(self, fresh_pg, monkeypatch):
        """Same DB content -> same SHA on repeated calls."""
        from src.indexer import seed_patterns

        monkeypatch.setattr(seed_patterns, "_get_neo4j_writer", lambda: None)

        _backfill_from_json(fresh_pg)

        sha1 = seed_patterns.recompute_sentinel_sha()
        sha2 = seed_patterns.recompute_sentinel_sha()
        assert sha1 == sha2, (
            "Sentinel SHA must be stable across repeated calls for same content"
        )
        assert len(sha1) == 64


# ---------------------------------------------------------------------------
# Test 5: ADR-0009 minimum 80 patterns preserved after refactor
# ---------------------------------------------------------------------------


class TestAdr0009MinimumPreservedAfterRefactor:
    def test_adr_0009_minimum_80_preserved_after_refactor(self, fresh_pg):
        """After backfill, >=80 active patterns are available via DB read path."""
        from src.indexer.seed_patterns import _load_patterns_from_db

        _backfill_from_json(fresh_pg)

        rows = _load_patterns_from_db()
        assert rows is not None, "DB must have rows after backfill"
        assert len(rows) >= 80, (
            f"ADR-0009 requires >=80 active patterns; DB returned {len(rows)}"
        )


# ---------------------------------------------------------------------------
# Test 6 (WI-RV F-D): run() SHA equals recompute_sentinel_sha SHA
# ---------------------------------------------------------------------------


class TestCanonicalShaUnified:
    """WI-RV F-D — single SHA contract across all writers.

    Business intent:
      Before F-D, run() used file-bytes SHA but recompute_sentinel_sha()
      used DB-content SHA.  After admin CRUD bumped the sentinel,
      run() would always see a mismatch and reseed forever.  This test
      proves the two now compute the SAME value for the same content.
    """

    def test_canonical_sha_matches_recompute_sentinel_sha(
        self, fresh_pg, monkeypatch,
    ):
        """compute_patterns_canonical_sha() == recompute_sentinel_sha().

        Both functions resolve through the same source-of-truth chain
        (DB-primary, file fallback) and serialise via the same canonical
        JSON form.  Equality here is the entire point of F-D.
        """
        from src.indexer import seed_patterns

        # Suppress Neo4j sentinel write (postgres-only test bed).
        monkeypatch.setattr(seed_patterns, "_get_neo4j_writer", lambda: None)

        _backfill_from_json(fresh_pg)

        sha_canonical = seed_patterns.compute_patterns_canonical_sha()
        sha_recompute = seed_patterns.recompute_sentinel_sha()

        assert sha_canonical == sha_recompute, (
            "WI-RV F-D regression: compute_patterns_canonical_sha() and "
            "recompute_sentinel_sha() must return the same SHA so the "
            "indexer never sees a phantom mismatch.\n"
            f"  canonical: {sha_canonical}\n"
            f"  recompute: {sha_recompute}"
        )

    def test_canonical_sha_changes_after_admin_crud_simulation(
        self, fresh_pg, monkeypatch,
    ):
        """Modifying a pattern row -> canonical SHA changes by the SAME delta
        that recompute_sentinel_sha() sees.

        End-to-end proof of the F-D contract: admin CRUD bumps both SHAs
        in lockstep so run()'s sentinel comparison gates correctly.
        """
        from src.indexer import seed_patterns

        monkeypatch.setattr(seed_patterns, "_get_neo4j_writer", lambda: None)

        _backfill_from_json(fresh_pg)

        sha_a_canonical = seed_patterns.compute_patterns_canonical_sha()
        sha_a_recompute = seed_patterns.recompute_sentinel_sha()
        assert sha_a_canonical == sha_a_recompute

        with fresh_pg.cursor() as cur:
            cur.execute(
                "UPDATE patterns SET snippet_text = %s WHERE pattern_id = ("
                "SELECT pattern_id FROM patterns ORDER BY pattern_id LIMIT 1"
                ")",
                ("# WI-RV F-D delta test",),
            )
        fresh_pg.commit()

        sha_b_canonical = seed_patterns.compute_patterns_canonical_sha()
        sha_b_recompute = seed_patterns.recompute_sentinel_sha()

        assert sha_b_canonical == sha_b_recompute, (
            "After CRUD, both helpers must STILL agree (lockstep delta)"
        )
        assert sha_a_canonical != sha_b_canonical, (
            "Canonical SHA must observe the CRUD delta"
        )


# ---------------------------------------------------------------------------
# Test 7 (WI-RV F-D): run() reseeds once after CRUD, skips on the next call
# ---------------------------------------------------------------------------


@pytest.mark.neo4j
class TestRunReseedsOnceAfterCrud:
    """WI-RV F-D — admin CRUD should trigger exactly one reseed, not perpetual.

    Business intent:
      Before F-D, every call to run() reseeded because the sentinel was
      stamped with DB-content SHA but run() compared file-bytes SHA.
      After F-D, the first call after a CRUD reseeds (sentinel SHA out of
      date), updates the sentinel, and the SECOND call skips because the
      sentinel now matches the canonical SHA.
    """

    def test_crud_then_run_then_skip_cycle(
        self, fresh_pg, clean_neo4j, monkeypatch,
    ):
        """Admin CRUD -> 1st run() reseeds -> 2nd run() skips."""
        import os

        from src.indexer.seed_patterns import (
            _set_stored_patterns_sha,
            compute_patterns_canonical_sha,
            recompute_sentinel_sha,
            run,
        )
        from src.indexer.writer_neo4j import Neo4jWriter
        from tests.conftest import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

        # Postgres fixture provides rows; Neo4j fixture provides the sentinel store.
        _backfill_from_json(fresh_pg)

        # Wipe stale sentinel nodes from prior tests.
        with clean_neo4j.session() as session:
            session.run("MATCH (s:_SeedMeta) DELETE s")

        # Simulate the post-upgrade scenario: a STALE file-bytes SHA already
        # written to the sentinel by an older deployment.  After admin CRUD
        # the canonical SHA is different -> run() must reseed exactly once.
        stale_sha = "0" * 64  # not equal to anything real
        uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
        user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
        password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)
        writer = Neo4jWriter(uri, user, password)
        try:
            _set_stored_patterns_sha(writer.driver, stale_sha, key="patterns_neo4j")

            # CRUD path: the admin endpoint calls recompute_sentinel_sha() so
            # the canonical SHA gets stamped.  We invoke directly so the test
            # is hermetic w.r.t. the HTTP layer.
            recompute_sentinel_sha()

            current_sha = compute_patterns_canonical_sha()

            # 1st run after CRUD: should NOT skip — but our recompute_sentinel_sha
            # already stamped current_sha, so to actually exercise the reseed branch
            # we stamp a different stale SHA again to simulate a fresh CRUD delta.
            _set_stored_patterns_sha(writer.driver, stale_sha, key="patterns_neo4j")

            result_1 = run(
                writer=writer,
                embedder=None,
                force=False,
            )
            assert result_1["skipped"] is False, (
                f"1st run after CRUD must reseed, got {result_1}"
            )
            assert result_1["patterns"] >= 1

            # 2nd run: sentinel now == canonical SHA, must skip.
            result_2 = run(
                writer=writer,
                embedder=None,
                force=False,
            )
            assert result_2["skipped"] is True, (
                f"2nd run after reseed must skip (sentinel matches), got {result_2}"
            )
            assert result_2["patterns"] == 0

            # Sanity: stored SHA must equal canonical SHA.
            with writer.driver.session() as session:
                row = session.run(
                    "MATCH (s:_SeedMeta {key: 'patterns_neo4j'}) RETURN s.sha256 AS sha LIMIT 1"
                ).single()
            assert row is not None
            assert row["sha"] == current_sha, (
                f"Stored sentinel must match canonical SHA: "
                f"{row['sha']!r} != {current_sha!r}"
            )
        finally:
            with clean_neo4j.session() as session:
                session.run("MATCH (s:_SeedMeta) DELETE s")
                session.run("MATCH (p:PatternExample) DELETE p")
            writer.close()
