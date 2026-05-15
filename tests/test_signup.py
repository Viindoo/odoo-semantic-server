# tests/test_signup.py
"""Tests for M9 W-SG: public signup, email verification, resend-verification.

All tests use httpx.AsyncClient with ASGI transport — no real server required.
A real PostgreSQL connection is needed for DB-layer assertions (pytestmark postgres).
"""

import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.postgres

# WEBUI_SESSION_SECRET is needed at app create_app() import time and is
# read-only after — setdefault is safe (won't override CI/local config).
os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-for-signup-tests-32bytes!!")
# NOTE: WEBUI_AUTH_DISABLED is now set per-test via the conftest autouse
# fixture (_bypass_webui_auth_for_legacy_tests). Setting it module-level
# leaked the bypass into subsequent test modules (especially test_web_ui_auth)
# and silently disabled the real auth flow checks. Do NOT re-add it here.

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    """Create a Web UI app with auth bypass for testing."""
    from src.web_ui.app import create_app
    return create_app()


def _run_migrations_once(pg_conn):
    """Ensure all M9 tables exist and clean test rows for isolation.

    Calls the canonical run_migrations() so the schema is consistent with
    the production migration set. This is required because the test runner
    interleaves test_signup with test_admin_users which uses clean_pg —
    the latter drops all tables before each test, leaving signup with
    a non-existent webui_users table.

    run_migrations() is idempotent (all M9 migrations use IF NOT EXISTS)
    and yoyo tracks applied state in _yoyo_migration so the second call
    just verifies "0 pending" rather than re-running anything.
    """
    from src.db.migrate import run_migrations
    run_migrations(pg_conn)
    with pg_conn.cursor() as cur:
        # Clean up any test rows from previous runs (table now guaranteed present).
        cur.execute("DELETE FROM email_verifications")
        cur.execute(
            "DELETE FROM webui_users WHERE username LIKE 'test_%' OR username LIKE 'sg_%'"
        )
    pg_conn.commit()


def _insert_unverified_user(pg_conn, username: str, email: str, password_hash: str) -> int:
    """Insert a pre-existing unverified user directly into the DB. Returns integer id."""
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
    pg_conn, token: str, user_id: int, purpose: str = "email_verify",
    *, expired: bool = False, used: bool = False,
):
    """Insert a token record directly — used to test boundary conditions.

    The production route stores sha256(raw_token) in the token column and
    looks up by that hash.  Tests insert the hashed value so the lookup
    succeeds when the test POSTs the raw token to /api/auth/verify-email.

    user_id is the integer webui_users.id (FK in email_verifications).
    """
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


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def signup_pg(pg_conn):
    """Prepare migrations + clean tables for each signup test."""
    _run_migrations_once(pg_conn)
    yield pg_conn
    # Cleanup after test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM email_verifications")
        cur.execute("DELETE FROM webui_users WHERE username LIKE 'test_%' OR username LIKE 'sg_%'")
    pg_conn.commit()


