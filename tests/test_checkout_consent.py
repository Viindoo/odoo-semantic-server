# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_checkout_consent.py
"""Tests for POST /api/account/checkout-consent (CRD withdrawal waiver, m13_017).

Business intent (CRD compliance):
  CC1  Unauthenticated → 401 (current_user_id returns None → route raises 401).
  CC2  Consumer + waiver_accepted=True → 200; DB records buyer_type='consumer'
       and a non-NULL withdrawal_waiver_accepted_at.
  CC3  Business + waiver_accepted absent/False → 200; DB records buyer_type='business',
       withdrawal_waiver_accepted_at is NULL.
  CC4  Consumer + waiver_accepted=False → 400 (CRD Art.22 requires active tick).
  CC5  Missing / invalid buyer_type → 400.
  CC6  Business + waiver_accepted=True → 200 but response + DB store NULL waiver timestamp
       (server-side correction; waiver does not apply to traders).
  CC7  GET /api/account/checkout-config returns paid_checkout_enabled + checkout_url_map
       + user_email for authenticated users; 401 for unauthenticated.

Migration test:
  MM1  m13_017 migration adds buyer_type + withdrawal_waiver_accepted_at columns
       (idempotent: running twice does not error).

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Auth pattern: monkeypatch current_user_id + httpx.AsyncClient, matching the
pattern used in test_account_subscription.py.
"""
import httpx
import pytest

pytestmark = pytest.mark.postgres


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def web_app(pg_conn):
    """Web UI app on the test DB with migrations + a seeded user id=1."""
    from src.db.migrate import run_migrations
    from src.web_ui.app import create_app

    run_migrations(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM subscriptions")
        cur.execute("DELETE FROM admin_audit_log")
        cur.execute(
            "DELETE FROM webui_users WHERE id = 1 OR username = '_consent_uid1'"
        )
        cur.execute(
            "INSERT INTO webui_users"
            " (username, password_hash, email, is_admin, is_active, id)"
            " VALUES (%s, %s, %s, TRUE, TRUE, 1)",
            ("_consent_uid1", "x", "consent-test@example.com"),
        )
    pg_conn.commit()

    app = create_app()
    yield app

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM subscriptions")
        cur.execute("DELETE FROM admin_audit_log")
        cur.execute(
            "DELETE FROM webui_users WHERE id = 1 OR username = '_consent_uid1'"
        )
    pg_conn.commit()


# ---------------------------------------------------------------------------
# CC1 — unauthenticated → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_consent_unauthenticated(web_app, monkeypatch):
    """CC1: current_user_id returns None (no valid session) → route raises 401.

    Pattern from test_account_subscription.py::TestSubscriptionAuthGate.
    """
    monkeypatch.setattr("src.web_ui.auth.is_test_bypass_active", lambda: False)
    monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: None)

    async with _client(web_app) as client:
        resp = await client.post(
            "/api/account/checkout-consent",
            json={"buyer_type": "consumer", "waiver_accepted": True},
        )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# CC2 — consumer + waiver → 200, DB records consent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_consent_consumer_waiver(web_app, pg_conn, monkeypatch):
    """CC2: Consumer ticks waiver → 200; withdrawal_waiver_accepted_at NOT NULL."""
    monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: 1)

    async with _client(web_app) as client:
        resp = await client.post(
            "/api/account/checkout-consent",
            json={"buyer_type": "consumer", "waiver_accepted": True, "plan_slug": "pro"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["buyer_type"] == "consumer"
    assert data["waiver_accepted"] is True
    assert data["status"] == "consent_recorded"

    # Verify DB row
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT buyer_type, withdrawal_waiver_accepted_at"
            "  FROM subscriptions"
            " WHERE buyer_type = 'consumer'"
            " ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
    assert row is not None, "No consumer subscription row inserted"
    assert row[0] == "consumer"
    assert row[1] is not None, "withdrawal_waiver_accepted_at should be non-NULL for consumer"


# ---------------------------------------------------------------------------
# CC3 — business + no waiver → 200; waiver_ts NULL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_consent_business_no_waiver(web_app, pg_conn, monkeypatch):
    """CC3: Business buyer → 200; withdrawal_waiver_accepted_at IS NULL."""
    monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: 1)

    async with _client(web_app) as client:
        resp = await client.post(
            "/api/account/checkout-consent",
            json={"buyer_type": "business", "plan_slug": "team"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["buyer_type"] == "business"
    assert data["waiver_accepted"] is False

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT buyer_type, withdrawal_waiver_accepted_at"
            "  FROM subscriptions"
            " WHERE buyer_type = 'business'"
            " ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
    assert row is not None, "No business subscription row inserted"
    assert row[0] == "business"
    assert row[1] is None, "withdrawal_waiver_accepted_at MUST be NULL for business"


# ---------------------------------------------------------------------------
# CC4 — consumer without waiver → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_consent_consumer_no_waiver_rejected(web_app, monkeypatch):
    """CC4: Consumer does not tick waiver → 400 (CRD Art.22 compliance)."""
    monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: 1)

    async with _client(web_app) as client:
        resp = await client.post(
            "/api/account/checkout-consent",
            json={"buyer_type": "consumer", "waiver_accepted": False},
        )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    detail = body.get("detail", "")
    # Detail should mention the waiver / withdrawal
    assert "withdrawal" in detail.lower() or "waiver" in detail.lower(), \
        f"Expected mention of withdrawal/waiver in detail, got: {detail!r}"


# ---------------------------------------------------------------------------
# CC5 — invalid buyer_type → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_consent_invalid_buyer_type(web_app, monkeypatch):
    """CC5: Invalid buyer_type → 400."""
    monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: 1)

    async with _client(web_app) as client:
        resp = await client.post(
            "/api/account/checkout-consent",
            json={"buyer_type": "alien", "waiver_accepted": True},
        )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# CC5b — missing buyer_type → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_consent_missing_buyer_type(web_app, monkeypatch):
    """CC5b: Missing buyer_type → 400 (None is not in valid set)."""
    monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: 1)

    async with _client(web_app) as client:
        resp = await client.post(
            "/api/account/checkout-consent",
            json={"waiver_accepted": True},
        )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# CC6 — business + waiver_accepted=True → server corrects to NULL in DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_consent_business_waiver_corrected(web_app, pg_conn, monkeypatch):
    """CC6: Business sends waiver_accepted=True → server ignores it; DB waiver is NULL."""
    monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: 1)

    async with _client(web_app) as client:
        resp = await client.post(
            "/api/account/checkout-consent",
            json={"buyer_type": "business", "waiver_accepted": True, "plan_slug": "pro"},
        )
    # Should succeed (server corrects the waiver)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["waiver_accepted"] is False  # server corrects this in the response

    # DB must have NULL waiver timestamp
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT withdrawal_waiver_accepted_at FROM subscriptions"
            " WHERE buyer_type = 'business' ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is None, "Business buyer's withdrawal_waiver_accepted_at must be NULL"


