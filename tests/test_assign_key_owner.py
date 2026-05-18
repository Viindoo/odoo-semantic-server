# tests/test_assign_key_owner.py
"""Tests for PATCH /api/admin/api-keys/{key_id}/owner (M9-rbac W2).

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Auth bypass is active via WEBUI_AUTH_DISABLED=1 (set by autouse fixture below).
The non-admin 403 test patches the bypass off explicitly.
"""
import os

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _enable_auth_bypass():
    """Enable WEBUI_AUTH_DISABLED for all tests in this file (same as test_admin_users.py)."""
    prev = os.environ.get("WEBUI_AUTH_DISABLED")
    os.environ["WEBUI_AUTH_DISABLED"] = "1"
    yield
    if prev is None:
        os.environ.pop("WEBUI_AUTH_DISABLED", None)
    else:
        os.environ["WEBUI_AUTH_DISABLED"] = prev


# ---------------------------------------------------------------------------
# Minimal schema DDL (mirrors test_admin_users.py)
# ---------------------------------------------------------------------------

_M9_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS webui_users (
    username      VARCHAR(64) PRIMARY KEY,
    password_hash VARCHAR(255) NOT NULL,
    created_at    TIMESTAMP DEFAULT NOW(),
    id            SERIAL,
    email         TEXT,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    mfa_enabled   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_webui_users_id2 ON webui_users(id);

CREATE TABLE IF NOT EXISTS active_sessions (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT,
    user_id    INTEGER NOT NULL,
    session_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM now()),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_active_sessions_user_id2 ON active_sessions(user_id);

CREATE TABLE IF NOT EXISTS email_verifications (
    id         BIGSERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    purpose    TEXT NOT NULL DEFAULT 'password_reset',
    token_hash TEXT NOT NULL UNIQUE,
    token      TEXT,
    expires_at TIMESTAMP NOT NULL,
    used_at    TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    actor_id    INTEGER,
    action      TEXT NOT NULL,
    target_id   INTEGER,
    detail_text TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW()
);

ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS actor_id INTEGER;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS target_id INTEGER;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS detail_text TEXT;
"""

_M9_TABLES = [
    "admin_audit_log",
    "email_verifications",
    "active_sessions",
    "webui_users",
]


def _apply_m9_schema(conn) -> None:
    for stmt in _M9_SCHEMA_SQL.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(stmt)
        except Exception:
            conn.rollback()


def _wipe_m9_tables(conn) -> None:
    for tbl in _M9_TABLES:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {tbl}")  # noqa: S608
        except Exception:
            conn.rollback()


@pytest.fixture
def migrated_pg(clean_pg):
    """Clean schema + run migrations — provides a fresh DB for each test."""
    run_migrations(clean_pg)
    _apply_m9_schema(clean_pg)
    _wipe_m9_tables(clean_pg)
    yield clean_pg
    _wipe_m9_tables(clean_pg)


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _seed_user(
    pg_conn,
    *,
    username: str,
    password_hash: str = "hash",
    is_admin: bool = False,
    is_active: bool = True,
) -> int:
    """Insert a webui_users row and return the auto-generated id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (username, password_hash, is_admin, is_active),
        )
        row = cur.fetchone()
    return row[0]


