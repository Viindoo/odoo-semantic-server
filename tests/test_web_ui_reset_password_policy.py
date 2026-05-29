# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_reset_password_policy.py
"""Integration tests: POST /api/auth/reset-password enforces password policy.

Business rules tested (each test name states the rule):

  R1  reset-password with a password shorter than auth.password_min_length
      must be rejected (HTTP 400) — the token must NOT be consumed.

  R2  reset-password with a password in the common-password blocklist must
      be rejected (HTTP 400) — the token must NOT be consumed.

  R3  reset-password with a password that meets the minimum length and is
      NOT in the blocklist must succeed (HTTP 200), consuming the token.

  R4  Policy check happens BEFORE token consumption: a weak-password
      submission must leave the token still valid (user can retry with a
      stronger password using the same link).

  R5  Password minimum length is live-configurable: raising
      auth.password_min_length via the settings overlay is respected
      immediately (no restart needed).

  R6  Missing / invalid token returns the appropriate error code (410/404)
      — policy check does not short-circuit these error paths.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Uses httpx.AsyncClient + ASGITransport + base_url="http://127.0.0.1" to
satisfy _LoopbackOnlyMiddleware (mirrors test_web_ui_forgot_password.py
pattern).
"""

import os

import httpx
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Module-level env setup (before create_app() is imported).
# ---------------------------------------------------------------------------
os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-reset-pw-policy-32bytes!!")
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


def _insert_verified_user(pg_conn, username: str) -> int:
    """Insert a verified webui user. Returns integer id."""
    from src.web_ui.auth import hash_password
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users"
            " (username, password_hash, email, email_verified, is_admin)"
            " VALUES (%s, %s, %s, TRUE, FALSE)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET email = EXCLUDED.email, email_verified = TRUE"
            " RETURNING id",
            (
                username,
                hash_password("ValidPassword1!"),
                f"{username}@example.com",
            ),
        )
        row = cur.fetchone()
    pg_conn.commit()
    return row[0]


def _create_reset_token(user_id: int) -> str:
    """Create a valid password_reset token for user_id. Returns raw token."""
    from src.db.pg import auth_store
    return auth_store().create_password_reset_token(user_id, ttl_seconds=3600)