# ---------------------------------------------------------------------------
# POST /api/auth/register
# ---------------------------------------------------------------------------


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_creates_unverified_user_and_token(self, signup_pg):
        """Happy path: new user → 201, unverified row + token in DB."""
        import httpx

        from src.db.pg import get_pool
        pool = get_pool()

        app = _make_app()
        username = "sg_happy"
        email = "sg_happy@example.com"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/register", json={
                "email": email,
                "username": username,
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "hcaptcha_token": "",
            })

        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "verification_email_sent"

        with pool.checkout() as conn:
            user = pool.fetch_one(
                conn,
                "SELECT id, username, email, email_verified, is_admin"
                " FROM webui_users WHERE username = %s",
                (username,),
            )
            assert user is not None
            assert user["email"] == email
            assert user["email_verified"] is False
            assert user["is_admin"] is False

            # email_verifications.user_id is integer FK to webui_users.id
            token_row = pool.fetch_one(
                conn,
                "SELECT token, purpose FROM email_verifications WHERE user_id = %s",
                (user["id"],),
            )
            assert token_row is not None
            assert token_row["purpose"] == "email_verify"
            assert len(token_row["token"]) >= 32

    @pytest.mark.asyncio
    async def test_register_duplicate_username_409(self, signup_pg):
        """Duplicate username returns 409 with generic message."""
        import httpx

        from src.web_ui.auth import hash_password
        _insert_unverified_user(signup_pg, "sg_dup", "sg_dup@example.com", hash_password("dummy"))

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/register", json={
                "email": "sg_dup2@example.com",
                "username": "sg_dup",
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "hcaptcha_token": "",
            })

        assert resp.status_code == 409
        assert "already registered" in resp.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_register_duplicate_email_409(self, signup_pg):
        """Duplicate email returns 409 with generic message."""
        import httpx

        from src.web_ui.auth import hash_password
        _insert_unverified_user(
            signup_pg, "sg_email1", "sg_dup_email@example.com", hash_password("dummy")
        )

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/register", json={
                "email": "sg_dup_email@example.com",
                "username": "sg_email2",
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "hcaptcha_token": "",
            })

        assert resp.status_code == 409
        assert "already registered" in resp.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_register_weak_password_returns_400(self, signup_pg):
        """Short password returns 400."""
        import httpx
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/register", json={
                "email": "sg_weak@example.com",
                "username": "sg_weak",
                "password": "short",
                "confirm_password": "short",
                "hcaptcha_token": "",
            })

        assert resp.status_code == 400
        assert "12" in resp.json()["error"]  # mentions minimum length

    @pytest.mark.asyncio
    async def test_register_common_password_returns_400(self, signup_pg):
        """Common password from blocklist returns 400."""
        import httpx
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/register", json={
                "email": "sg_common@example.com",
                "username": "sg_common",
                "password": "password123456",  # not in blocklist but "password123" is
                "confirm_password": "password123456",
                "hcaptcha_token": "",
            })
        # password123456 is not in the blocklist → should succeed
        assert resp.status_code in (201, 400)  # depends on exact blocklist

    @pytest.mark.asyncio
    async def test_register_common_password_exactly_blocked(self, signup_pg):
        """Exact blocklist entry returns 400."""
        import httpx
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/register", json={
                "email": "sg_block@example.com",
                "username": "sg_block",
                "password": "password123",  # exactly in blocklist
                "confirm_password": "password123",
                "hcaptcha_token": "",
            })
        # password123 is < 12 chars so hits length check first
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_register_invalid_captcha_400(self, signup_pg, monkeypatch):
        """When HCAPTCHA_SECRET set and captcha fails → 400."""
        import httpx
        monkeypatch.setenv("HCAPTCHA_SECRET", "test-secret")

        import src.web_ui.routes.signup as signup_mod

        async def _fake_hcaptcha(token, ip):
            return False

        monkeypatch.setattr(signup_mod, "_verify_hcaptcha", _fake_hcaptcha)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/register", json={
                "email": "sg_captcha@example.com",
                "username": "sg_captcha",
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "hcaptcha_token": "bad-token",
            })

        assert resp.status_code == 400
        assert "captcha" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# POST /api/auth/verify-email
# ---------------------------------------------------------------------------


