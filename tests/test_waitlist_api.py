# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_waitlist_api.py
"""Integration tests for POST /api/waitlist.

Business intent (8 cases):
  T1  201 on fresh email — row inserted into waitlist_emails.
  T2  409 on duplicate email — ON CONFLICT DO NOTHING path.
  T3  400 on invalid email format.
  T4  400 on invalid plan value.
  T5  429 rate limit exceeded (mock rate limiter).
  T6  hCaptcha skipped in dev mode (HCAPTCHA_SECRET unset).
  T7  hCaptcha failure returns 400 (HCAPTCHA_SECRET set, mock httpx).
  T8  Admin email notify called on success (mock send function).

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Uses httpx.AsyncClient + ASGITransport with base_url="http://127.0.0.1" to
correctly satisfy _LoopbackOnlyMiddleware (mirrors test_signup.py pattern).
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Module-level: ensure env vars available before create_app() is imported.
# ---------------------------------------------------------------------------
os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-waitlist-tests-32bytes!!")
os.environ.setdefault("WEBUI_SECURE_COOKIE", "0")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _drop_waitlist_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS waitlist_emails CASCADE")
    conn.commit()


def _reset_rate_limit() -> None:
    """Clear in-memory rate limit state so tests are isolated."""
    from src.web_ui import rate_limit as rl
    rl._per_ip_buckets.clear()
    rl._last_prune = 0.0
    rl._lock = None


@pytest.fixture(autouse=True)
def reset_rate_limit_state():
    """Reset per-IP rate limit buckets before every test in this module."""
    _reset_rate_limit()
    yield
    _reset_rate_limit()


@pytest.fixture
def migrated_pg(clean_pg):
    """Drop waitlist table, run all migrations, yield raw pg connection."""
    _drop_waitlist_table(clean_pg)
    run_migrations(clean_pg)
    yield clean_pg


def _make_app():
    from src.web_ui.app import create_app
    return create_app()


def _make_client(app):
    """Return an httpx.AsyncClient with ASGITransport + loopback base_url."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    )


# ---------------------------------------------------------------------------
# T1: 201 on fresh email
# ---------------------------------------------------------------------------

class TestFreshEmailSubscribes:
    """T1: POST /api/waitlist with a fresh email returns 201 and inserts DB row."""

    @pytest.mark.asyncio
    async def test_returns_201(self, migrated_pg):
        async with _make_client(_make_app()) as client:
            resp = await client.post("/api/waitlist", json={"email": "fresh@example.com"})
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_response_body_has_status_and_email(self, migrated_pg):
        async with _make_client(_make_app()) as client:
            resp = await client.post("/api/waitlist", json={"email": "fresh2@example.com"})
        data = resp.json()
        assert data.get("status") == "subscribed"
        assert data.get("email") == "fresh2@example.com"

    @pytest.mark.asyncio
    async def test_row_inserted_in_db(self, migrated_pg):
        email = "dbcheck@example.com"
        async with _make_client(_make_app()) as client:
            await client.post("/api/waitlist", json={"email": email})
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT email FROM waitlist_emails WHERE email = %s", (email,))
            row = cur.fetchone()
        assert row is not None, "Row must be inserted into waitlist_emails"
        assert row[0] == email

    @pytest.mark.asyncio
    async def test_plan_stored_when_provided(self, migrated_pg):
        email = "planuser@example.com"
        async with _make_client(_make_app()) as client:
            await client.post("/api/waitlist", json={"email": email, "plan": "pro"})
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT plan FROM waitlist_emails WHERE email = %s", (email,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "pro"


# ---------------------------------------------------------------------------
# T2: 409 on duplicate email
# ---------------------------------------------------------------------------

class TestDuplicateEmail:
    """T2: POST /api/waitlist with an already-subscribed email returns 409."""

    @pytest.mark.asyncio
    async def test_returns_409(self, migrated_pg):
        async with _make_client(_make_app()) as client:
            await client.post("/api/waitlist", json={"email": "dup@example.com"})
            resp = await client.post("/api/waitlist", json={"email": "dup@example.com"})
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_409_body_has_error_key(self, migrated_pg):
        async with _make_client(_make_app()) as client:
            await client.post("/api/waitlist", json={"email": "dup2@example.com"})
            resp = await client.post("/api/waitlist", json={"email": "dup2@example.com"})
        data = resp.json()
        assert "error" in data


# ---------------------------------------------------------------------------
# T3: 400 on invalid email
# ---------------------------------------------------------------------------

