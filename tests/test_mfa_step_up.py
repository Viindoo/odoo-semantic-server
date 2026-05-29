# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_mfa_step_up.py
"""Tests for MFA step-up endpoint and freshness gate (W3 fix verification).

Covers the bug where request.session["mfa_verified_at"] was never written,
causing require_admin_with_fresh_mfa to always return 403 for authenticated
admins who had just completed MFA via /api/auth/totp/login.

Test cases:
  1. Happy path — valid TOTP code → 200 {"ok": True}, mfa_verified_at in session.
  2. Gate passes after step-up — a fresh-MFA-gated route returns 200.
  3. Invalid TOTP code → 401 {"error": "invalid_code"}, mfa_verified_at NOT set.
  4. Valid backup code → 200 {"ok": True}, mfa_verified_at in session.
  5. Invalid backup code → 401 {"error": "invalid_backup_code"}.
  6. No session (unauthenticated) → 401 {"error": "not_authenticated"}.
  7. TOTP not enrolled → 400 {"error": "totp_not_setup"}.
  8. totp_login sets freshness — mfa_verified_at present after /api/auth/totp/login.
  9. Freshness expiry — old mfa_verified_at timestamp triggers 403 "Fresh MFA required".
 10. DB column write — _update_session_mfa_verified_at called with correct session_id.

All tests work without PostgreSQL (DB helpers patched).
"""

import os
import time
from unittest.mock import MagicMock, patch

import pyotp
import pytest

# ---------------------------------------------------------------------------
# Module-level env setup (mirrors test_totp.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_fernet_key(monkeypatch):
    """Ensure FERNET_KEY is set for Fernet encrypt/decrypt operations."""
    from cryptography.fernet import Fernet

    if not os.environ.get("FERNET_KEY"):
        monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _set_session_secret(monkeypatch):
    """Ensure WEBUI_SESSION_SECRET is set for HMAC + session middleware."""
    if not os.environ.get("WEBUI_SESSION_SECRET"):
        monkeypatch.setenv(
            "WEBUI_SESSION_SECRET", "test-session-secret-for-stepup-tests!!"
        )


