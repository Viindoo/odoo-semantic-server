# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vendor-parametric webhook pipeline (M10B P1, ADR-0039 Area A).

The 13-step webhook-processing pipeline — rate-limit, fail-closed secret check,
signature verification, ledger recording, dedup guard, event→action mapping,
plan resolution, grant/update/revoke dispatch, mark-processed — is vendor-agnostic.
Only four concerns differ per payment vendor: how a signature is verified, how an
event is parsed, how an event type maps to a grant/update/revoke action, and how a
payload resolves to a plan_id.  Those four (plus a handful of header names + the
buyer-email / status / interval extractors) are captured in :class:`WebhookAdapter`.

A new vendor (Paddle, an ERP, …) is therefore ~25 lines of adapter glue + a route
that builds a :class:`WebhookAdapter` and calls :func:`run_webhook_pipeline`.  The
pipeline itself lives **once**, here.

This module is a behaviour-preserving extraction of the original ``polar_webhook``
handler: every guard, status code, ledger write and log site is identical; the
``vendor`` string and the per-vendor callables are simply parameterised.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.billing import activation
from src.billing.activation import EntitlementGrant
from src.web_ui._json import _json_safe
from src.web_ui.rate_limit import check_ip_rate_limit, get_client_ip

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookAdapter:
    """Per-vendor binding consumed by :func:`run_webhook_pipeline`.

    Everything vendor-specific about a webhook is captured here so the pipeline
    stays vendor-agnostic.  A second adapter (Paddle/ERP) supplies its own
    callables + header names + ``vendor`` string and reuses the entire pipeline.

    Attributes:
        vendor: Ledger / subscription ``source`` discriminator — MUST be a value
            permitted by the ``billing_webhook_events.vendor`` and
            ``subscriptions.source`` CHECK enums (e.g. ``'polar'``).
        secret: The signing secret, or ``None`` when unconfigured → 503.
        tolerance_seconds: Signature timestamp tolerance window.
        rate_limit_rpm: Per-IP requests/minute ceiling for this endpoint.
        header_id: HTTP header carrying the event id (e.g. ``webhook-id``).
        header_timestamp: HTTP header carrying the signed timestamp.
        header_signature: HTTP header carrying the signature.
        verify_fn: ``(secret, *, msg_id, timestamp, body, signature_header,
            tolerance_seconds) -> bool`` — signature verifier (fail-closed).
        parse_event_fn: ``(payload: dict) -> (event_id, event_type,
            external_ref)``; raises ``ValueError`` on a malformed payload.
        event_action_fn: ``(event_type: str) -> 'grant'|'update'|'revoke'|None``;
            ``None`` means unmapped → acked-but-ignored.
        resolve_plan_fn: ``(payload: dict) -> int``; raises ``ValueError`` when the
            product/plan cannot be resolved (config error).  Called for
            grant/update only — never for revoke.
        extract_email_fn: ``(data: dict) -> str | None`` buyer-email extractor.
        map_status_fn: ``(payload: dict) -> str`` → ``subscriptions.status`` enum.
        normalize_interval_fn: ``(raw) -> str | None`` billing-interval normaliser.
    """

    vendor: str
    secret: str | None
    tolerance_seconds: int
    rate_limit_rpm: int
    header_id: str
    header_timestamp: str
    header_signature: str
    verify_fn: Callable[..., bool]
    parse_event_fn: Callable[[dict], tuple[str, str, str]]
    event_action_fn: Callable[[str], str | None]
    resolve_plan_fn: Callable[[dict], int]
    extract_email_fn: Callable[[dict], str | None]
    map_status_fn: Callable[[dict], str]
    normalize_interval_fn: Callable[[Any], str | None]


