# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_forgot_password.py
"""Integration tests for POST /api/auth/forgot-password (W1D-1, WFIX-1).

Business intent (3 required cases + extras):
  T1  Known + verified email → 200, token row created in email_verifications
      with purpose='password_reset'.
  T2  Unknown email → 200, no new token row, response body identical to T1
      (no enumeration).
  T3  Rate limit: 4th call within 60 s from same IP → 429.
  T4  DB failure does NOT leak to response (WFIX-1 H-1/H-2).
  T5  SMTP send failure does NOT leak to response (WFIX-1 H-3).
  T6  Timing: both verified-user and unknown-user branches return fast
      (BackgroundTasks pattern — WFIX-1 MEDIUM).

BackgroundTasks execution note:
  httpx.AsyncClient + ASGITransport executes BackgroundTasks synchronously
  within the same await before returning the response.  No extra synchronisation
  needed — DB state is observable immediately after the client call returns.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Uses httpx.AsyncClient + ASGITransport with base_url="http://127.0.0.1" to
satisfy _LoopbackOnlyMiddleware (mirrors test_signup.py / test_waitlist_api.py
pattern).
"""

import hashlib
import os
import time
from unittest.mock import patch

import httpx
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Module-level env setup (must happen before create_app() is imported).
# ---------------------------------------------------------------------------
os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-forgot-pw-tests-32bytes!!")
os.environ.setdefault("WEBUI_SECURE_COOKIE", "0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    from src.web_ui.app import create_app
    return create_app()


def _make_client(app):
    """Return an httpx.AsyncClient with ASGITransport + loopback base_url."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    )


def _reset_rate_limit() -> None:
    """Clear in-memory rate-limit state so tests are isolated."""
    from src.web_ui import rate_limit as rl
    rl._per_ip_buckets.clear()
    rl._last_prune = 0.0
    rl._lock = None


def _insert_verified_user(pg_conn, username: str, email: str) -> int:
    """Insert a verified user. Returns integer id."""
    from src.web_ui.auth import hash_password
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users"
            " (username, password_hash, email, email_verified, is_admin)"
            " VALUES (%s, %s, %s, TRUE, FALSE)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET email = EXCLUDED.email,"
            "       email_verified = TRUE"
            " RETURNING id",
            (username, hash_password("ValidPassword1!"), email),
        )
        row = cur.fetchone()
    pg_conn.commit()
    return row[0]


def _count_password_reset_tokens(pg_conn, user_id: int) -> int:
    """Return the number of password_reset rows for user_id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM email_verifications"
            " WHERE user_id = %s AND purpose = 'password_reset'",
            (user_id,),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_rate_limit_state():
    """Reset per-IP rate limit buckets before/after every test in this module."""
    _reset_rate_limit()
    yield
    _reset_rate_limit()


@pytest.fixture
def migrated_pg(pg_conn):
    """Run all migrations and clean test rows; yield pg connection."""
    run_migrations(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM email_verifications")
        cur.execute(
            "DELETE FROM webui_users"
            " WHERE username LIKE 'fp_%'"
        )
    pg_conn.commit()
    yield pg_conn
    # Cleanup after test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM email_verifications")
        cur.execute(
            "DELETE FROM webui_users"
            " WHERE username LIKE 'fp_%'"
        )
    pg_conn.commit()


# ---------------------------------------------------------------------------
# T1: Known + verified email → 200 + token row created
# ---------------------------------------------------------------------------


class TestKnownVerifiedEmail:
    """T1: Verified user gets a password_reset token; response is 200.

    BackgroundTasks execute synchronously within httpx.AsyncClient +
    ASGITransport, so DB state is observable immediately after the call.
    """

    @pytest.mark.asyncio
    async def test_forgot_password_known_verified_email_creates_token_and_returns_200(
        self, migrated_pg
    ):
        """Insert verified user, call endpoint, assert token row + 200 response."""
        user_id = _insert_verified_user(
            migrated_pg,
            username="fp_verified",
            email="fp_verified@example.com",
        )

        with patch(
            "src.web_ui.routes.forgot_password.send_password_reset_email"
        ):
            async with _make_client(_make_app()) as client:
                resp = await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "fp_verified@example.com"},
                )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json().get("status") == "ok"

        token_count = _count_password_reset_tokens(migrated_pg, user_id)
        assert token_count == 1, (
            f"Expected 1 password_reset token in email_verifications, got {token_count}"
        )

    @pytest.mark.asyncio
    async def test_token_stored_as_sha256_hash(self, migrated_pg):
        """Token stored in DB must be sha256(raw_token), not the raw token."""
        user_id = _insert_verified_user(
            migrated_pg,
            username="fp_hash",
            email="fp_hash@example.com",
        )

        captured_raw_token: list[str] = []

        def _capture_send(to, username, token, base_url):
            captured_raw_token.append(token)

        with patch(
            "src.web_ui.routes.forgot_password.send_password_reset_email",
            side_effect=_capture_send,
        ):
            async with _make_client(_make_app()) as client:
                await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "fp_hash@example.com"},
                )

        assert len(captured_raw_token) == 1
        raw_token = captured_raw_token[0]
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT token FROM email_verifications"
                " WHERE user_id = %s AND purpose = 'password_reset'",
                (user_id,),
            )
            row = cur.fetchone()

        assert row is not None
        assert row[0] == expected_hash, (
            "DB must store sha256(raw_token), not the raw token"
        )

    @pytest.mark.asyncio
    async def test_email_is_sent_for_verified_user(self, migrated_pg):
        """send_password_reset_email must be called once for a verified user."""
        _insert_verified_user(
            migrated_pg,
            username="fp_send",
            email="fp_send@example.com",
        )

        mock_send = patch(
            "src.web_ui.routes.forgot_password.send_password_reset_email"
        )
        with mock_send as m:
            async with _make_client(_make_app()) as client:
                await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "fp_send@example.com"},
                )

        m.assert_called_once()
        call_kwargs = m.call_args
        assert call_kwargs[1].get("to") == "fp_send@example.com" or (
            len(call_kwargs[0]) > 0 and call_kwargs[0][0] == "fp_send@example.com"
        )