@pytest.fixture(autouse=True)
def _set_webui_secure_cookie(monkeypatch):
    """Allow plain HTTP sessions in tests (mirrors test_restore_security)."""
    monkeypatch.setenv("WEBUI_SECURE_COOKIE", "0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_no_bypass():
    """Create the web UI app with auth bypass OFF.

    This module is in conftest.real_auth_flow_files so conftest does NOT
    set WEBUI_AUTH_DISABLED. We clear it here defensively in case a prior
    test leaked it (mirrors test_restore_security._make_app auth_bypass=False).
    """
    os.environ.pop("WEBUI_AUTH_DISABLED", None)
    from src.web_ui.app import create_app

    return create_app()


def _sign_mfa_token(user_id: int, ttl_seconds: int = 300) -> str:
    """Build a valid signed MFA token (mirrors create_mfa_token in totp.py)."""
    import hashlib
    import hmac as _hmac

    session_secret = os.environ.get("WEBUI_SESSION_SECRET", "dev-fallback-secret")
    expires_at = time.time() + ttl_seconds
    payload = f"{user_id}:{expires_at}"
    sig = _hmac.new(
        session_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def _encrypt_totp_secret(secret: str) -> str:
    """Fernet-encrypt a TOTP base32 secret (used to build fake totp_row)."""
    from src.web_ui.routes.totp import _encrypt_secret

    return _encrypt_secret(secret)


def _make_totp_row(secret: str, enabled: bool = True, backup_codes: list | None = None):
    """Build a fake totp_secrets row dict matching _get_totp_row() return shape."""
    from src.web_ui.routes.totp import _generate_backup_codes

    if backup_codes is None:
        _, hashed = _generate_backup_codes()
        backup_codes = hashed
    return {
        "user_id": 1,
        "secret_encrypted": _encrypt_totp_secret(secret),
        "enabled": enabled,
        "backup_codes_hash": backup_codes,
        "last_used_at": None,
    }




# ---------------------------------------------------------------------------
# Test 6: No session (unauthenticated) → 401 not_authenticated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_up_requires_authenticated_session():
    """POST step-up without any session → 401 {"error": "not_authenticated"}.

    current_user_id(request) returns None when session has no user_id/username
    and bypass is OFF.
    """
    import httpx

    app = _make_app_no_bypass()


    with patch("src.db.audit.write_audit_log", return_value=None):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            resp = await client.post(
                "/api/auth/totp/step-up",
                json={"code": "123456"},
            )

    assert resp.status_code == 401
    body = resp.json()
    assert body.get("error") == "not_authenticated"


# ---------------------------------------------------------------------------
# Helper: build an app with a pre-logged-in session (no TOTP at login time)
# ---------------------------------------------------------------------------


def _make_app_with_logged_in_session():
    """Return (app, login_mod, _sessions, originals) where all DB helpers are patched.

    Call _restore_login_patches(login_mod, originals) in finally to undo patches.
    The caller performs a login request to seed the session cookie.
    """
    app = _make_app_no_bypass()

    # Patch login_mod helpers (mirrors test_restore_security save/restore pattern)
    import src.web_ui.routes.login as login_mod
    from src.web_ui.auth import hash_password

    # Save originals before patching
    _ATTRS = (
        "_lookup_user",
        "_check_totp_enabled",
        "_create_session",
        "_revoke_all_user_sessions",
        "_lookup_session",
        "_update_session_last_seen",
        "record_login_attempt",
        "check_rate_limit",
        "_insert_audit_log",
    )
    originals = {attr: getattr(login_mod, attr) for attr in _ATTRS}

    test_hash = hash_password("password-long-enough-123")
    fake_user = {
        "id": 1,
        "password_hash": test_hash,
        "is_admin": True,
        "is_active": True,
    }

    _sessions: dict[str, dict] = {}

    def _fake_create_session(user_id, ip_address, user_agent):
        import secrets as _s

        sid = _s.token_urlsafe(16)
        _sessions[sid] = {"user_id": user_id}
        return sid

    def _fake_lookup_session(sid):
        return {"user_id": _sessions[sid]["user_id"]} if sid in _sessions else None

    login_mod._lookup_user = lambda u: fake_user if u == "admin" else None
    login_mod._check_totp_enabled = lambda u: None  # no TOTP at login step
    login_mod._create_session = _fake_create_session
    login_mod._revoke_all_user_sessions = lambda uid: None
    login_mod._lookup_session = _fake_lookup_session
    login_mod._update_session_last_seen = lambda sid: None
    login_mod.record_login_attempt = lambda **kwargs: None
    login_mod.check_rate_limit = lambda *a, **k: False
    login_mod._insert_audit_log = lambda **kwargs: None

    return app, login_mod, _sessions, originals


def _restore_login_patches(login_mod, originals: dict) -> None:
    """Undo patches applied by _make_app_with_logged_in_session."""
    for attr, orig in originals.items():
        setattr(login_mod, attr, orig)


# ---------------------------------------------------------------------------
# Test 1 & 2: Happy path — valid TOTP code sets freshness, gate passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_up_valid_code_sets_mfa_verified_at():
    """Authenticated session + valid TOTP code → 200 {"ok": True}.

    Assert: mfa_verified_at is written into the session dict.
    """
    import httpx

    from src.web_ui.routes import totp as totp_mod

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    # Generate a real TOTP secret and current code
    secret = pyotp.random_base32()
    totp_row = _make_totp_row(secret, enabled=True)
    current_code = pyotp.TOTP(secret).now()

    # Inject a session echo endpoint
    from fastapi.responses import JSONResponse as _JSONResponse
    from starlette.requests import Request as _Request

    @app.get("/_test/session_echo")
    async def _echo(request: _Request):
        return _JSONResponse(dict(request.session))

    mock_update_mfa = MagicMock()

    try:
        with (
            patch.object(totp_mod, "_get_totp_row", return_value=totp_row),
            patch.object(totp_mod, "_update_session_mfa_verified_at", mock_update_mfa),
            patch("src.web_ui.login_attempts.check_rate_limit", return_value=False),
            patch("src.web_ui.login_attempts.get_client_ip", return_value="127.0.0.1"),
            patch("src.web_ui.login_attempts.record_login_attempt"),
            patch("src.db.pg.auth_store") as mock_auth_store,
            patch("src.db.audit.write_audit_log", return_value=None),
        ):
            # _auth_store mock: resolve username for step-up
            fake_conn = MagicMock()
            fake_cur = MagicMock()
            fake_cur.fetchone.return_value = ("admin",)
            fake_cur.__enter__ = lambda s: s
            fake_cur.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value = fake_cur
            fake_conn.__enter__ = lambda s: s
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_pool = MagicMock()
            fake_pool.checkout.return_value.__enter__ = lambda s: fake_conn
            fake_pool.checkout.return_value.__exit__ = MagicMock(return_value=False)
            fake_store = MagicMock()
            fake_store._pool = fake_pool
            mock_auth_store.return_value = fake_store

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                # Login to get a session cookie
                login_resp = await client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": "password-long-enough-123",
                    },
                )
                assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"

                # Step-up with valid code
                step_up_resp = await client.post(
                    "/api/auth/totp/step-up",
                    json={"code": current_code},
                )
                assert step_up_resp.status_code == 200, (
                    f"Expected 200, got {step_up_resp.status_code}: {step_up_resp.text}"
                )
                body = step_up_resp.json()
                assert body.get("ok") is True

                # Verify mfa_verified_at is in the session
                echo_resp = await client.get("/_test/session_echo")
                session_state = echo_resp.json()
    finally:
        # Restore login_mod to a clean state (best-effort)
        _restore_login_patches(login_mod, originals)

    assert "mfa_verified_at" in session_state, (
        "mfa_verified_at must be set in session after successful step-up"
    )
    assert isinstance(session_state["mfa_verified_at"], (int, float)), (
        "mfa_verified_at must be a numeric epoch timestamp"
    )
    # _update_session_mfa_verified_at was called
    mock_update_mfa.assert_called_once()


