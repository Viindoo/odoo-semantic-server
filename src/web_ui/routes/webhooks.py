# SPDX-License-Identifier: AGPL-3.0-or-later
"""Polar.sh webhook sink — POST /api/webhooks/polar (public, auth-exempt).

Security model (per design §4.2 + §11):
  - HMAC-SHA256 Standard Webhooks signature verified BEFORE any DB write of
    business state.  Signature failure → 400; secret absent → 503 (fail-closed).
  - ALL events are recorded to the billing_webhook_events ledger (idempotency +
    forensics), even signature-invalid ones (with signature_valid=FALSE).
  - Duplicate (vendor, event_id) → 200 {"status":"duplicate"} — Polar safely
    retries idempotent deliveries.
  - Unmapped event_type → 200 {"status":"ignored"} — unmapped events do not error
    so Polar stops retrying; logged at WARNING with a descriptive processing_error
    on the ledger row so a forgotten mapping is ops-visible, never silent.
  - Unknown Polar product / unconfigured product map → processing_error set in
    ledger, ERROR log, 200 {"status":"config_error"} returned (we accept the
    delivery, we just can't map it; ops must fix billing.polar_product_map).  Only
    grant/update resolve a plan; a revoke (cancel/refund) carries no product_id
    and is never blocked by resolution.
  - IP rate limit (billing.webhook_rate_limit_rpm, default 120/min/IP) protects
    against flood attacks.

This route is a thin Polar binding: it builds a ``WebhookAdapter`` (signature
verifier, parser, event→action map, plan resolver, email/status/interval helpers)
and delegates to the vendor-agnostic ``src.billing.webhook_pipeline``.  All the
processing semantics above live once in that pipeline so a second payment adapter
(Paddle/ERP) reuses them unchanged.  Payload fields only ever flow as
%s-parameterised values (ADR-0039 §11 SQLi guard).
"""
import logging
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.billing import polar
from src.billing.webhook_pipeline import WebhookAdapter, run_webhook_pipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Polar-specific helpers (payload extractors + settings readers)
# ---------------------------------------------------------------------------


def _extract_buyer_email(data: dict) -> str | None:
    """Best-effort extraction of buyer email from a Polar payload data dict.

    Tolerant of every shape Polar may emit (the nested ``customer.email`` is
    confirmed for subscription events but unconfirmed over the wire — see
    01-polar-contract-verification.md), and never crashes when email is absent
    (it is optional/nullable on some events):

      1. ``data.customer.email`` (nested customer object — full schema);
      2. ``data.customer_email`` (flat fallback — simplified payloads);
      3. ``data.user.email`` (legacy/alternate user object).
    """
    # 1. Nested customer object: data.customer.email
    customer = data.get("customer")
    if isinstance(customer, dict):
        email = customer.get("email")
        if email:
            return str(email)
    # 2. Flat field: data.customer_email
    email = data.get("customer_email")
    if email:
        return str(email)
    # 3. Nested user object: data.user.email
    user = data.get("user")
    if isinstance(user, dict):
        email = user.get("email")
        if email:
            return str(email)
    return None


def _extract_cancel_at_period_end(data: dict) -> bool | None:
    """Vendor cancel-at-period-end flag from a Polar payload, or None if absent.

    Polar carries ``data.cancel_at_period_end`` (bool).  Returning ``None`` when
    the field is missing leaves the local schedule flag untouched (partial-write
    contract); when present we coerce to a strict bool so a
    ``subscription.uncanceled`` (flag=False) reconciles a locally-scheduled
    cancel and a scheduling update (flag=True) re-records it.
    """
    raw = data.get("cancel_at_period_end")
    if raw is None:
        return None
    return bool(raw)


def _extract_seats(data: dict) -> int:
    """Seat count from a Polar payload (default 1).

    Polar carries the quantity under ``seats`` (older) or ``quantity`` (newer
    SDK); accept either.  A non-int / absent / non-positive value falls back to 1
    so a grant is never built with an invalid seat count that the seats>0 CHECK
    would reject.
    """
    raw = data.get("seats")
    if raw is None:
        raw = data.get("quantity")
    try:
        seats = int(raw)
    except (TypeError, ValueError):
        return 1
    return seats if seats > 0 else 1


def _extract_amount(data: dict) -> int | None:
    """Amount in minor units (cents) from a Polar payload, or None.

    Reads ``amount`` (Polar's subscription/order amount in cents).  A non-int
    value yields None so we store NULL rather than a bad value.
    """
    raw = data.get("amount")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _extract_interval(data: dict):
    """Raw Polar billing-interval token from a payload data dict, or None.

    Polar carries the recurring cadence under ``recurring_interval`` (enum
    ``day|week|month|year``; ``null`` for a one-time order).  Some older payloads
    nested it under ``price.recurring_interval`` — accept that as a fallback.  The
    RAW token is returned unchanged; ``polar.normalize_billing_interval`` maps it
    to our enum.  Returning ``None`` (no recurring interval) → normalizer yields
    ``one_time``.
    """
    raw = data.get("recurring_interval")
    if raw is None:
        price = data.get("price")
        if isinstance(price, dict):
            raw = price.get("recurring_interval")
    return raw


