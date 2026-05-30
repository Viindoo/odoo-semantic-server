# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for src/billing/polar.py — Standard Webhooks verify + event mapping.

No database required.  All tests use a synthetic Standard-Webhooks vector:
  secret      = raw 32-byte secret (or whsec_-prefixed base64 variant)
  msg_id      = "msg_test_001"
  timestamp   = str(int(time.time()))   — fresh, within tolerance
  body        = b'{"type":"subscription.created","data":{"id":"sub_abc"}}'
  expected    = base64(HMAC-SHA256(secret_bytes, b"{msg_id}.{timestamp}." + body))
  sig_header  = f"v1,{expected}"
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest

from src.billing.polar import (
    EVENT_STATUS_MAP,
    _extract_product_id,
    map_subscription_status,
    normalize_billing_interval,
    parse_event,
    verify_signature,
)

# ---------------------------------------------------------------------------
# Helpers to build a valid Standard-Webhooks vector
# ---------------------------------------------------------------------------

_RAW_SECRET = b"super-secret-key-32-bytes-padded"  # 32 bytes raw
_MSG_ID = "msg_test_001"
_BODY = b'{"type":"subscription.created","data":{"id":"sub_abc"}}'


def _make_sig(secret_bytes: bytes, msg_id: str, timestamp: str, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}".encode() + b"." + body
    return base64.b64encode(
        hmac.new(secret_bytes, signed, hashlib.sha256).digest()
    ).decode()


def _fresh_ts() -> str:
    return str(int(time.time()))


def _valid_vector(
    secret_bytes: bytes | None = None,
    msg_id: str = _MSG_ID,
    body: bytes = _BODY,
    ts: str | None = None,
) -> tuple[str, str, str, bytes, str]:
    """Return (secret_str, msg_id, timestamp, body, sig_header) for a valid call."""
    sb = secret_bytes if secret_bytes is not None else _RAW_SECRET
    timestamp = ts if ts is not None else _fresh_ts()
    sig = _make_sig(sb, msg_id, timestamp, body)
    return sb.decode("latin-1"), msg_id, timestamp, body, f"v1,{sig}"


# ---------------------------------------------------------------------------
# I25: FROZEN known-answer vector — pins the signed-content WIRE FORMAT
# ---------------------------------------------------------------------------
#
# Every other test recomputes the expected signature with the SAME helper
# (_make_sig) that mirrors the implementation, so a format change (e.g. dropping
# the trailing "." separator, or reordering msg_id/timestamp) would change BOTH
# sides and the test would still pass — it can't catch a wire-format regression.
#
# This vector hardcodes the EXACT base64 signature as a literal constant,
# computed ONCE for a fixed (secret, msg_id, timestamp, body).  If anyone ever
# changes how the signed content is assembled, _FROZEN_SIG will no longer match
# and this test breaks — which is the point.
#
#   signed = b"{msg_id}.{timestamp}." + body  (Standard Webhooks)
#   _FROZEN_SIG = base64(HMAC_SHA256(_FROZEN_SECRET, signed))
#
_FROZEN_SECRET = b"frozen-test-secret-do-not-change!"   # fixed, do not edit
_FROZEN_MSG_ID = "msg_frozen_001"
_FROZEN_TS = "1700000000"                                # fixed past epoch
_FROZEN_BODY = b'{"type":"subscription.created","data":{"id":"sub_frozen"}}'
# Literal — paste-once, NEVER recomputed in the test. Pins the wire format.
_FROZEN_SIG = "Zahq6Nbz8NhQFRsdin+0F8GrMMJOoUM4NDCVpAf3HFE="


