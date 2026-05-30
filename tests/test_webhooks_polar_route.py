# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_webhooks_polar_route.py
"""Integration tests for POST /api/webhooks/polar (WI-5, ADR-0039 §4.2).

Business intent (7 cases required by spec):
  T1  valid signed webhook (subscription.created) -> 200 + subscription row active
       + billing_webhook_events row with signature_valid=TRUE, processed_at set.
  T2  duplicate (same webhook-id) -> 200 {"status":"duplicate"}, no second provision.
  T3  bad signature -> 400; billing_webhook_events row with signature_valid=FALSE,
       processed_at NULL (not processed).
  T4  missing POLAR_WEBHOOK_SECRET -> 503.
  T5  unknown event_type -> 200 {"status":"ignored"}.
  T6  unknown Polar product (product_id not in billing.polar_product_map) ->
       200 {"status":"config_error"}, no 500.  (Per I9 the status is the
       ops-LOUD ``config_error`` — a mis-configured product map on a real first
       purchase is ERROR-logged + queryable, never a quiet ``unprocessable``.)
  T7  malformed JSON body -> 400.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Uses httpx.AsyncClient + ASGITransport with base_url="http://127.0.0.1".

POLAR_WEBHOOK_SECRET injection:
  The secret is read at request-time from ``src.web_ui.config.POLAR_WEBHOOK_SECRET``
  (a module attribute, not an env var captured at import).  Tests set the
  attribute directly with monkeypatch.setattr before creating the app so every
  request from within the test sees the test secret.  For tests that require a
  valid signature, the test computes the expected HMAC using the same
  ``src.billing.polar.verify_signature`` algorithm (importing polar and
  calling the signing primitives directly).

Admin session:
  Not needed for webhook tests — the endpoint is public (auth-exempt).
"""

import base64
import hashlib
import hmac
import json
import os
import time

import httpx
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Module-level env: required before create_app() is called.
# ---------------------------------------------------------------------------
os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-webhook-tests-32bytes!!")
os.environ.setdefault("WEBUI_SECURE_COOKIE", "0")

_TEST_SECRET = "whsec_dGVzdC13ZWJob29rLXNlY3JldA=="  # base64("test-webhook-secret")
_TEST_PRODUCT_ID = "polar_prod_001"
_TEST_PLAN_SLUG = "pro"


# ---------------------------------------------------------------------------
# Signature helper — mirrors src/billing/polar.py exactly
# ---------------------------------------------------------------------------

def _build_signature(
    secret: str,
    msg_id: str,
    timestamp: str,
    body: bytes,
) -> str:
    """Compute the Standard-Webhooks v1 signature for a test payload."""
    if secret.startswith("whsec_"):
        secret_bytes = base64.b64decode(secret[6:])
    else:
        secret_bytes = secret.encode()
    signed = f"{msg_id}.{timestamp}".encode() + b"." + body
    digest = hmac.new(secret_bytes, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def _webhook_headers(
    msg_id: str,
    timestamp: str,
    body: bytes,
    *,
    secret: str = _TEST_SECRET,
    bad_sig: bool = False,
) -> dict:
    """Build Standard-Webhooks headers for a test request."""
    if bad_sig:
        sig = "v1,dGhpcyBpcyBhIGJhZCBzaWduYXR1cmU="
    else:
        sig = _build_signature(secret, msg_id, timestamp, body)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": sig,
    }


