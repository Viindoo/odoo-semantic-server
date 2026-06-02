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
from datetime import UTC, datetime

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


def _fake_extract_interval(data: dict):
    """Pull the RAW interval token from the fake-vendor data dict.

    Mirrors the Polar extractor (which reads ``recurring_interval``): the vendor
    field NAME lives in the adapter, not the pipeline.  This fake vendor carries
    it under ``cadence`` to prove the pipeline never hard-codes a field name.
    """
    return data.get("cadence")


def _fake_normalize_interval(value):
    return {"m": "monthly", "y": "annual"}.get(value)


def _fake_extract_seats(data: dict) -> int:
    try:
        return int(data.get("seats") or 1)
    except (TypeError, ValueError):
        return 1


def _fake_extract_amount(data: dict):
    return data.get("amount")


def _fake_extract_currency(data: dict):
    return data.get("currency")


def _parse_iso(value):
    """Lenient ISO-8601 → aware datetime (test mirror of the route helper)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _fake_extract_period(data: dict):
    return (
        _parse_iso(data.get("current_period_start")),
        _parse_iso(data.get("current_period_end")),
        _parse_iso(data.get("trial_ends_at")),
    )


def _fake_extract_event_at(headers, payload: dict):
    """Read the (Unix-epoch or ISO) event timestamp from the test timestamp header.

    Tests inject the ordering timestamp via the _H_TS header (Unix epoch); fall
    back to a payload ``modified_at`` so out-of-order tests can drive it via the
    body too.
    """
    ts = headers.get(_H_TS) if headers is not None else None
    if ts:
        try:
            return datetime.fromtimestamp(int(ts), tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    data = payload.get("data") or {}
    return _parse_iso(data.get("modified_at"))


def _make_fake_adapter(
    secret: str | None,
    *,
    resolve_plan_fn=None,
    rate_limit_rpm: int = 1000,
    watched_event_prefixes: frozenset[str] = frozenset(),
) -> WebhookAdapter:
    """Build a WebhookAdapter bound entirely to in-test fakes — no Polar code.

    ``resolve_plan_fn`` is overridable so a test can inject a transient/permanent
    failure at dispatch time; ``rate_limit_rpm`` is overridable for the
    per-vendor rate-limit test; ``watched_event_prefixes`` is overridable so a
    test can prove the forgotten-mapping-vs-benign-ignore distinction (default
    empty = every unmapped event is a benign ignore).
    """
    return WebhookAdapter(
        vendor=_TEST_VENDOR,
        secret=secret,
        tolerance_seconds=300,
        rate_limit_rpm=rate_limit_rpm,
        header_id=_H_ID,
        header_timestamp=_H_TS,
        header_signature=_H_SIG,
        verify_fn=_fake_verify,
        parse_event_fn=_fake_parse_event,
        event_action_fn=_fake_event_action,
        watched_event_prefixes=watched_event_prefixes,
        resolve_plan_fn=resolve_plan_fn or _fake_resolve_plan,
        extract_email_fn=_fake_extract_email,
        map_status_fn=_fake_map_status,
        normalize_interval_fn=_fake_normalize_interval,
        extract_interval_fn=_fake_extract_interval,
        extract_seats_fn=_fake_extract_seats,
        extract_amount_fn=_fake_extract_amount,
        extract_currency_fn=_fake_extract_currency,
        extract_period_fn=_fake_extract_period,
        extract_event_at_fn=_fake_extract_event_at,
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

# Module-level holder so the route can read the adapter config the fixture/test
# installs (secret + optional dispatch-failure injection + rate-limit override).
_CURRENT_SECRET: dict = {"secret": None}
_ADAPTER_OVERRIDES: dict = {
    "resolve_plan_fn": None,
    "rate_limit_rpm": 1000,
    "watched_event_prefixes": frozenset(),
}


@_test_router.post("/test")
async def _test_webhook(request: Request):
    adapter = _make_fake_adapter(
        _CURRENT_SECRET["secret"],
        resolve_plan_fn=_ADAPTER_OVERRIDES["resolve_plan_fn"],
        rate_limit_rpm=_ADAPTER_OVERRIDES["rate_limit_rpm"],
        watched_event_prefixes=_ADAPTER_OVERRIDES["watched_event_prefixes"],
    )
    return await run_webhook_pipeline(adapter, request)


def _payload(event_type="thing.created", ref="ref_abc", email="buyer@example.com"):
    return {
        "type": event_type,
        # 'cadence' is THIS fake vendor's interval field — the adapter's
        # extract_interval_fn reads it, proving the pipeline hard-codes no field
        # name (Polar reads its own 'recurring_interval').
        "data": {"ref": ref, "email": email, "amount": 1900, "currency": "USD",
                 "cadence": "m", "status": "active"},
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
    # Reset per-test adapter overrides so a dispatch-failure / rate-limit /
    # watched-prefix injection from one test never leaks into the next.
    _ADAPTER_OVERRIDES["resolve_plan_fn"] = None
    _ADAPTER_OVERRIDES["rate_limit_rpm"] = 1000
    _ADAPTER_OVERRIDES["watched_event_prefixes"] = frozenset()
    _wipe()
    yield
    _ADAPTER_OVERRIDES["resolve_plan_fn"] = None
    _ADAPTER_OVERRIDES["rate_limit_rpm"] = 1000
    _ADAPTER_OVERRIDES["watched_event_prefixes"] = frozenset()
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
        # The default fake adapter declares NO watched prefixes → an unmapped
        # event is a BENIGN IGNORE: marked processed but processing_error stays
        # NULL (the error column is reserved for genuine errors / forgotten
        # mappings, not events the vendor deliberately fires out of scope).
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
        assert row[1] is None, "a benign-ignore must leave processing_error NULL"
        # No subscription dispatched.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s",
                ("ref_pipe_unmapped",),
            )
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_unmapped_event_within_watched_prefix_flags_forgotten_mapping(
        self, migrated_pg, app_with_secret
    ):
        # An unmapped subtype of a WATCHED prefix is a likely FORGOTTEN MAPPING:
        # the pipeline still acks 200 'ignored' (no dispatch) but records a
        # processing_error so the missing mapping is ops-visible, never buried.
        _ADAPTER_OVERRIDES["watched_event_prefixes"] = frozenset({"thing."})
        msg_id = "evt_pipe_forgotten"
        body = json.dumps(
            _payload(event_type="thing.brandnew", ref="ref_pipe_forgotten")
        ).encode()
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
        assert row[0] is not None, "forgotten-mapping event must be marked processed"
        assert row[1] is not None and "unmapped" in row[1], (
            "an unmapped subtype of a watched prefix must record processing_error"
        )
        # No subscription dispatched.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s",
                ("ref_pipe_forgotten",),
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


# ---------------------------------------------------------------------------
# #2 reprocess-after-crash: a recorded-but-unprocessed event MUST re-dispatch
# ---------------------------------------------------------------------------


class TestReprocessAfterCrash:
    """A delivery that was recorded in the ledger but NEVER marked processed
    (a crash between the ledger INSERT and mark_event_processed) MUST be
    RE-DISPATCHED on the vendor's retry — not silently dropped as a duplicate.

    Business rule (money-critical): a paid grant interrupted by a crash is
    recovered by the idempotent replay; the sub ends up active.  Treating the
    replay as a 'duplicate' would lose the grant forever.
    """

    @pytest.mark.asyncio
    async def test_recorded_but_unprocessed_event_redispatches_grant(
        self, migrated_pg, app_with_secret
    ):
        msg_id = "evt_pipe_reproc"
        ref = "ref_pipe_reproc"
        body = json.dumps(_payload(ref=ref)).encode()
        ts = _now_ts()
        headers = _headers(msg_id, ts)

        # Simulate the crash: record the ledger row (is_new) but DO NOT mark it
        # processed and DO NOT create the subscription — exactly the state a
        # crash between INSERT and dispatch leaves behind.
        from src.db.pg import subscription_store
        subs = subscription_store()
        pk, is_new, already = subs.record_webhook_event(
            vendor=_TEST_VENDOR,
            event_id=msg_id,
            event_type="thing.created",
            signature_valid=True,
            payload=_payload(ref=ref),
        )
        assert is_new is True and already is False
        # Pre-condition: no subscription yet (the prior run crashed before grant).
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s", (ref,))
            assert cur.fetchone()[0] == 0

        # The vendor retries the SAME event_id.  This delivery sees is_new=False
        # AND already_processed=False → it must RE-DISPATCH, not return duplicate.
        async with _client(app_with_secret) as client:
            resp = await client.post(
                "/api/webhooks/test", content=body, headers=headers
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("status") != "duplicate", (
            "a recorded-but-unprocessed event must RE-DISPATCH, not be deduped — "
            f"got {data}"
        )
        assert data.get("status") == "ok" and data.get("action") == "grant"

        # The grant self-healed: the subscription is now active, and the ledger
        # row is finally marked processed.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT status FROM subscriptions WHERE external_ref = %s", (ref,)
            )
            row = cur.fetchone()
        assert row is not None and row[0] == "active", (
            "re-dispatch must create the lost subscription (active)"
        )
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT processed_at FROM billing_webhook_events WHERE id = %s", (pk,)
            )
            assert cur.fetchone()[0] is not None, "re-dispatch must mark processed"

    @pytest.mark.asyncio
    async def test_fully_processed_replay_is_duplicate_no_redispatch(
        self, migrated_pg, app_with_secret
    ):
        """The OTHER side of #2: a replay of a FULLY-processed event is a true
        duplicate (200 'duplicate'), and must NOT dispatch a second time."""
        msg_id = "evt_pipe_done_replay"
        ref = "ref_pipe_done_replay"
        body = json.dumps(_payload(ref=ref)).encode()
        headers = _headers(msg_id, _now_ts())

        async with _client(app_with_secret) as client:
            r1 = await client.post("/api/webhooks/test", content=body, headers=headers)
            r2 = await client.post("/api/webhooks/test", content=body, headers=headers)
        assert r1.status_code == 200 and r1.json().get("status") == "ok"
        assert r2.status_code == 200
        assert r2.json().get("status") == "duplicate", (
            "a fully-processed replay must be a duplicate (no re-dispatch)"
        )
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s", (ref,))
            assert cur.fetchone()[0] == 1, "no second subscription on a finished replay"


# ---------------------------------------------------------------------------
# #6 transient dispatch error → 5xx, ledger NOT processed (vendor retries)
# ---------------------------------------------------------------------------


class TestTransientDispatchErrorRetries:
    """A TRANSIENT error during dispatch (OperationalError / pool timeout) must
    NOT mark the event processed and must return 5xx so the vendor RETRIES.
    Money rule: when unsure, retry — never drop the grant."""

    @pytest.mark.asyncio
    async def test_operational_error_returns_5xx_and_leaves_event_unprocessed(
        self, migrated_pg, app_with_secret, monkeypatch
    ):
        # Inject a TRANSIENT failure INSIDE dispatch (step 12) — where the
        # transient/permanent classification lives — by making the real
        # activation.grant_entitlement raise OperationalError.  resolve_plan is
        # left normal so the failure is unambiguously a dispatch-time transient.
        from psycopg2 import OperationalError

        def _transient_grant(grant, *, last_event_at=None):
            raise OperationalError("simulated transient DB failure")

        import src.billing.webhook_pipeline as wp
        monkeypatch.setattr(wp.activation, "grant_entitlement", _transient_grant)

        msg_id = "evt_pipe_transient"
        ref = "ref_pipe_transient"
        body = json.dumps(_payload(ref=ref)).encode()
        headers = _headers(msg_id, _now_ts())

        async with _client(app_with_secret) as client:
            resp = await client.post("/api/webhooks/test", content=body, headers=headers)

        assert resp.status_code >= 500, (
            f"a transient dispatch error must return 5xx for retry, got {resp.status_code}"
        )

        # Ledger row recorded (is_new) but NOT processed → the retry re-dispatches.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT processed_at FROM billing_webhook_events WHERE event_id = %s",
                (msg_id,),
            )
            row = cur.fetchone()
        assert row is not None, "the event must still be recorded for the retry to find"
        assert row[0] is None, (
            "a transient failure must NOT mark the event processed — the vendor "
            "retry must be able to re-dispatch it"
        )


# ---------------------------------------------------------------------------
# #6 permanent dispatch error → 200, ledger processed-with-error (no retry)
# ---------------------------------------------------------------------------


def _resolve_then_checkviolation(payload):
    """resolve_plan_fn that returns a real plan id but then the grant will fail
    permanently.  Here we instead raise the permanent error directly: a
    ValueError out of resolve goes down the config_error path, so to exercise the
    DISPATCH permanent branch we resolve fine then raise CheckViolation by
    grafting a poison status — done via a monkeypatched grant below."""
    from src.billing._db import slug_to_plan_id
    from src.db.pg import get_pool
    with get_pool().checkout() as conn:
        return slug_to_plan_id(_TEST_PLAN_SLUG, conn)


class TestPermanentDispatchErrorAcks:
    """A PERMANENT error during dispatch (CheckViolation / IntegrityError /
    ValueError on bad data) must mark the event processed-with-error and return
    200 so the vendor STOPS retrying a poison event."""

    @pytest.mark.asyncio
    async def test_checkviolation_returns_200_and_marks_processed_with_error(
        self, migrated_pg, app_with_secret, monkeypatch
    ):
        # Resolve a real plan, but make the activation.grant_entitlement raise a
        # CheckViolation (a poison payload that will NEVER satisfy a CHECK).
        _ADAPTER_OVERRIDES["resolve_plan_fn"] = _resolve_then_checkviolation

        from psycopg2.errors import CheckViolation

        def _boom_grant(grant, *, last_event_at=None):
            raise CheckViolation("simulated permanent CHECK violation")

        import src.billing.webhook_pipeline as wp
        monkeypatch.setattr(wp.activation, "grant_entitlement", _boom_grant)

        msg_id = "evt_pipe_permanent"
        ref = "ref_pipe_permanent"
        body = json.dumps(_payload(ref=ref)).encode()
        headers = _headers(msg_id, _now_ts())

        async with _client(app_with_secret) as client:
            resp = await client.post("/api/webhooks/test", content=body, headers=headers)

        assert resp.status_code == 200, (
            f"a permanent dispatch error must return 200 (stop vendor retry), "
            f"got {resp.status_code}: {resp.text}"
        )
        assert resp.json().get("status") == "error"

        # Marked processed WITH an error → ops-queryable, vendor stops retrying.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT processed_at, processing_error FROM billing_webhook_events"
                " WHERE event_id = %s",
                (msg_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] is not None, "a permanent error must MARK the event processed"
        assert row[1] is not None, "a permanent error must record processing_error"


# ---------------------------------------------------------------------------
# CR1 period propagate: grant payload period bounds reach the subscription row
# ---------------------------------------------------------------------------


class TestPeriodFieldsPropagate:
    """A grant whose payload carries current_period_start/end + trial_ends_at
    must persist those bounds on the subscription snapshot (CR1).  Before the
    fix the pipeline dropped them, so a subscriber's renewal date was unknown."""

    @pytest.mark.asyncio
    async def test_grant_persists_current_period_end(self, migrated_pg, app_with_secret):
        msg_id = "evt_pipe_period"
        ref = "ref_pipe_period"
        payload = _payload(ref=ref)
        payload["data"]["current_period_start"] = "2026-01-01T00:00:00Z"
        payload["data"]["current_period_end"] = "2026-02-01T00:00:00Z"
        payload["data"]["trial_ends_at"] = "2026-01-08T00:00:00Z"
        body = json.dumps(payload).encode()
        headers = _headers(msg_id, _now_ts())

        async with _client(app_with_secret) as client:
            resp = await client.post("/api/webhooks/test", content=body, headers=headers)
        assert resp.status_code == 200, resp.text

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT current_period_start, current_period_end, trial_ends_at"
                " FROM subscriptions WHERE external_ref = %s",
                (ref,),
            )
            row = cur.fetchone()
        assert row is not None, "grant must create the subscription"
        start, end, trial = row
        assert end is not None, "current_period_end must be persisted (CR1)"
        assert end.year == 2026 and end.month == 2, f"period end mis-stored: {end!r}"
        assert start is not None and start.month == 1, f"period start mis-stored: {start!r}"
        assert trial is not None and trial.day == 8, f"trial end mis-stored: {trial!r}"


