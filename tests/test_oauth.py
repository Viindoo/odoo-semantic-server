# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_oauth.py
"""Unit tests for POST /api/auth/oauth-login (M9 W-OA).

All tests use httpx.AsyncClient with ASGI transport — no real DB or server
required.  DB calls are intercepted via unittest.mock.patch on the module-level
helpers in src.web_ui.routes.oauth.

Astro callback routes (/admin/auth/{google,github} and /callback/*) are Astro
SSR endpoints — not testable via ASGI/Python transport.  Integration testing of
those routes requires a running Astro dev server and is documented for future
browser tests (M9.1).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest import mock

import httpx
import pytest

os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-for-oauth-tests-32bytes!!")
os.environ.setdefault("WEBUI_SECURE_COOKIE", "0")  # allow plain HTTP in tests


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app():
    """Create the Web UI FastAPI app with loopback restriction bypassed."""
    from src.web_ui.app import create_app

    app = create_app()
    # Remove _LoopbackOnlyMiddleware so httpx ASGI transport (no real IP) works.
    app.middleware_stack = None  # type: ignore[assignment]
    # Rebuild without loopback middleware
    app.middleware_stack = app.build_middleware_stack()
    return app


# Simpler approach: patch the loopback check in the middleware
def _make_app_no_loopback():
    """Create app with loopback check disabled for unit tests."""
    import src.web_ui.app as app_mod

    # Temporarily patch _LoopbackOnlyMiddleware.dispatch to always call_next
    original_dispatch = app_mod._LoopbackOnlyMiddleware.dispatch

    async def _passthrough(self, request, call_next):
        return await call_next(request)

    app_mod._LoopbackOnlyMiddleware.dispatch = _passthrough  # type: ignore[method-assign]
    try:
        app = app_mod.create_app()
    finally:
        app_mod._LoopbackOnlyMiddleware.dispatch = original_dispatch  # type: ignore[method-assign]
    return app


# ---------------------------------------------------------------------------
# Fake DB helpers
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows=None):
        self._rows = rows or []
        self._idx = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=()):
        pass

    def fetchone(self):
        if self._rows:
            return self._rows[0]
        return None


class _FakeConn:
    autocommit = False

    def cursor(self, **_kw):
        return _FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass


class _FakePool:
    def checkout(self):
        @contextmanager
        def _ctx():
            yield _FakeConn()

        return _ctx()

    def fetch_one(self, conn, sql, params=()):
        return None


# ---------------------------------------------------------------------------
# Helper: build request with session-like env
# ---------------------------------------------------------------------------


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def _oauth_body(
    provider="google",
    oauth_id="uid_123",
    email="user@example.com",
    email_verified=True,
    name="Test User",
):
    return {
        "provider": provider,
        "oauth_id": oauth_id,
        "email": email,
        "email_verified": email_verified,
        "name": name,
    }


# ---------------------------------------------------------------------------
# Test 1 — new user is created when no existing record
# ---------------------------------------------------------------------------


class TestNewUserCreation:
    """oauth_login creates a new user when no matching oauth or email record exists."""

    @pytest.mark.asyncio
    async def test_oauth_login_new_user_creates_account(self):
        app = _make_app_no_loopback()

        new_user_row = {
            "id": 42,
            "username": "user_abc123",
            "email": "user@example.com",
            "email_verified": True,
            "is_admin": False,
            "is_active": True,
        }

        with (
            mock.patch(
                "src.web_ui.routes.oauth._lookup_user_by_oauth", return_value=None
            ),
            mock.patch(
                "src.web_ui.routes.oauth._lookup_user_by_email", return_value=None
            ),
            mock.patch(
                "src.web_ui.routes.oauth._create_oauth_user", return_value=new_user_row
            ) as mock_create,
            mock.patch(
                "src.web_ui.routes.oauth._create_session", return_value="sess_abc"
            ),
            mock.patch("src.web_ui.routes.oauth._insert_audit_log"),
        ):
            async with _client(app) as client:
                res = await client.post("/api/auth/oauth-login", json=_oauth_body())

        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["username"] == "user_abc123"
        mock_create.assert_called_once_with(
            provider="google",
            oauth_id="uid_123",
            email="user@example.com",
            email_verified=True,
            name="Test User",
        )


# ---------------------------------------------------------------------------
# Test 2 — existing user with verified email → merge OAuth credentials
# ---------------------------------------------------------------------------


class TestExistingEmailVerifiedMerges:
    """When email matches and email_verified=True, oauth columns are merged."""

    @pytest.mark.asyncio
    async def test_oauth_login_existing_email_verified_merges(self):
        app = _make_app_no_loopback()

        existing_user = {
            "id": 7,
            "username": "admin",
            "email": "user@example.com",
            "email_verified": True,
            "is_admin": True,
            "is_active": True,
            "oauth_provider": None,
            "oauth_id": None,
            "password_hash": "bcrypt_hash_here",
        }

        with (
            mock.patch(
                "src.web_ui.routes.oauth._lookup_user_by_oauth", return_value=None
            ),
            mock.patch(
                "src.web_ui.routes.oauth._lookup_user_by_email",
                return_value=existing_user,
            ),
            mock.patch(
                "src.web_ui.routes.oauth._merge_oauth_into_user"
            ) as mock_merge,
            mock.patch(
                "src.web_ui.routes.oauth._create_session", return_value="sess_xyz"
            ),
            mock.patch("src.web_ui.routes.oauth._insert_audit_log"),
        ):
            async with _client(app) as client:
                res = await client.post(
                    "/api/auth/oauth-login",
                    json=_oauth_body(email_verified=True),
                )

        assert res.status_code == 200
        mock_merge.assert_called_once_with(7, "google", "uid_123")


# ---------------------------------------------------------------------------
# Test 3 — existing email but NOT verified at provider → reject 409
# ---------------------------------------------------------------------------


class TestExistingEmailUnverifiedRejects409:
    """When email matches but email_verified=False, returns 409 to prevent takeover."""

    @pytest.mark.asyncio
    async def test_oauth_login_existing_email_unverified_rejects_409(self):
        app = _make_app_no_loopback()

        existing_user = {
            "id": 5,
            "username": "victim",
            "email": "victim@example.com",
            "email_verified": True,  # verified in our DB
            "is_admin": True,
            "is_active": True,
            "oauth_provider": None,
            "oauth_id": None,
            "password_hash": "bcrypt_hash",
        }

        with (
            mock.patch(
                "src.web_ui.routes.oauth._lookup_user_by_oauth", return_value=None
            ),
            mock.patch(
                "src.web_ui.routes.oauth._lookup_user_by_email",
                return_value=existing_user,
            ),
            mock.patch("src.web_ui.routes.oauth._insert_audit_log"),
        ):
            async with _client(app) as client:
                res = await client.post(
                    "/api/auth/oauth-login",
                    json=_oauth_body(
                        email="victim@example.com",
                        email_verified=False,  # provider has NOT verified this email
                    ),
                )

        assert res.status_code == 409
        data = res.json()
        assert data["error"] == "email_conflict"


# ---------------------------------------------------------------------------
# Test 4 — invalid provider returns 422
# ---------------------------------------------------------------------------


class TestInvalidProvider400:
    """Unknown provider is rejected by Pydantic validation (422)."""

    @pytest.mark.asyncio
    async def test_oauth_login_invalid_provider_400(self):
        app = _make_app_no_loopback()

        async with _client(app) as client:
            res = await client.post(
                "/api/auth/oauth-login",
                json=_oauth_body(provider="twitter"),
            )

        # Pydantic raises 422 Unprocessable Entity for field validation errors
        assert res.status_code == 422


# ---------------------------------------------------------------------------
# Test 5 — missing oauth_id returns 422
# ---------------------------------------------------------------------------


class TestMissingOauthId400:
    """Empty oauth_id is rejected by Pydantic validation (422)."""

    @pytest.mark.asyncio
    async def test_oauth_login_missing_oauth_id_400(self):
        app = _make_app_no_loopback()

        async with _client(app) as client:
            res = await client.post(
                "/api/auth/oauth-login",
                json=_oauth_body(oauth_id=""),  # empty string → validator rejects
            )

        assert res.status_code == 422


# ---------------------------------------------------------------------------
# Test 6 — inactive account is rejected 403
# ---------------------------------------------------------------------------


class TestInactiveAccountRejected:
    """OAuth login for an inactive account returns 403."""

    @pytest.mark.asyncio
    async def test_oauth_login_inactive_account_rejected(self):
        app = _make_app_no_loopback()

        inactive_user = {
            "id": 99,
            "username": "banned",
            "email": "banned@example.com",
            "email_verified": True,
            "is_admin": False,
            "is_active": False,  # <- inactive
        }

        with (
            mock.patch(
                "src.web_ui.routes.oauth._lookup_user_by_oauth",
                return_value=inactive_user,
            ),
            mock.patch("src.web_ui.routes.oauth._insert_audit_log"),
        ):
            async with _client(app) as client:
                res = await client.post(
                    "/api/auth/oauth-login",
                    json=_oauth_body(),
                )

        assert res.status_code == 403
        assert res.json()["error"] == "account_inactive"


# ---------------------------------------------------------------------------
# Test 7 — fast path: existing oauth match skips email lookup
# ---------------------------------------------------------------------------


class TestExistingOauthMatchFastPath:
    """When (provider, oauth_id) matches, email lookup is skipped."""

    @pytest.mark.asyncio
    async def test_oauth_fast_path_skips_email_lookup(self):
        app = _make_app_no_loopback()

        existing_oauth_user = {
            "id": 3,
            "username": "alice_gh",
            "email": "alice@example.com",
            "email_verified": True,
            "is_admin": False,
            "is_active": True,
        }

        with (
            mock.patch(
                "src.web_ui.routes.oauth._lookup_user_by_oauth",
                return_value=existing_oauth_user,
            ),
            mock.patch(
                "src.web_ui.routes.oauth._lookup_user_by_email"
            ) as mock_email_lookup,
            mock.patch(
                "src.web_ui.routes.oauth._create_session", return_value="sess_fast"
            ),
            mock.patch("src.web_ui.routes.oauth._insert_audit_log"),
        ):
            async with _client(app) as client:
                res = await client.post(
                    "/api/auth/oauth-login",
                    json=_oauth_body(provider="github", oauth_id="gh_999"),
                )

        assert res.status_code == 200
        mock_email_lookup.assert_not_called()