# ---------------------------------------------------------------------------
# T2: Unknown email → 200, no token, response byte-for-byte identical
# ---------------------------------------------------------------------------


class TestUnknownEmail:
    """T2: Unknown email — no token created, response identical (no enumeration)."""

    @pytest.mark.asyncio
    async def test_forgot_password_unknown_email_no_token_still_200_no_enumeration(
        self, migrated_pg
    ):
        """Unknown email returns 200 with the same body as a known email."""
        # Known-email control response
        user_id = _insert_verified_user(
            migrated_pg,
            username="fp_control",
            email="fp_control@example.com",
        )

        with patch("src.web_ui.routes.forgot_password.send_password_reset_email"):
            async with _make_client(_make_app()) as client:
                known_resp = await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "fp_control@example.com"},
                )
                unknown_resp = await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "nobody@example.invalid"},
                )

        assert unknown_resp.status_code == 200, (
            f"Unknown email must return 200, got {unknown_resp.status_code}"
        )
        # Responses must be byte-for-byte identical (no enumeration).
        assert unknown_resp.json() == known_resp.json(), (
            "Response body must be identical for known and unknown email (no enumeration)"
        )

        # No token created for unknown email.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM email_verifications WHERE purpose = 'password_reset'"
                " AND user_id != %s",
                (user_id,),
            )
            stray_count = cur.fetchone()[0]
        assert stray_count == 0, (
            "No password_reset token must be created for an unknown email"
        )

    @pytest.mark.asyncio
    async def test_unverified_user_no_token_still_200(self, migrated_pg):
        """Unverified (email_verified=False) user also returns 200, no token."""
        from src.web_ui.auth import hash_password
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users"
                " (username, password_hash, email, email_verified, is_admin)"
                " VALUES (%s, %s, %s, FALSE, FALSE)"
                " ON CONFLICT (username) DO NOTHING",
                ("fp_unverified", hash_password("ValidPassword1!"), "fp_unverified@example.com"),
            )
        migrated_pg.commit()

        async with _make_client(_make_app()) as client:
            resp = await client.post(
                "/api/auth/forgot-password",
                json={"email": "fp_unverified@example.com"},
            )

        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM email_verifications WHERE purpose = 'password_reset'"
            )
            count = cur.fetchone()[0]
        assert count == 0, "No token must be created for an unverified user"

    @pytest.mark.asyncio
    async def test_invalid_email_format_returns_200(self, migrated_pg):
        """Invalid email format silently returns 200 (no enumeration)."""
        async with _make_client(_make_app()) as client:
            resp = await client.post(
                "/api/auth/forgot-password",
                json={"email": "not-an-email"},
            )
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"


