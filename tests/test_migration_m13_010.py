# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migration_m13_010.py
"""Migration tests for m13_010_app_settings.sql.

Verifies that after run_migrations():
- app_settings table exists with correct columns, types, constraints.
- app_settings_history table exists with correct columns.
- Indexes are present.
- CHECK constraint app_settings_tenant_scope_consistency fires correctly.
- History rows survive parent-setting deletion (no cascade from app_settings).
- Tenant cascade: DELETE tenant → cascade app_settings + app_settings_history rows.

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


def _check_constraint_exists(conn, constraint_name: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM pg_constraint
             WHERE conname = %s
               AND conrelid = %s::regclass
            """,
            (constraint_name, table),
        )
        return cur.fetchone() is not None


def _seed_tenant(conn, name="test_tenant_m13009") -> int:
    """Insert a tenant row and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (name) VALUES (%s) "
            "ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name "
            "RETURNING id",
            (name,),
        )
        return cur.fetchone()[0]


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


def _has_seq_priv(conn, sequence: str, priv: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT has_sequence_privilege('osm_reader', %s, %s)",
            (sequence, priv),
        )
        return cur.fetchone()[0]


def _seed_user(conn, username="test_user_m13009") -> int:
    """Insert a webui_users row and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO webui_users (username, password_hash, is_admin, is_active)
            VALUES (%s, %s, FALSE, TRUE)
            ON CONFLICT (username) DO UPDATE SET username=EXCLUDED.username
            RETURNING id
            """,
            (username, "x"),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_M13_009_TABLES = ["app_settings_history", "app_settings"]


def _drop_m13_010_tables(conn):
    """Drop app_settings* tables so run_migrations starts fully clean.

    conftest.clean_pg drops all previously-known tables via DROP ... CASCADE
    but does NOT yet include app_settings / app_settings_history (they are
    added in this WI).  We drop them explicitly here so that repeated test
    runs see a fresh schema with intact FK constraints each time.

    app_settings_history first (no FK referencing it from other tables).
    """
    for tbl in _M13_009_TABLES:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


@pytest.fixture
def migrated_pg(clean_pg):
    """Run all migrations on a clean Postgres DB and return the connection.

    Extends clean_pg by also dropping the m13_010 tables before + after, so
    FK constraints are re-created fresh by run_migrations each time.
    """
    _drop_m13_010_tables(clean_pg)
    run_migrations(clean_pg)
    yield clean_pg
    _drop_m13_010_tables(clean_pg)


@pytest.fixture
def migrated_pg_with_reader(clean_pg):
    """Like migrated_pg but creates osm_reader BEFORE migrating, so the
    migration's pg_roles-guarded GRANT block actually fires and is assertable.
    """
    _drop_m13_010_tables(clean_pg)
    _ensure_osm_reader(clean_pg)
    run_migrations(clean_pg)
    yield clean_pg
    _drop_m13_010_tables(clean_pg)


# ---------------------------------------------------------------------------
# 1. app_settings table structure
# ---------------------------------------------------------------------------


class TestAppSettingsTableExists:
    def test_table_exists(self, migrated_pg):
        """app_settings table must exist after migration."""
        cols = _column_info(migrated_pg, "app_settings")
        assert cols, "app_settings table not found"

    def test_required_columns_present(self, migrated_pg):
        """All expected columns must be present."""
        cols = _column_info(migrated_pg, "app_settings")
        expected = {
            "id",
            "key",
            "value_json",
            "category",
            "scope",
            "tenant_id",
            "data_type",
            "validation_json",
            "default_value",
            "requires_restart",
            "requires_reseed",
            "is_secret",
            "description",
            "updated_by",
            "updated_at",
            "change_reason",
        }
        assert expected.issubset(cols.keys()), (
            f"Missing columns: {expected - cols.keys()}"
        )

    def test_id_is_primary_key(self, migrated_pg):
        """id column (BIGSERIAL surrogate PK) must be NOT NULL."""
        cols = _column_info(migrated_pg, "app_settings")
        assert "id" in cols, "id column missing"
        assert cols["id"][1] == "NO", "id must be NOT NULL"

    def test_key_is_not_nullable(self, migrated_pg):
        """key column must be NOT NULL (unique constraints enforced via partial indexes)."""
        cols = _column_info(migrated_pg, "app_settings")
        assert cols["key"][1] == "NO", "key must be NOT NULL"

    def test_value_json_is_not_nullable(self, migrated_pg):
        """value_json must be NOT NULL."""
        cols = _column_info(migrated_pg, "app_settings")
        assert cols["value_json"][1] == "NO", "value_json must be NOT NULL"

    def test_boolean_columns_not_nullable(self, migrated_pg):
        """Boolean flags must be NOT NULL (have defaults)."""
        cols = _column_info(migrated_pg, "app_settings")
        for col in ("requires_restart", "requires_reseed", "is_secret"):
            assert cols[col][1] == "NO", f"{col} must be NOT NULL"

    def test_updated_at_not_nullable(self, migrated_pg):
        """updated_at must be NOT NULL (has DEFAULT now())."""
        cols = _column_info(migrated_pg, "app_settings")
        assert cols["updated_at"][1] == "NO", "updated_at must be NOT NULL"

    def test_tenant_id_nullable(self, migrated_pg):
        """tenant_id must be nullable (system-scope rows have NULL)."""
        cols = _column_info(migrated_pg, "app_settings")
        assert cols["tenant_id"][1] == "YES", "tenant_id must be nullable"

    def test_scope_consistency_constraint_exists(self, migrated_pg):
        """CHECK constraint app_settings_tenant_scope_consistency must exist."""
        assert _check_constraint_exists(
            migrated_pg,
            "app_settings_tenant_scope_consistency",
            "app_settings",
        ), "app_settings_tenant_scope_consistency CHECK constraint missing"


# ---------------------------------------------------------------------------
# 2. app_settings indexes
# ---------------------------------------------------------------------------


class TestAppSettingsIndexesExist:
    def test_category_index_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "idx_app_settings_category"), (
            "idx_app_settings_category missing"
        )

    def test_scope_tenant_index_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "idx_app_settings_scope_tenant"), (
            "idx_app_settings_scope_tenant missing"
        )

    def test_partial_unique_system_key_exists(self, migrated_pg):
        """Partial unique index uq_app_settings_system_key must exist."""
        assert _index_exists(migrated_pg, "uq_app_settings_system_key"), (
            "uq_app_settings_system_key partial unique index missing"
        )

    def test_partial_unique_tenant_key_exists(self, migrated_pg):
        """Partial unique index uq_app_settings_tenant_key must exist."""
        assert _index_exists(migrated_pg, "uq_app_settings_tenant_key"), (
            "uq_app_settings_tenant_key partial unique index missing"
        )

    def test_partial_unique_per_key_exists(self, migrated_pg):
        """Partial unique index uq_app_settings_per_key must exist."""
        assert _index_exists(migrated_pg, "uq_app_settings_per_key"), (
            "uq_app_settings_per_key partial unique index missing"
        )


# ---------------------------------------------------------------------------
# 2b. Partial unique index enforcement
# ---------------------------------------------------------------------------


class TestPartialUniqueIndexEnforcement:
    def test_two_system_rows_same_key_fails(self, migrated_pg):
        """Two system-scope rows for the same key must violate uq_app_settings_system_key."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings
                    (key, value_json, category, scope, tenant_id, data_type, default_value)
                VALUES
                    ('test.dup_system', '{"v": 1}', 'test', 'system', NULL, 'int', '{"v": 1}')
                """
            )
        migrated_pg.commit()
        with pytest.raises(psycopg2.errors.UniqueViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_settings
                        (key, value_json, category, scope, tenant_id, data_type, default_value)
                    VALUES
                        ('test.dup_system', '{"v": 2}', 'test', 'system', NULL, 'int', '{"v": 1}')
                    """
                )
        migrated_pg.rollback()
        # Cleanup
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE key = 'test.dup_system'")
        migrated_pg.commit()

    def test_system_and_tenant_row_same_key_ok(self, migrated_pg):
        """A system row and a tenant row for the same key must both be allowed."""
        tenant_id = _seed_tenant(migrated_pg, "tenant_coexist_test")
        migrated_pg.commit()
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings
                    (key, value_json, category, scope, tenant_id, data_type, default_value)
                VALUES
                    ('test.coexist', '{"v": 1}', 'test', 'system', NULL, 'int', '{"v": 1}'),
                    ('test.coexist', '{"v": 99}', 'test', 'tenant', %s, 'int', '{"v": 1}')
                """,
                (tenant_id,),
            )
        migrated_pg.commit()
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM app_settings WHERE key = 'test.coexist'")
            assert cur.fetchone()[0] == 2
        # Cleanup
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE key = 'test.coexist'")
        migrated_pg.commit()

    def test_two_tenant_rows_same_key_same_tenant_fails(self, migrated_pg):
        """Two tenant-scope rows for the same (key, tenant_id) must violate
        uq_app_settings_tenant_key."""
        tenant_id = _seed_tenant(migrated_pg, "tenant_dup_test")
        migrated_pg.commit()
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings
                    (key, value_json, category, scope, tenant_id, data_type, default_value)
                VALUES
                    ('test.dup_tenant', '{"v": 1}', 'test', 'tenant', %s, 'int', '{"v": 1}')
                """,
                (tenant_id,),
            )
        migrated_pg.commit()
        with pytest.raises(psycopg2.errors.UniqueViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_settings
                        (key, value_json, category, scope, tenant_id, data_type, default_value)
                    VALUES
                        ('test.dup_tenant', '{"v": 2}', 'test', 'tenant', %s, 'int', '{"v": 1}')
                    """,
                    (tenant_id,),
                )
        migrated_pg.rollback()
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE key = 'test.dup_tenant'")
        migrated_pg.commit()

    def test_two_tenant_rows_same_key_different_tenants_ok(self, migrated_pg):
        """Two tenant-scope rows for the same key but different tenant_ids must both be allowed."""
        t1 = _seed_tenant(migrated_pg, "tenant_diff_t1")
        t2 = _seed_tenant(migrated_pg, "tenant_diff_t2")
        migrated_pg.commit()
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings
                    (key, value_json, category, scope, tenant_id, data_type, default_value)
                VALUES
                    ('test.two_tenants', '{"v": 10}', 'test', 'tenant', %s, 'int', '{"v": 1}'),
                    ('test.two_tenants', '{"v": 20}', 'test', 'tenant', %s, 'int', '{"v": 1}')
                """,
                (t1, t2),
            )
        migrated_pg.commit()
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM app_settings WHERE key = 'test.two_tenants'")
            assert cur.fetchone()[0] == 2
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE key = 'test.two_tenants'")
        migrated_pg.commit()