@pytest.mark.asyncio
async def test_step_up_gate_passes_after_step_up():
    """After step-up, require_admin_with_fresh_mfa-gated route returns 200.

    Strategy: monkeypatch require_admin → always return user_id=1, then
    check that _check_mfa_freshness does NOT raise when mfa_verified_at is fresh.
    """
    import httpx

    from src.web_ui.routes import totp as totp_mod

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    secret = pyotp.random_base32()
    totp_row = _make_totp_row(secret, enabled=True)
    current_code = pyotp.TOTP(secret).now()

    # Inject a session echo endpoint
    from fastapi.responses import JSONResponse as _JSONResponse
    from starlette.requests import Request as _Request

    @app.get("/_test/session_echo")
    async def _echo2(request: _Request):
        return _JSONResponse(dict(request.session))

    # Inject a fake gated route that calls require_admin_with_fresh_mfa
    from fastapi import Depends

    import src.web_ui.auth as auth_mod

    @app.get("/_test/gated")
    async def _gated(
        request: _Request,
        uid: int = Depends(auth_mod.require_admin_with_fresh_mfa),
    ):
        return _JSONResponse({"uid": uid, "gated": True})  # noqa

    try:
        with (
            patch.object(totp_mod, "_get_totp_row", return_value=totp_row),
            patch.object(totp_mod, "_update_session_mfa_verified_at", MagicMock()),
            patch("src.web_ui.login_attempts.check_rate_limit", return_value=False),
            patch("src.web_ui.login_attempts.get_client_ip", return_value="127.0.0.1"),
            patch("src.web_ui.login_attempts.record_login_attempt"),
            patch("src.db.pg.auth_store") as mock_auth_store,
            patch("src.db.audit.write_audit_log", return_value=None),
            # require_admin DB check (is_admin) — return True so admin gate passes
            patch.object(auth_mod, "is_admin_session", return_value=True),
        ):
            fake_conn = MagicMock()
            fake_cur = MagicMock()
            fake_cur.fetchone.return_value = ("admin",)
            fake_cur.__enter__ = lambda s: s
            fake_cur.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value = fake_cur
            fake_conn.__enter__ = lambda s: s
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_pool = MagicMock()
            fake_pool.checkout.return_value.__enter__ = lambda s: fake_conn
            fake_pool.checkout.return_value.__exit__ = MagicMock(return_value=False)
            fake_store = MagicMock()
            fake_store._pool = fake_pool
            mock_auth_store.return_value = fake_store

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                # Login
                login_resp = await client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": "password-long-enough-123",
                    },
                )
                assert login_resp.status_code == 200, login_resp.text

                # Gated route before step-up → 403 (no mfa_verified_at yet)
                before_resp = await client.get("/_test/gated")
                assert before_resp.status_code == 403, (
                    f"Expected 403 before step-up, got {before_resp.status_code}"
                )

                # Step-up
                step_up_resp = await client.post(
                    "/api/auth/totp/step-up",
                    json={"code": current_code},
                )
                assert step_up_resp.status_code == 200, step_up_resp.text

                # Gated route after step-up → 200
                after_resp = await client.get("/_test/gated")
                assert after_resp.status_code == 200, (
                    f"Expected 200 after step-up, got {after_resp.status_code}: "
                    f"{after_resp.text}"
                )
                after_body = after_resp.json()
                assert after_body.get("gated") is True
    finally:
        _restore_login_patches(login_mod, originals)


