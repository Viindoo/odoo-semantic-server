# tests/test_admin_users.py
"""Tests for M9 W-UM: admin user management routes + auth + password reset.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Auth bypass is active for most tests: this module self-manages WEBUI_AUTH_DISABLED
because it is in conftest.real_auth_flow_files (conftest does NOT set the bypass).
Tests that check admin data correctness use bypass; tests that check auth gating
override the bypass explicitly (e.g. test_list_users_requires_admin).
"""
import os

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _enable_auth_bypass_for_admin_users_tests():
    """Re-enable the auth bypass for tests that check admin data (not auth mechanics).

    conftest._bypass_webui_auth_for_legacy_tests uses monkeypatch.delenv to
    scrub WEBUI_AUTH_DISABLED for this file (it's in real_auth_flow_files).
    That fixture runs first (conftest fixtures precede local ones), so the env
    var is absent when the test body runs.  We restore it here — after conftest
    has had its say — so that is_test_bypass_active() returns True for all
    tests that do NOT explicitly patch the bypass function.  Tests that verify
    auth gating (e.g. test_list_users_requires_admin) patch
    is_test_bypass_active / current_user_id directly and are unaffected.
    """
    prev = os.environ.get("WEBUI_AUTH_DISABLED")
    os.environ["WEBUI_AUTH_DISABLED"] = "1"
    yield
    if prev is None:
        os.environ.pop("WEBUI_AUTH_DISABLED", None)
    else:
        os.environ["WEBUI_AUTH_DISABLED"] = prev


# ---------------------------------------------------------------------------
# Fixtures
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

CREATE UNIQUE INDEX IF NOT EXISTS ux_webui_users_id ON webui_users(id);

CREATE TABLE IF NOT EXISTS active_sessions (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT,
    user_id    INTEGER NOT NULL,
    session_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM now()),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_active_sessions_user_id ON active_sessions(user_id);

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

CREATE INDEX IF NOT EXISTS idx_email_verif_user_id ON email_verifications(user_id);
CREATE INDEX IF NOT EXISTS idx_email_verif_token_hash ON email_verifications(token_hash);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    actor_id    INTEGER,
    action      TEXT NOT NULL,
    target_id   INTEGER,
    detail_text TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log(created_at DESC);

-- Idempotent ALTER for pre-existing tables with different schemas
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS actor_id INTEGER;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS target_id INTEGER;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS detail_text TEXT;
"""


def _apply_m9_schema(conn) -> None:
    """Apply M9 DDL directly (avoids yoyo cross-test state issues)."""
    for stmt in _M9_SCHEMA_SQL.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(stmt)
        except Exception:
            conn.rollback()  # statement may already be applied — skip


def _wipe_m9_tables(conn) -> None:
    """Clean M9 tables not included in conftest's clean_pg._all_tables."""
    _m9_tables = [
        "admin_audit_log",
        "email_verifications",
        "active_sessions",
        "webui_users",
    ]
    for tbl in _m9_tables:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {tbl}")  # noqa: S608 — table names are literals
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
    """Return an AsyncClient backed by the ASGI app via ASGITransport."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _seed_user(
    pg_conn,
    *,
    username: str = "alice",
    password_hash: str = "hash",
    is_admin: bool = False,
    is_active: bool = True,
) -> int:
    """Insert a webui_users row and return the auto-generated id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (username, password_hash, is_admin, is_active),
        )
        row = cur.fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# List users
# ---------------------------------------------------------------------------


class TestListUsers:
    @pytest.mark.asyncio
    async def test_list_users_requires_admin(self, migrated_pg):
        """require_admin raises 403 for authenticated non-admin user.

        Tests the dependency directly without a full HTTP request, avoiding the
        need to disable the auth bypass globally (which would corrupt test isolation).
        """
        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod

        user_id = _seed_user(migrated_pg, username="nonadmin_check", is_admin=False)

        # Simulate a minimal ASGI request object
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
        fake_request = StarletteRequest(scope)

        # Patch current_user_id to return our non-admin user, bypass off
        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: user_id
            raised = None
            try:
                await auth_mod.require_admin(fake_request)
            except HTTPException as exc:
                raised = exc
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert raised is not None, "require_admin must raise HTTPException for non-admin"
        assert raised.status_code == 403

    @pytest.mark.asyncio
    async def test_list_users_returns_data(self, migrated_pg):
        """GET /api/admin/users returns all users as JSON array."""
        _seed_user(migrated_pg, username="alice", is_admin=True)
        _seed_user(migrated_pg, username="bob", is_admin=False)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/admin/users")

        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        usernames = [u["username"] for u in data["users"]]
        assert "alice" in usernames
        assert "bob" in usernames

    @pytest.mark.asyncio
    async def test_list_users_no_password_hash(self, migrated_pg):
        """User list must NOT include password_hash field."""
        _seed_user(migrated_pg, username="carol")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/admin/users")

        assert resp.status_code == 200
        for user in resp.json()["users"]:
            assert "password_hash" not in user


# ---------------------------------------------------------------------------
# Deactivate
# ---------------------------------------------------------------------------