# ---------------------------------------------------------------------------
# 3. CHECK constraint: scope ↔ tenant_id consistency
# ---------------------------------------------------------------------------


class TestCheckScopeConsistency:
    def test_system_scope_null_tenant_ok(self, migrated_pg):
        """scope='system' + tenant_id=NULL must succeed."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings
                    (key, value_json, category, scope, tenant_id, data_type, default_value)
                VALUES
                    ('test.system_null', '{"v": 1}', 'test', 'system', NULL, 'int', '{"v": 1}')
                ON CONFLICT (key) WHERE scope = 'system' AND tenant_id IS NULL DO NOTHING
                """
            )

    def test_system_scope_nonnull_tenant_fails(self, migrated_pg):
        """scope='system' + tenant_id=NOT NULL must violate CHECK constraint."""
        tenant_id = _seed_tenant(migrated_pg, "tenant_scope_test_sys")
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_settings
                        (key, value_json, category, scope, tenant_id, data_type, default_value)
                    VALUES
                        ('test.system_nonnull', '{"v": 1}', 'test', 'system', %s, 'int', '{"v": 1}')
                    """,
                    (tenant_id,),
                )
        migrated_pg.rollback()

    def test_tenant_scope_nonnull_tenant_ok(self, migrated_pg):
        """scope='tenant' + tenant_id=NOT NULL must succeed."""
        tenant_id = _seed_tenant(migrated_pg, "tenant_scope_test_ok")
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings
                    (key, value_json, category, scope, tenant_id, data_type, default_value)
                VALUES
                    ('test.tenant_nonnull', '{"v": 1}', 'test', 'tenant', %s, 'int', '{"v": 1}')
                ON CONFLICT (key, tenant_id)
                    WHERE scope = 'tenant' AND tenant_id IS NOT NULL DO NOTHING
                """,
                (tenant_id,),
            )

    def test_tenant_scope_null_tenant_fails(self, migrated_pg):
        """scope='tenant' + tenant_id=NULL must violate CHECK constraint."""
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_settings
                        (key, value_json, category, scope, tenant_id, data_type, default_value)
                    VALUES
                        ('test.tenant_null', '{"v": 1}', 'test', 'tenant', NULL, 'int', '{"v": 1}')
                    """
                )
        migrated_pg.rollback()


# ---------------------------------------------------------------------------
# 4. app_settings_history table structure
# ---------------------------------------------------------------------------


class TestAppSettingsHistoryTableExists:
    def test_table_exists(self, migrated_pg):
        """app_settings_history must exist after migration."""
        cols = _column_info(migrated_pg, "app_settings_history")
        assert cols, "app_settings_history table not found"

    def test_required_columns_present(self, migrated_pg):
        """All expected columns must be present."""
        cols = _column_info(migrated_pg, "app_settings_history")
        expected = {
            "id",
            "setting_key",
            "tenant_id",
            "old_value",
            "new_value",
            "changed_by",
            "changed_at",
            "change_reason",
            "audit_log_id",
        }
        assert expected.issubset(cols.keys()), (
            f"Missing history columns: {expected - cols.keys()}"
        )

    def test_id_not_nullable(self, migrated_pg):
        """id (BIGSERIAL PK) must be NOT NULL."""
        cols = _column_info(migrated_pg, "app_settings_history")
        assert cols["id"][1] == "NO", "history.id must be NOT NULL"

    def test_setting_key_not_nullable(self, migrated_pg):
        """setting_key must be NOT NULL."""
        cols = _column_info(migrated_pg, "app_settings_history")
        assert cols["setting_key"][1] == "NO", "history.setting_key must be NOT NULL"

    def test_new_value_not_nullable(self, migrated_pg):
        """new_value must be NOT NULL."""
        cols = _column_info(migrated_pg, "app_settings_history")
        assert cols["new_value"][1] == "NO", "history.new_value must be NOT NULL"

    def test_old_value_nullable(self, migrated_pg):
        """old_value must be nullable (first set has no prior value)."""
        cols = _column_info(migrated_pg, "app_settings_history")
        assert cols["old_value"][1] == "YES", "history.old_value must be nullable"


# ---------------------------------------------------------------------------
# 5. app_settings_history index
# ---------------------------------------------------------------------------


class TestAppSettingsHistoryIndexKeyTime:
    def test_key_time_index_exists(self, migrated_pg):
        assert _index_exists(
            migrated_pg, "idx_app_settings_history_key_time"
        ), "idx_app_settings_history_key_time missing"


# ---------------------------------------------------------------------------
# 6. Orphan safety: history survives parent-setting deletion
# ---------------------------------------------------------------------------


class TestHistoryOrphanAfterSettingDelete:
    def test_history_not_cascade_deleted(self, migrated_pg):
        """DELETE app_settings row must NOT cascade-delete history rows.

        This is the key forensic invariant: orphaned history rows for a
        removed setting must remain queryable.
        """
        # Insert a system-scope setting
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings
                    (key, value_json, category, scope, tenant_id, data_type, default_value)
                VALUES
                    ('test.orphan_check', '{"v": 42}', 'test', 'system', NULL, 'int', '{"v": 42}')
                ON CONFLICT (key)
                    WHERE scope = 'system' AND tenant_id IS NULL
                DO UPDATE SET value_json = EXCLUDED.value_json
                """
            )

        # Insert a history row referencing that setting key
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings_history
                    (setting_key, old_value, new_value, changed_at)
                VALUES
                    ('test.orphan_check', NULL, '{"v": 42}', now())
                RETURNING id
                """
            )
            history_id = cur.fetchone()[0]

        # Delete the parent setting
        with migrated_pg.cursor() as cur:
            cur.execute(
                "DELETE FROM app_settings WHERE key = 'test.orphan_check'"
            )

        # History row must still exist (orphan is intentional)
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT id FROM app_settings_history WHERE id = %s",
                (history_id,),
            )
            row = cur.fetchone()

        assert row is not None, (
            "History row was deleted when parent setting was removed. "
            "The setting_key column must NOT have a CASCADE FK to app_settings."
        )


# ---------------------------------------------------------------------------
# 7. Tenant cascade: DELETE tenant → cascade both tables
# ---------------------------------------------------------------------------


class TestTenantCascadeDelete:
    def test_cascade_deletes_settings_and_history(self, migrated_pg):
        """DELETE tenants row must cascade to app_settings + app_settings_history rows."""
        tenant_id = _seed_tenant(migrated_pg, "tenant_cascade_m13009")

        # Insert a tenant-scoped setting
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings
                    (key, value_json, category, scope, tenant_id, data_type, default_value)
                VALUES
                    ('test.cascade_tenant', '{"v": 99}', 'test', 'tenant', %s, 'int', '{"v": 99}')
                ON CONFLICT (key, tenant_id) WHERE scope = 'tenant' AND tenant_id IS NOT NULL
                DO UPDATE SET value_json = EXCLUDED.value_json
                """,
                (tenant_id,),
            )

        # Insert a history row for that tenant
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings_history
                    (setting_key, tenant_id, old_value, new_value, changed_at)
                VALUES
                    ('test.cascade_tenant', %s, NULL, '{"v": 99}', now())
                RETURNING id
                """,
                (tenant_id,),
            )
            history_id = cur.fetchone()[0]

        # Delete the tenant
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))

        # Both rows must be gone
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT key FROM app_settings WHERE key = 'test.cascade_tenant'"
            )
            setting_row = cur.fetchone()

            cur.execute(
                "SELECT id FROM app_settings_history WHERE id = %s",
                (history_id,),
            )
            history_row = cur.fetchone()

        assert setting_row is None, (
            "app_settings row was NOT deleted when tenant was removed (expected CASCADE)"
        )
        assert history_row is None, (
            "app_settings_history row was NOT deleted when tenant was removed (expected CASCADE)"
        )


# ---------------------------------------------------------------------------
# 8. osm_reader read grants (deploy-blocking R1 — ADR-0042 / ADR-0034)
# ---------------------------------------------------------------------------


class TestOsmReaderGrants:
    """The migration must self-grant the osm_reader read role.

    MCP (:8002) connects as osm_reader under RLS and reads app_settings on
    every authed request via get_setting().  Without the grant the read hits
    permission-denied, which the code swallows → silent fallback to the
    in-process code default (the operator-tunable layer goes dead with no
    500 and no log).  `python -m src.db.migrate` does NOT run
    ops/rls_create_osm_reader.sql, so the grant must live in the migration.

    app_settings needs INSERT too because bootstrap_settings_safe() UPSERTs
    catalogue rows ON CONFLICT DO NOTHING on MCP startup.
    """

    def test_app_settings_select_insert(self, migrated_pg_with_reader):
        assert _has_priv(migrated_pg_with_reader, "app_settings", "SELECT"), (
            "osm_reader missing SELECT on app_settings — get_setting() will "
            "silently fall back to code defaults"
        )
        assert _has_priv(migrated_pg_with_reader, "app_settings", "INSERT"), (
            "osm_reader missing INSERT on app_settings — bootstrap_settings_safe "
            "UPSERT will fail (catalogue rows never written)"
        )

    def test_app_settings_history_select_only(self, migrated_pg_with_reader):
        assert _has_priv(
            migrated_pg_with_reader, "app_settings_history", "SELECT"
        ), "osm_reader missing SELECT on app_settings_history"
        # History is written only by FastAPI (DB owner); MCP must not mutate it.
        for priv in ("INSERT", "UPDATE", "DELETE"):
            assert not _has_priv(
                migrated_pg_with_reader, "app_settings_history", priv
            ), f"osm_reader unexpectedly has {priv} on app_settings_history"

    def test_migration_safe_without_osm_reader(self, migrated_pg):
        """With no osm_reader role the migration must still apply cleanly
        (GRANT guarded by pg_roles EXISTS). migrated_pg does not create the
        role, so reaching this assertion proves no failure."""
        cols = _column_info(migrated_pg, "app_settings")
        assert cols, "app_settings table missing after migrate without osm_reader"

    def test_app_settings_sequence_usage(self, migrated_pg_with_reader):
        """osm_reader must have USAGE on app_settings_id_seq.

        BUG CLASS A (ADR-0042 follow-up): INSERT on a BIGSERIAL table is an
        INCOMPLETE grant without USAGE on its backing sequence — Postgres
        evaluates nextval('app_settings_id_seq') for the `id` column default
        BEFORE the ON CONFLICT DO NOTHING check, so bootstrap_settings_safe()
        UPSERT fails with "permission denied for sequence" if this is missing.
        This is exactly the prod deploy bug that was hotfixed live.
        """
        assert _has_seq_priv(
            migrated_pg_with_reader, "app_settings_id_seq", "USAGE"
        ), (
            "osm_reader missing USAGE on app_settings_id_seq — "
            "bootstrap_settings_safe UPSERT fails at nextval() with "
            "'permission denied for sequence' before ON CONFLICT runs"
        )

    def test_osm_reader_can_insert_app_settings_end_to_end(
        self, migrated_pg_with_reader
    ):
        """End-to-end proof: SET ROLE osm_reader can INSERT into app_settings.

        Exercises the table grant AND the sequence grant together inside a
        rolled-back transaction (no committed side effects).  This is the
        regression that would have caught the prod deploy bug: with INSERT
        granted but sequence USAGE missing, this INSERT raises
        InsufficientPrivilege on the implicit nextval().
        """
        conn = migrated_pg_with_reader
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE osm_reader")
                # Mirrors bootstrap_settings_safe()'s catalogue UPSERT shape.
                cur.execute(
                    """
                    INSERT INTO app_settings
                        (key, value_json, category, scope, tenant_id,
                         data_type, default_value)
                    VALUES
                        ('test.osm_reader_insert', '{"v": 1}', 'test',
                         'system', NULL, 'int', '{"v": 1}')
                    ON CONFLICT (key) WHERE scope = 'system' AND tenant_id IS NULL
                    DO NOTHING
                    RETURNING id
                    """
                )
                inserted = cur.fetchone()
            assert inserted is not None and inserted[0] is not None, (
                "osm_reader INSERT into app_settings returned no id — the "
                "BIGSERIAL default (nextval) did not execute"
            )
        finally:
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
            conn.rollback()
