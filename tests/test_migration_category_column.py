# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migration_category_column.py
"""Focused tests for migration 0002_add_category_to_patterns.sql.

Asserts:
  - column `category` exists on `patterns`, is nullable, is TEXT
  - CHECK constraint limits values to ('test', 'production')
  - index `idx_patterns_category` exists
  - migration is idempotent (second run raises no error)

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
from __future__ import annotations

import psycopg2
import psycopg2.errors
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixture: fresh schema with all migrations applied
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    """Run all migrations (0001 baseline + 0002 category) on a clean schema."""
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")
    run_migrations(clean_pg)
    yield clean_pg
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")


# ---------------------------------------------------------------------------
# Helper: minimal valid pattern row (no category)
# ---------------------------------------------------------------------------

_INSERT_PATTERN = """
INSERT INTO patterns (pattern_id, intent_keywords, file_ref,
    snippet_text, gotchas, odoo_version_min, language)
VALUES (%s, ARRAY['kw'], 'file.py:1', 'pass', '["g1"]'::jsonb, '17.0', 'python')
"""


def _query(conn, sql: str, params=None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# T1: column exists, is nullable, is TEXT
# ---------------------------------------------------------------------------


class TestCategoryColumnExists:
    def test_column_is_present_and_nullable(self, migrated_pg):
        """category column must exist on patterns and be nullable (TEXT, NULL allowed)."""
        rows = _query(
            migrated_pg,
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'patterns'
              AND column_name = 'category'
            """,
        )
        assert rows, "column `category` not found on table `patterns`"
        row = rows[0]
        assert row["data_type"] == "text", (
            f"category data_type should be 'text', got {row['data_type']!r}"
        )
        assert row["is_nullable"] == "YES", (
            f"category must be nullable (is_nullable='YES'), got {row['is_nullable']!r}"
        )

    def test_null_category_is_accepted(self, migrated_pg):
        """NULL category must be accepted (backward-compatible with existing rows)."""
        with migrated_pg.cursor() as cur:
            cur.execute(_INSERT_PATTERN, ("test-cat-null",))
            cur.execute(
                "SELECT category FROM patterns WHERE pattern_id = %s",
                ("test-cat-null",),
            )
            row = cur.fetchone()
        migrated_pg.rollback()
        assert row is not None
        assert row[0] is None, f"category should be NULL when omitted, got {row[0]!r}"


# ---------------------------------------------------------------------------
# T2: CHECK constraint enforces ('test', 'production')
# ---------------------------------------------------------------------------


class TestCategoryCheckConstraint:
    def test_valid_test_value_accepted(self, migrated_pg):
        """category='test' must be accepted by the CHECK constraint."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO patterns (pattern_id, intent_keywords, file_ref,
                    snippet_text, gotchas, odoo_version_min, language, category)
                VALUES ('test-cat-test', ARRAY['kw'], 'file.py:1',
                    'pass', '["g1"]'::jsonb, '17.0', 'python', 'test')
                RETURNING category
                """,
            )
            row = cur.fetchone()
        migrated_pg.rollback()
        assert row is not None
        assert row[0] == "test", f"category should be 'test', got {row[0]!r}"

    def test_valid_production_value_accepted(self, migrated_pg):
        """category='production' must be accepted by the CHECK constraint."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO patterns (pattern_id, intent_keywords, file_ref,
                    snippet_text, gotchas, odoo_version_min, language, category)
                VALUES ('test-cat-prod', ARRAY['kw'], 'file.py:1',
                    'pass', '["g1"]'::jsonb, '17.0', 'python', 'production')
                RETURNING category
                """,
            )
            row = cur.fetchone()
        migrated_pg.rollback()
        assert row is not None
        assert row[0] == "production", f"category should be 'production', got {row[0]!r}"

    def test_invalid_value_rejected(self, migrated_pg):
        """category='invalid' must be rejected by the CHECK constraint."""
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO patterns (pattern_id, intent_keywords, file_ref,
                        snippet_text, gotchas, odoo_version_min, language, category)
                    VALUES ('test-cat-bad', ARRAY['kw'], 'file.py:1',
                        'pass', '["g1"]'::jsonb, '17.0', 'python', 'invalid')
                    """,
                )
        migrated_pg.rollback()

    def test_check_constraint_exists_in_catalog(self, migrated_pg):
        """pg_constraint must have a CHECK entry for patterns.category."""
        rows = _query(
            migrated_pg,
            """
            SELECT conname, contype, pg_get_constraintdef(oid) AS def
            FROM pg_constraint
            WHERE conrelid = 'patterns'::regclass
              AND conname = 'patterns_category_check'
            """,
        )
        assert rows, "patterns_category_check not found in pg_constraint"
        row = rows[0]
        assert row["contype"] == "c", (
            f"patterns_category_check should be CHECK (type 'c'), got {row['contype']!r}"
        )
        assert "test" in row["def"] and "production" in row["def"], (
            f"CHECK def should reference 'test' and 'production'; got: {row['def']}"
        )


# ---------------------------------------------------------------------------
# T3: index idx_patterns_category exists
# ---------------------------------------------------------------------------


class TestCategoryIndexExists:
    def test_index_exists(self, migrated_pg):
        """idx_patterns_category must exist on patterns(category)."""
        rows = _query(
            migrated_pg,
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'patterns'
              AND indexname = 'idx_patterns_category'
            """,
        )
        assert rows, "index `idx_patterns_category` not found on table `patterns`"
        row = rows[0]
        assert "category" in row["indexdef"], (
            f"index def should reference 'category'; got: {row['indexdef']}"
        )


# ---------------------------------------------------------------------------
# T4: idempotent re-run (ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS)
# ---------------------------------------------------------------------------


class TestIdempotentRerun:
    def test_second_migration_run_is_safe(self, migrated_pg):
        """run_migrations() called a second time must not raise.

        ADD COLUMN IF NOT EXISTS and CREATE INDEX IF NOT EXISTS make the
        migration safe to re-apply against an already-migrated schema.
        """
        try:
            run_migrations(migrated_pg)
        except Exception as exc:
            pytest.fail(f"run_migrations raised on second call: {exc}")