# ---------------------------------------------------------------------------
# FIX-C interval extractor: the pipeline pulls the interval via the ADAPTER's
# extract_interval_fn (vendor field name lives in the adapter), normalizes it,
# and persists a non-NULL billing_interval — proving no field name is hard-coded.
# ---------------------------------------------------------------------------


class TestIntervalExtractedViaAdapter:
    """FIX-C: the pipeline must read the billing interval through the adapter's
    extract_interval_fn (this fake vendor carries it under 'cadence'), NOT a
    hard-coded 'billing_interval' key.  The normalized value must land on the
    subscription row (non-NULL)."""

    @pytest.mark.asyncio
    async def test_grant_persists_interval_from_adapter_field(
        self, migrated_pg, app_with_secret
    ):
        msg_id = "evt_pipe_interval"
        ref = "ref_pipe_interval"
        # _payload carries the cadence under 'cadence' (the fake vendor's field).
        body = json.dumps(_payload(ref=ref)).encode()
        headers = _headers(msg_id, _now_ts())

        async with _client(app_with_secret) as client:
            resp = await client.post("/api/webhooks/test", content=body, headers=headers)
        assert resp.status_code == 200, resp.text

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT billing_interval FROM subscriptions WHERE external_ref = %s",
                (ref,),
            )
            row = cur.fetchone()
        assert row is not None, "grant must create the subscription"
        assert row[0] == "monthly", (
            "the adapter's extract_interval_fn ('cadence'='m') must normalize to "
            f"'monthly' on the row — got {row[0]!r} (NULL ⇒ pipeline read the wrong field)"
        )