class TestFrozenSignatureVector:
    def test_frozen_known_answer_pins_wire_format(self):
        """The implementation must reproduce the pinned literal signature byte-for-byte.

        Reconstructs the expected signature from the implementation's documented
        format and asserts it equals the frozen literal — so a change to the
        signed-content assembly is caught even though every other test would
        silently follow the change.
        """
        recomputed = _make_sig(
            _FROZEN_SECRET, _FROZEN_MSG_ID, _FROZEN_TS, _FROZEN_BODY
        )
        assert recomputed == _FROZEN_SIG, (
            "signed-content wire format changed — frozen vector no longer matches. "
            "If this break is an INTENTIONAL format change, regenerate _FROZEN_SIG; "
            "otherwise a regression was introduced."
        )

    def test_frozen_vector_verifies_end_to_end(self):
        """verify_signature accepts the frozen literal (tolerance widened past the fixed ts).

        Proves the literal isn't just internally consistent with _make_sig but is
        what verify_signature actually accepts — the freshness window is widened
        so the fixed past timestamp is not rejected as stale.
        """
        # tolerance large enough to cover 'now - 1700000000'
        huge_tolerance = 10**12
        assert verify_signature(
            _FROZEN_SECRET.decode("latin-1"),
            msg_id=_FROZEN_MSG_ID,
            timestamp=_FROZEN_TS,
            body=_FROZEN_BODY,
            signature_header=f"v1,{_FROZEN_SIG}",
            tolerance_seconds=huge_tolerance,
        ) is True

    def test_frozen_vector_rejects_tampered_body(self):
        """The frozen signature must NOT verify against a one-byte-altered body."""
        huge_tolerance = 10**12
        assert verify_signature(
            _FROZEN_SECRET.decode("latin-1"),
            msg_id=_FROZEN_MSG_ID,
            timestamp=_FROZEN_TS,
            body=_FROZEN_BODY + b" ",
            signature_header=f"v1,{_FROZEN_SIG}",
            tolerance_seconds=huge_tolerance,
        ) is False


# ---------------------------------------------------------------------------
# Tests: verify_signature — PASS cases
# ---------------------------------------------------------------------------

class TestVerifySignaturePass:
    def test_valid_raw_secret(self):
        """A correctly computed signature with a raw (non-whsec_) secret passes."""
        ts = _fresh_ts()
        sig = _make_sig(_RAW_SECRET, _MSG_ID, ts, _BODY)
        assert verify_signature(
            _RAW_SECRET.decode("latin-1"),
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header=f"v1,{sig}",
            tolerance_seconds=300,
        ) is True

    def test_valid_whsec_prefixed_secret(self):
        """whsec_-prefixed base64 secret is correctly decoded and used for HMAC."""
        # Build a whsec_ secret from the raw bytes
        b64 = base64.b64encode(_RAW_SECRET).decode()
        whsec_secret = f"whsec_{b64}"

        ts = _fresh_ts()
        # Signature must be computed with the decoded raw bytes
        sig = _make_sig(_RAW_SECRET, _MSG_ID, ts, _BODY)
        assert verify_signature(
            whsec_secret,
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header=f"v1,{sig}",
            tolerance_seconds=300,
        ) is True

    def test_multiple_tokens_in_header_one_matches(self):
        """Header with multiple tokens (key rotation) passes when one matches."""
        ts = _fresh_ts()
        sig = _make_sig(_RAW_SECRET, _MSG_ID, ts, _BODY)
        # Add a bogus v1 token before the real one
        header = f"v1,AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= v1,{sig}"
        assert verify_signature(
            _RAW_SECRET.decode("latin-1"),
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header=header,
            tolerance_seconds=300,
        ) is True

    def test_whsec_decoded_differs_from_encoded(self):
        """Prove whsec_ and the un-prefixed b64-string are treated differently.

        If whsec_ decoding is skipped the HMAC key would be the base64 chars
        (wrong) → signature computed with the decoded raw bytes would NOT match.
        This test confirms the decode path is exercised.
        """
        b64 = base64.b64encode(_RAW_SECRET).decode()
        whsec_secret = f"whsec_{b64}"

        ts = _fresh_ts()
        # This sig is computed with raw _RAW_SECRET bytes (correct decode)
        sig_correct = _make_sig(_RAW_SECRET, _MSG_ID, ts, _BODY)

        # If we use b64 bytes as HMAC key (wrong — no decode) the sig differs
        sig_wrong = _make_sig(b64.encode(), _MSG_ID, ts, _BODY)
        assert sig_correct != sig_wrong, "Test setup error: sigs should differ"

        # Verify with whsec_ → should match sig_correct (decoded bytes)
        assert verify_signature(
            whsec_secret,
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header=f"v1,{sig_correct}",
            tolerance_seconds=300,
        ) is True

        # Verify with whsec_ → should NOT match sig computed with b64 bytes
        assert verify_signature(
            whsec_secret,
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header=f"v1,{sig_wrong}",
            tolerance_seconds=300,
        ) is False


