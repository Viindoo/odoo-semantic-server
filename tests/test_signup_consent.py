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
    """Enable public signup for all tests in this module."""
    monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", True)
    monkeypatch.setattr("src.web_ui.routes.signup.SIGNUP_ENABLED", True)


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

    def test_create_oauth_user_sql_includes_terms_accepted_at(self, monkeypatch):
        """Inspect the SQL executed by _create_oauth_user to verify it includes
        terms_accepted_at = NOW().  Uses a mock cursor to capture the exact SQL
        without a DB connection.

        This is a contract test: if the SQL changes and the column is dropped,
        this test catches it immediately.
        """
        captured_sql: list[str] = []
        captured_params: list[tuple] = []

        class _MockCursor:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def execute(self, sql, params=()):
                captured_sql.append(sql)
                captured_params.append(params)
            def fetchone(self):
                # Return a fake row matching the RETURNING columns
                return (99, "user_abcd1234", "oauth@example.com", True, False, True)

        class _MockConn:
            autocommit = False
            def cursor(self, **_): return _MockCursor()
            def commit(self): pass
            def rollback(self): pass

        class _MockPool:
            def checkout(self):
                from contextlib import contextmanager
                @contextmanager
                def _ctx():
                    yield _MockConn()
                return _ctx()

        import src.web_ui.routes.oauth as oauth_mod

        def _fake_auth_store():
            class _S:
                _pool = _MockPool()
            return _S()

        monkeypatch.setattr("src.web_ui.routes.oauth._lookup_user_by_oauth", lambda *a: None)
        # Patch auth_store inside the function
        import src.db.pg as pg_mod
        original_auth_store = pg_mod.auth_store
        monkeypatch.setattr(pg_mod, "auth_store", _fake_auth_store)

        try:
            result = oauth_mod._create_oauth_user(
                provider="google",
                oauth_id="uid_999",
                email="oauth@example.com",
                email_verified=True,
                name="Test OAuth",
            )
        finally:
            monkeypatch.setattr(pg_mod, "auth_store", original_auth_store)

        assert result["id"] == 99
        assert result["username"] == "user_abcd1234"

        # The INSERT SQL must include terms_accepted_at
        insert_sqls = [s for s in captured_sql if "INSERT INTO webui_users" in s]
        assert insert_sqls, "No INSERT INTO webui_users SQL was executed"
        insert_sql = insert_sqls[0]
        assert "terms_accepted_at" in insert_sql, (
            "OAuth _create_oauth_user INSERT must include terms_accepted_at.\n"
            f"Actual SQL: {insert_sql}"
        )
        assert "NOW()" in insert_sql, (
            "OAuth _create_oauth_user INSERT must set terms_accepted_at = NOW().\n"
            f"Actual SQL: {insert_sql}"
        )
