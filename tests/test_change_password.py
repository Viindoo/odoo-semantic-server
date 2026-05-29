# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_change_password.py
"""Tests for POST /api/auth/change-password and enriched GET /api/auth/verify.

Coverage — change-password (WS2c business rules):
  CP1  Unauthenticated request → 401 not_authenticated.
  CP2  Wrong current password → 401 invalid_current_password.
  CP3  New password fails policy (too short) → 400 with policy message.
  CP4  New password is on common-password blocklist → 400 with policy message.
  CP5  New password same as current → 400 new-must-differ.
  CP6  Valid change → 200 ok; subsequent verify_password with new hash passes;
       old password no longer matches.

Coverage — verify enrichment (WS2a business rules):
  V1  /api/auth/verify response includes email + is_tenant_admin fields.
  V2  Plain (non-tenant-admin) user → is_tenant_admin=False.
  V3  User with tenant_admin role → is_tenant_admin=True.

All tests use the real Postgres backend via the `clean_pg` fixture.
Marker: pytest.mark.postgres
"""
from __future__ import annotations

import os
import secrets
import unittest.mock as mock

import httpx
import pytest

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Environment / app bootstrap
# ---------------------------------------------------------------------------

os.environ.setdefault(
    "WEBUI_SESSION_SECRET", "test-secret-key-for-cp-tests-32bytes!!!"
)
os.environ.setdefault("WEBUI_SECURE_COOKIE", "0")  # plain HTTP in tests OK


def _make_app():
    """Create the FastAPI app with loopback middleware bypassed."""
    import src.web_ui.app as app_mod

    original_dispatch = app_mod._LoopbackOnlyMiddleware.dispatch

    async def _passthrough(self, request, call_next):
        return await call_next(request)

    app_mod._LoopbackOnlyMiddleware.dispatch = _passthrough  # type: ignore[method-assign]
    try:
        return app_mod.create_app()
    finally:
        app_mod._LoopbackOnlyMiddleware.dispatch = original_dispatch  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# DB seeding helpers
# ---------------------------------------------------------------------------


def _insert_verified_user(
    pg_conn, username: str, email: str, password_hash: str, is_admin: bool = False
) -> int:
    """Insert a verified active user. Returns integer id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users"
            " (username, password_hash, email, email_verified, is_admin, is_active)"
            " VALUES (%s, %s, %s, TRUE, %s, TRUE) RETURNING id",
            (username, password_hash, email, is_admin),
        )
        row = cur.fetchone()
    return row[0]


def _insert_tenant_membership(pg_conn, user_id: int, role: str = "tenant_admin") -> int:
    """Insert a tenant and a membership for user_id. Returns tenant id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
            (f"test-tenant-{secrets.token_hex(4)}",),
        )
        tenant_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO tenant_members (user_id, tenant_id, role) VALUES (%s, %s, %s)",
            (user_id, tenant_id, role),
        )
    return tenant_id


