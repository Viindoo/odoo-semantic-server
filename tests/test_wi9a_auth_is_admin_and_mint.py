# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_wi9a_auth_is_admin_and_mint.py
"""WI-9a: Tests for is_admin in auth success responses + _mint_default_api_key.

Coverage:
  A1  password login success response includes is_admin=True for admin user.
  A2  password login success response includes is_admin=False for non-admin user.
  A3  oauth_login success response includes is_admin field.
  A4  verify-email success response includes is_admin field.

  M1  verify-email: after success, one api_key exists for the user on the 'free' plan.
  M2  oauth new-user: after login, one api_key exists for the new user.
  M3  oauth returning-user (email merge): no duplicate key is minted.
  M4  oauth returning-user (oauth fast-path): no duplicate key is minted.
  M5  list_api_keys lazy-mint: non-admin user with zero keys gets one key on GET.
  M6  list_api_keys lazy-mint: admin user with zero keys does NOT get a key minted.
  M7  list_api_keys lazy-mint: non-admin user with existing keys does NOT get extras.

  MF  _mint_default_api_key failure is non-fatal: auth succeeds even when mint raises.

All password-login + oauth tests use httpx.AsyncClient with ASGI transport (no real DB).
DB-layer tests (M1, M2, M3, M5, M6, M7) use the real Postgres fixtures (mark=postgres).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import unittest.mock as mock

import httpx
import pytest

# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------
os.environ.setdefault(
    "WEBUI_SESSION_SECRET", "test-secret-key-for-wi9a-tests-32bytes!!"
)
os.environ.setdefault("WEBUI_SECURE_COOKIE", "0")  # allow plain HTTP in tests

# ---------------------------------------------------------------------------
# App factory helpers (mirror test_web_ui_auth.py style)
# ---------------------------------------------------------------------------


def _make_app_no_loopback():
    """Create app with loopback check disabled for unit tests (mirrors test_oauth.py)."""
    import src.web_ui.app as app_mod

    original_dispatch = app_mod._LoopbackOnlyMiddleware.dispatch

    async def _passthrough(self, request, call_next):
        return await call_next(request)

    app_mod._LoopbackOnlyMiddleware.dispatch = _passthrough  # type: ignore[method-assign]
    try:
        app = app_mod.create_app()
    finally:
        app_mod._LoopbackOnlyMiddleware.dispatch = original_dispatch  # type: ignore[method-assign]
    return app


def _make_login_app(users: dict, *, admin_usernames: set | None = None):
    """Create login app with in-memory user DB and patched sessions/audit/rate-limit.

    users: {username: plaintext_password}
    admin_usernames: set of usernames that should have is_admin=True (default: all).
    """
    import src.web_ui.routes.login as login_mod
    from src.web_ui.auth import hash_password

    if admin_usernames is None:
        admin_usernames = set(users)

    user_db: dict = {}
    for i, (u, p) in enumerate(users.items(), start=1):
        user_db[u] = {
            "id": i,
            "password_hash": hash_password(p),
            "is_admin": u in admin_usernames,
            "is_active": True,
            "password_hash_value": hash_password(p),
        }

    orig_lookup = login_mod._lookup_user
    orig_create = login_mod._create_session
    orig_revoke = login_mod._revoke_session
    orig_revoke_all = login_mod._revoke_all_user_sessions
    orig_lookup_sess = login_mod._lookup_session
    orig_update_last = login_mod._update_session_last_seen
    orig_audit = login_mod._insert_audit_log
    orig_check_rate = login_mod.check_rate_limit
    orig_record = login_mod.record_login_attempt
    orig_totp = login_mod._check_totp_enabled

    _sessions: dict = {}

    def _fake_lookup(username: str):
        return user_db.get(username)

    def _fake_create_session(user_id, ip_address, user_agent):
        sid = secrets.token_urlsafe(32)
        _sessions[sid] = {"user_id": user_id}
        return sid

    def _fake_revoke(session_id):
        _sessions.pop(session_id, None)

    def _fake_revoke_all(user_id):
        for k in [k for k, v in _sessions.items() if v["user_id"] == user_id]:
            del _sessions[k]

    def _fake_lookup_sess(session_id):
        if session_id in _sessions:
            return {"user_id": _sessions[session_id]["user_id"]}
        return None

    def _fake_noop(*args, **kwargs):
        pass

    def _fake_no_rate(identifier, ip_address=None):
        return False

    def _fake_no_totp(username):
        return None

    login_mod._lookup_user = _fake_lookup
    login_mod._create_session = _fake_create_session
    login_mod._revoke_session = _fake_revoke
    login_mod._revoke_all_user_sessions = _fake_revoke_all
    login_mod._lookup_session = _fake_lookup_sess
    login_mod._update_session_last_seen = _fake_noop
    login_mod._insert_audit_log = _fake_noop
    login_mod.check_rate_limit = _fake_no_rate
    login_mod.record_login_attempt = _fake_noop
    login_mod._check_totp_enabled = _fake_no_totp

    app = _make_app_no_loopback()

    # Restore after app creation
    login_mod._lookup_user = orig_lookup
    login_mod._create_session = orig_create
    login_mod._revoke_session = orig_revoke
    login_mod._revoke_all_user_sessions = orig_revoke_all
    login_mod._lookup_session = orig_lookup_sess
    login_mod._update_session_last_seen = orig_update_last
    login_mod._insert_audit_log = orig_audit
    login_mod.check_rate_limit = orig_check_rate
    login_mod.record_login_attempt = orig_record
    login_mod._check_totp_enabled = orig_totp

    return app, _sessions, user_db


# ---------------------------------------------------------------------------
# OAuth body helper (mirrors test_oauth.py)
# ---------------------------------------------------------------------------


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


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


# ============================================================================
# A: is_admin field in success responses (unit tests — no real DB)
# ============================================================================


class TestIsAdminInPasswordLogin:
    """A1/A2: password login success JSON includes is_admin: bool."""

    @pytest.mark.asyncio
    async def test_admin_user_login_returns_is_admin_true(self):
        """Admin user: POST /api/auth/login → is_admin=True in 200 response."""
        import src.web_ui.routes.login as login_mod
        from src.web_ui.auth import hash_password

        admin_user = {
            "id": 1,
            "password_hash": hash_password("GoodPassword123!"),
            "is_admin": True,
            "is_active": True,
        }

        _sessions: dict = {}

        def _fake_lookup(username):
            return admin_user if username == "adminuser" else None

        def _fake_create_session(user_id, ip_address, user_agent):
            sid = secrets.token_urlsafe(32)
            _sessions[sid] = {"user_id": user_id}
            return sid

        def _noop(*args, **kwargs):
            pass

        def _no_rate(identifier, ip_address=None):
            return False

        def _no_totp(username):
            return None

        app = _make_app_no_loopback()
        with (
            mock.patch.object(login_mod, "_lookup_user", _fake_lookup),
            mock.patch.object(login_mod, "_create_session", _fake_create_session),
            mock.patch.object(login_mod, "_revoke_all_user_sessions", _noop),
            mock.patch.object(login_mod, "_insert_audit_log", _noop),
            mock.patch.object(login_mod, "check_rate_limit", _no_rate),
            mock.patch.object(login_mod, "record_login_attempt", _noop),
            mock.patch.object(login_mod, "_check_totp_enabled", _no_totp),
        ):
            async with _client(app) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "adminuser", "password": "GoodPassword123!"},
                )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("ok") is True
        assert "is_admin" in data, "is_admin must be present in login success response"
        assert data["is_admin"] is True, (
            "Admin user must see is_admin=True in login response"
        )

    @pytest.mark.asyncio
    async def test_non_admin_user_login_returns_is_admin_false(self):
        """Non-admin user: POST /api/auth/login → is_admin=False in 200 response."""
        import src.web_ui.routes.login as login_mod
        from src.web_ui.auth import hash_password

        regular_user = {
            "id": 2,
            "password_hash": hash_password("GoodPassword123!"),
            "is_admin": False,
            "is_active": True,
        }

        _sessions: dict = {}

        def _fake_lookup(username):
            return regular_user if username == "regularuser" else None

        def _fake_create_session(user_id, ip_address, user_agent):
            sid = secrets.token_urlsafe(32)
            _sessions[sid] = {"user_id": user_id}
            return sid

        def _noop(*args, **kwargs):
            pass

        def _no_rate(identifier, ip_address=None):
            return False

        def _no_totp(username):
            return None

        app = _make_app_no_loopback()
        with (
            mock.patch.object(login_mod, "_lookup_user", _fake_lookup),
            mock.patch.object(login_mod, "_create_session", _fake_create_session),
            mock.patch.object(login_mod, "_revoke_all_user_sessions", _noop),
            mock.patch.object(login_mod, "_insert_audit_log", _noop),
            mock.patch.object(login_mod, "check_rate_limit", _no_rate),
            mock.patch.object(login_mod, "record_login_attempt", _noop),
            mock.patch.object(login_mod, "_check_totp_enabled", _no_totp),
        ):
            async with _client(app) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "regularuser", "password": "GoodPassword123!"},
                )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("ok") is True
        assert "is_admin" in data, "is_admin must be present in login success response"
        assert data["is_admin"] is False, (
            "Non-admin user must see is_admin=False in login response"
        )


class TestIsAdminInOauthLogin:
    """A3: oauth_login success JSON includes is_admin: bool."""

    @pytest.fixture(autouse=True)
    def _enable_signup(self, monkeypatch):
        monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", True)
        monkeypatch.setattr("src.web_ui.routes.oauth.SIGNUP_ENABLED", True)
        # Patch the gate function directly so any (real-or-leaked) app_settings
        # overlay row cannot override the open gate.  No-DB unit tests already
        # fall through to the constant, but patching the function keeps the
        # contract identical to the postgres TestMintAfterNewOauthLogin class.
        monkeypatch.setattr(
            "src.web_ui.routes.oauth.signup_enabled", lambda: True
        )

    @pytest.mark.asyncio
    async def test_oauth_login_existing_user_includes_is_admin(self):
        """Returning OAuth user: success response includes is_admin bool."""
        app = _make_app_no_loopback()

        existing_oauth_user = {
            "id": 5,
            "username": "alice_google",
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
                "src.web_ui.routes.oauth._create_session", return_value="sess_is_admin"
            ),
            mock.patch("src.web_ui.routes.oauth._insert_audit_log"),
        ):
            async with _client(app) as client:
                resp = await client.post("/api/auth/oauth-login", json=_oauth_body())

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("ok") is True
        assert "is_admin" in data, (
            "is_admin must be present in oauth_login success response"
        )
        assert data["is_admin"] is False

    @pytest.mark.asyncio
    async def test_oauth_login_new_user_includes_is_admin_false(self):
        """New OAuth user (just created): success response includes is_admin=False."""
        app = _make_app_no_loopback()

        new_user_row = {
            "id": 99,
            "username": "newuser_abc",
            "email": "newuser@example.com",
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
            ),
            mock.patch(
                "src.web_ui.routes.oauth._create_session", return_value="sess_new"
            ),
            mock.patch("src.web_ui.routes.oauth._insert_audit_log"),
            # Suppress the _mint_default_api_key call (new-user branch). Return
            # None (not a bare MagicMock) so the forwarded value stays
            # JSON-serializable — this test only asserts is_admin, not the key.
            mock.patch(
                "src.web_ui.routes.api_keys._mint_default_api_key", return_value=None
            ),
        ):
            async with _client(app) as client:
                resp = await client.post("/api/auth/oauth-login", json=_oauth_body())

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("ok") is True
        assert "is_admin" in data, (
            "is_admin must be present in oauth_login success response for new users"
        )
        assert data["is_admin"] is False


class TestNewApiKeyForwardedOnOauthLogin:
    """WI-A: oauth-login success response forwards the one-time plaintext key.

    Business rule under test (NOT implementation):
      - A brand-new OAuth user gets the freshly-minted plaintext key in the
        response so the client can show it once for copy.
      - A returning OAuth user must NOT have a key re-revealed (new_api_key None).
    These are unit tests: the mint helper is mocked to control its return value,
    so they run without a real DB.
    """

    @pytest.fixture(autouse=True)
    def _enable_signup(self, monkeypatch):
        monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", True)
        monkeypatch.setattr("src.web_ui.routes.oauth.SIGNUP_ENABLED", True)
        monkeypatch.setattr("src.web_ui.routes.oauth.signup_enabled", lambda: True)

    @pytest.mark.asyncio
    async def test_new_oauth_user_response_includes_plaintext_key(self):
        """is_new_user=True → response carries the minted plaintext key."""
        app = _make_app_no_loopback()
        new_user_row = {
            "id": 99,
            "username": "newuser_key",
            "email": "newuser_key@example.com",
            "email_verified": True,
            "is_admin": False,
            "is_active": True,
        }
        minted = "osm_unit_test_plaintext_key_value"

        with (
            mock.patch("src.web_ui.routes.oauth._lookup_user_by_oauth", return_value=None),
            mock.patch("src.web_ui.routes.oauth._lookup_user_by_email", return_value=None),
            mock.patch(
                "src.web_ui.routes.oauth._create_oauth_user", return_value=new_user_row
            ),
            mock.patch("src.web_ui.routes.oauth._create_session", return_value="sess_new"),
            mock.patch("src.web_ui.routes.oauth._insert_audit_log"),
            mock.patch(
                "src.web_ui.routes.api_keys._mint_default_api_key", return_value=minted
            ),
        ):
            async with _client(app) as client:
                resp = await client.post("/api/auth/oauth-login", json=_oauth_body())

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["new_api_key"] == minted, (
            "New OAuth user must receive the one-time plaintext key in the response"
        )
        assert data["new_api_key"].startswith("osm_")

    @pytest.mark.asyncio
    async def test_returning_oauth_user_response_has_no_key(self):
        """Returning user (is_new_user=False) → new_api_key is None (never re-revealed)."""
        app = _make_app_no_loopback()
        existing_row = {
            "id": 7,
            "username": "returning_key",
            "email": "user@example.com",
            "email_verified": True,
            "is_admin": False,
            "is_active": True,
        }

        with (
            # fast-path match on (provider, oauth_id) → returning user, no mint branch
            mock.patch(
                "src.web_ui.routes.oauth._lookup_user_by_oauth", return_value=existing_row
            ),
            mock.patch("src.web_ui.routes.oauth._create_session", return_value="sess_ret"),
            mock.patch("src.web_ui.routes.oauth._insert_audit_log"),
            # If the route ever called mint for a returning user this would leak a
            # value; asserting None proves the branch did not run.
            mock.patch(
                "src.web_ui.routes.api_keys._mint_default_api_key",
                return_value="osm_should_not_appear",
            ),
        ):
            async with _client(app) as client:
                resp = await client.post("/api/auth/oauth-login", json=_oauth_body())

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("new_api_key") is None, (
            "Returning OAuth user must NOT have a key re-revealed in the response"
        )

    @pytest.mark.asyncio
    async def test_oauth_login_succeeds_with_none_key_when_mint_raises(self):
        """Mint failure during new OAuth login → 200 with new_api_key=None (fail-closed)."""
        app = _make_app_no_loopback()
        new_user_row = {
            "id": 123,
            "username": "wi9a_oauth_mintfail",
            "email": "wi9a_oauth_mintfail@example.com",
            "email_verified": True,
            "is_admin": False,
            "is_active": True,
        }

        with (
            mock.patch("src.web_ui.routes.oauth._lookup_user_by_oauth", return_value=None),
            mock.patch("src.web_ui.routes.oauth._lookup_user_by_email", return_value=None),
            mock.patch(
                "src.web_ui.routes.oauth._create_oauth_user", return_value=new_user_row
            ),
            mock.patch("src.web_ui.routes.oauth._create_session", return_value="sess_mf"),
            mock.patch("src.web_ui.routes.oauth._insert_audit_log"),
            mock.patch(
                "src.web_ui.routes.api_keys._mint_default_api_key",
                side_effect=RuntimeError("simulated mint failure"),
            ),
        ):
            async with _client(app) as client:
                resp = await client.post("/api/auth/oauth-login", json=_oauth_body())

        assert resp.status_code == 200, (
            "oauth-login must return 200 even when mint raises, got "
            f"{resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("new_api_key") is None, (
            "On mint failure, oauth-login new_api_key must be None (fail-closed)"
        )


class TestIsAdminInVerifyEmail:
    """A4: verify-email success JSON includes is_admin: bool (requires real DB)."""

    pytestmark = pytest.mark.postgres

    @pytest.mark.asyncio
    async def test_verify_email_includes_is_admin_false_for_normal_user(
        self, signup_pg
    ):
        """After verify-email, response includes is_admin=False for a normal user."""
        from src.web_ui.auth import hash_password

        username = "wi9a_verify_isa"
        email = "wi9a_verify_isa@example.com"
        user_id = _insert_unverified_user(
            signup_pg, username, email, hash_password("SecurePass123!")
        )
        token = secrets.token_urlsafe(32)
        _insert_token(signup_pg, token, user_id)

        app = _make_app_no_loopback()
        async with _client(app) as client:
            resp = await client.post("/api/auth/verify-email", json={"token": token})

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("ok") is True
        assert "is_admin" in data, (
            "is_admin must be present in verify-email success response"
        )
        assert data["is_admin"] is False, (
            "Normal user must see is_admin=False after email verification"
        )


# ============================================================================
# M: _mint_default_api_key side effects (DB tests)
# ============================================================================

pytestmark_postgres = pytest.mark.postgres


# Reuse helpers from test_signup.py (inline copies for isolation).

def _insert_unverified_user(pg_conn, username: str, email: str, password_hash: str) -> int:
    """Insert a pre-existing unverified user directly into DB. Returns integer id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, email, email_verified, is_admin)"
            " VALUES (%s, %s, %s, FALSE, FALSE)"
            " ON CONFLICT (username) DO UPDATE SET email = EXCLUDED.email RETURNING id",
            (username, password_hash, email),
        )
        row = cur.fetchone()
    pg_conn.commit()
    return row[0]