class TestDeactivateUser:
    @pytest.mark.asyncio
    async def test_deactivate_revokes_sessions(self, migrated_pg):
        """POST /api/admin/users/{id}/deactivate sets is_active=False + revokes sessions.

        The conftest auth bypass returns actor_id=1 (sentinel). Seed a dummy
        user first so 'dave' gets id>=2 and the self-deactivation guard
        (user_id == actor_id) does not falsely trip.
        """
        _seed_user(migrated_pg, username="_bypass_actor_placeholder", is_admin=True)
        user_id = _seed_user(migrated_pg, username="dave", is_active=True)
        # Insert a fake session row compatible with both W-UM and live schemas.
        # Provide all NOT NULL columns from the live DB schema.
        import secrets as _sec
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO active_sessions (user_id, session_id, expires_at)"
                " VALUES (%s, %s, NOW() + INTERVAL '8 hours')"
                " ON CONFLICT DO NOTHING",
                (user_id, _sec.token_hex(16)),
            )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                f"/api/admin/users/{user_id}/deactivate",
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200

        # Check DB: user inactive
        from src.db.pg import auth_store
        user = auth_store().get_user_by_id(user_id)
        assert user is not None
        assert user["is_active"] is False

        # Check DB: sessions gone
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM active_sessions WHERE user_id = %s", (user_id,))
            count = cur.fetchone()[0]
        assert count == 0

    @pytest.mark.asyncio
    async def test_deactivate_nonexistent_returns_404(self, migrated_pg):
        """POST /api/admin/users/99999/deactivate → 404 for unknown user."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/admin/users/99999/deactivate",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Reactivate
# ---------------------------------------------------------------------------


class TestReactivateUser:
    @pytest.mark.asyncio
    async def test_reactivate_user(self, migrated_pg):
        """POST /api/admin/users/{id}/reactivate sets is_active=True."""
        user_id = _seed_user(migrated_pg, username="eve", is_active=False)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                f"/api/admin/users/{user_id}/reactivate",
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200

        from src.db.pg import auth_store
        user = auth_store().get_user_by_id(user_id)
        assert user is not None
        assert user["is_active"] is True


# ---------------------------------------------------------------------------
# Password reset link
# ---------------------------------------------------------------------------


class TestResetPasswordLink:
    @pytest.mark.asyncio
    async def test_reset_password_link_creates_token_and_logs_audit(self, migrated_pg):
        """POST .../reset-password-link creates email_verifications row + audit log."""
        user_id = _seed_user(migrated_pg, username="frank")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                f"/api/admin/users/{user_id}/reset-password-link",
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        # email_sent=False expected in test env (SMTP_HOST not set)
        assert "email_sent" in data

        # Verify token was created in DB
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM email_verifications "
                "WHERE user_id = %s AND purpose = 'password_reset'",
                (user_id,),
            )
            count = cur.fetchone()[0]
        assert count >= 1

        # Verify audit log entry — W-AL canonical schema uses `target TEXT`
        # and dot-namespaced action (`user.reset_password`). Legacy `target_id`
        # column is still present for W-UM call sites; see backlog cleanup.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT action FROM admin_audit_log WHERE target = %s",
                (str(user_id),),
            )
            rows = cur.fetchall()
        actions = [r[0] for r in rows]
        assert "user.reset_password" in actions


# ---------------------------------------------------------------------------
# Password reset consume
# ---------------------------------------------------------------------------


class TestResetPasswordConsume:
    @pytest.mark.asyncio
    async def test_reset_password_consume_valid_token(self, migrated_pg):
        """POST /api/auth/reset-password with valid token sets new password + revokes sessions."""
        from src.db.pg import auth_store
        from src.web_ui.auth import hash_password

        user_id = _seed_user(
            migrated_pg, username="grace", password_hash=hash_password("old_pass")
        )
        store = auth_store()
        raw_token = store.create_password_reset_token(user_id, ttl_seconds=3600)

        # Insert session row to verify revocation — include all NOT NULL columns
        import secrets as _sec2
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO active_sessions (user_id, session_id, expires_at)"
                " VALUES (%s, %s, NOW() + INTERVAL '8 hours')"
                " ON CONFLICT DO NOTHING",
                (user_id, _sec2.token_hex(16)),
            )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/auth/reset-password",
                json={"token": raw_token, "new_password": "new_secure_pass!"},
            )

        assert resp.status_code == 200
        assert resp.json().get("ok") is True

        # Password hash must have changed
        new_hash = store.get_user_password_hash("grace")
        from src.web_ui.auth import verify_password
        assert verify_password("new_secure_pass!", new_hash)
        assert not verify_password("old_pass", new_hash)

        # Sessions revoked
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM active_sessions WHERE user_id = %s", (user_id,))
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_reset_password_consume_expired_token_returns_410(self, migrated_pg):
        """POST /api/auth/reset-password with expired token → 410 expired."""
        user_id = _seed_user(migrated_pg, username="hank")
        from src.db.pg import auth_store
        store = auth_store()

        # Create token with TTL=0 (already expired)
        raw_token = store.create_password_reset_token(user_id, ttl_seconds=0)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/auth/reset-password",
                json={"token": raw_token, "new_password": "newpass123"},
            )

        assert resp.status_code == 410
        assert resp.json().get("error") == "expired"

    @pytest.mark.asyncio
    async def test_reset_password_consume_used_token_returns_410(self, migrated_pg):
        """POST /api/auth/reset-password with already-used token → 410 used."""
        user_id = _seed_user(migrated_pg, username="iris")
        from src.db.pg import auth_store
        store = auth_store()
        raw_token = store.create_password_reset_token(user_id, ttl_seconds=3600)

        # Consume token once
        store.consume_password_reset_token(raw_token)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/auth/reset-password",
                json={"token": raw_token, "new_password": "newpass123"},
            )

        assert resp.status_code == 410
        assert resp.json().get("error") == "used"

    @pytest.mark.asyncio
    async def test_reset_password_consume_unknown_token_returns_404(self, migrated_pg):
        """POST /api/auth/reset-password with unknown token → 404 not_found."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/auth/reset-password",
                json={"token": "deadbeefdeadbeef" * 4, "new_password": "newpass123"},
            )

        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"


