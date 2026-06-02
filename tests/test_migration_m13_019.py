# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for migration m13_019_public_tenant_isolation.sql.

SECURITY (ADR-0034): m13_019 closes the free-signup tenant-isolation hole.
It must:
  1. Move 'standard_viindoo_*' / 'viindoo_internal_*' profiles out of the shared
     (NULL) set into the Viindoo tenant ('Viindoo Technology JSC').
  2. Leave 'odoo_*' base profiles in the shared (NULL) set.
  3. Backfill already-minted NULL-tenant keys by email domain:
       - @viindoo.com non-admin → bound to Viindoo tenant.
       - other non-admin (gmail) → DEACTIVATED (active=false, tenant stays NULL).
       - admin + system/CLI keys → untouched (NULL & active).
  4. Be idempotent — re-running changes nothing further.

Requires PostgreSQL (pytestmark = pytest.mark.postgres). Runs ONLY against the
throwaway test DSN; never the prod database.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


_MIGRATION_SQL = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "m13_019_public_tenant_isolation.sql"
)


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply all migrations once on a clean schema."""
    run_migrations(clean_pg)
    return clean_pg


def _insert_user(conn, username, email, *, is_admin=False, user_id=None):
    cols = "username, email, password_hash, is_admin, is_active"
    vals = "%s, %s, %s, %s, %s"
    params = [username, email, "x", is_admin, True]
    if user_id is not None:
        cols += ", id"
        vals += ", %s"
        params.append(user_id)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO webui_users ({cols}) VALUES ({vals}) RETURNING id",  # noqa: S608
            tuple(params),
        )
        uid = cur.fetchone()[0]
    return uid


def _insert_profile(conn, name, version="17.0", *, tenant_id=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, tenant_id) "
            "VALUES (%s, %s, %s) RETURNING id",
            (name, version, tenant_id),
        )
        return cur.fetchone()[0]


def _insert_key(conn, name, *, user_id, tenant_id=None, active=True):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix, user_id, tenant_id, active) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (name, f"hash_{name}", name[:12], user_id, tenant_id, active),
        )
        return cur.fetchone()[0]


def _apply_m13_019(conn):
    """Re-execute the migration SQL directly (idempotency re-run check)."""
    sql = _MIGRATION_SQL.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    if not conn.autocommit:
        conn.commit()


def _profile_tenant(conn, name):
    with conn.cursor() as cur:
        cur.execute("SELECT tenant_id FROM profiles WHERE name = %s", (name,))
        return cur.fetchone()[0]


def _key_state(conn, key_id):
    with conn.cursor() as cur:
        cur.execute("SELECT tenant_id, active FROM api_keys WHERE id = %s", (key_id,))
        row = cur.fetchone()
    return {"tenant_id": row[0], "active": row[1]}


def _viindoo_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tenants WHERE name = %s", ("Viindoo Technology JSC",))
        return cur.fetchone()[0]


class TestMigrationM13019:
    def test_isolation_and_backfill(self, migrated_pg):
        conn = migrated_pg

        # --- seed: profiles (all in shared NULL set pre-migration) ----------
        # 'odoo_17' is already seeded by migration 0004 (NULL tenant). We add the
        # two restricted viindoo profiles in the shared set, as they were in prod
        # before m13_019.
        _insert_profile(conn, "standard_viindoo_17")
        _insert_profile(conn, "viindoo_internal_17")

        # --- seed: users + keys --------------------------------------------
        vii_uid = _insert_user(conn, "vuser", "dev@viindoo.com")
        gmail_uid = _insert_user(conn, "guser", "someone@gmail.com")
        admin_uid = _insert_user(conn, "admin1", "admin@viindoo.com", is_admin=True)

        vii_key = _insert_key(conn, "vii-key", user_id=vii_uid, tenant_id=None)
        gmail_key = _insert_key(conn, "gmail-key", user_id=gmail_uid, tenant_id=None)
        admin_key = _insert_key(conn, "admin-key", user_id=admin_uid, tenant_id=None)
        cli_key = _insert_key(conn, "cli-key", user_id=None, tenant_id=None)
        if not conn.autocommit:
            conn.commit()

        # --- apply migration ------------------------------------------------
        _apply_m13_019(conn)
        vid = _viindoo_id(conn)

        # --- assert: profiles ----------------------------------------------
        assert _profile_tenant(conn, "odoo_17") is None, "odoo base must stay shared"
        assert _profile_tenant(conn, "standard_viindoo_17") == vid
        assert _profile_tenant(conn, "viindoo_internal_17") == vid

        # --- assert: keys ---------------------------------------------------
        assert _key_state(conn, vii_key) == {"tenant_id": vid, "active": True}
        gmail = _key_state(conn, gmail_key)
        assert gmail["active"] is False, "gmail key must be deactivated"
        assert gmail["tenant_id"] is None, "gmail key tenant stays NULL"
        assert _key_state(conn, admin_key) == {"tenant_id": None, "active": True}
        assert _key_state(conn, cli_key) == {"tenant_id": None, "active": True}

    def test_idempotent_rerun(self, migrated_pg):
        conn = migrated_pg

        # 'odoo_17' already exists (seeded by migration 0004, NULL tenant).
        _insert_profile(conn, "standard_viindoo_17")
        vii_uid = _insert_user(conn, "vuser2", "dev2@viindoo.com")
        gmail_uid = _insert_user(conn, "guser2", "two@gmail.com")
        vii_key = _insert_key(conn, "vii-key2", user_id=vii_uid, tenant_id=None)
        gmail_key = _insert_key(conn, "gmail-key2", user_id=gmail_uid, tenant_id=None)
        if not conn.autocommit:
            conn.commit()

        _apply_m13_019(conn)
        vid = _viindoo_id(conn)
        after_first = {
            "vii_profile": _profile_tenant(conn, "standard_viindoo_17"),
            "odoo": _profile_tenant(conn, "odoo_17"),
            "vii_key": _key_state(conn, vii_key),
            "gmail_key": _key_state(conn, gmail_key),
        }

        # Re-run — must converge, no further change.
        _apply_m13_019(conn)
        after_second = {
            "vii_profile": _profile_tenant(conn, "standard_viindoo_17"),
            "odoo": _profile_tenant(conn, "odoo_17"),
            "vii_key": _key_state(conn, vii_key),
            "gmail_key": _key_state(conn, gmail_key),
        }

        assert after_first == after_second
        assert after_second["vii_profile"] == vid
        assert after_second["odoo"] is None
        assert after_second["vii_key"] == {"tenant_id": vid, "active": True}
        assert after_second["gmail_key"]["active"] is False