# ---------------------------------------------------------------------------
# T3: Rate limit — 4th call within 60 s → 429
# ---------------------------------------------------------------------------


class TestRateLimit:
    """T3: 4th request in 60 s from the same IP → 429."""

    @pytest.mark.asyncio
    async def test_forgot_password_rate_limit_after_3_attempts(self, migrated_pg):
        """4th request within the window from the same IP must return 429."""
        with patch("src.web_ui.routes.forgot_password.send_password_reset_email"):
            async with _make_client(_make_app()) as client:
                for i in range(3):
                    r = await client.post(
                        "/api/auth/forgot-password",
                        json={"email": f"rate_{i}@example.com"},
                    )
                    assert r.status_code == 200, (
                        f"Attempt {i + 1}/3 should succeed, got {r.status_code}"
                    )
                # 4th attempt — should be rate-limited.
                fourth = await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "rate_4th@example.com"},
                )

        assert fourth.status_code == 429, (
            f"4th attempt must return 429, got {fourth.status_code}: {fourth.text}"
        )
        assert fourth.json().get("error") == "rate_limited"

    @pytest.mark.asyncio
    async def test_rate_limit_mock_path(self, migrated_pg):
        """Patch check_ip_rate_limit to return False, verify 429 directly."""
        from unittest.mock import AsyncMock
        with patch(
            "src.web_ui.routes.forgot_password.check_ip_rate_limit",
            new=AsyncMock(return_value=False),
        ):
            async with _make_client(_make_app()) as client:
                resp = await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "rate_mock@example.com"},
                )

        assert resp.status_code == 429
        data = resp.json()
        assert data.get("error") == "rate_limited"
        assert "retry-after" in resp.headers, (
            "RFC 7231 §7.1.3: 429 must include Retry-After header"
        )
        assert resp.headers["retry-after"] == "60"


# ---------------------------------------------------------------------------
# T4: DB failure does NOT leak to response (WFIX-1 H-1 + H-2)
# ---------------------------------------------------------------------------


