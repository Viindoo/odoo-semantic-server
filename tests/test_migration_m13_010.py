# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migration_m13_010.py
"""Migration tests for m13_010_app_settings.sql — behaviour cases only.

One-shot catalog assertions (table/column existence, nullability, index presence,
constraint name presence) were removed — covered by test_squashed_baseline.py
golden snapshot.

Kept behaviour cases:
  2b  Partial unique index enforcement (duplicate raises UniqueViolation).
  3   CHECK constraint: scope <-> tenant_id consistency (bad combos raise).
  6   Orphan safety: history survives parent-setting deletion (no cascade).
  7   Tenant cascade: DELETE tenant -> cascade app_settings + history rows.
  8   osm_reader GRANT: SELECT + INSERT on app_settings, SELECT-only on history.

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
            """,
            (table,),
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_M13_009_TABLES = ["app_settings_history", "app_settings"]


def _drop_m13_010_tables(conn):
    for tbl in _M13_009_TABLES:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


@pytest.fixture
def migrated_pg(clean_pg):
    _drop_m13_010_tables(clean_pg)
    run_migrations(clean_pg)
    yield clean_pg
    _drop_m13_010_tables(clean_pg)


@pytest.fixture
def migrated_pg_with_reader(clean_pg):
    _drop_m13_010_tables(clean_pg)
    ensure_osm_reader_or_skip(clean_pg)
    run_migrations(clean_pg)

    try:
        with clean_pg.cursor() as cur:
            cur.execute("GRANT osm_reader TO CURRENT_USER")
        clean_pg.commit()
    except psycopg2.errors.InsufficientPrivilege:
        clean_pg.rollback()

    yield clean_pg

    try:
        with clean_pg.cursor() as cur:
            cur.execute("RESET ROLE")
        clean_pg.commit()
    except Exception:
        try:
            clean_pg.rollback()
        except Exception:
            pass
    _drop_m13_010_tables(clean_pg)
    drop_osm_reader(clean_pg)


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
# 3. CHECK constraint: scope <-> tenant_id consistency
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
# 6. Orphan safety: history survives parent-setting deletion
# ---------------------------------------------------------------------------


class TestHistoryOrphanAfterSettingDelete:
    def test_history_not_cascade_deleted(self, migrated_pg):
        """DELETE app_settings row must NOT cascade-delete history rows.

        This is the key forensic invariant: orphaned history rows for a
        removed setting must remain queryable.
        """
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

        with migrated_pg.cursor() as cur:
            cur.execute(
                "DELETE FROM app_settings WHERE key = 'test.orphan_check'"
            )

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
# 7. Tenant cascade: DELETE tenant -> cascade both tables
# ---------------------------------------------------------------------------


class TestTenantCascadeDelete:
    def test_cascade_deletes_settings_and_history(self, migrated_pg):
        """DELETE tenants row must cascade to app_settings + app_settings_history rows."""
        tenant_id = _seed_tenant(migrated_pg, "tenant_cascade_m13009")

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

        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))

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
    """The migration must self-grant the osm_reader read role."""

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
        for priv in ("INSERT", "UPDATE", "DELETE"):
            assert not _has_priv(
                migrated_pg_with_reader, "app_settings_history", priv
            ), f"osm_reader unexpectedly has {priv} on app_settings_history"

    def test_migration_safe_without_osm_reader(self, migrated_pg):
        """With no osm_reader role the migration must still apply cleanly."""
        cols = _column_info(migrated_pg, "app_settings")
        assert cols, "app_settings table missing after migrate without osm_reader"

    def test_app_settings_sequence_usage(self, migrated_pg_with_reader):
        """osm_reader must have USAGE on app_settings_id_seq.

        BUG CLASS A (ADR-0042 follow-up): INSERT on a BIGSERIAL table is an
        INCOMPLETE grant without USAGE on its backing sequence — Postgres
        evaluates nextval('app_settings_id_seq') for the `id` column default
        BEFORE the ON CONFLICT DO NOTHING check, so bootstrap_settings_safe()
        UPSERT fails with "permission denied for sequence" if this is missing.
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
        """End-to-end proof: SET ROLE osm_reader can INSERT into app_settings."""
        conn = migrated_pg_with_reader
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE osm_reader")
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