# ---------------------------------------------------------------------------
# Tests: verify_signature — FAIL cases
# ---------------------------------------------------------------------------

class TestVerifySignatureFail:
    def test_tampered_body(self):
        """Signature computed over original body does not verify against modified body."""
        ts = _fresh_ts()
        sig = _make_sig(_RAW_SECRET, _MSG_ID, ts, _BODY)
        tampered = _BODY + b"EXTRA"
        assert verify_signature(
            _RAW_SECRET.decode("latin-1"),
            msg_id=_MSG_ID,
            timestamp=ts,
            body=tampered,
            signature_header=f"v1,{sig}",
            tolerance_seconds=300,
        ) is False

    def test_stale_timestamp(self):
        """Timestamp older than tolerance_seconds is rejected (replay guard)."""
        tolerance = 300
        stale_ts = str(int(time.time()) - 10 * tolerance)  # 50 minutes old
        sig = _make_sig(_RAW_SECRET, _MSG_ID, stale_ts, _BODY)
        assert verify_signature(
            _RAW_SECRET.decode("latin-1"),
            msg_id=_MSG_ID,
            timestamp=stale_ts,
            body=_BODY,
            signature_header=f"v1,{sig}",
            tolerance_seconds=tolerance,
        ) is False

    def test_future_timestamp_beyond_tolerance(self):
        """Timestamp far in the future is also rejected (clock-skew attack guard)."""
        tolerance = 300
        future_ts = str(int(time.time()) + 10 * tolerance)
        sig = _make_sig(_RAW_SECRET, _MSG_ID, future_ts, _BODY)
        assert verify_signature(
            _RAW_SECRET.decode("latin-1"),
            msg_id=_MSG_ID,
            timestamp=future_ts,
            body=_BODY,
            signature_header=f"v1,{sig}",
            tolerance_seconds=tolerance,
        ) is False

    def test_wrong_version_token(self):
        """Token with version != 'v1' (e.g. 'v2') is not accepted."""
        ts = _fresh_ts()
        sig = _make_sig(_RAW_SECRET, _MSG_ID, ts, _BODY)
        # Present same base64 signature but tagged as v2
        assert verify_signature(
            _RAW_SECRET.decode("latin-1"),
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header=f"v2,{sig}",
            tolerance_seconds=300,
        ) is False

    def test_empty_signature_header(self):
        """Empty signature_header returns False without raising."""
        ts = _fresh_ts()
        assert verify_signature(
            _RAW_SECRET.decode("latin-1"),
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header="",
            tolerance_seconds=300,
        ) is False

    def test_empty_secret(self):
        """Empty secret string returns False without raising."""
        ts = _fresh_ts()
        sig = _make_sig(_RAW_SECRET, _MSG_ID, ts, _BODY)
        assert verify_signature(
            "",
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header=f"v1,{sig}",
            tolerance_seconds=300,
        ) is False

    def test_non_integer_timestamp(self):
        """Non-integer timestamp string returns False without raising."""
        ts_bad = "not-a-number"
        sig = _make_sig(_RAW_SECRET, _MSG_ID, ts_bad, _BODY)
        assert verify_signature(
            _RAW_SECRET.decode("latin-1"),
            msg_id=_MSG_ID,
            timestamp=ts_bad,
            body=_BODY,
            signature_header=f"v1,{sig}",
            tolerance_seconds=300,
        ) is False

    def test_wrong_secret(self):
        """Signature verified with a different secret returns False."""
        ts = _fresh_ts()
        other_secret = b"a-completely-different-secret!!!"
        sig = _make_sig(other_secret, _MSG_ID, ts, _BODY)
        assert verify_signature(
            _RAW_SECRET.decode("latin-1"),
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header=f"v1,{sig}",
            tolerance_seconds=300,
        ) is False

    def test_header_only_non_versioned_token(self):
        """Header with a token that has no comma separator returns False."""
        ts = _fresh_ts()
        assert verify_signature(
            _RAW_SECRET.decode("latin-1"),
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header="somerawtoken",
            tolerance_seconds=300,
        ) is False

    def test_malformed_whsec_returns_false(self):
        """Invalid base64 after whsec_ prefix returns False without raising."""
        ts = _fresh_ts()
        sig = _make_sig(_RAW_SECRET, _MSG_ID, ts, _BODY)
        assert verify_signature(
            "whsec_!!!not-valid-base64!!!",
            msg_id=_MSG_ID,
            timestamp=ts,
            body=_BODY,
            signature_header=f"v1,{sig}",
            tolerance_seconds=300,
        ) is False


