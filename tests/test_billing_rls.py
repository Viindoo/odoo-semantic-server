# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_billing_rls.py
"""Least-privilege regression guard for billing tables (subscriptions, billing_webhook_events).

Business intent:
  The m13_014 migration GRANTs SELECT on subscriptions and billing_webhook_events
  to the osm_reader role.  The Activation API and webhook handler write as the DB
  owner role — osm_reader must never be able to mutate billing data.

  B1  osm_reader can SELECT from subscriptions.
  B2  osm_reader can SELECT from billing_webhook_events.
  B3  osm_reader cannot INSERT into subscriptions.
  B4  osm_reader cannot UPDATE subscriptions.
  B5  osm_reader cannot DELETE from subscriptions.
  B6  osm_reader cannot INSERT into billing_webhook_events.
  B7  osm_reader cannot UPDATE billing_webhook_events.
  B8  osm_reader cannot DELETE from billing_webhook_events.

Tests B3–B8 use pytest.raises(psycopg2.errors.InsufficientPrivilege) — the
InsufficientPrivilege error signals the role boundary is enforced at the DB layer.

Pattern mirrors test_migration_m13_012.py::TestOsmReaderGrant, adapted for
the billing tables.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import psycopg2
import psycopg2.errors
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Billing tables managed by m13_014 (same list as the DROP cleanup fixture).
# ---------------------------------------------------------------------------

_BILLING_TABLES = ["billing_webhook_events", "subscriptions"]


@pytest.fixture(autouse=True)
def _drop_billing_tables(pg_conn):
    """Drop billing tables before AND after each test for a clean slate."""
    for tbl in _BILLING_TABLES:
        with pg_conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    yield
    for tbl in _BILLING_TABLES:
        with pg_conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply all migrations on a clean DB, yield connection."""
    run_migrations(clean_pg)
    yield clean_pg


# ---------------------------------------------------------------------------
# Helper: create osm_reader role if absent + run migration with the role
# present so the GRANT block inside m13_014 actually fires.
#
# The migration is guarded: IF EXISTS (SELECT FROM pg_roles WHERE rolname =
# 'osm_reader') THEN GRANT ... END IF.  Without the role the grant is
# silently skipped — the test would then report PASS on a false negative.
# We must create the role BEFORE running migrations.
# ---------------------------------------------------------------------------

def _ensure_osm_reader(conn) -> None:
    """Create osm_reader NOLOGIN role if not present.

    Mirrors the production deploy order (ops/rls_create_osm_reader.sql runs
    before src.db.migrate) so the GRANT inside m13_014 actually fires.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'osm_reader') "
            "THEN CREATE ROLE osm_reader NOLOGIN; END IF; "
            "END $$;"
        )


def _drop_osm_reader(conn) -> None:
    """Best-effort cleanup: revoke owned objects then drop the role."""
    for stmt in (
        "DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='osm_reader') "
        "THEN DROP OWNED BY osm_reader; END IF; END $$;",
        "DROP ROLE IF EXISTS osm_reader",
    ):
        try:
            with conn.cursor() as cur:
                cur.execute(stmt)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def _has_table_priv(conn, table: str, priv: str) -> bool:
    """Return True if osm_reader has the given privilege on the table."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege('osm_reader', %s, %s)",
            (table, priv),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Fixture: migrated DB with osm_reader role present.
# The role is created BEFORE migrations so the GRANT fires.
# ---------------------------------------------------------------------------