def _insert_token(
    pg_conn,
    token: str,
    user_id: int,
    purpose: str = "email_verify",
    *,
    expired: bool = False,
    used: bool = False,
):
    """Insert a hashed token into email_verifications."""
    from datetime import UTC, datetime, timedelta

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if expired:
        expires_at = datetime.now(UTC) - timedelta(hours=1)
    else:
        expires_at = datetime.now(UTC) + timedelta(hours=24)
    used_at = datetime.now(UTC) if used else None
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO email_verifications (token, user_id, purpose, expires_at, used_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (token_hash, user_id, purpose, expires_at, used_at),
        )
    pg_conn.commit()


def _run_migrations_once(pg_conn):
    from src.db.migrate import run_migrations
    run_migrations(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM email_verifications")
        cur.execute(
            "DELETE FROM webui_users"
            " WHERE username LIKE 'wi9a_%'"
        )
        cur.execute("DELETE FROM api_keys WHERE name LIKE 'Default key (wi9a_%)'")
    pg_conn.commit()


@pytest.fixture
def signup_pg(pg_conn):
    """Migrations + clean tables for WI-9a signup tests."""
    _run_migrations_once(pg_conn)
    yield pg_conn
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM email_verifications")
        cur.execute("DELETE FROM webui_users WHERE username LIKE 'wi9a_%'")
        cur.execute("DELETE FROM api_keys WHERE name LIKE 'Default key (wi9a_%)'")
    pg_conn.commit()


def _free_plan_id(pg_conn) -> int:
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = 'free'")
        row = cur.fetchone()
    assert row is not None, "'free' plan must exist for WI-9a tests"
    return row[0]


def _count_keys_for_user(pg_conn, user_id: int) -> int:
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM api_keys WHERE user_id = %s", (user_id,))
        return pg_conn.cursor().fetchone()[0] if False else cur.fetchone()[0]


class TestMintAfterVerifyEmail:
    """M1: after verify-email, one api_key exists for the user on the 'free' plan."""

    pytestmark = pytest.mark.postgres

    @pytest.mark.asyncio
    async def test_verify_email_mints_one_free_key(self, signup_pg):
        from src.db.pg import get_pool
        from src.web_ui.auth import hash_password

        pool = get_pool()
        username = "wi9a_mint_ve"
        email = "wi9a_mint_ve@example.com"
        user_id = _insert_unverified_user(
            signup_pg, username, email, hash_password("SecurePass123!")
        )
        token = secrets.token_urlsafe(32)
        _insert_token(signup_pg, token, user_id)

        free_id = _free_plan_id(signup_pg)

        app = _make_app_no_loopback()
        async with _client(app) as client:
            resp = await client.post("/api/auth/verify-email", json={"token": token})

        assert resp.status_code == 200, resp.text

        # WI-A: the one-time plaintext key must be forwarded in the response so
        # the client can show it for copy — and it must be the PLAINTEXT, not the
        # stored hash. We assert the osm_ prefix (plaintext shape) and that it is
        # NOT equal to any hashed value persisted in the DB.
        data = resp.json()
        assert isinstance(data.get("new_api_key"), str), (
            "verify-email must forward the minted plaintext key as a string"
        )
        assert data["new_api_key"].startswith("osm_"), (
            "new_api_key must be the plaintext key (osm_ prefix), not a hash"
        )

        # Verify exactly one key was minted for this user on the 'free' plan.
        with pool.checkout() as conn:
            rows = pool.fetch_all(
                conn,
                "SELECT id, plan_id, name, key_hash FROM api_keys WHERE user_id = %s",
                (user_id,),
            )
        assert len(rows) == 1, (
            f"verify-email must mint exactly one api_key, got {len(rows)}"
        )
        assert rows[0]["plan_id"] == free_id, (
            f"Minted key must be on 'free' plan (id={free_id}),"
            f" got plan_id={rows[0]['plan_id']}"
        )
        assert data["new_api_key"] != rows[0]["key_hash"], (
            "Forwarded key must be the plaintext, never the stored hash"
        )

    @pytest.mark.asyncio
    async def test_verify_email_with_existing_key_returns_none(self, signup_pg):
        """WI-A: re-verify of a user who already has a key → new_api_key is None.

        The mint helper is idempotent (returns None when a key already exists),
        so the response must withhold any key value — there is nothing new to
        reveal, and we never re-reveal an existing one-time secret.
        """
        from src.db.pg import auth_store
        from src.web_ui.auth import hash_password

        store = auth_store()
        username = "wi9a_ve_existing"
        email = "wi9a_ve_existing@example.com"
        user_id = _insert_unverified_user(
            signup_pg, username, email, hash_password("SecurePass123!")
        )
        # Pre-create a key so the idempotent mint returns None.
        store.create_api_key("pre-existing", user_id=user_id)

        token = secrets.token_urlsafe(32)
        _insert_token(signup_pg, token, user_id)

        app = _make_app_no_loopback()
        async with _client(app) as client:
            resp = await client.post("/api/auth/verify-email", json={"token": token})

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("new_api_key") is None, (
            "User who already has a key must get new_api_key=None (nothing new to reveal)"
        )

        # Cleanup
        with signup_pg.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM webui_users WHERE id = %s", (user_id,))
        signup_pg.commit()


class TestMintAfterNewOauthLogin:
    """M2/M3/M4: oauth mint behavior for new vs returning users."""

    pytestmark = pytest.mark.postgres

    @pytest.fixture(autouse=True)
    def _enable_signup(self, monkeypatch):
        # The oauth route gates new-user creation on signup_enabled(), which
        # consults the app_settings DB overlay FIRST and only falls back to the
        # SIGNUP_ENABLED constant when no overlay row exists.  A leaked
        # signup.enabled=False overlay row (from an earlier postgres test in the
        # same CI run) would otherwise win over a monkeypatched constant and 403
        # this test.  Patch signup_enabled() directly so the gate is open
        # deterministically regardless of any DB-overlay pollution.
        monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", True)
        monkeypatch.setattr("src.web_ui.routes.oauth.SIGNUP_ENABLED", True)
        monkeypatch.setattr(
            "src.web_ui.routes.oauth.signup_enabled", lambda: True
        )

    @pytest.mark.asyncio
    async def test_new_oauth_user_gets_one_key_minted(self, signup_pg):
        """M2: brand-new OAuth user gets one free-plan key after login."""
        from src.db.pg import get_pool

        pool = get_pool()
        free_id = _free_plan_id(signup_pg)

        # Use oauth route directly with a real DB — only stub the session creation.
        # We need a real DB for _create_oauth_user and _mint_default_api_key.
        app = _make_app_no_loopback()
        with mock.patch(
            "src.web_ui.routes.oauth._create_session", return_value="sess_oauth_new"
        ):
            async with _client(app) as client:
                resp = await client.post(
                    "/api/auth/oauth-login",
                    json=_oauth_body(
                        oauth_id="wi9a_ghid_001",
                        email="wi9a_oauthnew@example.com",
                        name="WI9A New",
                    ),
                )

        assert resp.status_code == 200, resp.text

        # WI-A: brand-new OAuth user must receive the one-time plaintext key.
        data = resp.json()
        assert isinstance(data.get("new_api_key"), str), (
            "New OAuth user must receive the minted plaintext key in the response"
        )
        assert data["new_api_key"].startswith("osm_")

        # Resolve the created user_id
        with pool.checkout() as conn:
            user_row = pool.fetch_one(
                conn,
                "SELECT id FROM webui_users WHERE email = %s",
                ("wi9a_oauthnew@example.com",),
            )
        assert user_row is not None, "User must be created by oauth_login"
        user_id = user_row["id"]

        with pool.checkout() as conn:
            keys = pool.fetch_all(
                conn,
                "SELECT id, plan_id FROM api_keys WHERE user_id = %s",
                (user_id,),
            )

        assert len(keys) == 1, (
            f"New OAuth user must get exactly 1 minted key, got {len(keys)}"
        )
        assert keys[0]["plan_id"] == free_id, (
            f"Minted key must be on 'free' plan (id={free_id}), got {keys[0]['plan_id']}"
        )

        # Cleanup
        with signup_pg.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM webui_users WHERE id = %s", (user_id,))
        signup_pg.commit()

    @pytest.mark.asyncio
    async def test_returning_oauth_user_email_merge_gets_no_extra_key(
        self, signup_pg
    ):
        """M3: email-merge OAuth path (returning user) does NOT mint a key."""
        from src.db.pg import auth_store, get_pool

        pool = get_pool()
        store = auth_store()

        # Create a returning user with an existing key
        with signup_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users"
                " (username, password_hash, email, email_verified, is_admin)"
                " VALUES ('wi9a_returning', NULL, 'wi9a_returning@example.com',"
                "         TRUE, FALSE)"
                " RETURNING id"
            )
            user_id = cur.fetchone()[0]
        signup_pg.commit()

        # Pre-create one key for this user
        store.create_api_key("existing-key", user_id=user_id)

        with pool.checkout() as conn:
            keys_before = pool.fetch_all(
                conn,
                "SELECT id FROM api_keys WHERE user_id = %s",
                (user_id,),
            )
        count_before = len(keys_before)
        assert count_before == 1, f"Expected 1 key before oauth, got {count_before}"

        app = _make_app_no_loopback()
        # Patch _create_session only; real DB is used for email lookup + merge
        with mock.patch(
            "src.web_ui.routes.oauth._create_session",
            return_value="sess_returning",
        ):
            async with _client(app) as client:
                resp = await client.post(
                    "/api/auth/oauth-login",
                    json=_oauth_body(
                        oauth_id="wi9a_ghid_returning",
                        email="wi9a_returning@example.com",
                        email_verified=True,
                    ),
                )

        assert resp.status_code == 200, resp.text

        # WI-A: returning user must NOT have a key re-revealed.
        assert resp.json().get("new_api_key") is None, (
            "Returning OAuth user (email-merge) must get new_api_key=None"
        )

        with pool.checkout() as conn:
            keys_after = pool.fetch_all(
                conn,
                "SELECT id FROM api_keys WHERE user_id = %s",
                (user_id,),
            )
        assert len(keys_after) == count_before, (
            f"Returning user (email-merge path) must NOT get extra keys: "
            f"before={count_before}, after={len(keys_after)}"
        )

        # Cleanup
        with signup_pg.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM webui_users WHERE id = %s", (user_id,))
        signup_pg.commit()

    @pytest.mark.asyncio
    async def test_returning_oauth_user_fast_path_gets_no_extra_key(self, signup_pg):
        """M4: returning OAuth user via (provider, oauth_id) fast-path does NOT get a key."""
        from src.db.pg import auth_store, get_pool

        pool = get_pool()
        store = auth_store()

        # Create user with oauth columns + existing key
        with signup_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users"
                " (username, password_hash, email, email_verified,"
                "  is_admin, oauth_provider, oauth_id)"
                " VALUES ('wi9a_fastpath', NULL, 'wi9a_fastpath@example.com',"
                "         TRUE, FALSE, 'google', 'wi9a_ghid_fp_001')"
                " RETURNING id"
            )
            user_id = cur.fetchone()[0]
        signup_pg.commit()

        # Pre-create one key
        store.create_api_key("fastpath-existing-key", user_id=user_id)

        with pool.checkout() as conn:
            keys_before = pool.fetch_all(
                conn,
                "SELECT id FROM api_keys WHERE user_id = %s",
                (user_id,),
            )
        count_before = len(keys_before)

        app = _make_app_no_loopback()
        with mock.patch(
            "src.web_ui.routes.oauth._create_session",
            return_value="sess_fastpath",
        ):
            async with _client(app) as client:
                resp = await client.post(
                    "/api/auth/oauth-login",
                    json=_oauth_body(
                        oauth_id="wi9a_ghid_fp_001",
                        email="wi9a_fastpath@example.com",
                        email_verified=True,
                    ),
                )

        assert resp.status_code == 200, resp.text

        # WI-A: returning user must NOT have a key re-revealed.
        assert resp.json().get("new_api_key") is None, (
            "Returning OAuth user (fast-path) must get new_api_key=None"
        )

        with pool.checkout() as conn:
            keys_after = pool.fetch_all(
                conn,
                "SELECT id FROM api_keys WHERE user_id = %s",
                (user_id,),
            )
        assert len(keys_after) == count_before, (
            "Returning user (fast-path) must NOT get extra keys: "
            f"before={count_before}, after={len(keys_after)}"
        )

        # Cleanup
        with signup_pg.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM webui_users WHERE id = %s", (user_id,))
        signup_pg.commit()