# ---------------------------------------------------------------------------
# Tests: parse_event
# ---------------------------------------------------------------------------

class TestParseEvent:
    def _base_payload(self, event_type: str = "subscription.created") -> dict:
        return {
            "id": "evt_001",
            "type": event_type,
            "data": {"id": "sub_xyz_123"},
        }

    def test_returns_correct_tuple(self):
        payload = self._base_payload()
        event_id, event_type, external_ref = parse_event(payload)
        assert event_id == "evt_001"
        assert event_type == "subscription.created"
        assert external_ref == "sub_xyz_123"

    def test_fallback_to_webhook_id_key(self):
        """When payload has no 'id' but has 'webhook_id', use webhook_id."""
        payload = {
            "webhook_id": "whk_001",
            "type": "order.paid",
            "data": {"id": "order_abc"},
        }
        event_id, event_type, external_ref = parse_event(payload)
        assert event_id == "whk_001"
        assert event_type == "order.paid"
        assert external_ref == "order_abc"

    def test_missing_type_raises(self):
        payload = {"id": "evt_x", "data": {"id": "sub_x"}}
        with pytest.raises(ValueError, match="type"):
            parse_event(payload)

    def test_missing_event_id_raises(self):
        payload = {"type": "subscription.active", "data": {"id": "sub_x"}}
        with pytest.raises(ValueError, match="event_id"):
            parse_event(payload)

    def test_missing_data_raises(self):
        payload = {"id": "evt_x", "type": "subscription.active"}
        with pytest.raises(ValueError, match="data"):
            parse_event(payload)

    def test_missing_data_id_raises(self):
        payload = {"id": "evt_x", "type": "subscription.active", "data": {}}
        with pytest.raises(ValueError, match="data.*id"):
            parse_event(payload)

    def test_data_not_dict_raises(self):
        payload = {"id": "evt_x", "type": "subscription.active", "data": "bad"}
        with pytest.raises(ValueError, match="data"):
            parse_event(payload)


# ---------------------------------------------------------------------------
# Tests: EVENT_STATUS_MAP coverage
# ---------------------------------------------------------------------------