# ---------------------------------------------------------------------------
# #5 out-of-order: a stale (older last_event_at) grant must NOT regress state
# ---------------------------------------------------------------------------


class TestOutOfOrderGrantDoesNotRegress:
    """The monotonic guard (#5): a grant carrying an OLDER event timestamp than
    the stored last_event_at must NOT overwrite the newer state.  A delayed,
    out-of-order redelivery of an earlier event must not resurrect / regress a
    subscription to a stale snapshot."""

    @pytest.mark.asyncio
    async def test_older_event_does_not_overwrite_newer_seats(
        self, migrated_pg, app_with_secret
    ):
        ref = "ref_pipe_ooo"

        # 1) NEWER event: grant seats=5 with a recent timestamp.
        newer_ts = int(time.time())
        p_new = _payload(ref=ref)
        p_new["data"]["seats"] = 5
        body_new = json.dumps(p_new).encode()
        async with _client(app_with_secret) as client:
            r1 = await client.post(
                "/api/webhooks/test",
                content=body_new,
                headers=_headers("evt_ooo_new", str(newer_ts)),
            )
        assert r1.status_code == 200, r1.text

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT seats FROM subscriptions WHERE external_ref = %s", (ref,))
            assert cur.fetchone()[0] == 5

        # 2) OLDER event (different event_id so it is not deduped): grant seats=1
        #    with a timestamp 200s in the PAST.  The monotonic guard must keep the
        #    newer seats=5, NOT regress to seats=1.
        older_ts = newer_ts - 200
        p_old = _payload(ref=ref)
        p_old["data"]["seats"] = 1
        body_old = json.dumps(p_old).encode()
        async with _client(app_with_secret) as client:
            r2 = await client.post(
                "/api/webhooks/test",
                content=body_old,
                headers=_headers("evt_ooo_old", str(older_ts)),
            )
        assert r2.status_code == 200, r2.text

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT seats FROM subscriptions WHERE external_ref = %s", (ref,))
            seats = cur.fetchone()[0]
        assert seats == 5, (
            "an out-of-order OLDER grant must NOT regress seats — monotonic guard "
            f"(#5) failed: seats={seats}"
        )


