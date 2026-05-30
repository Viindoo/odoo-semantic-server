# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vendor-parametric webhook pipeline (M10B P1, ADR-0039 Area A).

The webhook-processing pipeline — rate-limit, fail-closed secret check,
signature verification, ledger recording, dedup/reprocess guard, event→action
mapping, plan resolution, grant/update/revoke dispatch, transient-vs-permanent
error classification, mark-processed — is vendor-agnostic.  Only the vendor
specifics differ per payment vendor: how a signature is verified, how an event
is parsed, how an event type maps to a grant/update/revoke action, how a payload
resolves to a plan_id, and how the commercial snapshot fields (seats, amount,
currency, interval, period bounds, trial end, event timestamp) are extracted.
Those are captured in :class:`WebhookAdapter`.

A new vendor (Paddle, an ERP, …) is therefore a small adapter of glue + a route
that builds a :class:`WebhookAdapter` and calls :func:`run_webhook_pipeline`.  The
pipeline itself lives **once**, here.

This module is money-critical: a paid grant must never be silently lost.  Two
defenses make that concrete:

* **Reprocess-after-crash (#2):** the ledger records EVERY delivery before
  dispatch.  If a prior delivery was recorded but never marked processed (a crash
  between INSERT and ``mark_event_processed``), the replay RE-DISPATCHES the
  idempotent grant/update/revoke instead of treating it as a duplicate — so a
  crash mid-flight self-heals on Polar's automatic retry.

* **Transient-vs-permanent (#6):** a dispatch error is classified.  A *permanent*
  error (bad data that will never succeed: ``IntegrityError`` / ``CheckViolation``
  / ``ValueError``) marks the event processed-with-error and returns 200 so the
  vendor stops hammering a poison event.  A *transient* error (DB pool timeout,
  ``OperationalError``, network blip) does NOT mark the event processed and
  returns 5xx so the vendor RETRIES later — the grant is not lost, just deferred.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi.responses import JSONResponse
from psycopg2 import IntegrityError
from psycopg2.errors import CheckViolation
from starlette.requests import Request

from src.billing import activation
from src.billing.activation import EntitlementGrant
from src.web_ui._json import _json_safe
from src.web_ui.rate_limit import check_ip_rate_limit, get_client_ip

logger = logging.getLogger(__name__)


# Errors that mean "this event will NEVER succeed as delivered" — bad/poison
# data.  We ack (200) + mark processed-with-error so the vendor stops retrying a
# permanently-broken event; ops investigate via the ledger processing_error.
_PERMANENT_DISPATCH_ERRORS: tuple[type[BaseException], ...] = (
    IntegrityError,    # FK/UNIQUE/NOT-NULL violation — data shape is wrong
    CheckViolation,    # a CHECK enum/range violation — same
    ValueError,        # bad enum token, malformed field, business-rule reject
)
# NB: IntegrityError is the psycopg2 base class for CheckViolation in modern
# psycopg2; listing both is harmless (isinstance is fine with the duplicate) and
# documents intent.  A *transient* error (OperationalError, pool timeout, any
# non-permanent Exception) is NOT marked processed and returns 5xx → vendor
# RETRIES, so a momentary DB hiccup never loses a paid grant.


@dataclass(frozen=True)
class WebhookAdapter:
    """Per-vendor binding consumed by :func:`run_webhook_pipeline`.

    Everything vendor-specific about a webhook is captured here so the pipeline
    stays vendor-agnostic.  A second adapter (Paddle/ERP) supplies its own
    callables + header names + ``vendor`` string and reuses the entire pipeline.

    The commercial-field extractors (``extract_*_fn``) keep vendor field names
    (e.g. Polar's ``seats``/``amount``/``current_period_end``) OUT of the
    pipeline: the pipeline asks the adapter for the values, it never reaches into
    the ``data`` dict by key.  Each extractor has a safe default (returns ``None``
    / a benign value) so a vendor that does not carry a field is no special case.

    Attributes:
        vendor: Ledger / subscription ``source`` discriminator — MUST be a value
            permitted by the ``billing_webhook_events.vendor`` and
            ``subscriptions.source`` CHECK enums (e.g. ``'polar'``).
        secret: The signing secret, or ``None`` when unconfigured → 503.
        tolerance_seconds: Signature timestamp tolerance window.
        rate_limit_rpm: Per-VENDOR requests/minute ceiling for this endpoint
            (CR6 — keyed by ``vendor`` not client IP; see ``run_webhook_pipeline``).
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
        extract_cancel_at_period_end_fn: ``(data: dict) -> bool | None`` — reads
            the vendor's cancel-at-period-end flag from the payload (Polar:
            ``data.cancel_at_period_end``).  Returns ``None`` when the vendor did
            not carry the field, so the local flag is left untouched; returns the
            bool when present so an ``update`` (e.g. ``subscription.uncanceled``)
            reconciles a previously-scheduled local cancel.  Default returns
            ``None`` (a vendor that does not carry the field is no special case).
        extract_interval_fn: ``(data: dict) -> Any`` — pulls the RAW vendor
            billing-interval token out of the ``data`` dict (e.g. Polar's
            ``recurring_interval``).  Keeping the vendor field NAME in the adapter
            (not hard-coded in the pipeline) is what lets a second vendor that
            carries the interval under a different key reuse the pipeline; default
            returns ``None`` (one-time / no recurring interval).
        normalize_interval_fn: ``(raw) -> str | None`` billing-interval normaliser
            applied to the value ``extract_interval_fn`` returns.
        extract_seats_fn: ``(data: dict) -> int`` seat-count extractor (default 1).
        extract_amount_fn: ``(data: dict) -> int | None`` amount-cents extractor.
        extract_currency_fn: ``(data: dict) -> str | None`` ISO-4217 extractor.
        extract_period_fn: ``(data: dict) -> (start, end, trial)`` — the three
            TIMESTAMPTZ period/trial bounds (any may be ``None``).
        extract_event_at_fn: ``(headers: Mapping, payload: dict) -> datetime | None``
            — the vendor event timestamp used for the monotonic out-of-order
            guard (#5).  Standard Webhooks carry it in the ``webhook-timestamp``
            header; a vendor may instead carry it in the payload.
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
    # cancel-at-period-end reconciliation (default None = field absent → no-op):
    # an update event (e.g. subscription.uncanceled) carries the vendor's current
    # cancel_at_period_end so the local schedule flag tracks the vendor's truth.
    extract_cancel_at_period_end_fn: Callable[[dict], bool | None] = (
        lambda data: None
    )
    # CL1 — vendor-agnostic commercial extractors (safe defaults supplied below).
    # extract_interval_fn pulls the RAW vendor interval token out of ``data`` so
    # the pipeline never hard-codes a vendor field name (Polar = recurring_interval).
    extract_interval_fn: Callable[[dict], Any] = lambda data: None
    extract_seats_fn: Callable[[dict], int] = lambda data: 1
    extract_amount_fn: Callable[[dict], int | None] = lambda data: None
    extract_currency_fn: Callable[[dict], str | None] = lambda data: None
    extract_period_fn: Callable[
        [dict], tuple[Any, Any, Any]
    ] = lambda data: (None, None, None)
    extract_event_at_fn: Callable[
        [Any, dict], datetime | None
    ] = lambda headers, payload: None


async def run_webhook_pipeline(adapter: WebhookAdapter, request: Request) -> JSONResponse:
    """Process one inbound webhook delivery for ``adapter.vendor``.

    Processing order (money-critical guards spelled out):

      1.  Rate-limit gate keyed by ``adapter.vendor`` (CR6 — NOT client IP: behind
          nginx every delivery arrives from 127.0.0.1, so an IP bucket would be a
          single shared bucket and a legitimate retry-storm from one vendor would
          throttle ALL vendors.  The signature is HMAC-verified at step 4, so
          per-IP abuse protection is redundant; we bound load per *vendor*).
      2.  Fail-closed: 503 if ``adapter.secret`` is absent.
      3.  Read raw body + ``adapter.header_*`` headers.
      4.  Verify signature via ``adapter.verify_fn`` (fail-closed).
      5.  Parse JSON body (400 on unparseable / non-object envelope).
      6.  Extract (event_id, event_type, external_ref) via ``adapter.parse_event_fn``.
      7.  Record EVERY attempt to ``billing_webhook_events`` (signature-invalid too);
          ``record_webhook_event`` returns ``(pk, is_new, already_processed)``.
      8.  Guard: 400 on bad signature.
      9.  Dedup/reprocess (#2):
            - ``already_processed`` → 200 ``duplicate`` (a prior run finished; safe
              to drop).
            - recorded-but-not-processed (``is_new`` False, ``already_processed``
              False) → RE-DISPATCH (a prior delivery crashed mid-flight; the
              grant/update/revoke is idempotent on ``external_ref``, so replaying
              self-heals the lost grant).
            - ``is_new`` → process normally.
      10. Guard: 200 ``ignored`` on UNMAPPED event_type (ops-visible processing_error).
      11. Resolve plan_id ONLY for grant/update; a config / unknown-product failure
          → ERROR log + 200 ``config_error``.  Revoke NEVER resolves a plan.
      12. Dispatch grant / update / revoke via ``activation.*`` with the adapter's
          extracted commercial fields + the event timestamp (monotonic guard #5).
          A dispatch error is classified transient-vs-permanent (#6).
      13. Mark event processed; return 200.
    """
    # ------------------------------------------------------------------ step 1
    # CR6: rate-limit per VENDOR, not per client IP.  After nginx the TCP peer is
    # always 127.0.0.1 → a single shared IP bucket → one vendor's retry-storm
    # would throttle grants for every vendor.  The endpoint is HMAC-authenticated
    # (step 4) so per-IP abuse protection is redundant; we instead bound load by
    # the (signed) vendor identity.  ``check_ip_rate_limit`` keys on an opaque
    # string, so passing the vendor name gives one bucket per vendor.
    client_ip = await get_client_ip(request)
    rl_key = f"vendor:{adapter.vendor}"
    allowed = await check_ip_rate_limit(
        rl_key, limit=adapter.rate_limit_rpm, window_seconds=60
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
    try:
        raw_payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("%s_webhook: unparseable body — %s", adapter.vendor, exc)
        return JSONResponse(_json_safe({"error": "invalid_json"}), status_code=400)

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
    event_pk, is_new, already_processed = subs.record_webhook_event(
        vendor=adapter.vendor,
        event_id=event_id,
        event_type=event_type,
        signature_valid=sig_ok,
        payload=enriched_payload,
    )
    # The ledger upsert always RETURNS the row, so pk is non-None.  Guard anyway:
    # a None pk would make mark_event_processed a silent no-op and lose the
    # money-state audit trail — fail LOUD rather than process blind.
    if event_pk is None:
        logger.error(
            "%s_webhook: record_webhook_event returned a NULL pk for event_id=%s — "
            "cannot safely mark processed; rejecting for retry",
            adapter.vendor, event_id,
        )
        return JSONResponse(
            _json_safe({"error": "ledger_write_failed"}), status_code=500
        )

    # ------------------------------------------------------------------ step 8
    if not sig_ok:
        logger.warning(
            "%s_webhook: bad signature for event_id=%s event_type=%s ip=%s",
            adapter.vendor, event_id, event_type, client_ip,
        )
        return JSONResponse(_json_safe({"error": "invalid_signature"}), status_code=400)

    # ------------------------------------------------------------------ step 9
    # #2 reprocess-after-crash: distinguish a FINISHED replay (already_processed)
    # from a recorded-but-unfinished one (a crash between the ledger INSERT and
    # mark_event_processed).  Only the finished case is a true duplicate; the
    # unfinished case must RE-DISPATCH the (idempotent) grant/update/revoke so a
    # paid grant interrupted by a crash is not lost.
    if already_processed:
        logger.debug(
            "%s_webhook: event_id=%s already processed — returning 200 duplicate",
            adapter.vendor, event_id,
        )
        return JSONResponse(_json_safe({"status": "duplicate"}), status_code=200)
    if not is_new:
        logger.warning(
            "%s_webhook: event_id=%s was recorded but never processed "
            "(prior crash?) — RE-DISPATCHING idempotently to self-heal",
            adapter.vendor, event_id,
        )

    # ------------------------------------------------------------------ step 10
    action = adapter.event_action_fn(event_type)
    if action is None:
        ignore_msg = f"unmapped event_type={event_type!r}"
        logger.warning("%s_webhook: %s — acking without dispatch", adapter.vendor, ignore_msg)
        subs.mark_event_processed(event_pk, None, error=ignore_msg)
        return JSONResponse(
            _json_safe({"status": "ignored", "event_type": event_type}), status_code=200
        )

    data_dict = raw_payload.get("data") or {}

    # CR1/#5 — extract the commercial snapshot + event timestamp through the
    # adapter (no Polar field names leak into the pipeline).
    last_event_at = adapter.extract_event_at_fn(request.headers, enriched_payload)

    # ------------------------------------------------------------------ step 11
    plan_id: int | None = None
    if action in ("grant", "update"):
        try:
            plan_id = adapter.resolve_plan_fn(enriched_payload)
        except ValueError as exc:
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
            period_start, period_end, trial_ends_at = adapter.extract_period_fn(data_dict)
            grant = EntitlementGrant(
                plan_id=plan_id,
                external_ref=external_ref,
                source=adapter.vendor,
                buyer_email=buyer_email,
                seats=adapter.extract_seats_fn(data_dict),
                amount_cents=adapter.extract_amount_fn(data_dict),
                currency=adapter.extract_currency_fn(data_dict),
                # Normalize raw vendor enum-ish tokens before they reach SQL.  A
                # raw 'month'/'year' would violate the billing_interval CHECK →
                # IntegrityError → permanent → 200 → subscription silently lost.
                # The adapter's extractor pulls the RAW token from the vendor's
                # own field (Polar = recurring_interval) so no vendor field name
                # is hard-coded here.
                billing_interval=adapter.normalize_interval_fn(
                    adapter.extract_interval_fn(data_dict)
                ),
                current_period_start=period_start,
                current_period_end=period_end,
                trial_ends_at=trial_ends_at,
            )
            sub_id = activation.grant_entitlement(grant, last_event_at=last_event_at)

        elif action == "update":
            # Derive the REAL status from the payload instead of forcing 'active'.
            # A past_due / paused / trialing update must not silently keep a
            # non-paying subscriber on full paid access.
            mapped_status = adapter.map_status_fn(raw_payload or {})
            period_start, period_end, trial_ends_at = adapter.extract_period_fn(data_dict)
            # Reconcile the cancel-at-period-end schedule from the vendor payload
            # so a subscription.uncanceled (reactivation) clears a previously
            # locally-scheduled cancel.  None = vendor omitted the field → leave
            # the local flag untouched.  See 01-polar-contract-verification.md.
            cancel_at_period_end = adapter.extract_cancel_at_period_end_fn(data_dict)
            sub_id = activation.update_entitlement(
                external_ref,
                plan_id=plan_id,
                status=mapped_status,
                seats=adapter.extract_seats_fn(data_dict),
                current_period_start=period_start,
                current_period_end=period_end,
                trial_ends_at=trial_ends_at,
                cancel_at_period_end=cancel_at_period_end,
                last_event_at=last_event_at,
            )

        elif action == "revoke":
            sub = subs.get_by_external_ref(external_ref)
            if sub is not None:
                sub_id = sub["id"]
            activation.revoke_entitlement(
                external_ref, reason=event_type, last_event_at=last_event_at
            )

    except _PERMANENT_DISPATCH_ERRORS as exc:
        # #6 PERMANENT: bad/poison data that will NEVER succeed as delivered.  Ack
        # (200) + mark processed-with-error so the vendor stops retrying a broken
        # event; the processing_error makes it ops-queryable.
        logger.error(
            "%s_webhook: PERMANENT dispatch error for event_id=%s action=%s — %s",
            adapter.vendor, event_id, action, exc, exc_info=True,
        )
        subs.mark_event_processed(event_pk, sub_id, error=str(exc))
        return JSONResponse(
            _json_safe({"status": "error", "detail": "permanent processing error"}),
            status_code=200,
        )
    except Exception as exc:
        # #6 TRANSIENT: a momentary failure (OperationalError, DB pool timeout,
        # network blip, anything not classified permanent).  Do NOT mark
        # processed — leave the ledger row unfinished so the vendor's retry
        # RE-DISPATCHES it (step 9), and return 5xx to ASK for that retry.  This
        # is the money-safe default: when unsure, retry rather than drop the grant.
        logger.error(
            "%s_webhook: TRANSIENT dispatch error for event_id=%s action=%s — "
            "NOT marking processed, returning 5xx for vendor retry — %s",
            adapter.vendor, event_id, action, exc, exc_info=True,
        )
        return JSONResponse(
            _json_safe({"status": "retry", "detail": "transient processing error"}),
            status_code=503,
        )

    # ------------------------------------------------------------------ step 13
    subs.mark_event_processed(event_pk, sub_id)
    logger.info(
        "%s_webhook: processed event_id=%s event_type=%s action=%s "
        "external_ref=%s sub_id=%s",
        adapter.vendor, event_id, event_type, action, external_ref, sub_id,
    )
    return JSONResponse(_json_safe({"status": "ok", "action": action}), status_code=200)