class TestEventStatusMap:
    def test_grant_events(self):
        for event in ("subscription.created", "subscription.active", "order.paid"):
            assert EVENT_STATUS_MAP[event] == "grant", f"{event!r} should be 'grant'"

    def test_update_events(self):
        # subscription.updated plus the two contract-hardening additions: a
        # past_due and an uncanceled both route through "update" so the snapshot
        # re-reads data.status (past_due → terminal downgrade; uncanceled →
        # status=active + cancel_at_period_end reconciled).
        for event in (
            "subscription.updated",
            "subscription.past_due",
            "subscription.uncanceled",
        ):
            assert EVENT_STATUS_MAP[event] == "update", f"{event!r} should be 'update'"

    def test_revoke_events(self):
        for event in ("subscription.canceled", "subscription.revoked", "order.refunded"):
            assert EVENT_STATUS_MAP[event] == "revoke", f"{event!r} should be 'revoke'"

    def test_all_values_are_valid_actions(self):
        valid_actions = {"grant", "update", "revoke"}
        for event_type, action in EVENT_STATUS_MAP.items():
            assert action in valid_actions, (
                f"EVENT_STATUS_MAP[{event_type!r}] = {action!r} is not a valid action"
            )

    def test_no_unknown_keys(self):
        """All keys in the map are strings; no None or empty."""
        for k in EVENT_STATUS_MAP:
            assert isinstance(k, str) and k, f"Invalid key in EVENT_STATUS_MAP: {k!r}"

    def test_nine_entries(self):
        """Map has exactly 9 entries — the P1 event surface (7) plus the two
        contract-hardening additions (subscription.past_due / .uncanceled)."""
        assert len(EVENT_STATUS_MAP) == 9


# ---------------------------------------------------------------------------
# Product-id extraction — tolerant of BOTH the flat (subscription) and nested
# (order.paid) Polar shapes.  Pure unit, no DB.
# ---------------------------------------------------------------------------

class TestExtractProductId:
    def test_flat_product_id(self):
        """Subscription events carry a flat data.product_id."""
        assert _extract_product_id({"product_id": "prod_flat"}) == "prod_flat"

    def test_nested_product_id(self):
        """order.paid carries the product as a nested data.product object."""
        assert _extract_product_id({"product": {"id": "prod_nested"}}) == "prod_nested"

    def test_flat_wins_when_both_present(self):
        """If both shapes are present the flat product_id is preferred (and both
        should reference the same product anyway)."""
        assert (
            _extract_product_id(
                {"product_id": "prod_flat", "product": {"id": "prod_nested"}}
            )
            == "prod_flat"
        )

    def test_missing_returns_none(self):
        assert _extract_product_id({}) is None
        assert _extract_product_id({"product": "not-a-dict"}) is None
        assert _extract_product_id({"product": {}}) is None
        assert _extract_product_id({"product_id": ""}) is None


class TestResolvePlanIdProductShapes:
    """resolve_plan_id resolves product id from BOTH shapes; hermetic (stub conn)."""

    def _patch_lookup(self, monkeypatch, *, product_map):
        # Stub get_setting (used inside _resolve_with_conn via a local import of
        # src.settings.get_setting) and slug_to_plan_id so no DB is touched.
        import src.billing._db as billing_db
        import src.billing.polar as polar_mod
        import src.settings as settings_mod

        monkeypatch.setattr(
            settings_mod, "get_setting",
            lambda key, conn=None: product_map if key == "billing.polar_product_map" else None,
        )
        monkeypatch.setattr(billing_db, "slug_to_plan_id", lambda slug, conn: 42)
        return polar_mod

    def test_resolves_from_flat_product_id(self, monkeypatch):
        polar_mod = self._patch_lookup(monkeypatch, product_map={"prod_flat": "pro"})
        payload = {"data": {"id": "sub_1", "product_id": "prod_flat"}}
        assert polar_mod.resolve_plan_id(payload, conn=object()) == 42

    def test_resolves_from_nested_product_id(self, monkeypatch):
        polar_mod = self._patch_lookup(monkeypatch, product_map={"prod_nested": "pro"})
        payload = {"data": {"id": "ord_1", "product": {"id": "prod_nested"}}}
        assert polar_mod.resolve_plan_id(payload, conn=object()) == 42

    def test_missing_product_id_raises_clear_error(self, monkeypatch):
        polar_mod = self._patch_lookup(monkeypatch, product_map={})
        payload = {"data": {"id": "sub_1"}}
        with pytest.raises(ValueError, match="product id missing"):
            polar_mod.resolve_plan_id(payload, conn=object())


