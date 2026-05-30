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
        assert EVENT_STATUS_MAP["subscription.updated"] == "update"

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

    def test_seven_entries(self):
        """Map has exactly 7 entries — the full P1 event surface."""
        assert len(EVENT_STATUS_MAP) == 7


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

    @pytest.mark.parametrize("raw", ["weekly", "biennial", "", "bogus", None, 5, {}])
    def test_unknown_or_non_string_returns_none(self, raw):
        assert normalize_billing_interval(raw) is None


class TestMapSubscriptionStatus:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("active", "active"),
            ("trialing", "trialing"),
            ("trial", "trialing"),
            ("past_due", "past_due"),
            ("unpaid", "past_due"),
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