class TestInfraFailureIsolation:
    """T4/T5: Infrastructure failures must not reach the HTTP response (WFIX-1)."""

    @pytest.mark.asyncio
    async def test_db_failure_does_not_leak_to_response(self, migrated_pg, caplog):
        """Mock get_pool() to raise; assert response is 200 and error is logged.

        H-1: DB-lookup failure must NOT return 200 silently without logging.
        H-2: Token-INSERT failure must NOT return 200 silently without logging.
        Because both happen inside _process_forgot_password (background task),
        the HTTP response is already 200; but the failure MUST be logged at ERROR
        level with the stable key 'forgot_password.bg.db_failure'.
        """
        import logging as _logging

        with patch(
            "src.web_ui.routes.forgot_password.get_pool",
            side_effect=RuntimeError("simulated DB pool failure"),
        ):
            with caplog.at_level(_logging.ERROR, logger="src.web_ui.routes.forgot_password"):
                async with _make_client(_make_app()) as client:
                    resp = await client.post(
                        "/api/auth/forgot-password",
                        json={"email": "fp_dbfail@example.com"},
                    )

        # Response must still be 200 (infra failure must not enumerate users).
        assert resp.status_code == 200, (
            f"DB failure must not change HTTP status, got {resp.status_code}"
        )
        assert resp.json().get("status") == "ok", (
            "DB failure must not change response body"
        )

        # Error must be logged with the stable structured key.
        db_failure_logged = any(
            "forgot_password.bg.db_failure" in record.message
            for record in caplog.records
            if record.levelno >= _logging.ERROR
        )
        assert db_failure_logged, (
            "forgot_password.bg.db_failure must be logged at ERROR level on DB failure"
        )

    @pytest.mark.asyncio
    async def test_db_failure_increments_counter(self, migrated_pg):
        """DB failure in background task increments the Prometheus counter."""
        from src.metrics import forgot_password_db_failure_total

        before = forgot_password_db_failure_total._value.get()

        with patch(
            "src.web_ui.routes.forgot_password.get_pool",
            side_effect=RuntimeError("counter test DB failure"),
        ):
            async with _make_client(_make_app()) as client:
                await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "fp_counter@example.com"},
                )

        after = forgot_password_db_failure_total._value.get()
        assert after == before + 1, (
            f"forgot_password_db_failure_total should have incremented by 1 "
            f"(was {before}, now {after})"
        )

    @pytest.mark.asyncio
    async def test_email_send_failure_does_not_leak_to_response(
        self, migrated_pg, caplog
    ):
        """Mock send_password_reset_email to raise; assert 200 + error logged.

        H-3: SMTP send failure must NOT propagate to HTTP response.
        The stable log key is 'forgot_password.bg.email_send_failure'.
        """
        import logging as _logging

        _insert_verified_user(
            migrated_pg,
            username="fp_smtpfail",
            email="fp_smtpfail@example.com",
        )

        with patch(
            "src.web_ui.routes.forgot_password.send_password_reset_email",
            side_effect=RuntimeError("simulated SMTP failure"),
        ):
            with caplog.at_level(_logging.ERROR, logger="src.web_ui.routes.forgot_password"):
                async with _make_client(_make_app()) as client:
                    resp = await client.post(
                        "/api/auth/forgot-password",
                        json={"email": "fp_smtpfail@example.com"},
                    )

        # Response must still be 200.
        assert resp.status_code == 200, (
            f"SMTP failure must not change HTTP status, got {resp.status_code}"
        )
        assert resp.json().get("status") == "ok", (
            "SMTP failure must not change response body"
        )

        # Error must be logged with the stable structured key.
        smtp_failure_logged = any(
            "forgot_password.bg.email_send_failure" in record.message
            for record in caplog.records
            if record.levelno >= _logging.ERROR
        )
        assert smtp_failure_logged, (
            "forgot_password.bg.email_send_failure must be logged at ERROR level on SMTP failure"
        )

    @pytest.mark.asyncio
    async def test_email_send_failure_increments_counter(self, migrated_pg):
        """SMTP failure in background task increments the Prometheus counter."""
        from src.metrics import forgot_password_email_send_failure_total

        _insert_verified_user(
            migrated_pg,
            username="fp_smtpcnt",
            email="fp_smtpcnt@example.com",
        )

        before = forgot_password_email_send_failure_total._value.get()

        with patch(
            "src.web_ui.routes.forgot_password.send_password_reset_email",
            side_effect=RuntimeError("counter SMTP failure"),
        ):
            async with _make_client(_make_app()) as client:
                await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "fp_smtpcnt@example.com"},
                )

        after = forgot_password_email_send_failure_total._value.get()
        assert after == before + 1, (
            f"forgot_password_email_send_failure_total should have incremented by 1 "
            f"(was {before}, now {after})"
        )


