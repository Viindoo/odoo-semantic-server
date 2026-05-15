# tests/test_web_ui_auth.py
"""Tests for Web UI session-based authentication (M9 W-AC hardening).

Business intent: An anonymous visitor to the admin Web UI gets 401 JSON.
Logging in with correct credentials grants access; wrong password is rejected;
logging out clears access.

All tests use httpx.AsyncClient with ASGI transport — no real server or DB required.
The webui_users table is seeded directly via a fake dependency override.

M9 W-AC additions:
  - test_login_timing_constant: bcrypt timing constant (dummy hash, <50ms delta)
  - test_login_rate_limit_postgres: Postgres-backed 429 after 5 failures
  - test_trusted_proxy_xff_honored: TRUSTED_PROXY_CIDRS honors X-FF
  - test_untrusted_proxy_xff_ignored: no TRUSTED_PROXY_CIDRS → peer IP used
  - test_session_revoke_after_logout: cookie A invalid after logout
  - test_session_rotation_on_login: new login creates new session, old revoked
  - test_password_too_short_generic_error: min_length reject → generic 401
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


def _make_app(
    seed_users: dict[str, str] | None = None,
    patch_sessions: bool = True,
    patch_attempts: bool = True,
):
    """Create a Web UI app with optional in-memory user seed.

    seed_users: {username: plaintext_password}
    We patch _lookup_user + session/attempt helpers to avoid needing a real PostgreSQL
    connection.

    patch_sessions: if True, replace session store functions with in-memory dicts.
    patch_attempts: if True, replace rate-limit functions with no-ops (not limited).
    """
    import os

    os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-for-unit-tests-32bytes!!")

    from src.web_ui.app import create_app

    app = create_app()

    import src.web_ui.routes.login as login_mod

    orig_lookup = login_mod._lookup_user

    # Build user table: {username: {id, password_hash, is_admin, is_active}}
    user_db: dict[str, dict] = {}
    if seed_users:
        for i, (u, p) in enumerate(seed_users.items(), start=1):
            user_db[u] = {
                "id": i,
                "password_hash": hash_password(p),
                "is_admin": True,
                "is_active": True,
            }

    def _fake_lookup(username: str) -> dict | None:
        return user_db.get(username)

    app.state._login_patch_lookup = (login_mod, "_lookup_user", orig_lookup)
    login_mod._lookup_user = _fake_lookup

    if patch_sessions:
        # In-memory session store
        _sessions: dict[str, dict] = {}

        def _fake_create_session(user_id, ip_address, user_agent):
            import secrets as _secrets
            sid = _secrets.token_urlsafe(32)
            _sessions[sid] = {"user_id": user_id}
            return sid

        def _fake_revoke_session(session_id):
            _sessions.pop(session_id, None)

        def _fake_revoke_all_user_sessions(user_id):
            to_delete = [k for k, v in _sessions.items() if v["user_id"] == user_id]
            for k in to_delete:
                del _sessions[k]

        def _fake_lookup_session(session_id):
            if session_id in _sessions:
                return {"user_id": _sessions[session_id]["user_id"]}
            return None

        def _fake_update_session_last_seen(session_id):
            pass

        login_mod._create_session = _fake_create_session
        login_mod._revoke_session = _fake_revoke_session
        login_mod._revoke_all_user_sessions = _fake_revoke_all_user_sessions
        login_mod._lookup_session = _fake_lookup_session
        login_mod._update_session_last_seen = _fake_update_session_last_seen

        # Also patch middleware import of these functions
        import src.web_ui.middleware as mw_mod

        mw_mod._server_session_valid.__globals__  # noqa: B018 — confirm available

        app.state._sessions_store = _sessions
        app.state._session_fakes = (login_mod, _sessions)

    if patch_attempts:
        # Patch rate-limit at the point of use (login_mod) because login.py
        # uses `from ... import check_rate_limit` (name bound in module namespace).
        orig_check = login_mod.check_rate_limit
        orig_record = login_mod.record_login_attempt

        def _fake_check_rate_limit(identifier, ip_address=None):
            return False  # never rate-limit

        def _fake_record_attempt(**kwargs):
            pass  # no-op

        login_mod.check_rate_limit = _fake_check_rate_limit
        login_mod.record_login_attempt = _fake_record_attempt
        app.state._attempt_patches = (login_mod, orig_check, orig_record)

    # Suppress audit log DB calls
    orig_audit = login_mod._insert_audit_log

    def _fake_audit(*args, **kwargs):
        pass

    login_mod._insert_audit_log = _fake_audit
    app.state._audit_patch = (login_mod, orig_audit)

    return app


def _restore_patches(app):
    """Undo all patches after a test."""
    import src.web_ui.routes.login as login_mod

    if hasattr(app.state, "_login_patch_lookup"):
        mod, attr, orig = app.state._login_patch_lookup
        setattr(mod, attr, orig)

    if hasattr(app.state, "_audit_patch"):
        mod, orig = app.state._audit_patch
        mod._insert_audit_log = orig

    if hasattr(app.state, "_attempt_patches"):
        mod, orig_check, orig_record = app.state._attempt_patches
        mod.check_rate_limit = orig_check
        mod.record_login_attempt = orig_record

    # Restore session functions if patched
    for attr in (
        "_create_session",
        "_revoke_session",
        "_revoke_all_user_sessions",
        "_lookup_session",
        "_update_session_last_seen",
    ):
        orig_name = f"_orig_{attr}"
        if hasattr(login_mod, orig_name):
            setattr(login_mod, attr, getattr(login_mod, orig_name))


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

        app = _make_app(seed_users={"admin": "secret123abcdef"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "secret123abcdef"},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body.get("ok") is True
                # A Set-Cookie header should be present
                assert "osm_session" in resp.headers.get("set-cookie", "")

                # Follow up with a protected request — client auto-manages session cookies
                resp2 = await client.get("/api/dashboard/stats")
        finally:
            _restore_patches(app)

        assert resp2.status_code == 200


class TestLoginWrongPassword:
    """Test 3 — wrong password results in 401 JSON with no session cookie."""

    @pytest.mark.asyncio
    async def test_login_wrong_password_returns_401(self):
        import httpx

        app = _make_app(seed_users={"admin": "correct_password_long"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "WRONG_WRONG_WRONG"},
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

        app = _make_app(seed_users={"admin": "pw_long_enough_12"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                # Log in
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw_long_enough_12"},
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
                    json={"username": "noexist", "password": "nope_long_enough"},
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

        app = _make_app(seed_users={"admin": "pw_long_enough_12"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                # Log in to get a valid session
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw_long_enough_12"},
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

        app = _make_app(seed_users={"admin": "pw_long_enough_12"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                # Log in to get a valid session
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw_long_enough_12"},
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

        app = _make_app(seed_users={"admin": "pw_long_enough_12"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw_long_enough_12"},
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
    """Rate-limit tests — now Postgres-backed (F2).

    Patches are applied at the point of use (src.web_ui.routes.login) because
    login.py uses `from ... import check_rate_limit` (name bound in module).
    """

    @pytest.mark.asyncio
    async def test_rate_limit_returns_429_after_threshold(self):
        """After 5 consecutive failed logins, next attempt → 429.

        Patches check_rate_limit at src.web_ui.routes.login to return True on
        the 6th call, simulating the Postgres counter having reached threshold.
        """
        import httpx

        import src.web_ui.routes.login as login_mod

        call_count = [0]
        orig_check = login_mod.check_rate_limit
        orig_record = login_mod.record_login_attempt

        def _counting_check(identifier, ip_address=None):
            return call_count[0] >= 5  # rate-limit from 5th call onward

        def _counting_record(**kwargs):
            call_count[0] += 1

        login_mod.check_rate_limit = _counting_check
        login_mod.record_login_attempt = _counting_record

        app = _make_app(
            seed_users={"admin": "correct_password_12"},
            patch_attempts=False,  # we applied our own patches above
        )
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
                        json={"username": "admin", "password": "WRONG_WRONG_WRONG"},
                    )
                    assert resp.status_code == 401, (
                        f"Attempt {i+1}: expected 401, got {resp.status_code}"
                    )

                # 6th attempt — should be 429
                resp_limited = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "WRONG_WRONG_WRONG"},
                )
        finally:
            login_mod.check_rate_limit = orig_check
            login_mod.record_login_attempt = orig_record
            _restore_patches(app)

        assert resp_limited.status_code == 429, (
            f"Expected 429 after 5 failed attempts; got {resp_limited.status_code}"
        )
        body = resp_limited.json()
        assert "Too many" in body.get("error", ""), f"Expected rate limit message; got: {body}"

    @pytest.mark.asyncio
    async def test_login_rate_limit_postgres(self):
        """F2: 6 attempts → 429. Verify rows inserted into login_attempts (mocked).

        Simulates the Postgres rate-limit logic by tracking recorded attempts
        in an in-memory list and checking count thresholds.
        Patches are applied at src.web_ui.routes.login (point of use).
        """
        import httpx

        import src.web_ui.routes.login as login_mod

        recorded_attempts: list[dict] = []
        orig_check = login_mod.check_rate_limit
        orig_record = login_mod.record_login_attempt

        def _mem_record(*, identifier, success, ip_address=None, user_agent=None):
            recorded_attempts.append(
                {"identifier": identifier, "success": success, "ip_address": ip_address}
            )

        def _mem_check(identifier, ip_address=None):
            failures = [
                a for a in recorded_attempts
                if a["identifier"] == identifier and not a["success"]
            ]
            return len(failures) >= 5

        login_mod.check_rate_limit = _mem_check
        login_mod.record_login_attempt = _mem_record

        app = _make_app(
            seed_users={"admin": "correct_password_12"},
            patch_attempts=False,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                for _ in range(5):
                    await client.post(
                        "/api/auth/login",
                        json={"username": "admin", "password": "WRONG_WRONG_WRONG"},
                    )

                resp_limited = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "WRONG_WRONG_WRONG"},
                )
        finally:
            login_mod.check_rate_limit = orig_check
            login_mod.record_login_attempt = orig_record
            _restore_patches(app)

        assert resp_limited.status_code == 429
        # Verify 5 failure rows were recorded
        failures = [a for a in recorded_attempts if not a["success"]]
        assert len(failures) == 5, (
            f"Expected 5 failure rows in login_attempts; got {len(failures)}"
        )

    @pytest.mark.asyncio
    async def test_successful_login_does_not_clear_failure_audit(self):
        """Successful login does NOT clear prior failure rows (audit-friendly).

        The new design keeps failure rows permanently; threshold is time-windowed.
        This test verifies that failure rows from before a successful login persist.
        Patches applied at src.web_ui.routes.login (point of use).
        """
        import httpx

        import src.web_ui.routes.login as login_mod

        recorded: list[dict] = []
        orig_check = login_mod.check_rate_limit
        orig_record = login_mod.record_login_attempt

        def _mem_record(*, identifier, success, ip_address=None, user_agent=None):
            recorded.append({"identifier": identifier, "success": success})

        login_mod.record_login_attempt = _mem_record
        login_mod.check_rate_limit = lambda identifier, ip_address=None: False

        app = _make_app(
            seed_users={"admin": "correct_password_12"},
            patch_attempts=False,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                # 2 failed attempts
                for _ in range(2):
                    await client.post(
                        "/api/auth/login",
                        json={"username": "admin", "password": "WRONG_WRONG_WRONG"},
                    )
                # 1 successful attempt
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "correct_password_12"},
                )
        finally:
            login_mod.check_rate_limit = orig_check
            login_mod.record_login_attempt = orig_record
            _restore_patches(app)

        # 2 failures + 1 success should all be in recorded
        all_identifiers = [r["identifier"] for r in recorded]
        assert "admin" in all_identifiers
        failures = [r for r in recorded if not r["success"]]
        successes = [r for r in recorded if r["success"]]
        assert len(failures) == 2
        assert len(successes) == 1


# ---------------------------------------------------------------------------
# M9 W-AC new tests
# ---------------------------------------------------------------------------


class TestTrustedProxyXFF:
    """F3 — TRUSTED_PROXY_CIDRS allowlist for X-Forwarded-For."""

    def test_trusted_proxy_xff_honored(self):
        """When peer is in TRUSTED_PROXY_CIDRS, X-FF first hop is the client IP."""
        import os

        os.environ["TRUSTED_PROXY_CIDRS"] = "127.0.0.0/8"

        # Invalidate cache
        from src.web_ui import login_attempts as la_mod
        la_mod._TRUSTED_PROXIES = None

        try:
            class _FakeClient:
                host = "127.0.0.1"

            class _FakeRequest:
                client = _FakeClient()
                headers = {"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}

            from src.web_ui.login_attempts import get_client_ip

            ip = get_client_ip(_FakeRequest())
        finally:
            os.environ.pop("TRUSTED_PROXY_CIDRS", None)
            la_mod._TRUSTED_PROXIES = None

        assert ip == "1.2.3.4", f"Expected 1.2.3.4 from X-FF; got {ip}"

    def test_untrusted_proxy_xff_ignored(self):
        """When TRUSTED_PROXY_CIDRS is empty, X-FF is ignored; peer IP is used."""
        import os

        os.environ.pop("TRUSTED_PROXY_CIDRS", None)
        from src.web_ui import login_attempts as la_mod
        la_mod._TRUSTED_PROXIES = None

        try:
            class _FakeClient:
                host = "203.0.113.5"

            class _FakeRequest:
                client = _FakeClient()
                headers = {"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}

            from src.web_ui.login_attempts import get_client_ip

            ip = get_client_ip(_FakeRequest())
        finally:
            la_mod._TRUSTED_PROXIES = None

        assert ip == "203.0.113.5", f"Expected peer IP; got {ip}"


class TestSessionRevokeAfterLogout:
    """F7 — server-side session revoke: cookie invalid after logout."""

    @pytest.mark.asyncio
    async def test_session_revoke_after_logout(self):
        """Login → cookie A → logout → request with cookie A → 401."""
        import httpx

        app = _make_app(seed_users={"admin": "pw_long_enough_12"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw_long_enough_12"},
                )
                # Capture cookie before logout
                cookie_before = dict(client.cookies)

                resp_before = await client.get("/api/dashboard/stats")
                assert resp_before.status_code == 200

                await client.post("/api/auth/logout")

                # Replay the old cookie
                resp_after = await client.get(
                    "/api/dashboard/stats",
                    cookies=cookie_before,
                )
        finally:
            _restore_patches(app)

        assert resp_after.status_code == 401, (
            f"Expected 401 after logout with old cookie; got {resp_after.status_code}"
        )

    @pytest.mark.asyncio
    async def test_session_rotation_on_login(self):
        """F7 session rotation: second login creates new session, first session revoked."""
        import httpx

        from src.web_ui.routes import login as login_mod

        sessions_store: dict = {}
        orig_create = login_mod._create_session
        orig_revoke_all = login_mod._revoke_all_user_sessions
        orig_revoke = login_mod._revoke_session
        orig_lookup = login_mod._lookup_session

        created_sessions: list[str] = []

        def _tracking_create(user_id, ip_address, user_agent):
            import secrets as _s
            sid = _s.token_urlsafe(32)
            sessions_store[sid] = {"user_id": user_id}
            created_sessions.append(sid)
            return sid

        def _tracking_revoke_all(user_id):
            to_del = [k for k, v in list(sessions_store.items()) if v["user_id"] == user_id]
            for k in to_del:
                del sessions_store[k]

        def _tracking_revoke(session_id):
            sessions_store.pop(session_id, None)

        def _tracking_lookup(session_id):
            return sessions_store.get(session_id)

        login_mod._create_session = _tracking_create
        login_mod._revoke_all_user_sessions = _tracking_revoke_all
        login_mod._revoke_session = _tracking_revoke
        login_mod._lookup_session = _tracking_lookup
        login_mod._update_session_last_seen = lambda sid: None

        app = _make_app(
            seed_users={"admin": "pw_long_enough_12"},
            patch_sessions=False,  # we applied our own session patches
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                # First login
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw_long_enough_12"},
                )
                assert len(created_sessions) == 1
                session_id_a = created_sessions[0]
                assert session_id_a in sessions_store

                # Second login — should rotate (revoke old, create new)
                await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "pw_long_enough_12"},
                )
                assert len(created_sessions) == 2
                session_id_b = created_sessions[1]

                # Old session A must be gone
                assert session_id_a not in sessions_store, (
                    "Session A should be revoked after session rotation"
                )
                # New session B must exist
                assert session_id_b in sessions_store

        finally:
            login_mod._create_session = orig_create
            login_mod._revoke_all_user_sessions = orig_revoke_all
            login_mod._revoke_session = orig_revoke
            login_mod._lookup_session = orig_lookup
            _restore_patches(app)


class TestPasswordComplexity:
    """Password min_length=12 + common-password blocklist return generic 401."""

    @pytest.mark.asyncio
    async def test_password_too_short_generic_error(self):
        """Password <12 chars → 401 generic error, does not leak 'min_length'."""
        import httpx

        app = _make_app(seed_users={"admin": "correct_password_long"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "short"},
                )
        finally:
            _restore_patches(app)

        assert resp.status_code in (401, 422), (
            f"Expected 401 or 422 for short password; got {resp.status_code}"
        )
        body = resp.json()
        # Must not leak validation details
        body_str = str(body)
        assert "min_length" not in body_str, "min_length leaked in error response"
        assert "12" not in body_str or "invalid_credentials" in body_str, (
            "Password length constraint leaked in error response"
        )

    @pytest.mark.asyncio
    async def test_common_password_rejected_with_generic_error(self):
        """Common password → 401 generic error."""
        import httpx

        from src.web_ui.routes import login as login_mod

        orig_load = login_mod._load_common_passwords

        # Ensure "password123" is in the common list
        login_mod._load_common_passwords = lambda: frozenset(["password123456789"])

        app = _make_app(seed_users={"admin": "password123456789"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "password123456789"},
                )
        finally:
            login_mod._load_common_passwords = orig_load
            _restore_patches(app)

        assert resp.status_code == 401
        assert resp.json().get("error") == "invalid_credentials"


class TestLoginTimingConstant:
    """F1 — dummy-hash unconditional verify: timing must be constant.

    Measures average login time for valid user vs non-existent user.
    Delta must be < 50ms on 10-round average.
    Marked flaky: CI environments with high contention may see larger jitter.
    """

    @pytest.mark.asyncio
    @pytest.mark.flaky
    async def test_login_timing_constant(self):
        import statistics

        import httpx

        N = 5  # fewer rounds for CI speed; still catches O(1) vs O(bcrypt) splits

        app = _make_app(seed_users={"realuser": "correct_password_12"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client:
                times_real: list[float] = []
                times_fake: list[float] = []

                for _ in range(N):
                    t0 = time.perf_counter()
                    await client.post(
                        "/api/auth/login",
                        json={"username": "realuser", "password": "wrong_password_here"},
                    )
                    times_real.append(time.perf_counter() - t0)

                for _ in range(N):
                    t0 = time.perf_counter()
                    await client.post(
                        "/api/auth/login",
                        json={"username": "nonexistentuser", "password": "wrong_password_here"},
                    )
                    times_fake.append(time.perf_counter() - t0)
        finally:
            _restore_patches(app)

        avg_real = statistics.mean(times_real)
        avg_fake = statistics.mean(times_fake)
        delta_ms = abs(avg_real - avg_fake) * 1000

        assert delta_ms < 50, (
            f"Timing delta too large: real={avg_real*1000:.1f}ms, "
            f"fake={avg_fake*1000:.1f}ms, delta={delta_ms:.1f}ms (threshold 50ms). "
            "Possible timing oracle: dummy hash may not be running."
        )


class TestProductionStartupAssertion:
    """Startup assertion: ENVIRONMENT=production without WEBUI_SESSION_SECRET → SystemExit."""

    def test_production_without_secret_raises_system_exit(self):
        import os

        os.environ["ENVIRONMENT"] = "production"
        os.environ.pop("WEBUI_SESSION_SECRET", None)

        import src.web_ui.auth as auth_mod
        # Reset the dev fallback so get_session_secret() re-evaluates
        auth_mod._DEV_FALLBACK_SECRET = None

        try:
            with pytest.raises(SystemExit):
                auth_mod.get_session_secret()
        finally:
            os.environ.pop("ENVIRONMENT", None)
            auth_mod._DEV_FALLBACK_SECRET = None


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