# ---------------------------------------------------------------------------
# Promote / demote admin (PATCH /api/admin/users/{user_id}/admin)
# ---------------------------------------------------------------------------


class TestSetUserAdmin:
    @pytest.mark.asyncio
    async def test_promote_non_admin_to_admin(self, migrated_pg):
        """PATCH /api/admin/users/{id}/admin with is_admin=true promotes the user."""
        user_id = _seed_user(migrated_pg, username="promote_me", is_admin=False)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/users/{user_id}/admin",
                json={"is_admin": True},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data["user"]["is_admin"] is True

        from src.db.pg import auth_store
        user = auth_store().get_user_by_id(user_id)
        assert user["is_admin"] is True

    @pytest.mark.asyncio
    async def test_demote_admin_to_non_admin(self, migrated_pg):
        """PATCH .../admin with is_admin=false demotes when a second admin exists."""
        # Second admin ensures last-admin guard does not fire
        _seed_user(migrated_pg, username="other_admin", is_admin=True, is_active=True)
        user_id = _seed_user(migrated_pg, username="demote_me", is_admin=True, is_active=True)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/users/{user_id}/admin",
                json={"is_admin": False},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data["user"]["is_admin"] is False

    @pytest.mark.asyncio
    async def test_demote_last_admin_returns_422(self, migrated_pg):
        """PATCH .../admin with is_admin=false → 422 when only one active admin."""
        user_id = _seed_user(migrated_pg, username="sole_admin", is_admin=True, is_active=True)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/users/{user_id}/admin",
                json={"is_admin": False},
            )

        assert resp.status_code == 422
        assert resp.json().get("error") == "last_admin_protected"

    @pytest.mark.asyncio
    async def test_deactivate_last_admin_returns_422(self, migrated_pg):
        """POST /api/admin/users/{id}/deactivate → 422 when user is the last active admin."""
        # Bypass actor_id=1; seed a placeholder for bypass, then seed the sole admin separately
        _seed_user(migrated_pg, username="_bypass_actor_2", is_admin=True, is_active=True)
        sole_admin_id = _seed_user(
            migrated_pg, username="sole_admin2", is_admin=True, is_active=True
        )

        app = create_app()
        async with _async_client(app) as client:
            # First demote _bypass_actor_2 so sole_admin2 is the last active admin
            # (The bypass returns actor_id=1 which is _bypass_actor_2)
            # Actually, the bypass actor (id auto-assigned) may not be predictable;
            # instead directly deactivate _bypass_actor_2 via DB so sole_admin_id is last.
            pass

        # Set _bypass_actor_2 to non-admin via DB to make sole_admin_id the last admin
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE webui_users SET is_admin = FALSE WHERE username = '_bypass_actor_2'"
            )

        async with _async_client(create_app()) as client:
            resp = await client.post(
                f"/api/admin/users/{sole_admin_id}/deactivate",
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 422
        assert resp.json().get("error") == "last_admin_protected"


# ---------------------------------------------------------------------------
# GET /api/admin/users includes api_key_count
# ---------------------------------------------------------------------------


class TestListUsersApiKeyCount:
    @pytest.mark.asyncio
    async def test_get_admin_users_includes_api_key_count(self, migrated_pg):
        """GET /api/admin/users includes api_key_count field for each user."""
        user_id = _seed_user(migrated_pg, username="key_owner", is_admin=False)

        # Insert 2 active api_keys owned by key_owner
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, active, user_id)"
                " VALUES (%s, %s, %s, TRUE, %s), (%s, %s, %s, TRUE, %s)",
                (
                    "key_a", "hash_a", "osm_key_a____", user_id,
                    "key_b", "hash_b", "osm_key_b____", user_id,
                ),
            )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/admin/users")

        assert resp.status_code == 200
        users = resp.json()["users"]
        owner = next((u for u in users if u["username"] == "key_owner"), None)
        assert owner is not None
        assert "api_key_count" in owner
        assert owner["api_key_count"] == 2