def _token_is_consumed(pg_conn, raw_token: str) -> bool:
    """Return True if the token has been marked used_at IS NOT NULL."""
    import hashlib
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT used_at FROM email_verifications"
            " WHERE token_hash = %s AND purpose = 'password_reset'",
            (token_hash,),
        )
        row = cur.fetchone()
    return row is not None and row[0] is not None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cleanup_test_rows(conn):
    """Remove test rows inserted by this module's tests.

    Best-effort: silently ignores UndefinedTable if a prior ``clean_pg``-using
    test in the same session dropped the tables after us.  This mirrors the
    pattern used by ``test_web_ui_forgot_password.py``.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM email_verifications WHERE purpose = 'password_reset'"
            )
            cur.execute("DELETE FROM webui_users WHERE username LIKE 'rp_%'")
        conn.commit()
    except Exception:
        # Table already gone (e.g. clean_pg from another test dropped it).
        # Roll back the aborted transaction so the connection stays usable.
        conn.rollback()


@pytest.fixture
def migrated_pg(pg_conn):
    """Ensure all migrations are applied and test rows are clean.

    Uses the session-scoped ``pg_conn`` (not ``clean_pg``) so that
    ``run_migrations`` is idempotent across tests.  The same pattern is used
    by ``test_web_ui_forgot_password.py``.
    """
    run_migrations(pg_conn)
    _cleanup_test_rows(pg_conn)
    yield pg_conn
    _cleanup_test_rows(pg_conn)


# ---------------------------------------------------------------------------
# R1: Password shorter than min_length is rejected; token NOT consumed
# ---------------------------------------------------------------------------


class TestBelowMinLengthRejected:
    """R1: A password shorter than auth.password_min_length must be rejected.

    The business rule: any password shorter than the configured floor (default
    12) must never be accepted on the reset-password path, matching the
    registration path policy.
    """

    @pytest.mark.asyncio
    async def test_reset_password_below_min_length_rejected(self, migrated_pg):
        """Password of length 8 (< default 12) must return HTTP 400."""
        user_id = _insert_verified_user(migrated_pg, "rp_short1")
        token = _create_reset_token(user_id)
        app = _make_app()

        async with _make_client(app) as client:
            resp = await client.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "Short1!x"},  # len=8
            )

        assert resp.status_code == 400, (
            f"Password below min_length must return 400, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "error" in body, "Response must include 'error' key"
        assert "12" in body["error"] or "characters" in body["error"].lower(), (
            f"Error message should mention length requirement, got: {body['error']!r}"
        )

    @pytest.mark.asyncio
    async def test_reset_password_below_min_length_does_not_consume_token(
        self, migrated_pg
    ):
        """R4: Token must NOT be consumed when password is rejected (user can retry)."""
        user_id = _insert_verified_user(migrated_pg, "rp_short2")
        token = _create_reset_token(user_id)
        app = _make_app()

        async with _make_client(app) as client:
            await client.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "Short1!x"},  # len=8
            )

        assert not _token_is_consumed(migrated_pg, token), (
            "Token must remain unused after a rejected (too-short) password submission"
        )

    @pytest.mark.asyncio
    async def test_reset_password_empty_password_rejected(self, migrated_pg):
        """Empty password must also be rejected (length 0 < 12)."""
        user_id = _insert_verified_user(migrated_pg, "rp_empty")
        token = _create_reset_token(user_id)
        app = _make_app()

        async with _make_client(app) as client:
            resp = await client.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": ""},
            )

        assert resp.status_code in (400, 422), (
            f"Empty password must be rejected, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# R2: Common-password blocklist is enforced on reset path
# ---------------------------------------------------------------------------


class TestCommonPasswordBlocklistEnforced:
    """R2: Passwords in the common-password blocklist must be rejected.

    The blocklist is the same one used by the registration endpoint.  A user
    must not be able to reset their password to a well-known weak password.
    """

    @pytest.mark.asyncio
    async def test_reset_password_common_password_rejected(self, migrated_pg):
        """'administrator' is 13 chars (>= min_length) but in the blocklist — HTTP 400.

        Using 'administrator' because it is the only blocklisted entry that is
        also >= 12 chars (the default min_length), so it exercises the blocklist
        check independently of the length check.
        """
        user_id = _insert_verified_user(migrated_pg, "rp_common1")
        token = _create_reset_token(user_id)
        app = _make_app()

        async with _make_client(app) as client:
            resp = await client.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "administrator"},  # len=13, in blocklist
            )

        assert resp.status_code == 400, (
            f"Common password must return 400, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "error" in body
        assert "common" in body["error"].lower() or "strong" in body["error"].lower(), (
            f"Error message should mention common/weak password, got: {body['error']!r}"
        )

    @pytest.mark.asyncio
    async def test_reset_password_common_password_does_not_consume_token(
        self, migrated_pg
    ):
        """R4: Token must NOT be consumed when a blocklisted password is submitted."""
        user_id = _insert_verified_user(migrated_pg, "rp_common2")
        token = _create_reset_token(user_id)
        app = _make_app()

        async with _make_client(app) as client:
            await client.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "administrator"},  # len=13, in blocklist
            )

        assert not _token_is_consumed(migrated_pg, token), (
            "Token must remain unused after a blocked (common-password) submission"
        )


# ---------------------------------------------------------------------------
# R3: Valid password succeeds and consumes the token
# ---------------------------------------------------------------------------


class TestValidPasswordAccepted:
    """R3: A password that meets minimum length and is not in the blocklist
    must be accepted (HTTP 200) and the token must be consumed (single-use).
    """

    @pytest.mark.asyncio
    async def test_reset_password_meets_min_length_accepted(self, migrated_pg):
        """Password with 12+ chars and not in blocklist must return HTTP 200."""
        user_id = _insert_verified_user(migrated_pg, "rp_valid1")
        token = _create_reset_token(user_id)
        app = _make_app()

        async with _make_client(app) as client:
            resp = await client.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "ValidStr0ngPass!"},
            )

        assert resp.status_code == 200, (
            f"Valid password must return 200, got {resp.status_code}: {resp.text}"
        )
        assert resp.json().get("ok") is True

    @pytest.mark.asyncio
    async def test_reset_password_valid_consumes_token(self, migrated_pg):
        """Token must be marked as consumed after a successful reset."""
        user_id = _insert_verified_user(migrated_pg, "rp_valid2")
        token = _create_reset_token(user_id)
        app = _make_app()

        async with _make_client(app) as client:
            await client.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "ValidStr0ngPass!"},
            )

        assert _token_is_consumed(migrated_pg, token), (
            "Token must be marked as used after a successful password reset"
        )

    @pytest.mark.asyncio
    async def test_reset_password_exactly_min_length_accepted(self, migrated_pg):
        """Password with exactly 12 characters (the default floor) must be accepted."""
        user_id = _insert_verified_user(migrated_pg, "rp_exact12")
        token = _create_reset_token(user_id)
        app = _make_app()

        # Exactly 12 chars, not in blocklist
        password_12 = "aB3!xY9kLmNp"
        assert len(password_12) == 12

        async with _make_client(app) as client:
            resp = await client.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": password_12},
            )

        assert resp.status_code == 200, (
            f"Password of exactly min_length must be accepted, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# R5: Live-configurable minimum length is respected
# ---------------------------------------------------------------------------


class TestLiveMinLengthConfiguration:
    """R5: auth.password_min_length is read via get_setting() on every request
    so an admin override takes effect without a server restart.

    Tested by patching ``src.settings.get_setting`` (the canonical module object
    that validate_password imports lazily) to return a raised floor of 16, then
    verifying that a 12-char password is rejected even though 12 is the
    code-default.
    """

    @pytest.mark.asyncio
    async def test_reset_password_respects_raised_min_length_setting(
        self, migrated_pg
    ):
        """Patching get_setting to return 16 must cause a 12-char password to be
        rejected with an error message citing the live floor.
        """
        from unittest.mock import patch

        user_id = _insert_verified_user(migrated_pg, "rp_livecfg")
        token = _create_reset_token(user_id)
        app = _make_app()

        # validate_password does ``from src.settings import get_setting`` lazily
        # inside the function body, so we must patch the canonical module object.
        with patch("src.settings.get_setting", return_value=16):
            # 12 chars — meets the default but not the patched floor of 16
            async with _make_client(app) as client:
                resp = await client.post(
                    "/api/auth/reset-password",
                    json={"token": token, "new_password": "aB3!xY9kLmNp"},  # len=12
                )

        assert resp.status_code == 400, (
            f"12-char password must be rejected when min_length=16, got {resp.status_code}"
        )
        body = resp.json()
        assert "16" in body.get("error", ""), (
            f"Error message must mention the live floor (16), got: {body.get('error')!r}"
        )


# ---------------------------------------------------------------------------
# R6: Token errors are not short-circuited by policy check
# ---------------------------------------------------------------------------


class TestTokenErrorsStillReturned:
    """R6: When the password is valid but the token is invalid/expired/used,
    the correct error code must still be returned.
    """

    @pytest.mark.asyncio
    async def test_reset_password_invalid_token_returns_404(self, migrated_pg):
        """A valid password paired with a non-existent token must return 404."""
        app = _make_app()

        async with _make_client(app) as client:
            resp = await client.post(
                "/api/auth/reset-password",
                json={
                    "token": "completely-invalid-token-that-does-not-exist",
                    "new_password": "ValidStr0ngPass!",
                },
            )

        assert resp.status_code == 404, (
            f"Invalid token must return 404, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_reset_password_used_token_returns_410(self, migrated_pg):
        """A valid password paired with an already-consumed token must return 410."""
        user_id = _insert_verified_user(migrated_pg, "rp_used")
        token = _create_reset_token(user_id)
        app = _make_app()

        # First use — should succeed
        async with _make_client(app) as client:
            first = await client.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "ValidStr0ngPass!"},
            )
        assert first.status_code == 200

        # Second use with a different valid password — token already consumed
        async with _make_client(app) as client:
            second = await client.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "AnotherV@lid1Pass"},
            )

        assert second.status_code == 410, (
            f"Already-used token must return 410, got {second.status_code}: {second.text}"
        )
