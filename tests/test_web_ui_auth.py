# tests/test_web_ui_auth.py
"""Tests for Web UI session-based authentication (M7 W16).

Business intent: An anonymous visitor to the admin Web UI gets redirected to /login.
Logging in with correct credentials grants access; wrong password is rejected;
logging out clears access.

All 6 tests use httpx.AsyncClient with ASGI transport — no real server or DB required.
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

    # Always patch _get_conn to avoid DB connections (even without seed_users).
    import src.web_ui.routes.login as login_mod

    orig_get_conn = login_mod._get_conn
    orig_lookup = login_mod._lookup_user

    hashes: dict[str, str] = {}
    if seed_users:
        hashes = {u: hash_password(p) for u, p in seed_users.items()}

    def _fake_get_conn():
        return _FakeConn()

    def _fake_lookup(conn, username: str) -> str | None:
        return hashes.get(username)

    app.state._login_patch_get_conn = (login_mod, "_get_conn", orig_get_conn)
    app.state._login_patch_lookup = (login_mod, "_lookup_user", orig_lookup)

    login_mod._get_conn = _fake_get_conn
    login_mod._lookup_user = _fake_lookup

    return app


def _restore_patches(app):
    """Undo _get_conn / _lookup_user patches after a test."""
    if hasattr(app.state, "_login_patch_get_conn"):
        mod, attr, orig = app.state._login_patch_get_conn
        setattr(mod, attr, orig)
    if hasattr(app.state, "_login_patch_lookup"):
        mod, attr, orig = app.state._login_patch_lookup
        setattr(mod, attr, orig)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUnauthRedirect:
    """Test 1 — unauthenticated request is redirected to /login."""

    @pytest.mark.asyncio
    async def test_unauth_redirects_to_login(self):
        import httpx

        app = _make_app()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.get("/repos")
        finally:
            _restore_patches(app)

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "/login" in location
        assert "next" in location


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
                    "/login",
                    data={"username": "admin", "password": "secret123", "next": "/"},
                )
                assert resp.status_code == 302
                # A Set-Cookie header should be present
                assert "osm_session" in resp.headers.get("set-cookie", "")

                # Follow the redirect with the session cookie preserved
                cookies = client.cookies
                resp2 = await client.get("/", cookies=cookies)
        finally:
            _restore_patches(app)

        # Dashboard returns 200 (or redirect to login if session not carried — fail if so)
        assert resp2.status_code == 200


class TestLoginWrongPassword:
    """Test 3 — wrong password results in redirect to /login?error=... with no session cookie."""

    @pytest.mark.asyncio
    async def test_login_wrong_password_returns_error(self):
        import httpx

        app = _make_app(seed_users={"admin": "correct"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.post(
                    "/login",
                    data={"username": "admin", "password": "WRONG", "next": "/"},
                )
        finally:
            _restore_patches(app)

        # Should redirect back to login with error param, NOT set a session cookie
        assert resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "error=invalid_credentials" in location
        set_cookie = resp.headers.get("set-cookie", "")
        # If a session cookie is set it must not carry a username
        assert "osm_session" not in set_cookie or "username" not in set_cookie


class TestLogoutClearsSession:
    """Test 4 — after logout, accessing a protected route redirects to /login."""

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
                    "/login",
                    data={"username": "admin", "password": "pw", "next": "/"},
                )
                # Confirm protected page accessible
                resp_before = await client.get("/")
                assert resp_before.status_code == 200

                # Log out
                resp_logout = await client.get("/logout")
                assert resp_logout.status_code == 302
                assert "/login" in resp_logout.headers.get("location", "")

                # Protected page should now redirect to login
                resp_after = await client.get("/")
        finally:
            _restore_patches(app)

        assert resp_after.status_code == 302
        assert "/login" in resp_after.headers.get("location", "")


class TestExemptPaths:
    """Test 5 — /login and /static/* are accessible without auth."""

    @pytest.mark.asyncio
    async def test_login_page_exempt(self):
        import httpx

        app = _make_app()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.get("/login")
        finally:
            _restore_patches(app)

        # /login itself should not redirect to /login (infinite loop)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_static_path_exempt_from_redirect(self):
        """Requests to /static/* must not be redirect-looped (middleware must exempt them).

        Even if the file doesn't exist (404), the middleware must not issue a 302.
        """
        import httpx

        app = _make_app()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.get("/static/nonexistent.css")
        finally:
            _restore_patches(app)

        # May be 404 (file not found) but must NOT be 302 to /login
        assert resp.status_code != 302


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
                    "/login",
                    data={"username": "admin", "password": "pw", "next": "/"},
                )

                # Confirm currently accessible
                resp_now = await client.get("/")
                assert resp_now.status_code == 200

                # Advance time past TTL (8h + 1s)
                future_time = time.time() + SESSION_TTL_SECONDS + 1
                with unittest.mock.patch("src.web_ui.middleware.time") as mock_time:
                    mock_time.time.return_value = future_time
                    resp_expired = await client.get("/")
        finally:
            _restore_patches(app)

        assert resp_expired.status_code == 302
        assert "/login" in resp_expired.headers.get("location", "")

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
                    "/login",
                    data={"username": "admin", "password": "pw", "next": "/"},
                )
                # Confirm accessible
                resp_valid = await client.get("/")
                assert resp_valid.status_code == 200

                # Simulate clock going BACK (session_at appears to be in the future)
                # age = time.time() - session_at < 0
                past_time = time.time() - 10000  # current time looks 10000s ago
                with unittest.mock.patch("src.web_ui.middleware.time") as mock_time:
                    mock_time.time.return_value = past_time
                    resp_future = await client.get("/")
        finally:
            _restore_patches(app)

        # session_at in future → age < 0 → must reject (302 to /login)
        assert resp_future.status_code == 302, (
            f"Expected 302 when session_at is in the future (age < 0); "
            f"got {resp_future.status_code}"
        )
        assert "/login" in resp_future.headers.get("location", "")


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
    """Finding #17 (MED): POST /login returns 429 after too many failed attempts from same IP."""

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
                # 5 failed logins — should each 302 (not rate-limited yet)
                for i in range(5):
                    resp = await client.post(
                        "/login",
                        data={"username": "admin", "password": "WRONG", "next": "/"},
                    )
                    assert resp.status_code == 302, (
                        f"Attempt {i+1}: expected 302, got {resp.status_code}"
                    )

                # 6th attempt — should be 429
                resp_limited = await client.post(
                    "/login",
                    data={"username": "admin", "password": "WRONG", "next": "/"},
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
                        "/login",
                        data={"username": "admin", "password": "WRONG", "next": "/"},
                    )

                # Successful login — must clear counter
                resp_ok = await client.post(
                    "/login",
                    data={"username": "admin", "password": "correct", "next": "/"},
                )
                assert resp_ok.status_code == 302

                # Now fail 4 more times — should still be under threshold (counter cleared)
                for i in range(4):
                    resp = await client.post(
                        "/login",
                        data={"username": "admin", "password": "WRONG", "next": "/"},
                    )
                    assert resp.status_code == 302, (
                        f"Post-success attempt {i+1}: expected 302 not 429 "
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

        The conn must still be usable after the call (tx rollback happened).
        """
        from src.web_ui.routes.dashboard import _count_embeddings

        # Build a minimal fake connection that raises ProgrammingError on execute
        # (simulating "table does not exist") and records whether rollback was called.
        class _FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, _sql):
                import psycopg2
                raise psycopg2.ProgrammingError("relation \"embeddings\" does not exist")

        rollback_called = []

        class _FakeConn2:
            autocommit = False

            def cursor(self):
                return _FakeCursor()

            def rollback(self):
                rollback_called.append(True)

        conn = _FakeConn2()
        result = _count_embeddings(conn)

        # Must return None (not raise)
        assert result is None, f"Expected None when embeddings table absent; got {result}"
        # Must have called rollback to un-poison the connection
        assert rollback_called, (
            "_count_embeddings must call conn.rollback() after ProgrammingError "
            "to prevent aborted-tx from poisoning subsequent queries"
        )