# ---------------------------------------------------------------------------
# CR6 per-vendor rate limit: the bucket is keyed by vendor, not client IP
# ---------------------------------------------------------------------------


class TestPerVendorRateLimit:
    """CR6: the webhook rate-limit bucket is keyed by the (signed) vendor, not
    the client IP.  Behind nginx every delivery arrives from 127.0.0.1, so an
    IP-keyed bucket would be a single shared bucket throttling every vendor.
    Keying by vendor bounds load per vendor and frees the shared 127.0.0.1
    bucket for genuine traffic."""

    @pytest.mark.asyncio
    async def test_rate_limit_keys_on_vendor_not_ip(self, migrated_pg, app_with_secret):
        # Clear the limiter's global buckets so prior tests' erp traffic does not
        # pollute this low-limit window.
        import src.web_ui.rate_limit as rl
        rl._per_ip_buckets.clear()

        _ADAPTER_OVERRIDES["rate_limit_rpm"] = 2  # tiny ceiling for the test

        headers_ok = lambda mid: _headers(mid, _now_ts())  # noqa: E731

        async with _client(app_with_secret) as client:
            r1 = await client.post(
                "/api/webhooks/test",
                content=json.dumps(_payload(ref="ref_rl_1")).encode(),
                headers=headers_ok("evt_rl_1"),
            )
            r2 = await client.post(
                "/api/webhooks/test",
                content=json.dumps(_payload(ref="ref_rl_2")).encode(),
                headers=headers_ok("evt_rl_2"),
            )
            r3 = await client.post(
                "/api/webhooks/test",
                content=json.dumps(_payload(ref="ref_rl_3")).encode(),
                headers=headers_ok("evt_rl_3"),
            )

        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r3.status_code == 429, (
            "the 3rd delivery within the window must be rate-limited (limit=2)"
        )

        # The bucket the limiter used is the VENDOR key, not the client IP. Prove
        # it: a 'vendor:<vendor>' bucket exists and the 127.0.0.1 IP bucket does
        # not carry these webhook hits.
        assert f"vendor:{_TEST_VENDOR}" in rl._per_ip_buckets, (
            "rate-limit bucket must be keyed by vendor (CR6)"
        )
        assert "127.0.0.1" not in rl._per_ip_buckets, (
            "the verified webhook must NOT consume the shared client-IP bucket"
        )
