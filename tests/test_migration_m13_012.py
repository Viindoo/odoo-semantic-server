# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migration_m13_012.py
"""Migration tests for m13_012_patterns.sql — behaviour cases only.

One-shot catalog assertions (T1 table/column existence/nullability, T2 GIN
index method check) were removed — covered by test_squashed_baseline.py golden
snapshot.

Kept behaviour cases:
  T3   pattern_id is PRIMARY KEY (uniqueness enforced via INSERT + raises).
  T4   soft_deleted column defaults to FALSE (INSERT RETURNING check).
  T5   Migration is idempotent (re-run does not raise).
  T6   intent_keywords / core_symbol_names are TEXT[] (ARRAY type, not JSONB).
  T7   osm_reader READ grant; no write grant.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import psycopg2
import psycopg2.errors
import pytest

from src.db.migrate import run_migrations
from tests.conftest import drop_osm_reader, ensure_osm_reader_or_skip

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _column_info(conn, table: str) -> dict:
    """Return {column_name: (data_type, is_nullable)} for all columns in table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def _has_priv(conn, table: str, priv: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege('osm_reader', %s, %s)",
            (table, priv),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    """Run all migrations on a clean Postgres DB and return the connection.

    Drops patterns table before migration so FK constraints are re-created
    fresh by run_migrations each time, even on repeated test runs.
    """
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")
    run_migrations(clean_pg)
    yield clean_pg
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")


@pytest.fixture
def migrated_pg_with_reader(clean_pg):
    """Like migrated_pg but creates osm_reader BEFORE migrating, so the
    migration's pg_roles-guarded GRANT block actually fires and is assertable.

    If the test DB user lacks CREATE ROLE privilege the test is individually
    skipped — not a hard error — because the failure reason is infra, not code.
    """
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")
    # ensure_osm_reader_or_skip commits on success; skips on InsufficientPrivilege.
    ensure_osm_reader_or_skip(clean_pg)
    run_migrations(clean_pg)
    yield clean_pg
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")
    drop_osm_reader(clean_pg)


# ---------------------------------------------------------------------------
# T3: pattern_id is PRIMARY KEY (uniqueness)
# ---------------------------------------------------------------------------


class TestPatternIdPrimaryKey:
    def test_duplicate_pattern_id_rejected(self, migrated_pg):
        """Inserting duplicate pattern_id must raise UniqueViolation."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO patterns (pattern_id, intent_keywords, file_ref,
                    snippet_text, gotchas, odoo_version_min, language)
                VALUES ('test-pk-dup', ARRAY['kw'], 'file.py:1',
                    'pass', '["gotcha1","gotcha2","gotcha3"]'::jsonb,
                    '17.0', 'python')
                """
            )
        migrated_pg.commit()

        with pytest.raises(psycopg2.errors.UniqueViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO patterns (pattern_id, intent_keywords, file_ref,
                        snippet_text, gotchas, odoo_version_min, language)
                    VALUES ('test-pk-dup', ARRAY['kw2'], 'file2.py:2',
                        'pass2', '["g1","g2","g3"]'::jsonb,
                        '18.0', 'python')
                    """
                )
        migrated_pg.rollback()

        # Cleanup
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM patterns WHERE pattern_id = 'test-pk-dup'")
        migrated_pg.commit()


# ---------------------------------------------------------------------------
# T4: soft_deleted defaults to FALSE
# ---------------------------------------------------------------------------


class TestSoftDeletedDefaultFalse:
    def test_soft_deleted_default(self, migrated_pg):
        """soft_deleted must default to FALSE when not specified."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO patterns (pattern_id, intent_keywords, file_ref,
                    snippet_text, gotchas, odoo_version_min, language)
                VALUES ('test-soft-del-default', ARRAY['kw'], 'file.py:1',
                    'pass', '["g1","g2","g3"]'::jsonb, '17.0', 'python')
                RETURNING soft_deleted
                """
            )
            row = cur.fetchone()
        migrated_pg.rollback()

        assert row is not None
        assert row[0] is False, f"soft_deleted default should be FALSE, got {row[0]!r}"


# ---------------------------------------------------------------------------
# T5: idempotent re-run of migration
# ---------------------------------------------------------------------------


class TestIdempotentMigrationRerun:
    def test_rerun_is_safe(self, migrated_pg):
        """run_migrations() called a second time must not raise.

        CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS ensure
        re-entrant safety on an already-migrated schema.
        """
        try:
            run_migrations(migrated_pg)
        except Exception as exc:
            pytest.fail(f"run_migrations raised on second call: {exc}")


# ---------------------------------------------------------------------------
# T6: intent_keywords and core_symbol_names are TEXT[] not JSONB
# ---------------------------------------------------------------------------


class TestTagsColumnIsTextArrayNotJsonb:
    def test_intent_keywords_is_text_array(self, migrated_pg):
        """intent_keywords must be TEXT[] (ARRAY type), not jsonb.

        TEXT[] with GIN index has better per-element operator support
        (array overlap @> etc.) vs jsonb for simple keyword lists.
        """
        cols = _column_info(migrated_pg, "patterns")
        # psycopg2 / information_schema reports TEXT[] as 'ARRAY'
        dtype = cols["intent_keywords"][0]
        assert dtype == "ARRAY", (
            f"intent_keywords data_type should be 'ARRAY' (TEXT[]), got {dtype!r}"
        )

    def test_core_symbol_names_is_text_array(self, migrated_pg):
        """core_symbol_names must also be TEXT[] (ARRAY type), not jsonb."""
        cols = _column_info(migrated_pg, "patterns")
        dtype = cols["core_symbol_names"][0]
        assert dtype == "ARRAY", (
            f"core_symbol_names data_type should be 'ARRAY' (TEXT[]), got {dtype!r}"
        )


# ---------------------------------------------------------------------------
# T7: osm_reader read grant (deploy-blocking R1 — ADR-0042 / ADR-0034)
# ---------------------------------------------------------------------------


class TestOsmReaderGrant:
    """The migration must self-grant SELECT on patterns to osm_reader.

    MCP (which connects as osm_reader under RLS) reads the curated pattern
    catalogue at runtime.  Without the grant the read hits permission-denied,
    which the code swallows -> silent fallback to in-process pattern defaults.
    `python -m src.db.migrate` does NOT run ops/rls_create_osm_reader.sql, so
    the grant must be self-contained in the migration.
    """

    def test_osm_reader_has_select(self, migrated_pg_with_reader):
        assert _has_priv(migrated_pg_with_reader, "patterns", "SELECT"), (
            "osm_reader missing SELECT on patterns — MCP pattern catalogue "
            "read will silently fall back to the in-process defaults"
        )

    def test_osm_reader_has_no_write(self, migrated_pg_with_reader):
        """osm_reader is a read role — it must NOT get INSERT/UPDATE/DELETE."""
        for priv in ("INSERT", "UPDATE", "DELETE"):
            assert not _has_priv(migrated_pg_with_reader, "patterns", priv), (
                f"osm_reader unexpectedly has {priv} on patterns (read-only role)"
            )