# ---------------------------------------------------------------------------
# T6: Timing channel — both branches return fast (WFIX-1 MEDIUM)
# ---------------------------------------------------------------------------


class TestNoEnumerationTiming:
    """T6: BackgroundTasks closes the timing side-channel (WFIX-1 MEDIUM).

    With BackgroundTasks, the foreground route returns immediately after
    enqueueing the task.  The actual DB+SMTP work happens in the background.
    Both the verified-user branch and the unknown-user branch enqueue the
    same _process_forgot_password function — the response timing difference
    (if any) comes only from the tiny overhead of enqueueing vs. not enqueueing.

    We verify this by checking that the background task is scheduled for a
    valid-looking email address on both branches, and that the raw response
    time for an unknown-email request is not unexpectedly large.

    Note: wall-clock assertions are inherently noisy; we use a generous 500ms
    ceiling to avoid flakiness in CI, while still catching a regression where
    the no-enumeration path accidentally does synchronous DB + SMTP work.
    """

    @pytest.mark.asyncio
    async def test_no_enumeration_timing_both_branches_fast(self, migrated_pg):
        """Verified-user and unknown-user branches must both respond quickly.

        The test checks that:
          1. Both branches return 200 with the same body.
          2. The unknown-email branch responds within 500ms (i.e. no synchronous
             DB/SMTP work on the hot path).
        """
        _insert_verified_user(
            migrated_pg,
            username="fp_timing",
            email="fp_timing@example.com",
        )

        with patch("src.web_ui.routes.forgot_password.send_password_reset_email"):
            async with _make_client(_make_app()) as client:
                # Warm-up: verified user (background task runs here too)
                known_resp = await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "fp_timing@example.com"},
                )

                # Measure unknown-user branch (no DB hit expected on foreground path)
                t0 = time.monotonic()
                unknown_resp = await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "timing_unknown@example.invalid"},
                )
                elapsed_ms = (time.monotonic() - t0) * 1000

        assert known_resp.status_code == 200
        assert unknown_resp.status_code == 200
        assert known_resp.json() == unknown_resp.json(), (
            "Response body must be identical for both branches (no enumeration)"
        )
        # 500ms ceiling: should be well under 50ms in practice; 500ms catches
        # regressions where synchronous DB/SMTP work is accidentally added.
        assert elapsed_ms < 500, (
            f"Unknown-email branch took {elapsed_ms:.1f}ms — suspiciously slow, "
            "suggests synchronous work on foreground path"
        )

    @pytest.mark.asyncio
    async def test_background_task_scheduled_for_valid_email(self, migrated_pg):
        """_process_forgot_password must be scheduled as a BackgroundTask for
        any syntactically-valid email, regardless of whether it exists in the DB.

        We verify by patching _process_forgot_password and checking it was called
        with the correct email — which confirms BackgroundTasks.add_task() fired.
        """
        called_with: list[tuple] = []

        def _mock_process(email, base_url, client_ip):
            called_with.append((email, base_url, client_ip))

        with patch(
            "src.web_ui.routes.forgot_password._process_forgot_password",
            side_effect=_mock_process,
        ):
            async with _make_client(_make_app()) as client:
                # Known-user branch
                await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "fp_bg_known@example.com"},
                )
                # Unknown-user branch
                await client.post(
                    "/api/auth/forgot-password",
                    json={"email": "fp_bg_unknown@example.invalid"},
                )

        # Both valid-looking emails must schedule the background task.
        assert len(called_with) == 2, (
            f"Expected 2 background task invocations, got {len(called_with)}"
        )
        emails_called = {args[0] for args in called_with}
        assert "fp_bg_known@example.com" in emails_called
        assert "fp_bg_unknown@example.invalid" in emails_called