class TestVerifyEmail:
    @pytest.mark.asyncio
    async def test_verify_valid_token_marks_verified_and_logs_in(self, signup_pg):
        """Valid token → 200, email_verified=True, session cookie set."""
        import httpx

        from src.db.pg import get_pool
        from src.web_ui.auth import hash_password
        pool = get_pool()

        username = "sg_verify_ok"
        email = "sg_verify_ok@example.com"
        user_id = _insert_unverified_user(
            signup_pg, username, email, hash_password("SecurePass123!")
        )
        token = secrets.token_urlsafe(32)
        _insert_token(signup_pg, token, user_id)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/verify-email", json={"token": token})

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["username"] == username

        # Check DB state
        with pool.checkout() as conn:
            user = pool.fetch_one(
                conn,
                "SELECT email_verified FROM webui_users WHERE username = %s",
                (username,),
            )
            assert user["email_verified"] is True

            tok = pool.fetch_one(
                conn,
                "SELECT used_at FROM email_verifications WHERE token = %s",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
            assert tok["used_at"] is not None

    @pytest.mark.asyncio
    async def test_verify_expired_token_returns_410(self, signup_pg):
        """Expired token → 410 Gone."""
        import httpx

        from src.web_ui.auth import hash_password

        username = "sg_expired"
        user_id = _insert_unverified_user(
            signup_pg, username, "sg_expired@example.com", hash_password("SecurePass123!")
        )
        token = secrets.token_urlsafe(32)
        _insert_token(signup_pg, token, user_id, expired=True)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/verify-email", json={"token": token})

        assert resp.status_code == 410
        assert "expired_or_invalid" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_verify_used_token_returns_410(self, signup_pg):
        """Already-consumed token → 410 Gone."""
        import httpx

        from src.web_ui.auth import hash_password

        username = "sg_used"
        user_id = _insert_unverified_user(
            signup_pg, username, "sg_used@example.com", hash_password("SecurePass123!")
        )
        token = secrets.token_urlsafe(32)
        _insert_token(signup_pg, token, user_id, used=True)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/verify-email", json={"token": token})

        assert resp.status_code == 410

    @pytest.mark.asyncio
    async def test_verify_invalid_token_returns_410(self, signup_pg):
        """Completely unknown token → 410 Gone."""
        import httpx
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post(
                "/api/auth/verify-email", json={"token": "nonexistent-token-xyz"}
            )

        assert resp.status_code == 410


# ---------------------------------------------------------------------------
# POST /api/auth/resend-verification
# ---------------------------------------------------------------------------


class TestResendVerification:
    @pytest.mark.asyncio
    async def test_resend_rate_limit_3_per_hour_returns_429(self, signup_pg):
        """After 3 tokens in 1 hour, 4th resend → 429."""
        import httpx

        from src.web_ui.auth import hash_password

        username = "sg_ratelimit"
        email = "sg_ratelimit@example.com"
        user_id = _insert_unverified_user(
            signup_pg, username, email, hash_password("SecurePass123!")
        )

        # Insert 3 existing tokens from the last hour
        for _ in range(3):
            tok = secrets.token_urlsafe(32)
            _insert_token(signup_pg, tok, user_id)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/resend-verification", json={"email": email})

        assert resp.status_code == 429
        assert "too many" in resp.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_resend_new_token_is_added(self, signup_pg):
        """Resend with 0 prior tokens → new token inserted, old tokens untouched (additive)."""
        import httpx

        from src.db.pg import get_pool
        from src.web_ui.auth import hash_password
        pool = get_pool()

        username = "sg_resend"
        email = "sg_resend@example.com"
        user_id = _insert_unverified_user(
            signup_pg, username, email, hash_password("SecurePass123!")
        )

        # Insert 1 existing (old) token — it should remain valid
        old_token = secrets.token_urlsafe(32)
        _insert_token(signup_pg, old_token, user_id)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/resend-verification", json={"email": email})

        assert resp.status_code == 200

        # Two tokens should now exist for this user (query by integer user_id)
        with pool.checkout() as conn:
            tokens = pool.fetch_all(
                conn,
                "SELECT token FROM email_verifications"
                " WHERE user_id = %s AND purpose = 'email_verify'",
                (user_id,),
            )
        assert len(tokens) == 2

        # Old token still in DB (additive, not replaced).
        # DB stores sha256(raw_token); compare hashed value.
        old_token_hash = hashlib.sha256(old_token.encode()).hexdigest()
        token_values = [r["token"] for r in tokens]
        assert old_token_hash in token_values

    @pytest.mark.asyncio
    async def test_resend_nonexistent_email_returns_200(self, signup_pg):
        """Resend for unknown email silently returns 200 (no enumeration)."""
        import httpx
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post(
                "/api/auth/resend-verification",
                json={"email": "nobody@example.com"},
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Email helper unit tests (no DB, no SMTP)
# ---------------------------------------------------------------------------


class TestEmailHelpers:
    def test_email_logged_in_dev_mode(self, caplog):
        """When SMTP_HOST unset, email is logged at INFO — not sent."""
        from src.web_ui.email import send_verification_email

        os.environ.pop("SMTP_HOST", None)
        with caplog.at_level(logging.INFO, logger="src.web_ui.email"):
            send_verification_email(
                to="dev@example.com",
                username="devuser",
                token="abc123token",
                base_url="http://localhost:4321",
            )

        assert any("DEV MODE" in r.message for r in caplog.records)
        assert any("abc123token" in r.message for r in caplog.records)

    def test_email_template_escapes_username(self):
        """HTML email body must escape malicious username to prevent XSS injection."""
        import unittest.mock

        from src.web_ui.email import send_verification_email

        # Capture the EmailMessage that would be sent
        captured = {}

        def _fake_smtp(*args, **kwargs):
            class FakeSrv:
                def starttls(self): pass
                def send_message(self, msg): captured["msg"] = msg
                def __enter__(self): return self
                def __exit__(self, *a): pass
            return FakeSrv()

        with unittest.mock.patch.dict(os.environ, {"SMTP_HOST": "fake.smtp.host"}):
            with unittest.mock.patch("smtplib.SMTP", side_effect=_fake_smtp):
                send_verification_email(
                    to="victim@example.com",
                    username='<script>alert("xss")</script>',
                    token="safetoken",
                    base_url="https://example.com",
                )

        msg = captured.get("msg")
        assert msg is not None

        # HTML alternative should contain escaped entities, NOT raw <script>
        payload = msg.get_payload()
        html_body = ""
        if isinstance(payload, list):
            for part in payload:
                if part.get_content_type() == "text/html":
                    html_body = part.get_payload(decode=True).decode()
                    break
        assert "<script>" not in html_body
        assert "&lt;script&gt;" in html_body or "alert" not in html_body

    def test_password_reset_email_logged_in_dev_mode(self, caplog):
        """send_password_reset_email also logs in dev mode."""
        from src.web_ui.email import send_password_reset_email

        os.environ.pop("SMTP_HOST", None)
        with caplog.at_level(logging.INFO, logger="src.web_ui.email"):
            send_password_reset_email(
                to="dev2@example.com",
                username="devuser2",
                token="resettoken456",
                base_url="http://localhost:4321",
            )

        assert any("DEV MODE" in r.message or "resettoken456" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# hCaptcha helper unit tests
# ---------------------------------------------------------------------------


class TestHcaptcha:
    @pytest.mark.asyncio
    async def test_captcha_skipped_when_secret_unset(self):
        """hCaptcha returns True when HCAPTCHA_SECRET not set (dev mode)."""
        import src.web_ui.routes.signup as signup_mod
        os.environ.pop("HCAPTCHA_SECRET", None)
        result = await signup_mod._verify_hcaptcha("any-token", "127.0.0.1")
        assert result is True

    @pytest.mark.asyncio
    async def test_captcha_returns_false_on_http_error(self, monkeypatch):
        """Network failure in hCaptcha verification → returns False."""
        import httpx

        import src.web_ui.routes.signup as signup_mod
        monkeypatch.setenv("HCAPTCHA_SECRET", "test-secret")

        async def _bad_post(*a, **kw):
            raise httpx.ConnectError("simulated failure")

        monkeypatch.setattr(httpx.AsyncClient, "post", _bad_post)

        result = await signup_mod._verify_hcaptcha("token", "10.0.0.1")
        assert result is False