# ---------------------------------------------------------------------------
# Test 3: Invalid TOTP code → 401, mfa_verified_at NOT set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_up_invalid_code_returns_401_and_no_freshness():
    """Invalid TOTP code → 401 {"error": "invalid_code"}, session unchanged."""
    import httpx

    from src.web_ui.routes import totp as totp_mod

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    secret = pyotp.random_base32()
    totp_row = _make_totp_row(secret, enabled=True)

    # Inject session echo
    from fastapi.responses import JSONResponse as _JSONResponse
    from starlette.requests import Request as _Request

    @app.get("/_test/session_echo")
    async def _echo3(request: _Request):
        return _JSONResponse(dict(request.session))

    try:
        with (
            patch.object(totp_mod, "_get_totp_row", return_value=totp_row),
            patch.object(totp_mod, "_update_session_mfa_verified_at", MagicMock()),
            patch("src.web_ui.login_attempts.check_rate_limit", return_value=False),
            patch("src.web_ui.login_attempts.get_client_ip", return_value="127.0.0.1"),
            patch("src.web_ui.login_attempts.record_login_attempt"),
            patch("src.db.pg.auth_store") as mock_auth_store,
            patch("src.db.audit.write_audit_log", return_value=None),
        ):
            fake_conn = MagicMock()
            fake_cur = MagicMock()
            fake_cur.fetchone.return_value = ("admin",)
            fake_cur.__enter__ = lambda s: s
            fake_cur.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value = fake_cur
            fake_conn.__enter__ = lambda s: s
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_pool = MagicMock()
            fake_pool.checkout.return_value.__enter__ = lambda s: fake_conn
            fake_pool.checkout.return_value.__exit__ = MagicMock(return_value=False)
            fake_store = MagicMock()
            fake_store._pool = fake_pool
            mock_auth_store.return_value = fake_store

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                # Login
                login_resp = await client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": "password-long-enough-123",
                    },
                )
                assert login_resp.status_code == 200

                # Step-up with wrong code
                resp = await client.post(
                    "/api/auth/totp/step-up",
                    json={"code": "000000"},
                )
                assert resp.status_code == 401, (
                    f"Expected 401 for invalid code, got {resp.status_code}"
                )
                assert resp.json().get("error") == "invalid_code"

                # Session must NOT have mfa_verified_at
                echo_resp = await client.get("/_test/session_echo")
                session_state = echo_resp.json()
    finally:
        _restore_login_patches(login_mod, originals)

    assert "mfa_verified_at" not in session_state, (
        "mfa_verified_at must NOT be set when step-up code is invalid"
    )


# ---------------------------------------------------------------------------
# Test 4 & 5: Backup code paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_up_valid_backup_code_sets_freshness():
    """Valid backup code → 200 {"ok": True}, mfa_verified_at in session."""
    import httpx

    from src.web_ui.routes import totp as totp_mod
    from src.web_ui.routes.totp import _generate_backup_codes

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    secret = pyotp.random_base32()
    plain_codes, hashed_codes = _generate_backup_codes()
    totp_row = _make_totp_row(secret, enabled=True, backup_codes=hashed_codes)
    valid_backup = plain_codes[0]

    # Inject session echo
    from fastapi.responses import JSONResponse as _JSONResponse
    from starlette.requests import Request as _Request

    @app.get("/_test/session_echo")
    async def _echo4(request: _Request):
        return _JSONResponse(dict(request.session))

    mock_update_mfa = MagicMock()
    mock_update_backup = MagicMock()

    try:
        with (
            patch.object(totp_mod, "_get_totp_row", return_value=totp_row),
            patch.object(totp_mod, "_update_session_mfa_verified_at", mock_update_mfa),
            patch.object(totp_mod, "_update_backup_codes", mock_update_backup),
            patch("src.web_ui.login_attempts.check_rate_limit", return_value=False),
            patch("src.web_ui.login_attempts.get_client_ip", return_value="127.0.0.1"),
            patch("src.web_ui.login_attempts.record_login_attempt"),
            patch("src.db.pg.auth_store") as mock_auth_store,
            patch("src.db.audit.write_audit_log", return_value=None),
        ):
            fake_conn = MagicMock()
            fake_cur = MagicMock()
            fake_cur.fetchone.return_value = ("admin",)
            fake_cur.__enter__ = lambda s: s
            fake_cur.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value = fake_cur
            fake_conn.__enter__ = lambda s: s
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_pool = MagicMock()
            fake_pool.checkout.return_value.__enter__ = lambda s: fake_conn
            fake_pool.checkout.return_value.__exit__ = MagicMock(return_value=False)
            fake_store = MagicMock()
            fake_store._pool = fake_pool
            mock_auth_store.return_value = fake_store

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                login_resp = await client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": "password-long-enough-123",
                    },
                )
                assert login_resp.status_code == 200

                resp = await client.post(
                    "/api/auth/totp/step-up",
                    json={"backup_code": valid_backup},
                )
                assert resp.status_code == 200, (
                    f"Expected 200 for valid backup code, got {resp.status_code}: "
                    f"{resp.text}"
                )
                assert resp.json().get("ok") is True

                echo_resp = await client.get("/_test/session_echo")
                session_state = echo_resp.json()
    finally:
        _restore_login_patches(login_mod, originals)

    assert "mfa_verified_at" in session_state, (
        "mfa_verified_at must be set after valid backup code step-up"
    )
    mock_update_mfa.assert_called_once()
    # _update_backup_codes must have been called to mark the code as used
    mock_update_backup.assert_called_once()