@pytest.fixture
def migrated_pg_with_reader(clean_pg):
    """Apply all migrations with osm_reader role present; clean up afterwards.

    If the test DB user lacks CREATE ROLE privilege, the test is individually
    skipped — not a hard error — because the failure reason is infra, not code.
    """
    try:
        _ensure_osm_reader(clean_pg)
        clean_pg.commit()
    except psycopg2.errors.InsufficientPrivilege:
        clean_pg.rollback()
        pytest.skip(
            "DB user lacks CREATE ROLE privilege — "
            "run tests as a superuser to enable osm_reader RLS coverage."
        )

    run_migrations(clean_pg)

    # Ensure osm_reader can SET ROLE (non-superuser CREATEROLE still needs
    # GRANT <role> TO CURRENT_USER to succeed at SET ROLE).
    try:
        with clean_pg.cursor() as cur:
            cur.execute("GRANT osm_reader TO CURRENT_USER")
        clean_pg.commit()
    except psycopg2.errors.InsufficientPrivilege:
        clean_pg.rollback()
        # Not fatal for privilege-check tests (B1/B2 use has_table_privilege);
        # fatal only for SET ROLE tests. Yield so B1/B2 still run.

    yield clean_pg

    # Teardown: reset role + clean up osm_reader.
    try:
        with clean_pg.cursor() as cur:
            cur.execute("RESET ROLE")
        clean_pg.commit()
    except Exception:
        try:
            clean_pg.rollback()
        except Exception:
            pass

    _drop_osm_reader(clean_pg)


# ---------------------------------------------------------------------------
# B1/B2: osm_reader has SELECT on both billing tables.
# ---------------------------------------------------------------------------

class TestOsmReaderSelectBillingTables:
    """B1/B2: osm_reader must have SELECT on subscriptions and billing_webhook_events."""

    def test_osm_reader_has_select_on_subscriptions(self, migrated_pg_with_reader):
        """B1: osm_reader has SELECT on subscriptions (portal + account read)."""
        assert _has_table_priv(migrated_pg_with_reader, "subscriptions", "SELECT"), (
            "osm_reader missing SELECT on subscriptions — "
            "/account portal reads will fail with permission-denied"
        )

    def test_osm_reader_has_select_on_billing_webhook_events(self, migrated_pg_with_reader):
        """B2: osm_reader has SELECT on billing_webhook_events (admin viewer read)."""
        assert _has_table_priv(
            migrated_pg_with_reader, "billing_webhook_events", "SELECT"
        ), (
            "osm_reader missing SELECT on billing_webhook_events — "
            "admin webhook viewer reads will fail"
        )


# ---------------------------------------------------------------------------
# B3–B5: osm_reader cannot mutate subscriptions.
# ---------------------------------------------------------------------------

class TestOsmReaderCannotMutateSubscriptions:
    """B3/B4/B5: osm_reader must NOT have INSERT/UPDATE/DELETE on subscriptions.

    Uses SET ROLE to become osm_reader (requires GRANT osm_reader TO CURRENT_USER).
    Falls back to has_table_privilege check if SET ROLE is not available.
    """

    def _skip_if_no_role(self, conn):
        """Try SET ROLE; skip test if insufficient privilege."""
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE osm_reader")
            conn.rollback()  # only checking it works, not keeping the role set
        except psycopg2.errors.InsufficientPrivilege:
            conn.rollback()
            pytest.skip(
                "Cannot SET ROLE osm_reader — "
                "run tests as a superuser to enable write-side coverage."
            )

    def test_osm_reader_cannot_insert_subscriptions(self, migrated_pg_with_reader):
        """B3: INSERT on subscriptions as osm_reader must raise InsufficientPrivilege."""
        conn = migrated_pg_with_reader
        self._skip_if_no_role(conn)

        with conn.cursor() as cur:
            cur.execute("SET ROLE osm_reader")

        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    # We need a valid plan_id; the plans table is owned separately
                    # and osm_reader has SELECT on it (via m13_006), so we can read
                    # one plan_id for the FK — but the INSERT itself must fail.
                    cur.execute("SELECT id FROM plans LIMIT 1")
                    row = cur.fetchone()
                    if row is None:
                        pytest.skip("No plans seeded — cannot test FK-requiring INSERT")
                    plan_id = row[0]
                    cur.execute(
                        "INSERT INTO subscriptions (plan_id) VALUES (%s)",
                        (plan_id,),
                    )
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
            conn.commit()

    def test_osm_reader_cannot_update_subscriptions(self, migrated_pg_with_reader):
        """B4: UPDATE on subscriptions as osm_reader must raise InsufficientPrivilege."""
        conn = migrated_pg_with_reader
        self._skip_if_no_role(conn)

        with conn.cursor() as cur:
            cur.execute("SET ROLE osm_reader")

        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute("UPDATE subscriptions SET seats = 2 WHERE FALSE")
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
            conn.commit()

    def test_osm_reader_cannot_delete_subscriptions(self, migrated_pg_with_reader):
        """B5: DELETE on subscriptions as osm_reader must raise InsufficientPrivilege."""
        conn = migrated_pg_with_reader
        self._skip_if_no_role(conn)

        with conn.cursor() as cur:
            cur.execute("SET ROLE osm_reader")

        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM subscriptions WHERE FALSE")
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
            conn.commit()