class TestInvalidEmail:
    """T3: POST /api/waitlist with a malformed email returns 400."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_email", [
        "",
        "notanemail",
        "@nodomain",
        "no-at-sign",
        "a" * 255 + "@example.com",  # too long
    ])
    async def test_bad_email_returns_400(self, migrated_pg, bad_email):
        async with _make_client(_make_app()) as client:
            resp = await client.post("/api/waitlist", json={"email": bad_email})
        assert resp.status_code == 400, (
            f"Expected 400 for email={bad_email!r}, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# T4: 400 on invalid plan
# ---------------------------------------------------------------------------

class TestInvalidPlan:
    """T4: POST /api/waitlist with an unrecognised plan returns 400."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_plan", ["enterprise", "starter", "FREEE", "premium"])
    async def test_bad_plan_returns_400(self, migrated_pg, bad_plan):
        async with _make_client(_make_app()) as client:
            resp = await client.post(
                "/api/waitlist",
                json={"email": f"plantest_{bad_plan}@example.com", "plan": bad_plan},
            )
        assert resp.status_code == 400, (
            f"Expected 400 for plan={bad_plan!r}, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_valid_plan_accepted(self, migrated_pg):
        async with _make_client(_make_app()) as client:
            for plan in ("free", "pro", "team"):
                resp = await client.post(
                    "/api/waitlist",
                    json={"email": f"valid_{plan}@example.com", "plan": plan},
                )
                assert resp.status_code == 201, (
                    f"Expected 201 for plan={plan!r}, got {resp.status_code}: {resp.text}"
                )


# ---------------------------------------------------------------------------
# T5: 429 rate limit exceeded
# ---------------------------------------------------------------------------

class TestRateLimit:
    """T5: When the rate limiter returns False, the endpoint returns 429."""

    @pytest.mark.asyncio
    async def test_rate_limited_returns_429(self, migrated_pg):
        """Patch check_ip_rate_limit to return False, verify 429."""
        with patch(
            "src.web_ui.routes.waitlist.check_ip_rate_limit",
            new=AsyncMock(return_value=False),
        ):
            async with _make_client(_make_app()) as client:
                resp = await client.post("/api/waitlist", json={"email": "rate@example.com"})
        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_rate_limited_body_has_error(self, migrated_pg):
        with patch(
            "src.web_ui.routes.waitlist.check_ip_rate_limit",
            new=AsyncMock(return_value=False),
        ):
            async with _make_client(_make_app()) as client:
                resp = await client.post("/api/waitlist", json={"email": "rate2@example.com"})
        data = resp.json()
        assert data.get("error") == "rate_limited"


# ---------------------------------------------------------------------------
# T6: hCaptcha skipped in dev mode
# ---------------------------------------------------------------------------

class TestHCaptchaDevMode:
    """T6: When HCAPTCHA_SECRET is unset, captcha is skipped (dev mode)."""

    @pytest.mark.asyncio
    async def test_no_token_accepted_in_dev(self, migrated_pg, monkeypatch):
        """Without HCAPTCHA_SECRET, a request without a token must succeed."""
        monkeypatch.delenv("HCAPTCHA_SECRET", raising=False)
        async with _make_client(_make_app()) as client:
            resp = await client.post("/api/waitlist", json={"email": "devmode@example.com"})
        assert resp.status_code == 201, (
            f"Dev mode (no HCAPTCHA_SECRET) must skip captcha, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# T7: hCaptcha failure returns 400
# ---------------------------------------------------------------------------

class TestHCaptchaFailure:
    """T7: When HCAPTCHA_SECRET is set and captcha fails, return 400."""

    @pytest.mark.asyncio
    async def test_captcha_fail_returns_400(self, migrated_pg, monkeypatch):
        monkeypatch.setenv("HCAPTCHA_SECRET", "test-secret")
        with patch(
            "src.web_ui.routes.waitlist._verify_hcaptcha",
            new=AsyncMock(return_value=False),
        ):
            async with _make_client(_make_app()) as client:
                resp = await client.post(
                    "/api/waitlist",
                    json={"email": "captcha@example.com", "hcaptcha_token": "bad-token"},
                )
        assert resp.status_code == 400, (
            f"Captcha failure must return 400, got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_captcha_fail_body(self, migrated_pg, monkeypatch):
        monkeypatch.setenv("HCAPTCHA_SECRET", "test-secret")
        with patch(
            "src.web_ui.routes.waitlist._verify_hcaptcha",
            new=AsyncMock(return_value=False),
        ):
            async with _make_client(_make_app()) as client:
                resp = await client.post(
                    "/api/waitlist",
                    json={"email": "captcha2@example.com", "hcaptcha_token": "bad"},
                )
        data = resp.json()
        assert data.get("error") == "captcha_failed"


# ---------------------------------------------------------------------------
# T8: Admin email notify called on success
# ---------------------------------------------------------------------------

class TestAdminEmailNotify:
    """T8: On successful subscribe, send_waitlist_notify_email is called once."""

    @pytest.mark.asyncio
    async def test_notify_called_on_success(self, migrated_pg):
        mock_notify = MagicMock(return_value=True)
        with patch("src.web_ui.routes.waitlist.send_waitlist_notify_email", mock_notify):
            async with _make_client(_make_app()) as client:
                resp = await client.post(
                    "/api/waitlist",
                    json={"email": "notify@example.com", "plan": "pro"},
                )
        assert resp.status_code == 201
        mock_notify.assert_called_once_with(
            submitter_email="notify@example.com",
            plan="pro",
            source="pricing-page",
        )

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_cause_500(self, migrated_pg):
        """If admin notify raises, the endpoint must still return 201 (best-effort)."""
        def _raise(*args, **kwargs):
            raise RuntimeError("SMTP gone")

        with patch(
            "src.web_ui.routes.waitlist.send_waitlist_notify_email",
            side_effect=_raise,
        ):
            async with _make_client(_make_app()) as client:
                resp = await client.post(
                    "/api/waitlist",
                    json={"email": "notifyfail@example.com"},
                )
        assert resp.status_code == 201, (
            f"Admin notify failure must NOT fail the endpoint, got {resp.status_code}"
        )
