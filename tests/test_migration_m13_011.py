# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migration_m13_011.py
"""Migration tests for m13_011_ee_modules.sql.

Verifies that after run_migrations():
- ee_modules table exists with correct columns, types, and UNIQUE constraint.
- idx_ee_modules_name index exists.
- Backfill count == 16 (matching _FALLBACK_EE_MODULES).
- Backfill data matches the static fallback list exactly.
- Migration is idempotent (re-run is safe, count stays 16).
- UNIQUE name constraint rejects duplicates.
- Soft-delete via deprecated=TRUE is honoured by the active-row filtered query.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import psycopg2
import psycopg2.errors
import pytest

from src.data.ee_modules import _FALLBACK_EE_MODULES
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


def _count_ee_modules(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ee_modules")
        return cur.fetchone()[0]


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

_M13_010_TABLES = ["ee_modules"]


def _drop_m13_011_tables(conn):
    for tbl in _M13_010_TABLES:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


@pytest.fixture
def migrated_pg(clean_pg):
    """Run all migrations on a clean Postgres DB and return the connection."""
    _drop_m13_011_tables(clean_pg)
    run_migrations(clean_pg)
    yield clean_pg
    _drop_m13_011_tables(clean_pg)


@pytest.fixture
def migrated_pg_with_reader(clean_pg):
    """Like migrated_pg but creates osm_reader BEFORE migrating, so the
    migration's pg_roles-guarded GRANT block actually fires and is assertable.

    If the test DB user lacks CREATE ROLE privilege the test is individually
    skipped — not a hard error — because the failure reason is infra, not code.
    """
    _drop_m13_011_tables(clean_pg)
    # ensure_osm_reader_or_skip commits on success; skips on InsufficientPrivilege.
    ensure_osm_reader_or_skip(clean_pg)
    run_migrations(clean_pg)
    yield clean_pg
    _drop_m13_011_tables(clean_pg)
    drop_osm_reader(clean_pg)


# ---------------------------------------------------------------------------
# 1. ee_modules table structure
# ---------------------------------------------------------------------------


class TestEeModulesTableExists:
    def test_table_exists(self, migrated_pg):
        """ee_modules table must exist after migration."""
        cols = _column_info(migrated_pg, "ee_modules")
        assert cols, "ee_modules table not found"

    def test_required_columns_present(self, migrated_pg):
        """All expected columns must be present."""
        cols = _column_info(migrated_pg, "ee_modules")
        expected = {
            "id",
            "name",
            "since_version",
            "vt_equivalent",
            "description",
            "deprecated",
            "created_at",
            "updated_at",
            "updated_by",
        }
        assert expected.issubset(cols.keys()), (
            f"Missing columns: {expected - cols.keys()}"
        )

    def test_name_not_nullable(self, migrated_pg):
        """name column must be NOT NULL."""
        cols = _column_info(migrated_pg, "ee_modules")
        assert cols["name"][1] == "NO", "name must be NOT NULL"

    def test_deprecated_not_nullable(self, migrated_pg):
        """deprecated column must be NOT NULL (has DEFAULT FALSE)."""
        cols = _column_info(migrated_pg, "ee_modules")
        assert cols["deprecated"][1] == "NO", "deprecated must be NOT NULL"

    def test_since_version_nullable(self, migrated_pg):
        """since_version must be nullable."""
        cols = _column_info(migrated_pg, "ee_modules")
        assert cols["since_version"][1] == "YES", "since_version must be nullable"

    def test_vt_equivalent_nullable(self, migrated_pg):
        """vt_equivalent must be nullable."""
        cols = _column_info(migrated_pg, "ee_modules")
        assert cols["vt_equivalent"][1] == "YES", "vt_equivalent must be nullable"


# ---------------------------------------------------------------------------
# 2. Index
# ---------------------------------------------------------------------------


class TestEeModulesIndexName:
    def test_index_name_exists(self, migrated_pg):
        """idx_ee_modules_name must exist."""
        assert _index_exists(migrated_pg, "idx_ee_modules_name"), (
            "idx_ee_modules_name missing after migration"
        )


# ---------------------------------------------------------------------------
# 3. Backfill count
# ---------------------------------------------------------------------------


class TestBackfillCount:
    def test_backfill_count(self, migrated_pg):
        """ee_modules must contain exactly 16 rows after migration."""
        count = _count_ee_modules(migrated_pg)
        assert count == 16, (
            f"Expected 16 backfilled rows, got {count}"
        )


# ---------------------------------------------------------------------------
# 4. Backfill data matches static fallback
# ---------------------------------------------------------------------------


class TestBackfillDataMatchesStaticFallback:
    def test_backfill_data_matches_static_fallback(self, migrated_pg):
        """DB rows must match _FALLBACK_EE_MODULES name + vt_equivalent exactly."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT name, vt_equivalent FROM ee_modules ORDER BY name"
            )
            db_rows = {row[0]: row[1] for row in cur.fetchall()}

        static_rows = {
            entry["name"]: entry["vt_equivalent"] for entry in _FALLBACK_EE_MODULES
        }

        assert db_rows == static_rows, (
            f"DB backfill does not match static fallback.\n"
            f"DB only:     {set(db_rows) - set(static_rows)}\n"
            f"Static only: {set(static_rows) - set(db_rows)}"
        )