class TestLazyMintOnListApiKeys:
    """M5/M6/M7: list_api_keys lazy-mint behavior."""

    pytestmark = pytest.mark.postgres

    @pytest.fixture
    def webui_app(self, pg_conn):
        """App with real DB + bypass auth (admin uid=1 sentinel)."""
        from src.db.migrate import run_migrations
        from src.web_ui.app import create_app

        run_migrations(pg_conn)

        with pg_conn.cursor() as cur:
            # Admin user (id=1) — bypass auth sentinel requirement
            cur.execute(
                "DELETE FROM webui_users WHERE username = %s",
                ("_wi9a_bypass_id1",),
            )
            cur.execute(
                "INSERT INTO webui_users"
                " (username, password_hash, is_admin, is_active, id)"
                " VALUES (%s, %s, TRUE, TRUE, 1)"
                " ON CONFLICT (username) DO NOTHING",
                ("_wi9a_bypass_id1", "x"),
            )
            # Non-admin user (id=10) for lazy-mint tests
            cur.execute(
                "DELETE FROM webui_users WHERE username = %s",
                ("_wi9a_nonadmin_id10",),
            )
            cur.execute(
                "INSERT INTO webui_users"
                " (username, password_hash, is_admin, is_active)"
                " VALUES (%s, %s, FALSE, TRUE)"
                " RETURNING id",
                ("_wi9a_nonadmin_id10", "x"),
            )
            nonadmin_id = cur.fetchone()[0]
        if not pg_conn.autocommit:
            pg_conn.commit()

        app = create_app()
        yield app, nonadmin_id

        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE name LIKE '%wi9a_lazy%'")
            cur.execute(
                "DELETE FROM webui_users WHERE username IN (%s, %s)",
                ("_wi9a_bypass_id1", "_wi9a_nonadmin_id10"),
            )
        if not pg_conn.autocommit:
            pg_conn.commit()

    @pytest.mark.asyncio
    async def test_non_admin_with_zero_keys_gets_one_minted_on_list(
        self, webui_app, pg_conn
    ):
        """M5: non-admin user with 0 keys → lazy-mint fires on GET /api/api-keys."""
        from src.db.pg import get_pool

        pool = get_pool()
        app, nonadmin_id = webui_app

        # Ensure no keys for this user
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE user_id = %s", (nonadmin_id,))
        pg_conn.commit()

        import src.web_ui.auth as auth_mod

        with mock.patch.object(auth_mod, "current_user_id", lambda _req: nonadmin_id):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/api-keys")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        keys = body.get("keys", [])
        assert len(keys) == 1, (
            f"Lazy-mint must produce exactly 1 key for keyless non-admin, got {len(keys)}"
        )

        # Also confirm in DB
        with pool.checkout() as conn:
            db_keys = pool.fetch_all(
                conn,
                "SELECT id, plan_id FROM api_keys WHERE user_id = %s",
                (nonadmin_id,),
            )
        assert len(db_keys) == 1, (
            f"DB must have exactly 1 minted key for user {nonadmin_id}, got {len(db_keys)}"
        )

    @pytest.mark.asyncio
    async def test_admin_user_with_zero_keys_does_not_get_mint(
        self, webui_app, pg_conn
    ):
        """M6: admin user (id=1) with 0 keys → lazy-mint must NOT fire."""
        from src.db.pg import get_pool

        pool = get_pool()
        app, _nonadmin_id = webui_app

        # Ensure no keys for admin uid=1
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE user_id = 1")
        pg_conn.commit()

        # Bypass auth gives uid=1, is_admin=True — admin path does not lazy-mint
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/api-keys")

        assert resp.status_code == 200, resp.text

        with pool.checkout() as conn:
            db_keys = pool.fetch_all(
                conn,
                "SELECT id FROM api_keys WHERE user_id = 1",
                (),
            )
        assert len(db_keys) == 0, (
            f"Admin user must NOT get a lazy-minted key, but found {len(db_keys)} key(s)"
        )

    @pytest.mark.asyncio
    async def test_non_admin_with_existing_key_does_not_get_extra(
        self, webui_app, pg_conn
    ):
        """M7: non-admin with 1 existing key → lazy-mint must NOT fire."""
        from src.db.pg import auth_store, get_pool

        pool = get_pool()
        app, nonadmin_id = webui_app

        # Pre-create one key for this user
        auth_store().create_api_key("wi9a_lazy_existing", user_id=nonadmin_id)

        with pool.checkout() as conn:
            keys_before = pool.fetch_all(
                conn,
                "SELECT id FROM api_keys WHERE user_id = %s",
                (nonadmin_id,),
            )
        assert len(keys_before) == 1, "Pre-condition: 1 key must exist"

        import src.web_ui.auth as auth_mod

        with mock.patch.object(auth_mod, "current_user_id", lambda _req: nonadmin_id):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/api-keys")

        assert resp.status_code == 200, resp.text

        with pool.checkout() as conn:
            keys_after = pool.fetch_all(
                conn,
                "SELECT id FROM api_keys WHERE user_id = %s",
                (nonadmin_id,),
            )
        assert len(keys_after) == 1, (
            f"Non-admin with existing key must NOT get extra minted key, "
            f"got {len(keys_after)} keys"
        )


