# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_backfill_patterns.py
"""Tests for ops/backfill_patterns.py.

Verifies that the backfill script:
1. Inserts all 115 (or actual count) patterns from patterns.json.
2. Data written to DB matches JSON source for 3 randomly sampled patterns.
3. Running backfill twice yields 0 inserted, 0 updated on second run.
4. A drifted DB row is detected and corrected on re-run.
5. DB row count meets ADR-0009 minimum of 80 patterns.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
from __future__ import annotations

import json
import random

import pytest

from ops.backfill_patterns import PATTERNS_JSON, backfill
from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_patterns(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM patterns WHERE soft_deleted = FALSE")
        return cur.fetchone()[0]


def _fetch_pattern(conn, pattern_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pattern_id, intent_keywords, file_ref, snippet_text,
                   gotchas, odoo_version_min, odoo_version_max,
                   language, core_symbol_names
              FROM patterns
             WHERE pattern_id = %s
            """,
            (pattern_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "pattern_id": row[0],
        "intent_keywords": row[1],
        "file_ref": row[2],
        "snippet_text": row[3],
        "gotchas": row[4],
        "odoo_version_min": row[5],
        "odoo_version_max": row[6],
        "language": row[7],
        "core_symbol_names": row[8],
    }


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_pg(clean_pg):
    """Migrate schema (patterns table) and yield a clean connection.

    Drops patterns table before + after to ensure full isolation across
    repeated test runs.
    """
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")
    run_migrations(clean_pg)
    yield clean_pg
    with clean_pg.cursor() as cur:
        cur.execute("DELETE FROM patterns")
    clean_pg.commit()


# ---------------------------------------------------------------------------
# Test 1: backfill inserts all patterns
# ---------------------------------------------------------------------------


class TestBackfillInsertsAll:
    def test_inserts_all_patterns(self, fresh_pg):
        """Backfill must insert exactly as many rows as patterns.json has entries."""
        expected = len(json.loads(PATTERNS_JSON.read_text()))
        ins, upd = backfill(fresh_pg)
        fresh_pg.commit()

        db_count = _count_patterns(fresh_pg)

        assert ins == expected, (
            f"Expected {expected} inserted rows, got {ins}"
        )
        assert upd == 0, f"Expected 0 updated on first run, got {upd}"
        assert db_count == expected, (
            f"DB has {db_count} rows, expected {expected}"
        )


# ---------------------------------------------------------------------------
# Test 2: data parity between DB and JSON
# ---------------------------------------------------------------------------


class TestBackfillDataParity:
    def test_three_sampled_patterns_match_json(self, fresh_pg):
        """3 randomly sampled patterns must have identical field values in DB."""
        backfill(fresh_pg)
        fresh_pg.commit()

        source = json.loads(PATTERNS_JSON.read_text())
        # Use fixed seed for reproducibility inside the test suite
        rng = random.Random(42)
        samples = rng.sample(source, min(3, len(source)))

        for raw in samples:
            pid = raw["pattern_id"]
            db = _fetch_pattern(fresh_pg, pid)
            assert db is not None, f"pattern_id={pid!r} not found in DB"

            # intent_keywords: JSON list == DB TEXT[]
            assert sorted(db["intent_keywords"]) == sorted(
                raw.get("intent_keywords", [])
            ), f"{pid}: intent_keywords mismatch"

            # file_ref
            assert db["file_ref"] == raw["file_ref"], f"{pid}: file_ref mismatch"

            # snippet_text
            assert db["snippet_text"] == raw["snippet_text"], (
                f"{pid}: snippet_text mismatch"
            )

            # gotchas: JSON array stored as JSONB; compare as Python lists
            db_gotchas = (
                db["gotchas"]
                if isinstance(db["gotchas"], list)
                else json.loads(db["gotchas"])
            )
            assert db_gotchas == raw.get("gotchas", []), (
                f"{pid}: gotchas mismatch"
            )

            # version fields
            assert db["odoo_version_min"] == raw["odoo_version_min"], (
                f"{pid}: odoo_version_min mismatch"
            )
            assert db["odoo_version_max"] == raw.get("odoo_version_max"), (
                f"{pid}: odoo_version_max mismatch (expected {raw.get('odoo_version_max')!r})"
            )

            # language
            assert db["language"] == raw["language"], f"{pid}: language mismatch"

            # core_symbol_names: JSON list == DB TEXT[] (order may differ)
            assert sorted(db["core_symbol_names"]) == sorted(
                raw.get("core_symbol_names", [])
            ), f"{pid}: core_symbol_names mismatch"


