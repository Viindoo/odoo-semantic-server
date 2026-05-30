# SPDX-License-Identifier: AGPL-3.0-or-later
"""Polar.sh webhook adapter — Standard Webhooks (https://www.standardwebhooks.com).

This module implements the Standard Webhooks signature-verification scheme that
Polar.sh uses for outgoing webhooks, plus event-type → internal-action mapping
and plan resolution.

FLAG — CONFIRM BEFORE PRODUCTION:
  The following constants and field paths MUST be verified against the live Polar
  webhook documentation (https://docs.polar.sh/webhooks) and/or a captured sample
  payload before merging to production.  The module is intentionally structured so
  every Polar-specific detail lives here (constants at top, field paths in helpers),
  making the confirmation sweep straightforward:

  (a) Header names — Standard Webhooks canonical: ``webhook-id`` / ``webhook-timestamp``
      / ``webhook-signature``.  Polar historically also emitted ``x-polar-*`` variants;
      confirm which the current Polar SDK emits.

  (b) Secret encoding — whsec_ prefix + base64 body (Standard Webhooks convention);
      confirm Polar's dashboard exports secrets in this exact format.

  (c) Event-type spellings — ``subscription.canceled`` (US spelling), ``order.paid``
      vs ``order.created``; confirm each against the current Polar event catalogue.

  (d) Payload field paths — ``data.id`` as the external_ref, ``data.product_id`` for
      plan resolution, ``data.customer.email`` for buyer email; confirm against a real
      captured sample.

  All four items can be confirmed by:
    1. Registering a test endpoint in the Polar dashboard → send a test event.
    2. Reading https://docs.polar.sh/api/v1 (webhooks section).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (FLAG: confirm all against live Polar docs — see module docstring)
# ---------------------------------------------------------------------------

# Standard Webhooks header names (canonical).
# FLAG (a): verify Polar uses these exact names, not "x-polar-*" variants.
HEADER_ID = "webhook-id"
HEADER_TIMESTAMP = "webhook-timestamp"
HEADER_SIGNATURE = "webhook-signature"

# whsec_ prefix used by Standard Webhooks-compatible vendors.
# FLAG (b): confirm Polar dashboard exports secrets with this prefix.
_WHSEC_PREFIX = "whsec_"

# Signature algorithm token in the "v1,<b64sig>" format.
_SIG_VERSION = "v1"

# Payload field paths.
# FLAG (d): confirm against a real captured sample from Polar.
_FIELD_EVENT_TYPE = "type"                  # payload["type"]
_FIELD_EXTERNAL_REF = ("data", "id")        # payload["data"]["id"]
_FIELD_PRODUCT_ID = ("data", "product_id")  # payload["data"]["product_id"]


# ---------------------------------------------------------------------------
# Event-type → action map
# FLAG (c): confirm event-type spellings against Polar's event catalogue.
# ---------------------------------------------------------------------------

# SSOT for the event-type → internal-action routing.  An event type that is
# NOT a key here is an UNKNOWN event: the route layer records it in the
# billing_webhook_events ledger (ops-visible) and acks it WITHOUT dispatching —
# unknown events are never silently dropped, just not acted upon.  Keep this map
# the single place that decides grant/update/revoke.
EVENT_STATUS_MAP: dict[str, str] = {
    # Grant (create or reactivate entitlement)
    "subscription.created": "grant",
    "subscription.active":  "grant",
    "order.paid":           "grant",
    # Update (plan change, renewal, seat change)
    "subscription.updated": "update",
    # Revoke (cancel, revoke, refund)
    "subscription.canceled": "revoke",   # FLAG (c): US spelling — confirm
    "subscription.revoked":  "revoke",
    "order.refunded":        "revoke",
}


# ---------------------------------------------------------------------------
# Field normalizers (FLAG: confirm raw Polar field values against live docs)
# ---------------------------------------------------------------------------

# Polar raw billing-interval token → our plans/subscriptions enum
# ('free' | 'monthly' | 'annual' | 'one_time').  Unknown → None so the caller
# stores NULL (the subscriptions CHECK permits NULL) rather than a bad enum that
# would trip the constraint.
_BILLING_INTERVAL_MAP: dict[str, str] = {
    "month":     "monthly",
    "monthly":   "monthly",
    "year":      "annual",
    "annual":    "annual",
    "yearly":    "annual",
    "one_time":  "one_time",
    "one-time":  "one_time",
    "onetime":   "one_time",
    "free":      "free",
}

# Polar raw subscription/order status → our subscriptions.status enum
# ('pending' | 'active' | 'past_due' | 'cancelled' | 'expired' | 'trialing'
#  | 'refunded').  Anything not clearly mapped falls back to 'active' ONLY when
# it is a clearly-active token (handled in map_subscription_status); other
# unknowns fall back to 'active' as the safe default for a grant-class event.
_STATUS_MAP: dict[str, str] = {
    "active":            "active",
    "incomplete":        "pending",
    "incomplete_expired": "expired",
    "trialing":          "trialing",
    "trial":             "trialing",
    "past_due":          "past_due",
    "unpaid":            "past_due",
    "canceled":          "cancelled",   # US spelling (Polar)
    "cancelled":         "cancelled",
    "revoked":           "cancelled",
    "expired":           "expired",
    "ended":             "expired",
    "refunded":          "refunded",
    "pending":           "pending",
}


def normalize_billing_interval(value: Any) -> str | None:
    """Map a raw Polar billing-interval token to our enum, or ``None`` if unknown.

    Accepts e.g. ``'month'`` → ``'monthly'``, ``'year'`` → ``'annual'``,
    ``'one_time'``/``'one-time'`` → ``'one_time'``.  Case-insensitive.  Returns
    ``None`` for an unrecognised or non-string value so the caller stores NULL
    (the ``subscriptions.billing_interval`` CHECK permits NULL) rather than a
    value that would violate the constraint.
    """
    if not isinstance(value, str):
        return None
    return _BILLING_INTERVAL_MAP.get(value.strip().lower())


def map_subscription_status(payload: dict[str, Any]) -> str:
    """Map a Polar webhook payload's status to our ``subscriptions.status`` enum.

    Reads ``payload["data"]["status"]`` (the Polar object status).  Returns one
    of pending/active/past_due/cancelled/expired/trialing/refunded.  When the
    status is missing or unrecognised, defaults to ``'active'`` — these helpers
    are only invoked on grant/update-class events where the object is live, so
    'active' is the safe default; revoke-class events drive their own
    status via ``mark_cancelled`` and do not rely on this mapping.
    """
    data = payload.get("data")
    raw = data.get("status") if isinstance(data, dict) else None
    if isinstance(raw, str):
        mapped = _STATUS_MAP.get(raw.strip().lower())
        if mapped is not None:
            return mapped
    return "active"


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def verify_signature(
    secret: str,
    *,
    msg_id: str,
    timestamp: str,
    body: bytes,
    signature_header: str,
    tolerance_seconds: int,
) -> bool:
    """Verify a Standard Webhooks HMAC-SHA256 signature.  FAIL-CLOSED.

    Returns ``True`` only when ALL of the following hold:
      - secret is non-empty and decodable;
      - timestamp is a valid integer within ``tolerance_seconds`` of now;
      - at least one ``v1,<b64>`` token in ``signature_header`` matches the
        expected HMAC computed over ``"{msg_id}.{timestamp}.{body}"``;
      - comparison is performed with ``hmac.compare_digest`` (constant-time).

    On any malformed input (bad timestamp int, empty secret, empty/missing
    signature header, base64 decode error) returns ``False`` — never raises.

    Args:
        secret: Webhook signing secret.  May be plain-text or ``whsec_``-prefixed
            base64 (Standard Webhooks convention).
        msg_id: Value of the ``webhook-id`` header.
        timestamp: Value of the ``webhook-timestamp`` header (Unix epoch string).
        body: Raw request body bytes (before any JSON parsing).
        signature_header: Full value of the ``webhook-signature`` header;
            space-separated list of ``"v1,<base64sig>"`` tokens.
        tolerance_seconds: Maximum age (in seconds) for the timestamp.  Requests
            older than this are rejected to prevent replay attacks.

    Returns:
        ``True`` if the signature is valid and fresh; ``False`` otherwise.
    """
    # Step 1: resolve secret bytes.
    if not secret:
        log.warning("verify_signature called with empty secret — rejecting")
        return False
    try:
        if secret.startswith(_WHSEC_PREFIX):
            secret_bytes = base64.b64decode(secret[len(_WHSEC_PREFIX):])
        else:
            secret_bytes = secret.encode()
    except Exception:
        log.warning("verify_signature: failed to decode secret — rejecting")
        return False

    # Step 2: timestamp replay guard.
    try:
        ts_int = int(timestamp)
    except (ValueError, TypeError):
        log.warning("verify_signature: non-integer timestamp %r — rejecting", timestamp)
        return False

    if abs(time.time() - ts_int) > tolerance_seconds:
        log.warning(
            "verify_signature: timestamp %d outside tolerance (%ds) — rejecting",
            ts_int,
            tolerance_seconds,
        )
        return False

    # Step 3: build signed content.
    #   Standard Webhooks: signed = b"{msg_id}.{timestamp}." + body
    signed = f"{msg_id}.{timestamp}".encode() + b"." + body

    # Step 4: compute expected signature (base64 of HMAC-SHA256 digest).
    expected = base64.b64encode(
        hmac.new(secret_bytes, signed, hashlib.sha256).digest()
    ).decode()

    # Step 5: compare each token in the header (supports key rotation).
    if not signature_header:
        return False
    for token in signature_header.split():
        version, _, sig = token.partition(",")
        if version == _SIG_VERSION and hmac.compare_digest(sig, expected):
            return True

    return False


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------

def parse_event(payload: dict[str, Any]) -> tuple[str, str, str]:
    """Extract (event_id, event_type, external_ref) from a Polar webhook payload.

    ``event_id`` is read from ``payload["id"]`` when present (Polar includes it
    in the envelope).  Callers that receive it only as a header value should pass
    an enriched payload with the header value injected under the key ``"id"``, or
    use the header value directly — the design choice here is to prefer the
    envelope field so the function is self-contained and testable without headers.

    ``external_ref`` is ``payload["data"]["id"]`` — the Polar subscription or
    checkout object id (FLAG d: confirm this field path from a real sample).

    Raises ``ValueError`` with a clear message on missing required fields.
    """
    # event_type
    event_type = payload.get(_FIELD_EVENT_TYPE)
    if not event_type:
        raise ValueError(
            f"parse_event: missing required field '{_FIELD_EVENT_TYPE}' in payload"
        )

    # event_id — prefer envelope key "id"; callers may inject header value here
    event_id = payload.get("id") or payload.get("webhook_id") or ""
    if not event_id:
        raise ValueError(
            "parse_event: event_id not found in payload['id'] or payload['webhook_id']; "
            "caller must inject the webhook-id header value under payload['id']"
        )

    # external_ref — FLAG (d): payload["data"]["id"]
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("parse_event: payload['data'] is missing or not a dict")
    external_ref = data.get("id")
    if not external_ref:
        raise ValueError(
            "parse_event: payload['data']['id'] is missing — FLAG (d): confirm field path"
        )

    return str(event_id), str(event_type), str(external_ref)


# ---------------------------------------------------------------------------
# Plan resolution
# ---------------------------------------------------------------------------

def resolve_plan_id(payload: dict[str, Any], *, conn: Any = None) -> int:
    """Map a Polar product id from the webhook payload to the internal ``plans.id``.

    Resolution path:
      1. Read ``payload["data"]["product_id"]`` (FLAG d: confirm field path).
      2. Fetch ``get_setting("billing.polar_product_map", conn=conn)`` — a dict
         mapping ``{polar_product_id: plan_slug}``.  Default is ``{}`` (catalogue
         default in ``settings_registry.py``).
      3. Look up the product_id in the map to get a slug.
      4. SELECT ``plans.id`` WHERE ``slug = %s`` (parameterised, safe against SQL-i).

    Args:
        payload: Parsed Polar webhook payload dict.
        conn: Optional open psycopg2 connection.  Passed through to
            ``get_setting`` and the shared ``_db.slug_to_plan_id``.  When ``None``
            this helper
            checks out its OWN connection from the pool (``get_pool``) for the
            whole resolution — so a route caller never has to reach into a
            store's private pool.

    Returns:
        Integer ``plans.id`` for the resolved plan.

    Raises:
        ValueError: If the product_id is missing from the payload, not in the
            product map, or no matching plan row exists.
    """
    # Step 1: extract product_id from payload.
    # FLAG (d): confirm field path — may be data.product_id or data.product.id
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("resolve_plan_id: payload['data'] is missing or not a dict")
    product_id = data.get("product_id")
    if not product_id:
        raise ValueError(
            "resolve_plan_id: payload['data']['product_id'] is missing — "
            "FLAG (d): confirm the Polar payload field for product id"
        )

    # When no caller connection is supplied, own one for the full resolution so
    # the route does not need to reach into any store's private pool (I9/I14).
    if conn is None:
        from src.db.pg import get_pool  # local import avoids import-time pool dep

        with get_pool().checkout() as own_conn:
            return _resolve_with_conn(payload=payload, product_id=product_id, conn=own_conn)

    return _resolve_with_conn(payload=payload, product_id=product_id, conn=conn)


def _resolve_with_conn(*, payload: dict[str, Any], product_id: Any, conn: Any) -> int:
    """Resolve product_id → plans.id using an open ``conn`` (settings + slug lookup)."""
    from src.billing._db import slug_to_plan_id  # shared vendor-neutral slug helper
    from src.settings import get_setting  # local import avoids circular at module load

    # Step 2: fetch product → slug map from settings (scoped to this conn).
    product_map: dict[str, str] = get_setting("billing.polar_product_map", conn=conn) or {}

    # Step 3: resolve slug.
    slug = product_map.get(str(product_id))
    if not slug:
        raise ValueError(
            f"resolve_plan_id: unknown Polar product_id={product_id!r}; "
            f"configure billing.polar_product_map to include this product"
        )

    # Step 4: resolve slug to integer plans.id.
    return slug_to_plan_id(slug, conn)