class TestMintFailureIsNonFatal:
    """MF: _mint_default_api_key failure does not break the auth flow."""

    pytestmark = pytest.mark.postgres

    @pytest.mark.asyncio
    async def test_verify_email_succeeds_even_when_mint_raises(self, signup_pg):
        """MF: if _mint_default_api_key raises, verify-email still returns 200."""
        from src.web_ui.auth import hash_password
        from src.web_ui.routes import api_keys as api_keys_mod

        username = "wi9a_mintfail"
        email = "wi9a_mintfail@example.com"
        user_id = _insert_unverified_user(
            signup_pg, username, email, hash_password("SecurePass123!")
        )
        token = secrets.token_urlsafe(32)
        _insert_token(signup_pg, token, user_id)

        app = _make_app_no_loopback()
        with mock.patch.object(
            api_keys_mod,
            "_mint_default_api_key",
            side_effect=RuntimeError("simulated DB failure"),
        ):
            async with _client(app) as client:
                resp = await client.post(
                    "/api/auth/verify-email", json={"token": token}
                )

        # Auth must succeed regardless of mint failure
        assert resp.status_code == 200, (
            f"verify-email must return 200 even when _mint_default_api_key raises, "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data.get("ok") is True, "verify-email body must have ok=True"
        assert data.get("username") == username
        # WI-A: a mint failure must fail-closed — no fabricated key value. The
        # response is still 200 but new_api_key is None (we never invent a key).
        assert data.get("new_api_key") is None, (
            "On mint failure, new_api_key must be None (fail-closed, no fabricated value)"
        )


# ============================================================================
# T: is_admin in totp_login response (unit test — no real DB)
# ============================================================================


class TestIsAdminInTotpLogin:
    """T1/T2: totp_login returns is_admin=True/False matching the user's DB row."""

    @pytest.fixture(autouse=True)
    def _session_secret(self, monkeypatch):
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "test-secret-wi9a-totp-login")

    def _make_mfa_token(self, user_id: int, expires_offset: float = 300.0) -> str:
        """Build a valid signed MFA token (mirrors create_mfa_token logic)."""
        import time

        session_secret = os.environ["WEBUI_SESSION_SECRET"]
        expires_at = time.time() + expires_offset
        payload = f"{user_id}:{expires_at}"
        sig = hmac.new(
            session_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"{payload}.{sig}"

    @pytest.mark.asyncio
    async def test_totp_login_admin_user_returns_is_admin_true(self):
        """T1: MFA login for an admin user → is_admin=True in success response."""
        import src.web_ui.routes.totp as totp_mod

        user_id = 42
        username = "admin_mfa"
        mfa_token = self._make_mfa_token(user_id)

        # Fake cursor that returns (username, is_admin) tuple
        fake_row = (username, True)

        class _FakeCur:
            def execute(self, sql, params):
                pass
            def fetchone(self):
                return fake_row
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        class _FakeConn:
            def cursor(self):
                return _FakeCur()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        class _FakePool:
            def checkout(self):
                return _FakeConn()

        class _FakeStore:
            _pool = _FakePool()

        app = _make_app_no_loopback()

        with (
            mock.patch("src.db.pg.auth_store", return_value=_FakeStore()),
            mock.patch.object(
                totp_mod, "_get_totp_row",
                return_value={"enabled": True, "encrypted_secret": "x", "backup_codes": []},
            ),
            mock.patch.object(
                totp_mod, "_verify_totp_or_backup", return_value=(True, None)
            ),
            # _create_session is imported inline from login — patch the source module.
            mock.patch(
                "src.web_ui.routes.login._create_session",
                return_value="sess_totp_admin",
            ),
            mock.patch.object(totp_mod, "_update_session_mfa_verified_at"),
        ):
            async with _client(app) as client:
                resp = await client.post(
                    "/api/auth/totp/login",
                    json={"mfa_token": mfa_token, "code": "123456"},
                )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("ok") is True
        assert "is_admin" in data, "totp_login must return is_admin in success response"
        assert data["is_admin"] is True, (
            "Admin user's totp_login must return is_admin=True so the frontend "
            "can redirect to /admin/ correctly"
        )

    @pytest.mark.asyncio
    async def test_totp_login_regular_user_returns_is_admin_false(self):
        """T2: MFA login for a non-admin user → is_admin=False in success response."""
        import src.web_ui.routes.totp as totp_mod

        user_id = 7
        username = "regular_mfa"
        mfa_token = self._make_mfa_token(user_id)

        fake_row = (username, False)

        class _FakeCur:
            def execute(self, sql, params):
                pass
            def fetchone(self):
                return fake_row
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        class _FakeConn:
            def cursor(self):
                return _FakeCur()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        class _FakePool:
            def checkout(self):
                return _FakeConn()

        class _FakeStore:
            _pool = _FakePool()

        app = _make_app_no_loopback()

        with (
            mock.patch("src.db.pg.auth_store", return_value=_FakeStore()),
            mock.patch.object(
                totp_mod, "_get_totp_row",
                return_value={"enabled": True, "encrypted_secret": "x", "backup_codes": []},
            ),
            mock.patch.object(
                totp_mod, "_verify_totp_or_backup", return_value=(True, None)
            ),
            # _create_session is imported inline from login — patch the source module.
            mock.patch(
                "src.web_ui.routes.login._create_session",
                return_value="sess_totp_regular",
            ),
            mock.patch.object(totp_mod, "_update_session_mfa_verified_at"),
        ):
            async with _client(app) as client:
                resp = await client.post(
                    "/api/auth/totp/login",
                    json={"mfa_token": mfa_token, "code": "123456"},
                )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("ok") is True
        assert "is_admin" in data, "totp_login must return is_admin in success response"
        assert data["is_admin"] is False, (
            "Non-admin user's totp_login must return is_admin=False"
        )