@pytest.mark.asyncio
async def test_step_up_invalid_backup_code_returns_401():
    """Invalid backup code → 401 {"error": "invalid_backup_code"}."""
    import httpx

    from src.web_ui.routes import totp as totp_mod
    from src.web_ui.routes.totp import _generate_backup_codes

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    secret = pyotp.random_base32()
    _, hashed_codes = _generate_backup_codes()
    totp_row = _make_totp_row(secret, enabled=True, backup_codes=hashed_codes)

    try:
        with (
            patch.object(totp_mod, "_get_totp_row", return_value=totp_row),
            patch.object(totp_mod, "_update_session_mfa_verified_at", MagicMock()),
            patch("src.web_ui.login_attempts.check_rate_limit", return_value=False),
            patch("src.web_ui.login_attempts.get_client_ip", return_value="127.0.0.1"),
            patch("src.web_ui.login_attempts.record_login_attempt"),
            patch("src.db.pg.auth_store") as mock_auth_store,
            patch("src.db.audit.write_audit_log", return_value=None),
        ):
            fake_conn = MagicMock()
            fake_cur = MagicMock()
            fake_cur.fetchone.return_value = ("admin",)
            fake_cur.__enter__ = lambda s: s
            fake_cur.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value = fake_cur
            fake_conn.__enter__ = lambda s: s
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_pool = MagicMock()
            fake_pool.checkout.return_value.__enter__ = lambda s: fake_conn
            fake_pool.checkout.return_value.__exit__ = MagicMock(return_value=False)
            fake_store = MagicMock()
            fake_store._pool = fake_pool
            mock_auth_store.return_value = fake_store

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                login_resp = await client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": "password-long-enough-123",
                    },
                )
                assert login_resp.status_code == 200

                resp = await client.post(
                    "/api/auth/totp/step-up",
                    json={"backup_code": "0000000000000000"},  # wrong backup code
                )
    finally:
        _restore_login_patches(login_mod, originals)

    assert resp.status_code == 401
    assert resp.json().get("error") == "invalid_backup_code"


# ---------------------------------------------------------------------------
# Test 7: TOTP not enrolled → 400 totp_not_setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_up_totp_not_setup_returns_400():
    """Authenticated session but no TOTP enrolled → 400 {"error": "totp_not_setup"}."""
    import httpx

    from src.web_ui.routes import totp as totp_mod

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    try:
        with (
            # _get_totp_row returns None (not enrolled)
            patch.object(totp_mod, "_get_totp_row", return_value=None),
            patch("src.web_ui.login_attempts.check_rate_limit", return_value=False),
            patch("src.web_ui.login_attempts.get_client_ip", return_value="127.0.0.1"),
            patch("src.web_ui.login_attempts.record_login_attempt"),
            patch("src.db.pg.auth_store") as mock_auth_store,
            patch("src.db.audit.write_audit_log", return_value=None),
        ):
            fake_conn = MagicMock()
            fake_cur = MagicMock()
            fake_cur.fetchone.return_value = ("admin",)
            fake_cur.__enter__ = lambda s: s
            fake_cur.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value = fake_cur
            fake_conn.__enter__ = lambda s: s
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_pool = MagicMock()
            fake_pool.checkout.return_value.__enter__ = lambda s: fake_conn
            fake_pool.checkout.return_value.__exit__ = MagicMock(return_value=False)
            fake_store = MagicMock()
            fake_store._pool = fake_pool
            mock_auth_store.return_value = fake_store

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                login_resp = await client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": "password-long-enough-123",
                    },
                )
                assert login_resp.status_code == 200

                resp = await client.post(
                    "/api/auth/totp/step-up",
                    json={"code": "123456"},
                )
    finally:
        _restore_login_patches(login_mod, originals)

    assert resp.status_code == 400
    assert resp.json().get("error") == "totp_not_setup"