def _seed_key(pg_conn, *, name: str, user_id: int | None = None) -> int:
    """Insert an api_keys row and return its id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix, active, user_id)"
            " VALUES (%s, %s, %s, TRUE, %s) RETURNING id",
            (name, f"hash_{name}", f"osm_{name[:8]}", user_id),
        )
        row = cur.fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAssignKeyOwner:
    @pytest.mark.asyncio
    async def test_admin_assigns_null_key_to_user(self, migrated_pg):
        """PATCH /api/admin/api-keys/{id}/owner assigns a system (NULL) key to a user (200)."""
        user_id = _seed_user(migrated_pg, username="alice_owner")
        key_id = _seed_key(migrated_pg, name="global_key", user_id=None)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/owner",
                json={"user_id": user_id},
            )

        assert resp.status_code == 200
        assert resp.json().get("ok") is True

        # Verify DB assignment
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT user_id FROM api_keys WHERE id = %s", (key_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == user_id

    @pytest.mark.asyncio
    async def test_admin_reassigns_owned_key_to_another_user(self, migrated_pg):
        """PATCH /api/admin/api-keys/{id}/owner reassigns a key from one user to another (200)."""
        user_a = _seed_user(migrated_pg, username="user_a")
        user_b = _seed_user(migrated_pg, username="user_b")
        key_id = _seed_key(migrated_pg, name="owned_key", user_id=user_a)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/owner",
                json={"user_id": user_b},
            )

        assert resp.status_code == 200
        assert resp.json().get("ok") is True

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT user_id FROM api_keys WHERE id = %s", (key_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == user_b

    @pytest.mark.asyncio
    async def test_non_admin_assign_key_owner_returns_403(self, migrated_pg):
        """PATCH /api/admin/api-keys/{id}/owner returns 403 when caller is not admin."""
        import src.web_ui.auth as auth_mod

        user_id = _seed_user(migrated_pg, username="nonadmin_user", is_admin=False)
        key_id = _seed_key(migrated_pg, name="some_key")

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: user_id

            app = create_app()
            async with _async_client(app) as client:
                resp = await client.patch(
                    f"/api/admin/api-keys/{key_id}/owner",
                    json={"user_id": user_id},
                )
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_assign_to_nonexistent_user_returns_404(self, migrated_pg):
        """PATCH /api/admin/api-keys/{id}/owner with unknown user_id → 404."""
        key_id = _seed_key(migrated_pg, name="orphan_key", user_id=None)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/owner",
                json={"user_id": 999999},
            )

        assert resp.status_code == 404
        assert resp.json().get("error") == "user_not_found"

    @pytest.mark.asyncio
    async def test_assign_owner_audit_log_includes_old_and_new_user_id(self, migrated_pg):
        """PATCH /api/admin/api-keys/{id}/owner audit row must contain old_user_id + new_user_id.

        Verifies the forensic detail capture added by the Opus post-review fix:
        the route fetches the current owner before reassigning so the audit record
        captures the before→after transition.
        """
        import json

        old_user_id = _seed_user(migrated_pg, username="old_owner_audit")
        new_user_id = _seed_user(migrated_pg, username="new_owner_audit")
        key_id = _seed_key(migrated_pg, name="audit_trail_key", user_id=old_user_id)

        # Also seed the api_keys table (key_id already inserted above).
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/owner",
                json={"user_id": new_user_id},
            )

        assert resp.status_code == 200
        assert resp.json().get("ok") is True

        # Read audit log — `detail` is stored as JSONB by write_audit_log (canonical schema
        # from m9_003_admin_audit_log.sql). Cast to text for Python-side JSON parse.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT detail::text FROM admin_audit_log "
                "WHERE action = %s AND target = %s "
                "ORDER BY created_at DESC LIMIT 1",
                ("api_key.assign_owner", str(key_id)),
            )
            row = cur.fetchone()

        assert row is not None, "audit log must contain a row for api_key.assign_owner"
        detail_text = row[0]
        assert detail_text is not None, "detail must not be NULL"

        try:
            detail = json.loads(detail_text)
        except json.JSONDecodeError:
            pytest.fail(f"detail is not valid JSON: {detail_text!r}")

        assert "old_user_id" in detail, (
            f"audit detail must contain old_user_id (before→after traceability). Got: {detail}"
        )
        assert "new_user_id" in detail, (
            f"audit detail must contain new_user_id. Got: {detail}"
        )
        assert detail["old_user_id"] == old_user_id, (
            f"old_user_id mismatch: expected {old_user_id}, got {detail['old_user_id']}"
        )
        assert detail["new_user_id"] == new_user_id, (
            f"new_user_id mismatch: expected {new_user_id}, got {detail['new_user_id']}"
        )