# ---------------------------------------------------------------------------
# CC7 — GET /api/account/checkout-config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_config_authenticated(web_app, monkeypatch):
    """CC7a: Authenticated → 200 with required keys."""
    monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: 1)

    async with _client(web_app) as client:
        resp = await client.get("/api/account/checkout-config")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "paid_checkout_enabled" in data
    assert "checkout_url_map" in data
    assert "user_email" in data
    assert isinstance(data["checkout_url_map"], dict)


@pytest.mark.asyncio
async def test_checkout_config_unauthenticated(web_app, monkeypatch):
    """CC7b: current_user_id returns None → route raises 401."""
    monkeypatch.setattr("src.web_ui.auth.is_test_bypass_active", lambda: False)
    monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: None)

    async with _client(web_app) as client:
        resp = await client.get("/api/account/checkout-config")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# MM1 — m13_017 migration idempotency
# ---------------------------------------------------------------------------


def test_m13_017_migration_idempotent(pg_conn):
    """MM1: Running m13_017 twice does not raise an error."""
    from src.db.migrate import run_migrations

    # Run once (already done by web_app fixture but isolated here)
    run_migrations(pg_conn)

    # Verify columns exist after first run
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'subscriptions'"
            "   AND column_name IN ('buyer_type', 'withdrawal_waiver_accepted_at')"
            " ORDER BY column_name"
        )
        cols = [row[0] for row in cur.fetchall()]
    assert "buyer_type" in cols, "buyer_type column must exist after m13_017"
    assert "withdrawal_waiver_accepted_at" in cols, \
        "withdrawal_waiver_accepted_at column must exist after m13_017"

    # Run again — must not raise
    run_migrations(pg_conn)


# ---------------------------------------------------------------------------
# CC8 — double-submit: two rapid POSTs must create only ONE pending row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_consent_double_submit_creates_one_row(
    web_app, pg_conn, monkeypatch
):
    """CC8: Submitting the consent form twice in quick succession (double-click / retry)
    must result in exactly ONE pending subscription row, not two.

    Business rule: the application-level guard in record_checkout_consent checks for an
    existing pending+polar+NULL-external_ref row for this user created in the last 10 min
    and skips the INSERT if one already exists.  This prevents compliance row proliferation
    and avoids confusing the webhook claim-on-login matcher.
    """
    monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: 1)

    async with _client(web_app) as client:
        resp1 = await client.post(
            "/api/account/checkout-consent",
            json={"buyer_type": "consumer", "waiver_accepted": True, "plan_slug": "pro"},
        )
        resp2 = await client.post(
            "/api/account/checkout-consent",
            json={"buyer_type": "consumer", "waiver_accepted": True, "plan_slug": "pro"},
        )

    assert resp1.status_code == 200, resp1.text
    assert resp2.status_code == 200, resp2.text

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM subscriptions"
            " WHERE buyer_email = 'consent-test@example.com'"
            "   AND status = 'pending'"
            "   AND source = 'polar'"
            "   AND external_ref IS NULL"
        )
        count = cur.fetchone()[0]

    assert count == 1, (
        f"Double-submit must create exactly 1 pending row, got {count}. "
        "ON CONFLICT DO NOTHING was a silent no-op because NULL != NULL in Postgres; "
        "the application-level guard (SELECT ... LIMIT 1 before INSERT) is the fix."
    )
