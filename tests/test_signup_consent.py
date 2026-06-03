# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_signup_consent.py
"""Tests for W6 D4 consent gate: signup consent requirement + terms_accepted_at recording.

Business rules being protected:
  1. POST /api/auth/register WITHOUT consent=True → 422 consent_required.
  2. POST /api/auth/register WITH consent=True → 201 AND terms_accepted_at is
     set (non-NULL) in webui_users after email verification.
  3. Existing signup tests that now require consent still pass after updating
     them to include consent=True (consent is a real requirement, not a test
     convenience — verified by the 422 case above).
  4. OAuth _create_oauth_user records terms_accepted_at = NOW() in the INSERT
     (verified by mock inspection of the SQL statement).
"""

import os

import pytest

pytestmark = pytest.mark.postgres

os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-for-consent-tests-32bytes!")


# ---------------------------------------------------------------------------
# Helpers (mirrors test_signup.py helpers to stay self-contained)
# ---------------------------------------------------------------------------


def _make_app():
    from src.web_ui.app import create_app
    return create_app()


def _run_migrations(pg_conn):
    from src.db.migrate import run_migrations
    run_migrations(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM email_verifications")
        cur.execute(
            "DELETE FROM webui_users"
            " WHERE username LIKE 'cs_%' OR username LIKE 'consent_%'"
        )
    pg_conn.commit()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def consent_pg(pg_conn):
    """Prepare schema + clean test rows for each consent test."""
    _run_migrations(pg_conn)
    yield pg_conn
    # Teardown: clean test rows.  Guard with try/except in case a test ran before
    # migrations applied (e.g. a 422 returned before any DB work was done).
    try:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM email_verifications")
            cur.execute(
                "DELETE FROM webui_users"
                " WHERE username LIKE 'cs_%' OR username LIKE 'consent_%'"
            )
        pg_conn.commit()
    except Exception:
        pg_conn.rollback()


@pytest.fixture(autouse=True)
def _enable_signup(monkeypatch):
    """Enable public signup for all tests in this module.

    The register route gates on ``signup_enabled()`` (src/web_ui/config.py), which
    consults the DB overlay FIRST and only falls back to the ``SIGNUP_ENABLED``
    constant when no row exists. ``create_app()`` → ``bootstrap_settings_safe()``
    seeds a system-scope ``signup.enabled=False`` row, so the DB overlay wins and
    patching the constant is dead code here. Patch the function as it is looked up
    in the route module (``signup.py`` does ``from ...config import signup_enabled``,
    binding the name into its own namespace), which deterministically enables
    signup regardless of DB state — isolating these tests to the consent contract.
    """
    monkeypatch.setattr("src.web_ui.routes.signup.signup_enabled", lambda: True)


# ---------------------------------------------------------------------------
# Rule 1 — register WITHOUT consent → 422
# ---------------------------------------------------------------------------


class TestConsentRequired:
    @pytest.mark.asyncio
    async def test_register_without_consent_returns_422(self, consent_pg):
        """Omitting consent (default False) must return 422 consent_required."""
        import httpx
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/register", json={
                "email": "cs_noconsent@example.com",
                "username": "cs_noconsent",
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "hcaptcha_token": "",
                # consent intentionally omitted (defaults to False)
            })

        assert resp.status_code == 422, resp.text
        data = resp.json()
        assert data.get("error") == "consent_required", data

    @pytest.mark.asyncio
    async def test_register_with_consent_false_returns_422(self, consent_pg):
        """Explicitly sending consent=False must return 422."""
        import httpx
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/register", json={
                "email": "cs_falseconsent@example.com",
                "username": "cs_falseconsent",
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "hcaptcha_token": "",
                "consent": False,
            })

        assert resp.status_code == 422, resp.text
        assert resp.json().get("error") == "consent_required"

    @pytest.mark.asyncio
    async def test_no_user_row_created_when_consent_missing(self, consent_pg):
        """No webui_users row must be inserted when consent is rejected."""
        import httpx

        from src.db.pg import get_pool
        pool = get_pool()

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            await client.post("/api/auth/register", json={
                "email": "cs_norow@example.com",
                "username": "cs_norow",
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "hcaptcha_token": "",
            })

        with pool.checkout() as conn:
            row = pool.fetch_one(
                conn,
                "SELECT 1 FROM webui_users WHERE username = 'cs_norow'",
                (),
            )
        assert row is None, "No user row should be created when consent is missing"


# ---------------------------------------------------------------------------
# Rule 2 — register WITH consent → 201 + terms_accepted_at set after verify
# ---------------------------------------------------------------------------


class TestConsentRecorded:
    @pytest.mark.asyncio
    async def test_register_with_consent_returns_201(self, consent_pg):
        """Happy path: consent=True → 201."""
        import httpx
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/register", json={
                "email": "cs_ok@example.com",
                "username": "cs_ok",
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "hcaptcha_token": "",
                "consent": True,
            })

        assert resp.status_code == 201, resp.text
        assert resp.json().get("status") == "verification_email_sent"

    @pytest.mark.asyncio
    async def test_terms_accepted_at_set_after_register(self, consent_pg):
        """After registration with consent=True, terms_accepted_at must be non-NULL
        in webui_users immediately (the column is set at INSERT, before email verify).
        """
        import httpx

        from src.db.pg import get_pool
        pool = get_pool()

        app = _make_app()
        username = "cs_terms_check"
        email = "cs_terms_check@example.com"

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
                "consent": True,
            })

        assert resp.status_code == 201, resp.text

        with pool.checkout() as conn:
            row = pool.fetch_one(
                conn,
                "SELECT id, terms_accepted_at FROM webui_users WHERE username = %s",
                (username,),
            )

        assert row is not None, "User row must exist after registration"
        assert row["terms_accepted_at"] is not None, (
            "terms_accepted_at must be set (non-NULL) when consent=True is given at signup"
        )


# ---------------------------------------------------------------------------
# Rule 4 — OAuth _create_oauth_user includes terms_accepted_at in INSERT
# ---------------------------------------------------------------------------


class TestOAuthConsentRecorded:
    """OAuth new-user path records terms_accepted_at = NOW() in the INSERT SQL."""

    def test_create_oauth_user_records_terms_accepted_at(self, consent_pg):
        """OAuth new-user creation records consent: the persisted row has a
        non-NULL terms_accepted_at.

        Asserts the observable DB outcome (consent timestamp is stored) rather
        than scanning the INSERT SQL string for the literals "terms_accepted_at"
        / "NOW()". A SQL-text scan passes even if the column ends up NULL (e.g.
        the value is bound but never committed, or a later migration changes the
        write path); reading the stored value catches the real regression —
        consent not actually recorded.
        """
        import src.web_ui.routes.oauth as oauth_mod
        from src.db.pg import get_pool

        email = "consent_oauth_check@example.com"
        result = oauth_mod._create_oauth_user(
            provider="google",
            oauth_id="uid_consent_999",
            email=email,
            email_verified=True,
            name="Test OAuth Consent",
        )

        pool = get_pool()
        try:
            with pool.checkout() as conn:
                row = pool.fetch_one(
                    conn,
                    "SELECT terms_accepted_at FROM webui_users WHERE id = %s",
                    (result["id"],),
                )

            assert row is not None, "OAuth user row must exist after creation"
            assert row["terms_accepted_at"] is not None, (
                "OAuth _create_oauth_user must record consent (terms_accepted_at "
                "non-NULL) for a newly-created account"
            )
        finally:
            with pool.checkout() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM webui_users WHERE id = %s", (result["id"],))
                conn.commit()