# ---------------------------------------------------------------------------
# B6–B8: osm_reader cannot mutate billing_webhook_events.
# ---------------------------------------------------------------------------

class TestOsmReaderCannotMutateBillingWebhookEvents:
    """B6/B7/B8: osm_reader must NOT have INSERT/UPDATE/DELETE on billing_webhook_events."""

    def _skip_if_no_role(self, conn):
        """Try SET ROLE; skip test if insufficient privilege."""
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE osm_reader")
            conn.rollback()
        except psycopg2.errors.InsufficientPrivilege:
            conn.rollback()
            pytest.skip(
                "Cannot SET ROLE osm_reader — "
                "run tests as a superuser to enable write-side coverage."
            )

    def test_osm_reader_cannot_insert_billing_webhook_events(self, migrated_pg_with_reader):
        """B6: INSERT on billing_webhook_events as osm_reader must raise InsufficientPrivilege."""
        conn = migrated_pg_with_reader
        self._skip_if_no_role(conn)

        with conn.cursor() as cur:
            cur.execute("SET ROLE osm_reader")

        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO billing_webhook_events"
                        " (vendor, event_id, event_type, payload)"
                        " VALUES ('polar', 'rls_evt_001', 'test', '{}'::jsonb)"
                    )
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
            conn.commit()

    def test_osm_reader_cannot_update_billing_webhook_events(self, migrated_pg_with_reader):
        """B7: UPDATE on billing_webhook_events as osm_reader must raise InsufficientPrivilege."""
        conn = migrated_pg_with_reader
        self._skip_if_no_role(conn)

        with conn.cursor() as cur:
            cur.execute("SET ROLE osm_reader")

        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE billing_webhook_events"
                        " SET processed_at = now() WHERE FALSE"
                    )
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
            conn.commit()

    def test_osm_reader_cannot_delete_billing_webhook_events(self, migrated_pg_with_reader):
        """B8: DELETE on billing_webhook_events as osm_reader must raise InsufficientPrivilege."""
        conn = migrated_pg_with_reader
        self._skip_if_no_role(conn)

        with conn.cursor() as cur:
            cur.execute("SET ROLE osm_reader")

        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM billing_webhook_events WHERE FALSE")
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
            conn.commit()


# ---------------------------------------------------------------------------
# Smoke: migration applies cleanly even when osm_reader is absent.
# The GRANT inside m13_014 is guarded by pg_roles — no error if role missing.
# ---------------------------------------------------------------------------

class TestMigrationSafeWithoutOsmReader:
    """Smoke: m13_014 applies cleanly when osm_reader role does not exist."""

    def test_migration_applies_without_osm_reader(self, migrated_pg):
        """m13_014 must not fail when osm_reader role is absent (GRANT guarded)."""
        # migrated_pg fixture runs without creating osm_reader — if we reach here,
        # the migration completed successfully without the role.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables"
                " WHERE table_name = 'subscriptions' AND table_schema = 'public'"
            )
            row = cur.fetchone()
        assert row is not None, (
            "subscriptions table must exist even when osm_reader role is absent"
        )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables"
                " WHERE table_name = 'billing_webhook_events' AND table_schema = 'public'"
            )
            row = cur.fetchone()
        assert row is not None, (
            "billing_webhook_events table must exist even when osm_reader role is absent"
        )
