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
    """Best-effort extraction of buyer email from a Polar payload data dict."""
    # Direct field: data.customer_email
    email = data.get("customer_email")
    if email:
        return str(email)
    # Nested customer object: data.customer.email
    customer = data.get("customer")
    if isinstance(customer, dict):
        email = customer.get("email")
        if email:
            return str(email)
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