# ---------------------------------------------------------------------------
# Test 7b: TOTP row exists but enabled=False → 400 totp_not_setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_up_totp_disabled_row_returns_400():
    """TOTP row exists but enabled=False → 400 {"error": "totp_not_setup"}."""
    import httpx

    from src.web_ui.routes import totp as totp_mod

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    secret = pyotp.random_base32()
    # enabled=False simulates enrollment started but not yet confirmed
    totp_row = _make_totp_row(secret, enabled=False)

    try:
        with (
            patch.object(totp_mod, "_get_totp_row", return_value=totp_row),
            patch("src.web_ui.login_attempts.check_rate_limit", return_value=False),
            patch("src.web_ui.login_attempts.get_client_ip", return_value="127.0.0.1"),
            patch("src.web_ui.login_attempts.record_login_attempt"),
            patch("src.db.pg.auth_store") as mock_auth_store,
            patch("src.db.audit.write_audit_log", return_value=None),
        ):
            fake_conn = MagicMock()
            fake_cur = MagicMock()
            fake_cur.fetchone.return_value = ("admin",)
            fake_cur.__enter__ = lambda s: s
            fake_cur.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value = fake_cur
            fake_conn.__enter__ = lambda s: s
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_pool = MagicMock()
            fake_pool.checkout.return_value.__enter__ = lambda s: fake_conn
            fake_pool.checkout.return_value.__exit__ = MagicMock(return_value=False)
            fake_store = MagicMock()
            fake_store._pool = fake_pool
            mock_auth_store.return_value = fake_store

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                login_resp = await client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": "password-long-enough-123",
                    },
                )
                assert login_resp.status_code == 200

                resp = await client.post(
                    "/api/auth/totp/step-up",
                    json={"code": "123456"},
                )
    finally:
        _restore_login_patches(login_mod, originals)

    assert resp.status_code == 400
    assert resp.json().get("error") == "totp_not_setup"