# ---------------------------------------------------------------------------
# 5. Idempotent re-run
# ---------------------------------------------------------------------------


class TestIdempotentRerun:
    def test_idempotent_rerun(self, migrated_pg):
        """Applying migration a second time must not change the row count."""
        count_before = _count_ee_modules(migrated_pg)
        # Re-run migrations (yoyo is idempotent; already-applied skipped)
        run_migrations(migrated_pg)
        count_after = _count_ee_modules(migrated_pg)
        assert count_after == count_before, (
            f"Row count changed after re-run: {count_before} → {count_after}"
        )


# ---------------------------------------------------------------------------
# 6. UNIQUE name constraint
# ---------------------------------------------------------------------------


class TestUniqueNameConstraint:
    def test_unique_name_constraint(self, migrated_pg):
        """INSERT of a duplicate name must raise UniqueViolation."""
        with pytest.raises(psycopg2.errors.UniqueViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO ee_modules (name) VALUES ('knowledge')"
                )
        migrated_pg.rollback()


# ---------------------------------------------------------------------------
# 7. Soft-delete via deprecated flag
# ---------------------------------------------------------------------------


class TestSoftDeleteViaDeprecatedFlag:
    def test_soft_delete_via_deprecated_flag(self, migrated_pg):
        """Setting deprecated=TRUE on one row must reduce filtered count to 15."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE ee_modules SET deprecated = TRUE WHERE name = 'knowledge'"
            )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ee_modules WHERE deprecated = FALSE"
            )
            active_count = cur.fetchone()[0]

        assert active_count == 15, (
            f"Expected 15 active rows after soft-deleting 1, got {active_count}"
        )

        # Restore for other tests in same session
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE ee_modules SET deprecated = FALSE WHERE name = 'knowledge'"
            )


# ---------------------------------------------------------------------------
# 8. osm_reader read grant (deploy-blocking R1 — ADR-0042 / ADR-0034)
# ---------------------------------------------------------------------------


class TestOsmReaderGrant:
    """The migration must self-grant SELECT on ee_modules to osm_reader.

    Without this, MCP (which connects as osm_reader under RLS) hits
    permission-denied on the EE-confusion guard read, which the code swallows
    → silent fallback to the in-process default EE list.  `python -m
    src.db.migrate` does NOT run ops/rls_create_osm_reader.sql, so the grant
    must be self-contained in the migration.
    """

    def test_osm_reader_has_select(self, migrated_pg_with_reader):
        assert _has_priv(migrated_pg_with_reader, "ee_modules", "SELECT"), (
            "osm_reader missing SELECT on ee_modules — MCP EE-guard read will "
            "silently fall back to the in-process default list"
        )

    def test_osm_reader_has_no_write(self, migrated_pg_with_reader):
        """osm_reader is a read role — it must NOT get INSERT/UPDATE/DELETE."""
        for priv in ("INSERT", "UPDATE", "DELETE"):
            assert not _has_priv(migrated_pg_with_reader, "ee_modules", priv), (
                f"osm_reader unexpectedly has {priv} on ee_modules (read-only role)"
            )

    def test_migration_safe_without_osm_reader(self, migrated_pg):
        """With no osm_reader role the migration must still apply cleanly
        (the GRANT is guarded by a pg_roles EXISTS check). migrated_pg does
        not create the role, so reaching this assertion proves no failure."""
        cols = _column_info(migrated_pg, "ee_modules")
        assert cols, "ee_modules table missing after migrate without osm_reader"
