# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migration_m13_012.py
"""Migration tests for m13_012_patterns.sql.

Verifies that after run_migrations():
1. patterns table exists with correct columns, types, and 3 indexes.
2. GIN index on intent_keywords is present and is actually a GIN index.
3. pattern_id is PRIMARY KEY (uniqueness enforced).
4. soft_deleted column defaults to FALSE.
5. Re-running migration is idempotent (safe to apply twice).
6. intent_keywords / core_symbol_names are TEXT[] (not JSONB).

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import psycopg2
import pytest

from src.db.migrate import run_migrations

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


def _index_exists(conn, index_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s",
            (index_name,),
        )
        return cur.fetchone() is not None


def _index_method(conn, index_name: str) -> str | None:
    """Return the access method (e.g. 'btree', 'gin') for index_name, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT am.amname
              FROM pg_indexes  idx
              JOIN pg_class    cls ON cls.relname = idx.indexname
              JOIN pg_am       am  ON am.oid      = cls.relam
             WHERE idx.indexname = %s
            """,
            (index_name,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
            (table,),
        )
        return cur.fetchone() is not None


def _ensure_osm_reader(conn) -> None:
    """Create the osm_reader role if absent so the migration's guarded GRANT
    block actually runs and can be asserted.  Mirrors deploy order (ops creates
    the role, then migrate runs).  Passwordless NOLOGIN — the test only needs
    the grant target to exist; it never connects as it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='osm_reader') "
            "THEN CREATE ROLE osm_reader NOLOGIN; END IF; END $$;"
        )


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
    """
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")
    _ensure_osm_reader(clean_pg)
    run_migrations(clean_pg)
    yield clean_pg
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")


# ---------------------------------------------------------------------------
# Test 1: table exists with correct columns
# ---------------------------------------------------------------------------


class TestPatternsTableExists:
    def test_table_exists(self, migrated_pg):
        """patterns table must exist after migration."""
        assert _table_exists(migrated_pg, "patterns"), "patterns table not found"

    def test_required_columns_present(self, migrated_pg):
        """All expected columns must be present."""
        cols = _column_info(migrated_pg, "patterns")
        expected = {
            "pattern_id",
            "intent_keywords",
            "file_ref",
            "snippet_text",
            "gotchas",
            "odoo_version_min",
            "odoo_version_max",
            "language",
            "core_symbol_names",
            "metadata",
            "created_at",
            "updated_at",
            "updated_by",
            "soft_deleted",
        }
        assert expected.issubset(cols.keys()), (
            f"Missing columns: {expected - cols.keys()}"
        )

    def test_not_null_columns(self, migrated_pg):
        """Core NOT NULL columns must be NOT NULL."""
        cols = _column_info(migrated_pg, "patterns")
        not_null_expected = {
            "pattern_id",
            "intent_keywords",
            "file_ref",
            "snippet_text",
            "gotchas",
            "odoo_version_min",
            "language",
            "core_symbol_names",
            "metadata",
            "created_at",
            "updated_at",
            "soft_deleted",
        }
        for col in not_null_expected:
            assert cols[col][1] == "NO", f"{col} must be NOT NULL"

    def test_nullable_columns(self, migrated_pg):
        """Optional columns must be nullable."""
        cols = _column_info(migrated_pg, "patterns")
        for col in ("odoo_version_max", "updated_by"):
            assert cols[col][1] == "YES", f"{col} must be nullable"

    def test_three_indexes_exist(self, migrated_pg):
        """All three expected indexes must exist."""
        for idx in (
            "idx_patterns_intent_keywords_gin",
            "idx_patterns_language",
            "idx_patterns_version_min",
        ):
            assert _index_exists(migrated_pg, idx), f"Index {idx!r} missing"


# ---------------------------------------------------------------------------
# Test 2: GIN index on intent_keywords
# ---------------------------------------------------------------------------


class TestGinIndexOnIntentKeywords:
    def test_gin_index_method(self, migrated_pg):
        """idx_patterns_intent_keywords_gin must be a GIN index (not btree)."""
        method = _index_method(migrated_pg, "idx_patterns_intent_keywords_gin")
        assert method == "gin", (
            f"Expected GIN index, got {method!r}. "
            "TEXT[] with GIN enables fast overlap/containment queries."
        )


# ---------------------------------------------------------------------------
# Test 3: pattern_id is PRIMARY KEY (uniqueness)
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
# Test 4: soft_deleted defaults to FALSE
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
# Test 5: idempotent re-run of migration
# ---------------------------------------------------------------------------


class TestIdempotentMigrationRerun:
    def test_rerun_is_safe(self, migrated_pg):
        """run_migrations() called a second time must not raise.

        CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS ensure
        re-entrant safety on an already-migrated schema.
        """
        # Second call — must not raise
        try:
            run_migrations(migrated_pg)
        except Exception as exc:
            pytest.fail(f"run_migrations raised on second call: {exc}")

        # Table must still exist
        assert _table_exists(migrated_pg, "patterns"), (
            "patterns table disappeared after second migration run"
        )


# ---------------------------------------------------------------------------
# Test 6: intent_keywords and core_symbol_names are TEXT[] not JSONB
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
# Test 7: osm_reader read grant (deploy-blocking R1 — ADR-0042 / ADR-0034)
# ---------------------------------------------------------------------------


class TestOsmReaderGrant:
    """The migration must self-grant SELECT on patterns to osm_reader.

    MCP (which connects as osm_reader under RLS) reads the curated pattern
    catalogue at runtime.  Without the grant the read hits permission-denied,
    which the code swallows → silent fallback to in-process pattern defaults.
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

    def test_migration_safe_without_osm_reader(self, migrated_pg):
        """With no osm_reader role the migration must still apply cleanly
        (GRANT guarded by pg_roles EXISTS). migrated_pg does not create the
        role, so reaching this assertion proves no failure."""
        assert _table_exists(migrated_pg, "patterns"), (
            "patterns table missing after migrate without osm_reader"
        )