def _make_payload(
    event_type: str = "subscription.created",
    external_ref: str = "sub_abc123",
    product_id: str = _TEST_PRODUCT_ID,
    buyer_email: str = "buyer@example.com",
) -> dict:
    """Build a minimal Polar-style webhook payload."""
    return {
        "type": event_type,
        "data": {
            "id": external_ref,
            "product_id": product_id,
            "customer_email": buyer_email,
            "amount": 1900,
            "currency": "USD",
            # Polar sends the cadence under recurring_interval (NOT billing_interval).
            "recurring_interval": "month",
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def migrated_pg(pg_conn):
    """Run migrations once per module; yield the pg connection."""
    run_migrations(pg_conn)
    yield pg_conn


@pytest.fixture(autouse=True)
def _clean_billing_tables(migrated_pg):
    """Delete test-inserted billing rows before and after each test."""
    def _wipe():
        for tbl in (
            "billing_webhook_events",
            "subscriptions",
        ):
            try:
                with migrated_pg.cursor() as cur:
                    cur.execute(f"DELETE FROM {tbl}")  # noqa: S608
            except Exception:
                migrated_pg.rollback()
    _wipe()
    yield
    _wipe()


@pytest.fixture(autouse=True)
def _seed_product_map(migrated_pg):
    """Ensure billing.polar_product_map maps _TEST_PRODUCT_ID -> 'pro' in app_settings.

    value_json is stored as {"v": <actual>} per the settings layer contract
    (_unwrap in src/settings.py reads the 'v' key).

    The migration bootstraps the catalogue row via register_settings_idempotent;
    we simply UPDATE it with the test product map, then restore after the test.
    If the row doesn't exist yet (clean DB), bootstrap first.
    """
    import json as _json

    value_with_map = _json.dumps({"v": {_TEST_PRODUCT_ID: _TEST_PLAN_SLUG}})
    value_empty = _json.dumps({"v": {}})

    def _ensure_row():
        """Bootstrap settings catalogue if the billing key row doesn't exist yet."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app_settings WHERE key = 'billing.polar_product_map'"
            )
            count = cur.fetchone()[0]
        if count == 0:
            try:
                from src.db.pg import get_pool  # noqa: PLC0415
                from src.settings_registry import register_settings_idempotent  # noqa: PLC0415
                pool = get_pool()
                with pool.checkout() as conn:
                    conn.autocommit = False
                    register_settings_idempotent(conn)
            except Exception:
                pass

    _ensure_row()

    with migrated_pg.cursor() as cur:
        cur.execute(
            """
            UPDATE app_settings
            SET value_json = %s::jsonb
            WHERE key = 'billing.polar_product_map'
              AND scope = 'system' AND tenant_id IS NULL
            """,
            (value_with_map,),
        )

    # Invalidate settings cache so tests see the fresh value
    try:
        from src.settings import invalidate_setting
        invalidate_setting("billing.polar_product_map")
    except Exception:
        pass
    yield
    # Restore to empty map to avoid cross-test contamination
    with migrated_pg.cursor() as cur:
        cur.execute(
            """
            UPDATE app_settings
            SET value_json = %s::jsonb
            WHERE key = 'billing.polar_product_map'
              AND scope = 'system' AND tenant_id IS NULL
            """,
            (value_empty,),
        )
    try:
        from src.settings import invalidate_setting
        invalidate_setting("billing.polar_product_map")
    except Exception:
        pass


@pytest.fixture
def app_with_secret(monkeypatch):
    """Create app with POLAR_WEBHOOK_SECRET set in config module attribute."""
    import src.web_ui.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "POLAR_WEBHOOK_SECRET", _TEST_SECRET)
    from src.web_ui.app import create_app
    return create_app()


@pytest.fixture
def app_no_secret(monkeypatch):
    """Create app with POLAR_WEBHOOK_SECRET absent (None)."""
    import src.web_ui.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "POLAR_WEBHOOK_SECRET", None)
    from src.web_ui.app import create_app
    return create_app()


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    )


def _now_ts() -> str:
    return str(int(time.time()))


# ---------------------------------------------------------------------------
# T1: valid signed webhook -> 200 + subscription active
# ---------------------------------------------------------------------------

class TestValidSignedWebhook:
    """T1: Correctly signed subscription.created event provisions a subscription."""

    @pytest.mark.asyncio
    async def test_returns_200_ok(self, migrated_pg, app_with_secret):
        payload = _make_payload()
        body = json.dumps(payload).encode()
        ts = _now_ts()
        msg_id = "msg_t1_valid"
        headers = _webhook_headers(msg_id, ts, body)

        async with _client(app_with_secret) as client:
            resp = await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data.get("status") == "ok"
        assert data.get("action") == "grant"

    @pytest.mark.asyncio
    async def test_subscription_row_active(self, migrated_pg, app_with_secret):
        external_ref = "sub_t1_active"
        payload = _make_payload(external_ref=external_ref)
        body = json.dumps(payload).encode()
        ts = _now_ts()
        msg_id = "msg_t1_sub_active"
        headers = _webhook_headers(msg_id, ts, body)

        async with _client(app_with_secret) as client:
            await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT status FROM subscriptions WHERE external_ref = %s",
                (external_ref,),
            )
            row = cur.fetchone()
        assert row is not None, "Subscription row must exist after valid webhook"
        assert row[0] == "active", f"Expected status='active', got {row[0]!r}"

    @pytest.mark.asyncio
    async def test_ledger_row_signature_valid_and_processed(self, migrated_pg, app_with_secret):
        msg_id = "msg_t1_ledger"
        payload = _make_payload(external_ref="sub_t1_ledger")
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body)

        async with _client(app_with_secret) as client:
            await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT signature_valid, processed_at FROM billing_webhook_events"
                " WHERE vendor = 'polar' AND event_id = %s",
                (msg_id,),
            )
            row = cur.fetchone()
        assert row is not None, "billing_webhook_events row must exist"
        sig_valid, processed_at = row
        assert sig_valid is True, "signature_valid must be TRUE for valid event"
        assert processed_at is not None, "processed_at must be set after successful processing"


# ---------------------------------------------------------------------------
# T2: duplicate (same webhook-id) -> 200 {"status":"duplicate"}
# ---------------------------------------------------------------------------

class TestDuplicateWebhook:
    """T2: Replaying the same webhook-id returns 200 duplicate, no second provision."""

    @pytest.mark.asyncio
    async def test_duplicate_returns_200_duplicate(self, migrated_pg, app_with_secret):
        msg_id = "msg_t2_dup"
        external_ref = "sub_t2_dup"
        payload = _make_payload(external_ref=external_ref)
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body)

        async with _client(app_with_secret) as client:
            r1 = await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )
            r2 = await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json().get("status") == "duplicate"

    @pytest.mark.asyncio
    async def test_duplicate_no_second_subscription(self, migrated_pg, app_with_secret):
        msg_id = "msg_t2_no_dup_sub"
        external_ref = "sub_t2_no_dup"
        payload = _make_payload(external_ref=external_ref)
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body)

        async with _client(app_with_secret) as client:
            await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )
            await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s",
                (external_ref,),
            )
            count = cur.fetchone()[0]
        assert count == 1, f"Expected exactly 1 subscription, got {count}"

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM billing_webhook_events"
                " WHERE vendor = 'polar' AND event_id = %s",
                (msg_id,),
            )
            ledger_count = cur.fetchone()[0]
        assert ledger_count == 1, f"Expected 1 ledger row (dedup), got {ledger_count}"


# ---------------------------------------------------------------------------
# T3: bad signature -> 400 + ledger row signature_valid=FALSE
# ---------------------------------------------------------------------------

class TestBadSignature:
    """T3: Invalid signature returns 400; event recorded but NOT processed."""

    @pytest.mark.asyncio
    async def test_bad_sig_returns_400(self, migrated_pg, app_with_secret):
        msg_id = "msg_t3_badsig"
        payload = _make_payload(external_ref="sub_t3_bad")
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body, bad_sig=True)

        async with _client(app_with_secret) as client:
            resp = await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_bad_sig_ledger_row_signature_invalid(self, migrated_pg, app_with_secret):
        msg_id = "msg_t3_ledger_invalid"
        payload = _make_payload(external_ref="sub_t3_ledger_bad")
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body, bad_sig=True)

        async with _client(app_with_secret) as client:
            await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT signature_valid, processed_at FROM billing_webhook_events"
                " WHERE vendor = 'polar' AND event_id = %s",
                (msg_id,),
            )
            row = cur.fetchone()
        assert row is not None, "billing_webhook_events row must be recorded even for bad sig"
        sig_valid, processed_at = row
        assert sig_valid is False, "signature_valid must be FALSE for bad signature"
        assert processed_at is None, "processed_at must be NULL — bad-sig events are NOT processed"

    @pytest.mark.asyncio
    async def test_bad_sig_no_subscription_created(self, migrated_pg, app_with_secret):
        msg_id = "msg_t3_no_sub"
        external_ref = "sub_t3_no_sub"
        payload = _make_payload(external_ref=external_ref)
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body, bad_sig=True)

        async with _client(app_with_secret) as client:
            await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s",
                (external_ref,),
            )
            count = cur.fetchone()[0]
        assert count == 0, "No subscription should be created for a bad-signature event"


# ---------------------------------------------------------------------------
# T4: missing POLAR_WEBHOOK_SECRET -> 503
# ---------------------------------------------------------------------------

class TestMissingSecret:
    """T4: Absent POLAR_WEBHOOK_SECRET returns 503 (fail-closed)."""

    @pytest.mark.asyncio
    async def test_missing_secret_returns_503(self, migrated_pg, app_no_secret):
        msg_id = "msg_t4_nosecret"
        payload = _make_payload(external_ref="sub_t4_nosecret")
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body)

        async with _client(app_no_secret) as client:
            resp = await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )
        assert resp.status_code == 503, (
            f"Expected 503 when secret is absent, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# T5: unknown event_type -> 200 {"status":"ignored"}
# ---------------------------------------------------------------------------

class TestUnknownEventType:
    """T5: Unrecognised event_type returns 200 'ignored' — Polar stops retrying."""

    @pytest.mark.asyncio
    async def test_unknown_event_returns_200_ignored(self, migrated_pg, app_with_secret):
        msg_id = "msg_t5_unknown"
        payload = _make_payload(
            event_type="order.unknown_future_event",
            external_ref="sub_t5_unknown",
        )
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body)

        async with _client(app_with_secret) as client:
            resp = await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ignored", (
            f"Expected status='ignored' for unknown event, got: {data}"
        )

    @pytest.mark.asyncio
    async def test_unknown_event_ledger_marked_processed(self, migrated_pg, app_with_secret):
        msg_id = "msg_t5_ledger"
        payload = _make_payload(
            event_type="subscription.some_unknown_type",
            external_ref="sub_t5_ledger",
        )
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body)

        async with _client(app_with_secret) as client:
            await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT processed_at, processing_error FROM billing_webhook_events"
                " WHERE vendor = 'polar' AND event_id = %s",
                (msg_id,),
            )
            row = cur.fetchone()
        assert row is not None
        processed_at, _ = row
        assert processed_at is not None, "Unknown-event row must be marked processed (ignored)"


# ---------------------------------------------------------------------------
# T6: unknown Polar product -> 200 {"status":"config_error"} (I9: ops-loud)
# ---------------------------------------------------------------------------

class TestUnknownProduct:
    """T6: Product not in billing.polar_product_map -> 200 config_error (no 500).

    Per I9 the response status is ``config_error`` (ERROR-logged, queryable
    processing_error) rather than a quiet ``unprocessable`` — a real first
    purchase against a mis-configured product map must be ops-LOUD, not silent.
    """

    @pytest.mark.asyncio
    async def test_unknown_product_returns_200_config_error(self, migrated_pg, app_with_secret):
        msg_id = "msg_t6_prod"
        payload = _make_payload(
            product_id="unknown_product_xyz",
            external_ref="sub_t6_unknown_prod",
        )
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body)

        async with _client(app_with_secret) as client:
            resp = await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )
        assert resp.status_code == 200, (
            f"Expected 200 (not 5xx), got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data.get("status") == "config_error", (
            f"Expected status='config_error' for unknown product, got: {data}"
        )

    @pytest.mark.asyncio
    async def test_unknown_product_processing_error_set(self, migrated_pg, app_with_secret):
        msg_id = "msg_t6_error"
        payload = _make_payload(
            product_id="unknown_product_abc",
            external_ref="sub_t6_error",
        )
        body = json.dumps(payload).encode()
        ts = _now_ts()
        headers = _webhook_headers(msg_id, ts, body)

        async with _client(app_with_secret) as client:
            await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT processing_error FROM billing_webhook_events"
                " WHERE vendor = 'polar' AND event_id = %s",
                (msg_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] is not None, "processing_error must be set for unknown-product events"


# ---------------------------------------------------------------------------
# I23: end-to-end route coverage for the dispatch fixes (I1, I3, I8)
# ---------------------------------------------------------------------------


def _seed_user_and_key(pg_conn, *, email: str, plan_slug: str = "pro") -> tuple[int, int]:
    """Insert a verified webui_user + an api_key on ``plan_slug``; return (user_id, key_id)."""
    import bcrypt
    pw_hash = bcrypt.hashpw(b"testpassword", bcrypt.gensalt(rounds=4)).decode()
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, email, is_admin, email_verified)"
            " VALUES (%s, %s, %s, FALSE, TRUE) RETURNING id",
            (email.split("@")[0], pw_hash, email),
        )
        user_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM plans WHERE slug = %s", (plan_slug,))
        plan_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO api_keys (key_prefix, key_hash, name, user_id, plan_id)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            ("tst_", "wh_testhash_" + email, f"key_{user_id}", user_id, plan_id),
        )
        key_id = cur.fetchone()[0]
    return user_id, key_id


@pytest.fixture(autouse=True)
def _clean_i23_rows(migrated_pg):
    """Wipe user/key rows the I23 tests create (the module fixture only wipes
    subscriptions + ledger).  Non-destructive DELETE, never DROP."""
    def _wipe():
        # api_keys must go before webui_users (FK). Restrict to test rows.
        stmts = (
            "DELETE FROM api_keys WHERE key_prefix = 'tst_'",
            "DELETE FROM webui_users WHERE email LIKE 'wh_%@example.com'",
        )
        for stmt in stmts:
            try:
                with migrated_pg.cursor() as cur:
                    cur.execute(stmt)
            except Exception:
                migrated_pg.rollback()
    yield
    _wipe()


class TestRevokeWithoutProductId:
    """I1: a cancellation with NO product_id must revoke — never blocked by plan
    resolution.  The customer cancels → subscription cancelled AND the linked key
    is downgraded to free."""

    @pytest.mark.asyncio
    async def test_cancel_revokes_and_downgrades_key_without_product_id(
        self, migrated_pg, app_with_secret
    ):
        from src.db.pg import subscription_store

        external_ref = "sub_i23_revoke"
        email = "wh_revoke@example.com"
        user_id, key_id = _seed_user_and_key(migrated_pg, email=email, plan_slug="pro")

        # 1) Grant + provision via a real signed webhook (so the sub is claimed
        #    and the key is linked) — carries a product_id.
        grant_payload = _make_payload(
            event_type="subscription.created",
            external_ref=external_ref,
            buyer_email=email,
        )
        gbody = json.dumps(grant_payload).encode()
        gts = _now_ts()
        gheaders = _webhook_headers("msg_i23_grant", gts, gbody)
        async with _client(app_with_secret) as client:
            gr = await client.post(
                "/api/webhooks/polar", content=gbody,
                headers={**gheaders, "content-type": "application/json"},
            )
        assert gr.status_code == 200, gr.text

        # Ensure the sub is linked to the seeded key (claim-on-grant links by
        # verified email; belt-and-suspenders, link explicitly if not already).
        subs = subscription_store()
        sub = subs.get_by_external_ref(external_ref)
        assert sub is not None
        if sub["api_key_id"] is None:
            subs.link_to_api_key(sub["id"], key_id)
            subs.link_to_user(sub["id"], user_id)

        # 2) Cancellation payload — NO product_id (proves I1: resolution skipped).
        cancel_payload = {
            "type": "subscription.canceled",
            "data": {"id": external_ref, "status": "canceled"},
        }
        cbody = json.dumps(cancel_payload).encode()
        cts = _now_ts()
        cheaders = _webhook_headers("msg_i23_cancel", cts, cbody)
        async with _client(app_with_secret) as client:
            rv = await client.post(
                "/api/webhooks/polar", content=cbody,
                headers={**cheaders, "content-type": "application/json"},
            )
        assert rv.status_code == 200, rv.text
        assert rv.json().get("action") == "revoke", (
            f"Cancellation must dispatch revoke, got: {rv.json()}"
        )

        # 3) subscription cancelled
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT status FROM subscriptions WHERE external_ref = %s", (external_ref,)
            )
            status = cur.fetchone()[0]
        assert status == "cancelled", f"Expected cancelled, got {status!r}"

        # 4) linked key downgraded to free
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT p.slug FROM api_keys ak JOIN plans p ON p.id = ak.plan_id"
                " WHERE ak.id = %s",
                (key_id,),
            )
            slug = cur.fetchone()[0]
        assert slug == "free", (
            f"Cancelled sub's key must be downgraded to free, got {slug!r}"
        )


class TestUpdateStatusNotForcedActive:
    """I3: a subscription.updated carrying a non-active Polar status must NOT be
    forced to 'active'."""

    @pytest.mark.asyncio
    async def test_past_due_update_is_not_forced_active(self, migrated_pg, app_with_secret):
        external_ref = "sub_i23_update"

        # 1) Grant so the sub exists + is active.
        grant_payload = _make_payload(
            event_type="subscription.created", external_ref=external_ref,
        )
        gbody = json.dumps(grant_payload).encode()
        gheaders = _webhook_headers("msg_i23_upd_grant", _now_ts(), gbody)
        async with _client(app_with_secret) as client:
            gr = await client.post(
                "/api/webhooks/polar", content=gbody,
                headers={**gheaders, "content-type": "application/json"},
            )
        assert gr.status_code == 200, gr.text

        # 2) Update carrying a past_due status.
        update_payload = {
            "type": "subscription.updated",
            "data": {
                "id": external_ref,
                "product_id": _TEST_PRODUCT_ID,
                "status": "past_due",
            },
        }
        ubody = json.dumps(update_payload).encode()
        uheaders = _webhook_headers("msg_i23_update", _now_ts(), ubody)
        async with _client(app_with_secret) as client:
            ur = await client.post(
                "/api/webhooks/polar", content=ubody,
                headers={**uheaders, "content-type": "application/json"},
            )
        assert ur.status_code == 200, ur.text

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT status FROM subscriptions WHERE external_ref = %s", (external_ref,)
            )
            status = cur.fetchone()[0]
        assert status != "active", (
            f"past_due update must NOT be forced to active, got {status!r}"
        )
        assert status == "past_due", (
            f"Expected derived status 'past_due', got {status!r}"
        )


class TestGrantBillingIntervalNormalized:
    """FIX-C/FIX-D: a grant's cadence comes from Polar's ``recurring_interval``
    field (NOT ``billing_interval``); the raw token is normalized to our enum so
    the subscription stores a valid, non-NULL value (no silent drop from a CHECK
    violation, and no NULL from reading the wrong field name)."""

    async def _grant_and_read_interval(
        self, migrated_pg, app_with_secret, *, external_ref, data_extra, msg_id
    ):
        payload = {
            "type": "subscription.created",
            "data": {
                "id": external_ref,
                "product_id": _TEST_PRODUCT_ID,
                "customer_email": "wh_interval@example.com",
                "amount": 1900,
                "currency": "USD",
                **data_extra,
            },
        }
        body = json.dumps(payload).encode()
        headers = _webhook_headers(msg_id, _now_ts(), body)
        async with _client(app_with_secret) as client:
            resp = await client.post(
                "/api/webhooks/polar", content=body,
                headers={**headers, "content-type": "application/json"},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("status") == "ok", (
            f"grant must succeed (no silent drop), got: {resp.json()}"
        )
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT billing_interval FROM subscriptions WHERE external_ref = %s",
                (external_ref,),
            )
            row = cur.fetchone()
        assert row is not None, "Subscription must be written (proves no CHECK drop)"
        return row[0]

    @pytest.mark.asyncio
    async def test_recurring_interval_month_normalized_to_monthly(
        self, migrated_pg, app_with_secret
    ):
        """FIX-C: the cadence is read from ``recurring_interval`` (Polar's real
        field), normalized 'month'→'monthly' — NOT NULL (which is what reading the
        wrong 'billing_interval' key would have produced)."""
        interval = await self._grant_and_read_interval(
            migrated_pg, app_with_secret,
            external_ref="sub_i23_interval",
            data_extra={"recurring_interval": "month"},
            msg_id="msg_i23_interval",
        )
        assert interval == "monthly", (
            f"recurring_interval 'month' must normalize to 'monthly', got {interval!r}"
        )

    @pytest.mark.asyncio
    async def test_recurring_interval_is_not_null_proving_correct_field_read(
        self, migrated_pg, app_with_secret
    ):
        """FIX-C regression guard: a payload carrying the cadence ONLY under
        ``recurring_interval`` (and the old wrong key absent) must still persist a
        non-NULL interval — proving the pipeline reads recurring_interval."""
        interval = await self._grant_and_read_interval(
            migrated_pg, app_with_secret,
            external_ref="sub_i23_interval_notnull",
            data_extra={"recurring_interval": "year"},
            msg_id="msg_i23_interval_notnull",
        )
        assert interval == "annual", (
            f"recurring_interval 'year' must normalize to 'annual' (non-NULL), got {interval!r}"
        )

    @pytest.mark.asyncio
    async def test_recurring_interval_day_falls_back_to_monthly(
        self, migrated_pg, app_with_secret
    ):
        """FIX-D: Polar's 'day' cadence has no own enum; it falls back to a valid
        'monthly' (CHECK-safe) rather than NULL/CHECK-violation — the grant is
        never silently dropped."""
        interval = await self._grant_and_read_interval(
            migrated_pg, app_with_secret,
            external_ref="sub_i23_interval_day",
            data_extra={"recurring_interval": "day"},
            msg_id="msg_i23_interval_day",
        )
        assert interval == "monthly", (
            f"recurring_interval 'day' must fall back to 'monthly', got {interval!r}"
        )
