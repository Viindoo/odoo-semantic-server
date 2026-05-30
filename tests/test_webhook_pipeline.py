# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_webhook_pipeline.py
"""Vendor-parametric tests for src.billing.webhook_pipeline (M10B P1, ADR-0039 Area A).

Business intent: the webhook pipeline is VENDOR-AGNOSTIC.  The Polar route is just
one binding; a second payment adapter (Paddle/ERP) must be able to reuse the exact
same pipeline by supplying its own ``WebhookAdapter`` and a ~25-line route.  These
tests prove that with a FAKE adapter (vendor='test') — no Polar code involved:

  P1  vendor-parametric grant: a signed event with action='grant' writes the
       ledger row with the ADAPTER's vendor, dispatches through activation, and
       marks the event processed → 200 {"status":"ok","action":"grant"}.
  P2  the pipeline records the ledger row under ``adapter.vendor`` (not 'polar').
  P3  dedup is vendor-parametric: replaying the same event_id → 200 'duplicate',
       no second ledger row, no second dispatch.
  P4  unmapped event_type (adapter.event_action_fn → None) → 200 'ignored',
       ledger marked processed (ops-visible), no dispatch.
  P5  bad signature (adapter.verify_fn → False) → 400, ledger row recorded with
       signature_valid=FALSE and processed_at NULL.
  P6  missing secret (adapter.secret=None) → 503 fail-closed.

The fake adapter binds trivial in-test callables, NOT polar.*, so a green run
proves a non-Polar adapter is a drop-in.  ``vendor='erp'`` is permitted by BOTH
the ``billing_webhook_events_vendor_check`` AND the ``subscriptions_source_check``
CHECK enums (migration m13_014) — so a grant flowing through the shared activation
contract is accepted, exercising the full dispatch path with a non-Polar vendor.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""

import json
import os
import time

import httpx
import pytest
from fastapi import APIRouter
from starlette.requests import Request

from src.billing.webhook_pipeline import WebhookAdapter, run_webhook_pipeline
from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Module-level env: required before create_app() is called.
# ---------------------------------------------------------------------------
os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-pipeline-tests-32bytes!!")
os.environ.setdefault("WEBUI_SECURE_COOKIE", "0")

# 'erp' is valid in BOTH the ledger vendor CHECK and the subscriptions source
# CHECK, so a non-Polar grant flows end-to-end through activation.* unblocked.
_TEST_VENDOR = "erp"
_TEST_PLAN_SLUG = "pro"

# Headers a fake vendor might use — arbitrary, supplied by the adapter.
_H_ID = "x-test-id"
_H_TS = "x-test-timestamp"
_H_SIG = "x-test-signature"


# ---------------------------------------------------------------------------
# Fake vendor callables — deliberately NOT polar.*  (drop-in proof)
# ---------------------------------------------------------------------------


def _fake_verify(secret, *, msg_id, timestamp, body, signature_header, tolerance_seconds):
    """Trivial 'signature' scheme: header equals the secret → valid."""
    return bool(secret) and signature_header == f"valid:{secret}"


def _fake_parse_event(payload: dict) -> tuple[str, str, str]:
    """(event_id, event_type, external_ref) from a fake-vendor payload."""
    event_id = payload.get("id") or ""
    event_type = payload.get("type") or ""
    data = payload.get("data") or {}
    external_ref = data.get("ref") or ""
    if not event_type:
        raise ValueError("fake parse_event: missing type")
    if not external_ref:
        raise ValueError("fake parse_event: missing data.ref")
    return str(event_id), str(event_type), str(external_ref)


_FAKE_ACTION_MAP = {
    "thing.created": "grant",
    "thing.changed": "update",
    "thing.gone": "revoke",
}


def _fake_event_action(event_type: str):
    return _FAKE_ACTION_MAP.get(event_type)


def _fake_extract_email(data: dict):
    return data.get("email")


def _fake_map_status(payload: dict) -> str:
    data = payload.get("data") or {}
    return data.get("status") or "active"


def _fake_normalize_interval(value):
    return {"m": "monthly", "y": "annual"}.get(value)


def _make_fake_adapter(secret: str | None) -> WebhookAdapter:
    """Build a WebhookAdapter bound entirely to in-test fakes — no Polar code."""
    return WebhookAdapter(
        vendor=_TEST_VENDOR,
        secret=secret,
        tolerance_seconds=300,
        rate_limit_rpm=1000,
        header_id=_H_ID,
        header_timestamp=_H_TS,
        header_signature=_H_SIG,
        verify_fn=_fake_verify,
        parse_event_fn=_fake_parse_event,
        event_action_fn=_fake_event_action,
        resolve_plan_fn=_fake_resolve_plan,
        extract_email_fn=_fake_extract_email,
        map_status_fn=_fake_map_status,
        normalize_interval_fn=_fake_normalize_interval,
    )


def _fake_resolve_plan(payload: dict) -> int:
    """Resolve the test plan id via the SHARED vendor-neutral slug helper."""
    from src.billing._db import slug_to_plan_id
    from src.db.pg import get_pool
    with get_pool().checkout() as conn:
        return slug_to_plan_id(_TEST_PLAN_SLUG, conn)


# ---------------------------------------------------------------------------
# App with a test-vendor route mounted (the "~25-line 2nd adapter" in miniature)
# ---------------------------------------------------------------------------

_test_router = APIRouter(prefix="/api/webhooks", tags=["webhooks-test"])

# Module-level holder so the route can read the adapter the fixture installs.
_CURRENT_SECRET: dict = {"secret": None}


@_test_router.post("/test")
async def _test_webhook(request: Request):
    adapter = _make_fake_adapter(_CURRENT_SECRET["secret"])
    return await run_webhook_pipeline(adapter, request)


def _payload(event_type="thing.created", ref="ref_abc", email="buyer@example.com"):
    return {
        "type": event_type,
        "data": {"ref": ref, "email": email, "amount": 1900, "currency": "USD",
                 "billing_interval": "m", "status": "active"},
    }


def _headers(msg_id, ts, *, secret="sek", bad_sig=False):
    sig = "invalid:nope" if bad_sig else f"valid:{secret}"
    return {_H_ID: msg_id, _H_TS: ts, _H_SIG: sig, "content-type": "application/json"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def migrated_pg(pg_conn):
    run_migrations(pg_conn)
    yield pg_conn


@pytest.fixture(autouse=True)
def _clean_billing_tables(migrated_pg):
    """Wipe only the rows these tests create (ledger by vendor, subs by source)."""
    def _wipe():
        stmts = (
            ("DELETE FROM billing_webhook_events WHERE vendor = %s", (_TEST_VENDOR,)),
            ("DELETE FROM subscriptions WHERE source = %s", (_TEST_VENDOR,)),
        )
        for sql, params in stmts:
            try:
                with migrated_pg.cursor() as cur:
                    cur.execute(sql, params)
            except Exception:
                migrated_pg.rollback()
    _wipe()
    yield
    _wipe()


@pytest.fixture
def app_with_secret(monkeypatch):
    """App + the test-vendor route mounted, with a configured fake secret."""
    _CURRENT_SECRET["secret"] = "sek"
    from src.web_ui.app import create_app
    app = create_app()
    app.include_router(_test_router)
    return app


@pytest.fixture
def app_no_secret():
    """App + the test-vendor route, but the adapter secret is None (fail-closed)."""
    _CURRENT_SECRET["secret"] = None
    from src.web_ui.app import create_app
    app = create_app()
    app.include_router(_test_router)
    return app


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    )


def _now_ts() -> str:
    return str(int(time.time()))


# ---------------------------------------------------------------------------
# P1 + P2: vendor-parametric grant + ledger recorded under adapter.vendor
# ---------------------------------------------------------------------------


class TestVendorParametricGrant:
    """The pipeline drives a full grant via a NON-Polar adapter (vendor='test')."""

    @pytest.mark.asyncio
    async def test_grant_returns_ok_and_records_ledger_under_adapter_vendor(
        self, migrated_pg, app_with_secret
    ):
        msg_id = "evt_pipe_grant"
        ref = "ref_pipe_grant"
        body = json.dumps(_payload(ref=ref)).encode()
        ts = _now_ts()

        async with _client(app_with_secret) as client:
            resp = await client.post(
                "/api/webhooks/test", content=body, headers=_headers(msg_id, ts)
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("status") == "ok"
        assert data.get("action") == "grant"

        # P2: ledger row written under the ADAPTER's vendor ('test'), not 'polar'.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT vendor, signature_valid, processed_at FROM billing_webhook_events"
                " WHERE event_id = %s",
                (msg_id,),
            )
            row = cur.fetchone()
        assert row is not None, "pipeline must record the ledger row"
        vendor, sig_valid, processed_at = row
        assert vendor == _TEST_VENDOR, f"ledger vendor must be the adapter's, got {vendor!r}"
        assert sig_valid is True
        assert processed_at is not None, "successful grant must mark the event processed"

        # The subscription was created via the shared activation contract.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT status, source FROM subscriptions WHERE external_ref = %s", (ref,)
            )
            sub = cur.fetchone()
        assert sub is not None, "grant must create a subscription via activation.*"
        assert sub[0] == "active"
        assert sub[1] == _TEST_VENDOR, "subscription.source must be the adapter vendor"


# ---------------------------------------------------------------------------
# P3: vendor-parametric dedup
# ---------------------------------------------------------------------------


class TestVendorParametricDedup:
    """Replaying the same event_id → 200 duplicate, single ledger row, single sub."""

    @pytest.mark.asyncio
    async def test_replay_is_deduped(self, migrated_pg, app_with_secret):
        msg_id = "evt_pipe_dup"
        ref = "ref_pipe_dup"
        body = json.dumps(_payload(ref=ref)).encode()
        ts = _now_ts()
        headers = _headers(msg_id, ts)

        async with _client(app_with_secret) as client:
            r1 = await client.post("/api/webhooks/test", content=body, headers=headers)
            r2 = await client.post("/api/webhooks/test", content=body, headers=headers)

        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200
        assert r2.json().get("status") == "duplicate"

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM billing_webhook_events WHERE event_id = %s", (msg_id,)
            )
            assert cur.fetchone()[0] == 1, "dedup must keep exactly one ledger row"
            cur.execute("SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s", (ref,))
            assert cur.fetchone()[0] == 1, "no second provision on replay"


# ---------------------------------------------------------------------------
# P4: unmapped event_type → ignored (ops-visible), no dispatch
# ---------------------------------------------------------------------------


class TestVendorParametricUnmapped:
    @pytest.mark.asyncio
    async def test_unmapped_event_is_ignored_and_marked_processed(
        self, migrated_pg, app_with_secret
    ):
        msg_id = "evt_pipe_unmapped"
        body = json.dumps(_payload(event_type="thing.unknown", ref="ref_pipe_unmapped")).encode()
        ts = _now_ts()

        async with _client(app_with_secret) as client:
            resp = await client.post(
                "/api/webhooks/test", content=body, headers=_headers(msg_id, ts)
            )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("status") == "ignored"

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT processed_at, processing_error FROM billing_webhook_events"
                " WHERE event_id = %s",
                (msg_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] is not None, "unmapped event must be marked processed (ops-visible)"
        assert row[1] is not None and "unmapped" in row[1]
        # No subscription dispatched.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s",
                ("ref_pipe_unmapped",),
            )
            assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# P5: bad signature → 400, ledger recorded signature_valid=FALSE, not processed
# ---------------------------------------------------------------------------


class TestVendorParametricBadSignature:
    @pytest.mark.asyncio
    async def test_bad_signature_400_and_recorded_not_processed(
        self, migrated_pg, app_with_secret
    ):
        msg_id = "evt_pipe_badsig"
        body = json.dumps(_payload(ref="ref_pipe_badsig")).encode()
        ts = _now_ts()

        async with _client(app_with_secret) as client:
            resp = await client.post(
                "/api/webhooks/test", content=body, headers=_headers(msg_id, ts, bad_sig=True)
            )
        assert resp.status_code == 400, resp.text

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT signature_valid, processed_at FROM billing_webhook_events"
                " WHERE event_id = %s",
                (msg_id,),
            )
            row = cur.fetchone()
        assert row is not None, "bad-sig event must still be recorded (forensics)"
        assert row[0] is False
        assert row[1] is None, "bad-sig events must NOT be marked processed"
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s",
                ("ref_pipe_badsig",),
            )
            assert cur.fetchone()[0] == 0, "no subscription for a bad-sig event"


# ---------------------------------------------------------------------------
# P6: missing secret → 503 fail-closed
# ---------------------------------------------------------------------------


class TestVendorParametricMissingSecret:
    @pytest.mark.asyncio
    async def test_missing_secret_returns_503(self, migrated_pg, app_no_secret):
        msg_id = "evt_pipe_nosecret"
        body = json.dumps(_payload(ref="ref_pipe_nosecret")).encode()
        ts = _now_ts()

        async with _client(app_no_secret) as client:
            resp = await client.post(
                "/api/webhooks/test", content=body, headers=_headers(msg_id, ts, secret="sek")
            )
        assert resp.status_code == 503, resp.text