# ---------------------------------------------------------------------------
# I8: field normalizers — billing interval + subscription status
# ---------------------------------------------------------------------------

class TestNormalizeBillingInterval:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("month", "monthly"),
            ("monthly", "monthly"),
            ("MONTH", "monthly"),
            ("year", "annual"),
            ("annual", "annual"),
            ("yearly", "annual"),
            ("one_time", "one_time"),
            ("one-time", "one_time"),
            ("ONE-TIME", "one_time"),
            ("free", "free"),
            ("  month  ", "monthly"),
        ],
    )
    def test_known_intervals_map_to_enum(self, raw, expected):
        assert normalize_billing_interval(raw) == expected

    @pytest.mark.parametrize("raw", ["day", "daily", "week", "weekly", "  WEEK "])
    def test_day_and_week_fall_back_to_monthly(self, raw):
        """FIX-D: Polar's day/week recurring_interval has no own enum value; it
        falls back to 'monthly' (a valid CHECK value) so a paid grant is never
        dropped on a NULL/CHECK violation.  OWNER-FLAG: revisit if we sell
        day/week products."""
        assert normalize_billing_interval(raw) == "monthly"

    @pytest.mark.parametrize("raw", [None, "", "   ", 5, {}, ["x"]])
    def test_missing_recurring_interval_is_one_time(self, raw):
        """FIX-D: Polar sends recurring_interval=null (or omits it) for a ONE-TIME
        order.  A non-string / empty value must map to 'one_time', NOT NULL — a
        NULL would lose the one-time purchase classification on the subscription
        snapshot."""
        assert normalize_billing_interval(raw) == "one_time"

    @pytest.mark.parametrize("raw", ["biennial", "bogus", "fortnightly"])
    def test_unknown_recurring_string_returns_none(self, raw):
        """A non-empty but UNRECOGNISED interval string maps to None so the caller
        stores NULL (CHECK permits NULL) rather than a value that would violate
        the constraint — a forward-compat guard for a new Polar enum value."""
        assert normalize_billing_interval(raw) is None


class TestMapSubscriptionStatus:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("active", "active"),
            ("trialing", "trialing"),
            ("trial", "trialing"),
            ("past_due", "past_due"),
            # Polar 'unpaid' is a TERMINAL payment failure → 'expired' (money-safe
            # downgrade), NOT 'past_due' (which is a retry-in-progress).
            ("unpaid", "expired"),
            ("canceled", "cancelled"),   # US spelling
            ("cancelled", "cancelled"),
            ("revoked", "cancelled"),
            ("expired", "expired"),
            ("ended", "expired"),
            ("refunded", "refunded"),
            ("incomplete", "pending"),
            ("ACTIVE", "active"),
        ],
    )
    def test_known_statuses_map_to_enum(self, raw, expected):
        assert map_subscription_status({"data": {"status": raw}}) == expected

    def test_unpaid_maps_to_terminal_expired_not_past_due(self):
        """Polar 'unpaid' is a definitive payment failure (terminal), not a
        dunning retry.  It MUST map to 'expired' (a terminal status that drives
        the CR3 key downgrade) and NOT to 'past_due' — leaving a non-paying
        subscriber on 'past_due' would keep full paid access while not paying."""
        assert map_subscription_status({"data": {"status": "unpaid"}}) == "expired"
        assert map_subscription_status({"data": {"status": "UNPAID"}}) == "expired"

    def test_unknown_status_defaults_to_active(self):
        assert map_subscription_status({"data": {"status": "weird"}}) == "active"

    def test_missing_status_defaults_to_active(self):
        assert map_subscription_status({"data": {}}) == "active"

    def test_missing_data_defaults_to_active(self):
        assert map_subscription_status({}) == "active"

    def test_all_mapped_values_are_valid_db_enum(self):
        valid = {
            "pending", "active", "past_due", "cancelled",
            "expired", "trialing", "refunded",
        }
        for raw in (
            "active", "trialing", "trial", "past_due", "unpaid", "canceled",
            "cancelled", "revoked", "expired", "ended", "refunded",
            "incomplete", "incomplete_expired", "pending", "weird-unknown",
        ):
            assert map_subscription_status({"data": {"status": raw}}) in valid