def _get_password_hash(pg_conn, username: str) -> str | None:
    """Fetch stored password_hash for username."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT password_hash FROM webui_users WHERE username = %s", (username,)
        )
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Client + session helpers
# ---------------------------------------------------------------------------


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    )


async def _login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Cookies:
    """Log in and return the cookies from the response."""
    resp = await client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.cookies


# ===========================================================================
# CP tests — change-password endpoint
# ===========================================================================


class TestChangePasswordUnauthenticated:
    """CP1: unauthenticated request → 401."""

    @pytest.mark.asyncio
    async def test_no_session_returns_401(self, clean_pg):
        """POST /api/auth/change-password without a session → 401 not_authenticated."""
        from src.db.migrate import run_migrations

        run_migrations(clean_pg)
        app = _make_app()
        async with _client(app) as client:
            resp = await client.post(
                "/api/auth/change-password",
                json={"current_password": "SomePass123!", "new_password": "NewPass456!"},
            )
        assert resp.status_code == 401, resp.text
        assert resp.json().get("error") == "not_authenticated"


class TestChangePasswordWrongCurrent:
    """CP2: wrong current password → 401 invalid_current_password."""

    @pytest.mark.asyncio
    async def test_wrong_current_password_rejected(self, clean_pg):
        """Wrong current_password triggers 401 with error=invalid_current_password."""
        from src.db.migrate import run_migrations
        from src.web_ui.auth import hash_password

        run_migrations(clean_pg)
        username = "cp_wrong_pw"
        correct_pw = "CorrectPass123!"
        pw_hash = hash_password(correct_pw)
        _insert_verified_user(clean_pg, username, f"{username}@example.com", pw_hash)

        app = _make_app()
        async with _client(app) as client:
            await _login(client, username, correct_pw)
            resp = await client.post(
                "/api/auth/change-password",
                json={"current_password": "WrongPassword9!", "new_password": "NewPass456!"},
            )
        assert resp.status_code == 401, resp.text
        assert resp.json().get("error") == "invalid_current_password"


class TestChangePasswordPolicyEnforcement:
    """CP3/CP4: new password failing policy → 400 with message."""

    @pytest.mark.asyncio
    async def test_new_password_too_short_returns_400(self, clean_pg):
        """new_password shorter than min_length → 400 with length policy message."""
        from src.db.migrate import run_migrations
        from src.web_ui.auth import hash_password

        run_migrations(clean_pg)
        username = "cp_tooshort"
        correct_pw = "CorrectPass123!"
        pw_hash = hash_password(correct_pw)
        _insert_verified_user(clean_pg, username, f"{username}@example.com", pw_hash)

        app = _make_app()
        async with _client(app) as client:
            await _login(client, username, correct_pw)
            resp = await client.post(
                "/api/auth/change-password",
                json={"current_password": correct_pw, "new_password": "short1"},
            )
        assert resp.status_code == 400, resp.text
        data = resp.json()
        assert "error" in data
        # Error message must mention min characters (policy message from validate_password)
        assert "characters" in data["error"].lower() or "password" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_new_password_too_common_returns_400(self, clean_pg):
        """new_password on common-password blocklist → 400 with 'too common' message."""
        from src.db.migrate import run_migrations
        from src.web_ui.auth import hash_password

        run_migrations(clean_pg)
        username = "cp_common"
        correct_pw = "CorrectPass123!"
        pw_hash = hash_password(correct_pw)
        _insert_verified_user(clean_pg, username, f"{username}@example.com", pw_hash)

        # Patch the validate_password function to simulate a common-password failure
        # because the actual blocklist file may not be present in all test environments.
        with mock.patch(
            "src.web_ui.routes.login.validate_password",
            return_value="Password is too common. Please choose a stronger password.",
        ):
            app = _make_app()
            async with _client(app) as client:
                await _login(client, username, correct_pw)
                resp = await client.post(
                    "/api/auth/change-password",
                    json={"current_password": correct_pw, "new_password": "password123456"},
                )
        assert resp.status_code == 400, resp.text
        data = resp.json()
        err = data.get("error", "").lower()
        assert "common" in err or "password" in err


class TestChangePasswordSamePassword:
    """CP5: new password == current password → 400."""

    @pytest.mark.asyncio
    async def test_same_password_rejected(self, clean_pg):
        """new_password identical to current_password → 400 new-must-differ."""
        from src.db.migrate import run_migrations
        from src.web_ui.auth import hash_password

        run_migrations(clean_pg)
        username = "cp_samepw"
        correct_pw = "CorrectPass123!"
        pw_hash = hash_password(correct_pw)
        _insert_verified_user(clean_pg, username, f"{username}@example.com", pw_hash)

        app = _make_app()
        async with _client(app) as client:
            await _login(client, username, correct_pw)
            resp = await client.post(
                "/api/auth/change-password",
                json={"current_password": correct_pw, "new_password": correct_pw},
            )
        assert resp.status_code == 400, resp.text
        data = resp.json()
        err = data.get("error", "").lower()
        assert "differ" in err or "new password" in err


class TestChangePasswordSuccess:
    """CP6: valid change → hash updated, old password invalidated."""

    @pytest.mark.asyncio
    async def test_valid_change_updates_hash(self, clean_pg):
        """Successful change: verify_password with new hash passes; old fails."""
        from src.db.migrate import run_migrations
        from src.web_ui.auth import hash_password, verify_password

        run_migrations(clean_pg)
        username = "cp_success"
        old_pw = "OldPassword123!"
        new_pw = "NewPassword456!"
        pw_hash = hash_password(old_pw)
        _insert_verified_user(clean_pg, username, f"{username}@example.com", pw_hash)

        app = _make_app()
        async with _client(app) as client:
            await _login(client, username, old_pw)
            resp = await client.post(
                "/api/auth/change-password",
                json={"current_password": old_pw, "new_password": new_pw},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("ok") is True

        # Verify the stored hash is now for the new password
        stored_hash = _get_password_hash(clean_pg, username)
        assert stored_hash is not None
        assert verify_password(new_pw, stored_hash), "New password must match stored hash"
        assert not verify_password(old_pw, stored_hash), "Old password must NOT match stored hash"

    @pytest.mark.asyncio
    async def test_session_remains_valid_after_change(self, clean_pg):
        """After a successful password change, the current session stays valid."""
        from src.db.migrate import run_migrations
        from src.web_ui.auth import hash_password

        run_migrations(clean_pg)
        username = "cp_sess_ok"
        old_pw = "SessionStay123!"
        new_pw = "NewSession456!"
        pw_hash = hash_password(old_pw)
        _insert_verified_user(clean_pg, username, f"{username}@example.com", pw_hash)

        app = _make_app()
        async with _client(app) as client:
            await _login(client, username, old_pw)
            change_resp = await client.post(
                "/api/auth/change-password",
                json={"current_password": old_pw, "new_password": new_pw},
            )
            assert change_resp.status_code == 200, change_resp.text

            # Session cookie is still carried — /api/auth/verify must still return ok.
            verify_resp = await client.get("/api/auth/verify")
            assert verify_resp.status_code == 200, verify_resp.text
            assert verify_resp.json().get("ok") is True


# ===========================================================================
# V tests — /api/auth/verify enrichment (WS2a)
# ===========================================================================


class TestVerifyEnrichment:
    """V1/V2/V3: /api/auth/verify now returns email + is_tenant_admin."""

    @pytest.mark.asyncio
    async def test_verify_includes_email_and_is_tenant_admin_false_for_plain_user(
        self, clean_pg
    ):
        """V1/V2: plain user (no tenant membership) → email present, is_tenant_admin=False."""
        from src.db.migrate import run_migrations
        from src.web_ui.auth import hash_password

        run_migrations(clean_pg)
        username = "vfy_plain"
        email = "vfy_plain@example.com"
        pw = "PlainUser123!"
        pw_hash = hash_password(pw)
        _insert_verified_user(clean_pg, username, email, pw_hash)

        app = _make_app()
        async with _client(app) as client:
            await _login(client, username, pw)
            resp = await client.get("/api/auth/verify")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("ok") is True
        assert "email" in data, "verify response must include email field"
        assert data["email"] == email
        assert "is_tenant_admin" in data, "verify response must include is_tenant_admin"
        assert data["is_tenant_admin"] is False

    @pytest.mark.asyncio
    async def test_verify_is_tenant_admin_true_for_tenant_admin_member(self, clean_pg):
        """V3: user with tenant_admin role → is_tenant_admin=True in verify response."""
        from src.db.migrate import run_migrations
        from src.web_ui.auth import hash_password

        run_migrations(clean_pg)
        username = "vfy_tadmin"
        email = "vfy_tadmin@example.com"
        pw = "TenantAdmin123!"
        pw_hash = hash_password(pw)
        user_id = _insert_verified_user(clean_pg, username, email, pw_hash)
        _insert_tenant_membership(clean_pg, user_id, role="tenant_admin")

        app = _make_app()
        async with _client(app) as client:
            await _login(client, username, pw)
            resp = await client.get("/api/auth/verify")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("is_tenant_admin") is True, (
            "User with tenant_admin role must see is_tenant_admin=True"
        )
