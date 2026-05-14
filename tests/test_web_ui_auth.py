# tests/test_web_ui_auth.py
"""Tests for Web UI session-based authentication (M8 W1 — pure JSON API).

Business intent: An anonymous visitor to the admin Web UI gets 401 JSON.
Logging in with correct credentials grants access; wrong password is rejected;
logging out clears access.

All tests use httpx.AsyncClient with ASGI transport — no real server or DB required.
The webui_users table is seeded directly via a fake dependency override.
"""

import time
import unittest.mock

import pytest

from src.web_ui.auth import (
    SESSION_TTL_SECONDS,
    hash_password,
    verify_password,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeConn:
    """Minimal fake DB connection — supports close() and cursor() context manager."""

    def close(self):
        pass


def _make_app(seed_users: dict[str, str] | None = None):
    """Create a Web UI app with optional in-memory user seed.

    seed_users: {username: plaintext_password}
    We patch _lookup_user + _get_conn to avoid needing a real PostgreSQL connection.
    """
    import os

    os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-for-unit-tests-32bytes!!")

    from src.web_ui.app import create_app

    app = create_app()

    # Patch _lookup_user to avoid needing a real PostgreSQL connection.
    import src.web_ui.routes.login as login_mod

    orig_lookup = login_mod._lookup_user

    hashes: dict[str, str] = {}
    if seed_users:
        hashes = {u: hash_password(p) for u, p in seed_users.items()}

    def _fake_lookup(username: str) -> str | None:
        return hashes.get(username)

    app.state._login_patch_lookup = (login_mod, "_lookup_user", orig_lookup)

    login_mod._lookup_user = _fake_lookup

    return app


def _restore_patches(app):
    """Undo _lookup_user patch after a test."""
    if hasattr(app.state, "_login_patch_lookup"):
        mod, attr, orig = app.state._login_patch_lookup
        setattr(mod, attr, orig)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUnauthRedirect:
    """Test 1 — unauthenticated request gets 401 JSON."""

    @pytest.mark.asyncio
    async def test_unauth_returns_401_json(self):
        import httpx

        app = _make_app()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.get("/api/repos/profiles")
        finally:
            _restore_patches(app)

        assert resp.status_code == 401
        body = resp.json()
        assert "not_authenticated" in body.get("error", "") or "not_authenticated" in str(body)


class TestLoginCorrectCredentials:
    """Test 2 — correct credentials set session; subsequent protected request returns 200."""

    @pytest.mark.asyncio
    async def test_login_correct_credentials_sets_session(self):
        import httpx

        app = _make_app(seed_users={"admin": "secret123"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "secret123"},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body.get("ok") is True
                # A Set-Cookie header should be present
                assert "osm_session" in resp.headers.get("set-cookie", "")

                # Follow up with a protected request using session cookie
                cookies = client.cookies
                resp2 = await client.get("/api/dashboard/stats", cookies=cookies)
        finally:
            _restore_patches(app)

        assert resp2.status_code == 200


class TestLoginWrongPassword:
    """Test 3 — wrong password results in 401 JSON with no session cookie."""

    @pytest.mark.asyncio
    async def test_login_wrong_password_returns_401(self):
        import httpx

        app = _make_app(seed_users={"admin": "correct"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "WRONG"},
                )
        finally:
            _restore_patches(app)

        assert resp.status_code == 401
        body = resp.json()
        assert "invalid_credentials" in body.get("error", "")
        set_cookie = resp.headers.get("set-cookie", "")
        assert "osm_session" not in set_cookie or "username" not in set_cookie


class TestLogoutClearsSession:
    """Test 4 — after logout, accessing a protected route returns 401."""

    @pytest.mark.asyncio
    async def test_logout_clears_session(self):
        import httpx

        app = _make_app(seed_users={"admin": "pw"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                # Log in
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw"},
                )
                # Confirm protected page accessible
                resp_before = await client.get("/api/dashboard/stats")
                assert resp_before.status_code == 200

                # Log out
                resp_logout = await client.post("/api/auth/logout")
                assert resp_logout.status_code == 200
                body = resp_logout.json()
                assert body.get("ok") is True

                # Protected page should now return 401
                resp_after = await client.get("/api/dashboard/stats")
        finally:
            _restore_patches(app)

        assert resp_after.status_code == 401


class TestExemptPaths:
    """Test 5 — /api/auth/* paths are accessible without auth."""

    @pytest.mark.asyncio
    async def test_login_endpoint_exempt(self):
        import httpx

        app = _make_app()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "noexist", "password": "nope"},
                )
        finally:
            _restore_patches(app)

        # Should get 401 (bad creds), not looped or blocked
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_verify_endpoint_returns_401_when_no_session(self):
        """GET /api/auth/verify returns 401 when no session present."""
        import httpx

        app = _make_app()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.get("/api/auth/verify")
        finally:
            _restore_patches(app)

        assert resp.status_code == 401
        body = resp.json()
        assert body.get("ok") is False


class TestSessionExpiry:
    """Test 6 — session cookie is rejected after TTL expires."""

    @pytest.mark.asyncio
    async def test_session_expires_after_ttl(self):
        import httpx

        app = _make_app(seed_users={"admin": "pw"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                # Log in to get a valid session
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw"},
                )

                # Confirm currently accessible
                resp_now = await client.get("/api/dashboard/stats")
                assert resp_now.status_code == 200

                # Advance time past TTL (8h + 1s)
                future_time = time.time() + SESSION_TTL_SECONDS + 1
                with unittest.mock.patch("src.web_ui.middleware.time") as mock_time:
                    mock_time.time.return_value = future_time
                    resp_expired = await client.get("/api/dashboard/stats")
        finally:
            _restore_patches(app)

        assert resp_expired.status_code == 401

    @pytest.mark.asyncio
    async def test_session_invalid_when_session_at_in_future(self):
        """Finding #14 (MED): session_at in future (negative age) must be rejected.

        Tampered or clock-skewed session_at far in the future would satisfy
        `age < SESSION_TTL_SECONDS` since age is large-negative. _session_valid
        must require 0 <= age < SESSION_TTL_SECONDS.
        """
        import httpx

        app = _make_app(seed_users={"admin": "pw"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                # Log in to get a valid session
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw"},
                )
                # Confirm accessible
                resp_valid = await client.get("/api/dashboard/stats")
                assert resp_valid.status_code == 200

                # Simulate clock going BACK (session_at appears to be in the future)
                # age = time.time() - session_at < 0
                past_time = time.time() - 10000  # current time looks 10000s ago
                with unittest.mock.patch("src.web_ui.middleware.time") as mock_time:
                    mock_time.time.return_value = past_time
                    resp_future = await client.get("/api/dashboard/stats")
        finally:
            _restore_patches(app)

        # session_at in future → age < 0 → must reject (401)
        assert resp_future.status_code == 401, (
            f"Expected 401 when session_at is in the future (age < 0); "
            f"got {resp_future.status_code}"
        )


class TestVerifyEndpoint:
    """Test GET /api/auth/verify — Astro session proxy."""

    @pytest.mark.asyncio
    async def test_verify_returns_200_when_session_valid(self):
        import httpx

        app = _make_app(seed_users={"admin": "pw"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw"},
                )
                resp = await client.get("/api/auth/verify")
        finally:
            _restore_patches(app)

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("username") == "admin"


# ---------------------------------------------------------------------------
# Unit tests for auth helpers
# ---------------------------------------------------------------------------

class TestAuthHelpers:
    def test_hash_and_verify_roundtrip(self):
        h = hash_password("my_password")
        assert verify_password("my_password", h) is True

    def test_wrong_password_rejected(self):
        h = hash_password("correct")
        assert verify_password("wrong", h) is False

    def test_hash_is_not_plaintext(self):
        h = hash_password("secret")
        assert "secret" not in h

    def test_verify_malformed_hash_returns_false(self):
        # Should not raise
        assert verify_password("pw", "not-a-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# Regression test — dashboard _count_embeddings rollback on missing table
# ---------------------------------------------------------------------------

class TestLoginRateLimit:
    """Finding #17 (MED): POST /api/auth/login returns 429 after too many failed attempts."""

    @pytest.mark.asyncio
    async def test_rate_limit_returns_429_after_threshold(self):
        """After 5 consecutive failed logins from 127.0.0.1, next attempt → 429."""
        import httpx

        import src.web_ui.routes.login as login_mod  # noqa: I001

        # Reset the module-level failure dict so tests don't interfere
        login_mod._LOGIN_FAILURES.clear()

        app = _make_app(seed_users={"admin": "correct"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                # 5 failed logins — should each 401 (not rate-limited yet)
                for i in range(5):
                    resp = await client.post(
                        "/api/auth/login",
                        json={"username": "admin", "password": "WRONG"},
                    )
                    assert resp.status_code == 401, (
                        f"Attempt {i+1}: expected 401, got {resp.status_code}"
                    )

                # 6th attempt — should be 429
                resp_limited = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "WRONG"},
                )
        finally:
            login_mod._LOGIN_FAILURES.clear()
            _restore_patches(app)

        assert resp_limited.status_code == 429, (
            f"Expected 429 after 5 failed attempts; got {resp_limited.status_code}"
        )
        body = resp_limited.json()
        assert "Too many" in body.get("error", ""), f"Expected rate limit message; got: {body}"

    @pytest.mark.asyncio
    async def test_successful_login_clears_failure_counter(self):
        """Successful login resets the failure counter so subsequent failures start fresh."""
        import httpx

        import src.web_ui.routes.login as login_mod  # noqa: I001

        login_mod._LOGIN_FAILURES.clear()

        app = _make_app(seed_users={"admin": "correct"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                # 4 failed attempts (just under threshold)
                for _ in range(4):
                    await client.post(
                        "/api/auth/login",
                        json={"username": "admin", "password": "WRONG"},
                    )

                # Successful login — must clear counter
                resp_ok = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "correct"},
                )
                assert resp_ok.status_code == 200

                # Now fail 4 more times — should still be under threshold (counter cleared)
                for i in range(4):
                    resp = await client.post(
                        "/api/auth/login",
                        json={"username": "admin", "password": "WRONG"},
                    )
                    assert resp.status_code == 401, (
                        f"Post-success attempt {i+1}: expected 401 not 429 "
                        f"(counter should have been cleared); got {resp.status_code}"
                    )
        finally:
            login_mod._LOGIN_FAILURES.clear()
            _restore_patches(app)


class TestDashboardCountEmbeddingsRollback:
    """Finding #4 (HIGH): dashboard.py must rollback aborted tx when embeddings
    table is absent, so subsequent queries on the same connection are not poisoned.
    """

    def test_count_embeddings_handles_missing_table(self):
        """_count_embeddings returns None (not raise) when embeddings table absent.

        After refactoring, _count_embeddings() uses the centralized pool internally.
        We mock get_pool() to inject a fake connection that raises ProgrammingError.
        """
        import unittest.mock as mock

        import psycopg2

        from src.web_ui.routes.dashboard import _count_embeddings

        class _FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, _sql, _params=()):
                raise psycopg2.ProgrammingError("relation \"embeddings\" does not exist")

        class _FakeConn:
            autocommit = False

            def cursor(self, **_kw):
                return _FakeCursor()

            def rollback(self):
                pass

        class _FakePool:
            def checkout(self):
                from contextlib import contextmanager
                @contextmanager
                def _ctx():
                    yield _FakeConn()
                return _ctx()

            def fetch_one(self, conn, sql, params=()):
                with conn.cursor() as cur:
                    cur.execute(sql, params)

        with mock.patch("src.db.pg.get_pool", return_value=_FakePool()):
            result = _count_embeddings()

        # Must return None (not raise)
        assert result is None, f"Expected None when embeddings table absent; got {result}"