# ---------------------------------------------------------------------------
# Test 8: totp_login sets mfa_verified_at (the original bug fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_totp_login_sets_mfa_verified_at():
    """After successful /api/auth/totp/login, mfa_verified_at is present in session.

    This is the root fix: totp_login now writes request.session["mfa_verified_at"]
    so subsequent require_admin_with_fresh_mfa calls succeed without a step-up.
    """
    import httpx

    from src.web_ui.routes import totp as totp_mod

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    secret = pyotp.random_base32()
    totp_row = _make_totp_row(secret, enabled=True)
    current_code = pyotp.TOTP(secret).now()

    # Build a valid mfa_token for user_id=1
    mfa_token = _sign_mfa_token(user_id=1, ttl_seconds=300)

    # Inject session echo
    from fastapi.responses import JSONResponse as _JSONResponse
    from starlette.requests import Request as _Request

    @app.get("/_test/session_echo")
    async def _echo8(request: _Request):
        return _JSONResponse(dict(request.session))

    try:
        with (
            patch.object(totp_mod, "_get_totp_row", return_value=totp_row),
            patch.object(totp_mod, "_update_session_mfa_verified_at", MagicMock()),
            # Patch auth_store for totp/login's user lookup (username by user_id)
            patch("src.db.pg.auth_store") as mock_auth_store_login,
        ):
            fake_conn = MagicMock()
            fake_cur = MagicMock()
            # fetchone for SELECT username, is_admin FROM webui_users WHERE id = %s
            fake_cur.fetchone.return_value = ("admin", False)
            fake_cur.__enter__ = lambda s: s
            fake_cur.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value = fake_cur
            fake_conn.__enter__ = lambda s: s
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_pool = MagicMock()
            fake_pool.checkout.return_value.__enter__ = lambda s: fake_conn
            fake_pool.checkout.return_value.__exit__ = MagicMock(return_value=False)
            fake_store = MagicMock()
            fake_store._pool = fake_pool
            mock_auth_store_login.return_value = fake_store

            # Also patch _create_session inside totp.py's totp_login.
            # The session_id it returns must be known to _fake_lookup_session so
            # AuthRequiredMiddleware's _server_session_valid does not revoke
            # the follow-up session-echo request.
            _totp_login_sid = "totp-login-session-id"
            _sessions[_totp_login_sid] = {"user_id": 1}

            def _mock_create_session_fn(user_id, ip_address, user_agent):
                return _totp_login_sid

            mock_create_session = MagicMock(side_effect=_mock_create_session_fn)
            with (
                patch("src.web_ui.routes.login._create_session", mock_create_session),
                patch("src.db.audit.write_audit_log", return_value=None),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    resp = await client.post(
                        "/api/auth/totp/login",
                        json={
                            "mfa_token": mfa_token,
                            "code": current_code,
                        },
                    )
                    assert resp.status_code == 200, (
                        f"totp_login failed: {resp.status_code}: {resp.text}"
                    )
                    assert resp.json().get("ok") is True

                    echo_resp = await client.get("/_test/session_echo")
                    session_state = echo_resp.json()
    finally:
        _restore_login_patches(login_mod, originals)

    assert "mfa_verified_at" in session_state, (
        "totp_login must write mfa_verified_at into the session (root fix)"
    )
    assert isinstance(session_state["mfa_verified_at"], (int, float)), (
        "mfa_verified_at must be a numeric epoch"
    )


# ---------------------------------------------------------------------------
# Test 9: Freshness expiry — stale mfa_verified_at → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freshness_expiry_returns_403():
    """A stale mfa_verified_at (older than window) → 403 "Fresh MFA required".

    Strategy:
      1. monkeypatch get_mfa_freshness() → 1 (1-second window).
      2. Set mfa_verified_at to time.time() - 10 (expired).
      3. Hit a require_admin_with_fresh_mfa-gated route → expect 403.
    """
    import httpx

    import src.web_ui.auth as auth_mod

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    # Inject a gated route + a route to manually seed the session
    from fastapi import Depends
    from fastapi.responses import JSONResponse as _JSONResponse
    from starlette.requests import Request as _Request

    @app.get("/_test/gated_fresh")
    async def _gated_fresh(
        request: _Request,
        uid: int = Depends(auth_mod.require_admin_with_fresh_mfa),
    ):
        return _JSONResponse({"gated": True})  # noqa

    @app.post("/_test/seed_session")
    async def _seed_session(request: _Request):
        """Seed mfa_verified_at into the session (test-only helper)."""

        body = await request.json()
        for k, v in body.items():
            request.session[k] = v
        return _JSONResponse({"seeded": True})  # noqa

    # Save original require_admin for restoration
    orig_require_admin = auth_mod.require_admin

    async def _fake_require_admin(request):
        """Stub: always return user_id=1 (admin check bypassed, MFA check kept)."""
        return 1

    try:
        with (
            # Tiny freshness window: 1 second
            patch.object(auth_mod, "get_mfa_freshness", return_value=1),
            patch("src.db.audit.write_audit_log", return_value=None),
        ):
            # Patch require_admin at module level so the Depends injected into the
            # test route resolves to the stub (mirrors test_restore_security pattern).
            auth_mod.require_admin = _fake_require_admin
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                # Login
                login_resp = await client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": "password-long-enough-123",
                    },
                )
                assert login_resp.status_code == 200

                # Seed an EXPIRED mfa_verified_at (10 seconds ago, window=1s)
                seed_resp = await client.post(
                    "/_test/seed_session",
                    json={"mfa_verified_at": time.time() - 10},
                )
                assert seed_resp.status_code == 200

                # Gated route → 403 (expired freshness)
                gated_resp = await client.get("/_test/gated_fresh")
    finally:
        auth_mod.require_admin = orig_require_admin
        _restore_login_patches(login_mod, originals)

    assert gated_resp.status_code == 403, (
        f"Expected 403 for stale MFA, got {gated_resp.status_code}: {gated_resp.text}"
    )
    # Detail is now a structured dict: {"error": ..., "message": ...} (ADR-0043 D5).
    detail = gated_resp.json().get("detail", "")
    assert isinstance(detail, dict), f"403 detail must be a structured dict, got: {detail!r}"
    assert detail.get("error") == "mfa_freshness_required", (
        f"403 detail.error must be the stable step-up code, got: {detail!r}"
    )
    assert "fresh mfa required" in detail.get("message", "").lower(), (
        f"403 detail.message must retain the sentinel, got: {detail!r}"
    )