async def run_webhook_pipeline(adapter: WebhookAdapter, request: Request) -> JSONResponse:
    """Process one inbound webhook delivery for ``adapter.vendor``.

    Reproduces the exact 13-step processing order (per design §4.2):

      1.  IP rate-limit gate (``adapter.rate_limit_rpm``).
      2.  Fail-closed: 503 if ``adapter.secret`` is absent.
      3.  Read raw body + ``adapter.header_*`` headers.
      4.  Verify signature via ``adapter.verify_fn`` (fail-closed).
      5.  Parse JSON body (400 on unparseable).
      6.  Extract (event_id, event_type, external_ref) via ``adapter.parse_event_fn``,
          injecting the header event id under ``data['id']``'s sibling key ``'id'``.
      7.  Record EVERY attempt to ``billing_webhook_events`` (signature-invalid too).
      8.  Guard: 400 on bad signature.
      9.  Guard: 200 ``duplicate`` on replay.
      10. Guard: 200 ``ignored`` on UNMAPPED event_type (ops-visible processing_error).
      11. Resolve plan_id ONLY for grant/update; a config / unknown-product failure
          → ERROR log + 200 ``config_error``.  Revoke NEVER resolves a plan.
      12. Dispatch grant / update / revoke via ``activation.*``.
      13. Mark event processed; return 200.

    Returns a :class:`JSONResponse`; the status codes + bodies are identical to the
    pre-refactor Polar handler, only ``vendor`` and the per-vendor callables vary.
    """
    # ------------------------------------------------------------------ step 1
    client_ip = await get_client_ip(request)
    allowed = await check_ip_rate_limit(
        client_ip, limit=adapter.rate_limit_rpm, window_seconds=60
    )
    if not allowed:
        return JSONResponse(
            _json_safe({"error": "rate_limited", "retry_after": 60}),
            status_code=429,
            headers={"Retry-After": "60"},
        )

    # ------------------------------------------------------------------ step 2
    if not adapter.secret:
        logger.error(
            "%s_webhook: signing secret is not set — rejecting (503)", adapter.vendor
        )
        return JSONResponse(_json_safe({"error": "webhook_not_configured"}), status_code=503)

    # ------------------------------------------------------------------ step 3
    raw_body: bytes = await request.body()
    msg_id: str = request.headers.get(adapter.header_id, "")
    timestamp: str = request.headers.get(adapter.header_timestamp, "")
    signature_header: str = request.headers.get(adapter.header_signature, "")

    # ------------------------------------------------------------------ step 4
    sig_ok = adapter.verify_fn(
        adapter.secret,
        msg_id=msg_id,
        timestamp=timestamp,
        body=raw_body,
        signature_header=signature_header,
        tolerance_seconds=adapter.tolerance_seconds,
    )

    # ------------------------------------------------------------------ step 5 + 6
    # Pydantic-gate happens at the route boundary if needed; here we parse the
    # body for processing.  A malformed body is a 400; we skip ledger recording
    # for unparseable bodies.
    try:
        raw_payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("%s_webhook: unparseable body — %s", adapter.vendor, exc)
        return JSONResponse(_json_safe({"error": "invalid_json"}), status_code=400)

    # A valid-JSON-but-not-object body (list, scalar) is a malformed envelope: a
    # clean 400, never a 500 from a later dict operation.
    if not isinstance(raw_payload, dict):
        logger.warning(
            "%s_webhook: payload is not a JSON object — rejecting", adapter.vendor
        )
        return JSONResponse(_json_safe({"error": "invalid_envelope"}), status_code=400)

    # Inject header event_id into the payload so parse_event can read it when the
    # envelope omits it (event id often arrives only as a header).
    event_id = msg_id or raw_payload.get("id") or ""
    if not event_id:
        logger.warning("%s_webhook: no event_id in header or body — rejecting", adapter.vendor)
        return JSONResponse(_json_safe({"error": "missing_event_id"}), status_code=400)

    enriched_payload = dict(raw_payload)
    enriched_payload["id"] = event_id

    try:
        _, event_type, external_ref = adapter.parse_event_fn(enriched_payload)
    except ValueError as exc:
        logger.warning("%s_webhook: parse_event failed — %s", adapter.vendor, exc)
        return JSONResponse(
            _json_safe({"error": "parse_error", "detail": str(exc)}), status_code=400
        )

    # ------------------------------------------------------------------ step 7
    from src.db.pg import subscription_store
    subs = subscription_store()
    event_pk, is_new = subs.record_webhook_event(
        vendor=adapter.vendor,
        event_id=event_id,
        event_type=event_type,
        signature_valid=sig_ok,
        payload=enriched_payload,
    )

    # ------------------------------------------------------------------ step 8
    if not sig_ok:
        logger.warning(
            "%s_webhook: bad signature for event_id=%s event_type=%s ip=%s",
            adapter.vendor, event_id, event_type, client_ip,
        )
        return JSONResponse(_json_safe({"error": "invalid_signature"}), status_code=400)

    # ------------------------------------------------------------------ step 9
    if not is_new:
        logger.debug(
            "%s_webhook: duplicate event_id=%s — returning 200", adapter.vendor, event_id
        )
        return JSONResponse(_json_safe({"status": "duplicate"}), status_code=200)

    # ------------------------------------------------------------------ step 10
    # Unmapped event_type → ack WITHOUT dispatch, but make it ops-VISIBLE: record
    # a descriptive processing_error on the ledger row and log at WARNING so a
    # newly-emitted event type we forgot to map surfaces in monitoring.
    action = adapter.event_action_fn(event_type)
    if action is None:
        ignore_msg = f"unmapped event_type={event_type!r}"
        logger.warning("%s_webhook: %s — acking without dispatch", adapter.vendor, ignore_msg)
        subs.mark_event_processed(event_pk, None, error=ignore_msg)
        return JSONResponse(
            _json_safe({"status": "ignored", "event_type": event_type}), status_code=200
        )

    data_dict = raw_payload.get("data") or {}

    # ------------------------------------------------------------------ step 11
    # Resolve plan_id ONLY for actions that need it (grant / update).  A revoke
    # (cancellation / refund) carries no product_id, so resolving here would raise
    # and block the cancellation entirely — the customer would cancel yet KEEP
    # paid access.  Revoke skips resolution outright.
    plan_id: int | None = None
    if action in ("grant", "update"):
        try:
            plan_id = adapter.resolve_plan_fn(enriched_payload)
        except ValueError as exc:
            # Distinguish a genuinely-unknown product from an unconfigured map.
            # Either way a real first purchase is at stake → ops-LOUD: ERROR log +
            # config_error status + a queryable processing_error.  HTTP stays 200
            # so the vendor does not hammer the endpoint while ops fix the map.
            logger.error(
                "%s_webhook: CONFIG/PRODUCT error resolving plan for event_id=%s "
                "event_type=%s — %s",
                adapter.vendor, event_id, event_type, exc,
            )
            subs.mark_event_processed(event_pk, None, error=str(exc))
            return JSONResponse(
                _json_safe({"status": "config_error", "detail": str(exc)}), status_code=200
            )

    # ------------------------------------------------------------------ step 12
    buyer_email = adapter.extract_email_fn(data_dict)
    sub_id: int | None = None
    try:
        if action == "grant":
            grant = EntitlementGrant(
                plan_id=plan_id,
                external_ref=external_ref,
                source=adapter.vendor,
                buyer_email=buyer_email,
                seats=int(data_dict.get("seats") or 1),
                amount_cents=data_dict.get("amount"),
                currency=data_dict.get("currency"),
                # Normalize raw vendor enum-ish tokens before they reach SQL.  A
                # raw 'month'/'year' would violate the billing_interval CHECK →
                # IntegrityError → caught below → 200 → subscription silently lost.
                billing_interval=adapter.normalize_interval_fn(
                    data_dict.get("billing_interval")
                ),
            )
            sub_id = activation.grant_entitlement(grant)

        elif action == "update":
            # Derive the REAL status from the payload instead of forcing 'active'.
            # A past_due / paused / trialing update must not silently keep a
            # non-paying subscriber on full paid access.
            mapped_status = adapter.map_status_fn(raw_payload or {})
            sub_id = activation.update_entitlement(
                external_ref,
                plan_id=plan_id,
                status=mapped_status,
            )

        elif action == "revoke":
            sub = subs.get_by_external_ref(external_ref)
            if sub is not None:
                sub_id = sub["id"]
            activation.revoke_entitlement(external_ref, reason=event_type)

    except Exception as exc:
        logger.error(
            "%s_webhook: dispatch error for event_id=%s action=%s — %s",
            adapter.vendor, event_id, action, exc, exc_info=True,
        )
        subs.mark_event_processed(event_pk, sub_id, error=str(exc))
        # Return 200 to prevent the vendor from retrying a permanently-broken
        # event; processing_error is set in the ledger for ops investigation.
        return JSONResponse(
            _json_safe({"status": "error", "detail": "internal processing error"}),
            status_code=200,
        )

    # ------------------------------------------------------------------ step 13
    subs.mark_event_processed(event_pk, sub_id)
    logger.info(
        "%s_webhook: processed event_id=%s event_type=%s action=%s "
        "external_ref=%s sub_id=%s",
        adapter.vendor, event_id, event_type, action, external_ref, sub_id,
    )
    return JSONResponse(_json_safe({"status": "ok", "action": action}), status_code=200)