# ---------------------------------------------------------------------------
# Outbound cancel HTTP contract (confirmed vs https://docs.polar.sh 2026-05-30).
#   cancel-at-period-end : PATCH {"cancel_at_period_end": true}   (Update API)
#   immediate            : DELETE (no body)                       (Revoke API)
# Polar has NO PATCH {"revoke": true} endpoint — the dedicated immediate-cancel
# is DELETE /v1/subscriptions/{id} with no request body.  These tests intercept
# the httpx request to assert the method + JSON body, with NO real network call
# (no DB either).
# ---------------------------------------------------------------------------


class _CapturingResponse:
    """Minimal httpx-response stand-in returning a 200 JSON body."""

    status_code = 200
    text = "{}"

    def json(self):
        return {"id": "sub_x", "status": "revoked"}


class _CapturingAsyncClient:
    """Records the (method, url, json) of the single request the cancel makes."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, *, headers=None, json=None):
        type(self).captured = {"method": method, "url": url, "json": json,
                               "headers": headers}
        return _CapturingResponse()


class TestOutboundCancelHttpContract:
    @pytest.mark.asyncio
    async def test_immediate_cancel_uses_delete_with_no_body(self, monkeypatch):
        """An immediate cancel (at_period_end=False) must issue a DELETE with NO
        body — Polar's dedicated Revoke endpoint is DELETE /v1/subscriptions/{id}
        (there is no PATCH {"revoke": true}).  A wrong method/body would 404/405
        and leave the user still billed."""
        import src.billing.polar_api as polar_api
        from src.web_ui import config as web_config

        monkeypatch.setattr(web_config, "POLAR_API_KEY", "polar_test_token")
        monkeypatch.setattr(polar_api.httpx, "AsyncClient", _CapturingAsyncClient)
        _CapturingAsyncClient.captured = {}

        result = await polar_api.cancel_subscription("sub_imm_001", at_period_end=False)

        cap = _CapturingAsyncClient.captured
        assert cap["method"] == "DELETE", (
            f"immediate cancel must DELETE (Polar's Revoke endpoint), got {cap['method']!r}"
        )
        assert cap["json"] is None, (
            f"immediate cancel must send NO body (json=None), got {cap['json']!r}"
        )
        assert cap["url"].endswith("/v1/subscriptions/sub_imm_001")
        assert result == {"id": "sub_x", "status": "revoked"}

    @pytest.mark.asyncio
    async def test_period_end_cancel_uses_patch_with_schedule_flag(self, monkeypatch):
        """The at_period_end path (the in-app default) stays PATCH
        {"cancel_at_period_end": true} — unchanged by FIX-E."""
        import src.billing.polar_api as polar_api
        from src.web_ui import config as web_config

        monkeypatch.setattr(web_config, "POLAR_API_KEY", "polar_test_token")
        monkeypatch.setattr(polar_api.httpx, "AsyncClient", _CapturingAsyncClient)
        _CapturingAsyncClient.captured = {}

        await polar_api.cancel_subscription("sub_pe_001", at_period_end=True)

        cap = _CapturingAsyncClient.captured
        assert cap["method"] == "PATCH"
        assert cap["json"] == {"cancel_at_period_end": True}, (
            f"period-end cancel body must set cancel_at_period_end, got {cap['json']!r}"
        )