# ---------------------------------------------------------------------------
# Test 3: idempotency — zero diff on re-run
# ---------------------------------------------------------------------------


class TestBackfillIdempotentZeroDiff:
    def test_second_run_zero_inserted_updated(self, fresh_pg):
        """Running backfill twice must yield 0 inserted, 0 updated on second run.

        This verifies the ON CONFLICT ... WHERE (drift detection) logic correctly
        identifies that no field has changed between runs.
        """
        # First run — populate
        ins1, upd1 = backfill(fresh_pg)
        fresh_pg.commit()
        assert ins1 > 0, "First run should have inserted rows"

        # Second run — must be a no-op
        ins2, upd2 = backfill(fresh_pg)
        fresh_pg.commit()

        assert ins2 == 0, (
            f"Second run should insert 0, got {ins2}. "
            "ON CONFLICT skip-when-no-diff not working."
        )
        assert upd2 == 0, (
            f"Second run should update 0, got {upd2}. "
            "Drift detection fired falsely on identical data."
        )


# ---------------------------------------------------------------------------
# Test 4: drift detection — drifted row is corrected on re-run
# ---------------------------------------------------------------------------


class TestBackfillDetectsDrift:
    def test_drifted_row_is_updated(self, fresh_pg):
        """A row modified in DB must be corrected when backfill runs again.

        This ensures the ON CONFLICT ... WHERE drift detection covers
        at least the snippet_text field.
        """
        # Populate DB
        backfill(fresh_pg)
        fresh_pg.commit()

        # Pick the first pattern from JSON as target
        source = json.loads(PATTERNS_JSON.read_text())
        target_id = source[0]["pattern_id"]
        original_snippet = source[0]["snippet_text"]

        # Corrupt the snippet in DB
        with fresh_pg.cursor() as cur:
            cur.execute(
                "UPDATE patterns SET snippet_text = %s WHERE pattern_id = %s",
                ("# CORRUPTED", target_id),
            )
        fresh_pg.commit()

        # Confirm corruption
        db_before = _fetch_pattern(fresh_pg, target_id)
        assert db_before["snippet_text"] == "# CORRUPTED", "Corruption setup failed"

        # Re-run backfill — must correct the drifted row
        ins, upd = backfill(fresh_pg)
        fresh_pg.commit()

        db_after = _fetch_pattern(fresh_pg, target_id)
        assert db_after["snippet_text"] == original_snippet, (
            f"Drift not corrected. snippet_text still: {db_after['snippet_text']!r}"
        )
        assert upd >= 1, (
            f"Expected at least 1 updated row (the drifted one), got {upd}"
        )
        assert ins == 0, (
            f"Expected 0 inserted on re-run (only update), got {ins}"
        )


# ---------------------------------------------------------------------------
# Test 5: ADR-0009 minimum 80 patterns regression guard
# ---------------------------------------------------------------------------


class TestMeetsAdr0009Minimum:
    def test_at_least_80_patterns(self, fresh_pg):
        """After backfill, patterns table must have >= 80 rows.

        ADR-0009 enforces a catalogue minimum of 80 curated entries.
        This test is a regression guard: if patterns.json is accidentally
        truncated, this test turns red before production is affected.
        """
        backfill(fresh_pg)
        fresh_pg.commit()

        count = _count_patterns(fresh_pg)
        assert count >= 80, (
            f"patterns table has only {count} rows; ADR-0009 requires >= 80. "
            "Was patterns.json accidentally truncated?"
        )