def _extract_currency(data: dict) -> str | None:
    """ISO-4217 currency from a Polar payload, upper-cased, or None.

    The subscriptions.currency CHECK requires ``^[A-Z]{3}$``; we upper-case here
    so a lower-case ``usd`` from the vendor does not silently violate it.  A
    value that is not a 3-letter string yields None (store NULL, CHECK permits).
    """
    raw = data.get("currency")
    if not isinstance(raw, str):
        return None
    cur = raw.strip().upper()
    return cur if len(cur) == 3 and cur.isalpha() else None


def _parse_ts(value) -> datetime | None:
    """Parse an ISO-8601 / Unix-epoch timestamp into an aware ``datetime``, or None.

    Polar carries period bounds as ISO-8601 strings (``2026-01-01T00:00:00Z``).
    Accepts the trailing ``Z``, an existing offset, or a Unix-epoch int/float.
    Returns ``None`` for anything unparseable so a bad value stores NULL rather
    than raising mid-dispatch.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Normalize a trailing 'Z' (UTC) to the +00:00 offset fromisoformat wants.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _extract_period(data: dict) -> tuple:
    """(current_period_start, current_period_end, trial_ends_at) as datetimes/None.

    Reads Polar's ``current_period_start`` / ``current_period_end`` and the trial
    end (``trial_ends_at`` or ``ends_at`` of a trial).  Each is parsed leniently
    (``_parse_ts``); an absent/unparseable value yields ``None`` so the
    subscription snapshot stores NULL for that bound.
    """
    start = _parse_ts(data.get("current_period_start"))
    end = _parse_ts(data.get("current_period_end"))
    trial = _parse_ts(data.get("trial_ends_at"))
    return start, end, trial


def _extract_event_at(headers, payload: dict) -> datetime | None:
    """Vendor event timestamp for the monotonic out-of-order guard (#5).

    Standard Webhooks deliver the signed event time in the ``webhook-timestamp``
    header (Unix-epoch seconds); prefer it because it is part of the signed
    envelope (tamper-evident).  Fall back to a payload ``modified_at`` /
    ``created_at`` field if the header is missing.  Returns ``None`` when neither
    yields a parseable value → the registry guard treats NULL as "no ordering
    info" and lets the write through (last-write), which is the safe default.
    """
    ts_header = headers.get(polar.HEADER_TIMESTAMP) if headers is not None else None
    parsed = _parse_ts(ts_header)
    if parsed is not None:
        return parsed
    data = payload.get("data")
    if isinstance(data, dict):
        return _parse_ts(data.get("modified_at") or data.get("created_at"))
    return None


def _get_webhook_tolerance() -> int:
    """Read billing.webhook_tolerance_seconds from settings (default 300)."""
    try:
        from src.settings import get_setting
        val = get_setting("billing.webhook_tolerance_seconds")
        return int(val) if val is not None else 300
    except Exception:
        return 300


def _get_webhook_rate_limit() -> int:
    """Read billing.webhook_rate_limit_rpm from settings (default 120)."""
    try:
        from src.settings import get_setting
        val = get_setting("billing.webhook_rate_limit_rpm")
        return int(val) if val is not None else 120
    except Exception:
        return 120


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------


def _build_polar_adapter() -> WebhookAdapter:
    """Bind the Polar.sh vendor specifics to the shared webhook pipeline.

    The pipeline (rate-limit, ledger, dedup, dispatch, mark-processed) is
    vendor-agnostic and lives in ``src.billing.webhook_pipeline``; this builder
    supplies only the Polar-specific pieces — secret source, header names,
    signature verifier, parser, event→action map, plan resolver, and the
    buyer-email / status / interval extractors.  A future ``paddle_webhook`` is
    the same ~25 lines of glue against its own ``polar``-equivalent module.
    """
    from src.web_ui import config as web_config

    return WebhookAdapter(
        vendor="polar",
        secret=web_config.POLAR_WEBHOOK_SECRET,
        tolerance_seconds=_get_webhook_tolerance(),
        rate_limit_rpm=_get_webhook_rate_limit(),
        header_id=polar.HEADER_ID,
        header_timestamp=polar.HEADER_TIMESTAMP,
        header_signature=polar.HEADER_SIGNATURE,
        verify_fn=polar.verify_signature,
        parse_event_fn=polar.parse_event,
        event_action_fn=polar.EVENT_STATUS_MAP.get,
        resolve_plan_fn=polar.resolve_plan_id,
        extract_email_fn=_extract_buyer_email,
        map_status_fn=polar.map_subscription_status,
        normalize_interval_fn=polar.normalize_billing_interval,
        extract_cancel_at_period_end_fn=_extract_cancel_at_period_end,
        extract_interval_fn=_extract_interval,
        extract_seats_fn=_extract_seats,
        extract_amount_fn=_extract_amount,
        extract_currency_fn=_extract_currency,
        extract_period_fn=_extract_period,
        extract_event_at_fn=_extract_event_at,
    )


@router.post("/polar")
async def polar_webhook(request: Request) -> JSONResponse:
    """Receive and process a Polar.sh webhook event.

    Thin vendor binding: builds the Polar :class:`WebhookAdapter` and delegates
    to the shared 13-step :func:`run_webhook_pipeline`.  All processing semantics
    (rate-limit, fail-closed secret, signature guard, ledger, dedup, event→action
    mapping, plan resolution, grant/update/revoke dispatch, mark-processed) are
    identical to the prior inline implementation — they now live once in the
    pipeline so a second payment adapter reuses them unchanged.
    """
    adapter = _build_polar_adapter()
    return await run_webhook_pipeline(adapter, request)