# ---------------------------------------------------------------------------
# Test 10: DB column write — _update_session_mfa_verified_at called with session_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_up_calls_update_session_mfa_with_session_id():
    """On success, _update_session_mfa_verified_at is called with the session_id.

    This verifies the DB column (active_sessions.mfa_verified_at) is kept in
    sync with the cookie session — important for server-side revocation.
    No real DB required: we patch the helper and assert it was called.
    """
    import httpx

    from src.web_ui.routes import totp as totp_mod

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    secret = pyotp.random_base32()
    totp_row = _make_totp_row(secret, enabled=True)
    current_code = pyotp.TOTP(secret).now()

    mock_update = MagicMock()

    try:
        with (
            patch.object(totp_mod, "_get_totp_row", return_value=totp_row),
            patch.object(totp_mod, "_update_session_mfa_verified_at", mock_update),
            patch("src.web_ui.login_attempts.check_rate_limit", return_value=False),
            patch("src.web_ui.login_attempts.get_client_ip", return_value="127.0.0.1"),
            patch("src.web_ui.login_attempts.record_login_attempt"),
            patch("src.db.pg.auth_store") as mock_auth_store,
            patch("src.db.audit.write_audit_log", return_value=None),
        ):
            fake_conn = MagicMock()
            fake_cur = MagicMock()
            fake_cur.fetchone.return_value = ("admin",)
            fake_cur.__enter__ = lambda s: s
            fake_cur.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value = fake_cur
            fake_conn.__enter__ = lambda s: s
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_pool = MagicMock()
            fake_pool.checkout.return_value.__enter__ = lambda s: fake_conn
            fake_pool.checkout.return_value.__exit__ = MagicMock(return_value=False)
            fake_store = MagicMock()
            fake_store._pool = fake_pool
            mock_auth_store.return_value = fake_store

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                # Login to establish session_id in cookie
                login_resp = await client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": "password-long-enough-123",
                    },
                )
                assert login_resp.status_code == 200

                # Step-up
                step_up_resp = await client.post(
                    "/api/auth/totp/step-up",
                    json={"code": current_code},
                )
                assert step_up_resp.status_code == 200
    finally:
        _restore_login_patches(login_mod, originals)

    # _update_session_mfa_verified_at must have been called exactly once
    mock_update.assert_called_once()
    # The argument is the session_id (a string — may be empty if session has no session_id key,
    # but the function is called regardless per the implementation)
    call_args = mock_update.call_args[0]
    assert len(call_args) == 1, "Must be called with exactly one positional arg (session_id)"
    assert isinstance(call_args[0], str), "session_id arg must be a string"


# ---------------------------------------------------------------------------
# Test: rate-limited step-up → 429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_up_rate_limited_returns_429():
    """check_rate_limit returns True → 429 (too many attempts)."""
    import httpx

    from src.web_ui.routes import totp as totp_mod

    app, login_mod, _sessions, originals = _make_app_with_logged_in_session()

    secret = pyotp.random_base32()
    totp_row = _make_totp_row(secret, enabled=True)

    try:
        with (
            patch.object(totp_mod, "_get_totp_row", return_value=totp_row),
            patch("src.web_ui.login_attempts.check_rate_limit", return_value=True),  # rate limited
            patch("src.web_ui.login_attempts.get_client_ip", return_value="10.0.0.1"),
            patch("src.web_ui.login_attempts.record_login_attempt"),
            patch("src.db.pg.auth_store") as mock_auth_store,
            patch("src.db.audit.write_audit_log", return_value=None),
        ):
            fake_conn = MagicMock()
            fake_cur = MagicMock()
            fake_cur.fetchone.return_value = ("admin",)
            fake_cur.__enter__ = lambda s: s
            fake_cur.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value = fake_cur
            fake_conn.__enter__ = lambda s: s
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_pool = MagicMock()
            fake_pool.checkout.return_value.__enter__ = lambda s: fake_conn
            fake_pool.checkout.return_value.__exit__ = MagicMock(return_value=False)
            fake_store = MagicMock()
            fake_store._pool = fake_pool
            mock_auth_store.return_value = fake_store

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                login_resp = await client.post(
                    "/api/auth/login",
                    json={
                        "username": "admin",
                        "password": "password-long-enough-123",
                    },
                )
                assert login_resp.status_code == 200

                resp = await client.post(
                    "/api/auth/totp/step-up",
                    json={"code": "123456"},
                )
    finally:
        _restore_login_patches(login_mod, originals)

    assert resp.status_code == 429
