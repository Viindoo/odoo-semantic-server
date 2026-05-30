# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outbound Polar REST client — in-app subscription cancel (M10B P1 W3).

Owner decision (overrides the plan's "defer outbound to P2"): the in-app
``POST /api/account/subscription/cancel`` endpoint CALLS the Polar API so the
cancel is authoritative at the vendor, not merely a local UI flag.  The local
``cancel_at_period_end`` flag is only flipped AFTER Polar confirms — so a user
is never told "cancelled" while Polar would still charge them.

Fail-closed posture (IRON LAW — money logic):

* ``config.POLAR_API_KEY is None`` → :class:`PolarApiNotConfigured` (caller → 503).
  We never silently "succeed" without an authoritative vendor cancel.
* Network error / 4xx / 5xx → :class:`PolarApiError` carrying status + body
  (caller → 502).  The caller does NOT set the local schedule flag in this case.

Cancel contract — Confirmed against https://docs.polar.sh 2026-05-30
--------------------------------------------------------------------
The exact cancel endpoint + method + payload are centralised in the module
constants below so they can be adjusted in ONE place.  Verified shapes:

    cancel-at-period-end :  PATCH {base}/v1/subscriptions/{id}
                            JSON  {"cancel_at_period_end": true}
    immediate cancel     :  DELETE {base}/v1/subscriptions/{id}
                            (no request body)

cancel-at-period-end is Polar's Update Subscription API (PATCH with
``SubscriptionCancel`` body); the immediate path is Polar's dedicated Revoke
Subscription endpoint (``DELETE /v1/subscriptions/{id}`` with NO body — there is
no ``PATCH {"revoke": true}`` endpoint).  ``httpx`` omits the request body when
``json=None`` so the DELETE goes out body-less.  See the verification report
``docs/reference/polar-contract-verification.md`` (immediate-cancel MISMATCH).

``external_ref`` stored on the subscription IS the Polar subscription id, so it
is interpolated directly as ``{id}``.  Auth is ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import logging

import httpx

from src.settings import get_setting
from src.web_ui import config

logger = logging.getLogger(__name__)

# --- Polar cancel contract (confirmed vs https://docs.polar.sh 2026-05-30) --
# Path template is formatted with the Polar subscription id (= external_ref).
_CANCEL_PATH_TEMPLATE = "/v1/subscriptions/{id}"
# cancel-at-period-end is the Update Subscription API (PATCH + schedule flag);
# immediate cancel is the dedicated Revoke endpoint (DELETE, NO body).  There is
# no PATCH {"revoke": true} endpoint — see docs/reference/polar-contract-verification.md.
_CANCEL_AT_PERIOD_END_METHOD = "PATCH"
_CANCEL_AT_PERIOD_END_PAYLOAD = {"cancel_at_period_end": True}
_CANCEL_IMMEDIATE_METHOD = "DELETE"
_CANCEL_IMMEDIATE_PAYLOAD = None  # DELETE /v1/subscriptions/{id} carries no body
# Short timeout — this is a synchronous user-facing call; do not hang the request.
_REQUEST_TIMEOUT_SECONDS = 10.0


class PolarApiError(RuntimeError):
    """Polar returned a non-2xx response or the request failed at the network layer.

    Carries ``status_code`` (None for transport-level failures) and the raw
    response ``body`` text so the caller can log it.  Caller maps this to HTTP
    502 (bad gateway) — we could not reach/satisfy the upstream seller-of-record.
    """

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class PolarApiNotConfigured(RuntimeError):
    """``POLAR_API_KEY`` is unset → no outbound cancel is possible.

    Caller maps this to HTTP 503 and surfaces the Polar customer-portal link so
    the user can still cancel via the seller-of-record.
    """


def _api_base() -> str:
    """Return the Polar REST base URL (admin-configurable), trailing slash stripped."""
    base = get_setting("billing.polar_api_base") or "https://api.polar.sh"
    return str(base).rstrip("/")


async def cancel_subscription(external_ref: str, *, at_period_end: bool = True) -> dict:
    """Cancel a Polar subscription. Returns the parsed JSON body on success.

    ``external_ref`` is the Polar subscription id.  ``at_period_end=True`` (the
    default and the only mode the in-app endpoint uses per owner decision #1 —
    no refund, access to period end) schedules a cancel-at-period-end; passing
    ``False`` performs an immediate cancel.

    Raises:
        PolarApiNotConfigured: ``config.POLAR_API_KEY`` is None (caller → 503).
        PolarApiError: transport failure or non-2xx response (caller → 502).
    """
    api_key = config.POLAR_API_KEY
    if api_key is None:
        raise PolarApiNotConfigured(
            "POLAR_API_KEY is not configured; cannot perform an outbound cancel"
        )

    url = _api_base() + _CANCEL_PATH_TEMPLATE.format(id=external_ref)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if at_period_end:
        method = _CANCEL_AT_PERIOD_END_METHOD
        json_body: dict | None = _CANCEL_AT_PERIOD_END_PAYLOAD
    else:
        # Immediate cancel = DELETE /v1/subscriptions/{id} with NO body (Polar's
        # dedicated Revoke endpoint).  json_body is None → httpx sends no body.
        method = _CANCEL_IMMEDIATE_METHOD
        json_body = _CANCEL_IMMEDIATE_PAYLOAD

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            # json=None → httpx omits the request body entirely (correct for DELETE).
            response = await client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        # Transport-level failure (DNS, connect, timeout) — never reached Polar.
        logger.warning(
            "polar_api.cancel_subscription: transport error for external_ref=%r: %s",
            external_ref, exc,
        )
        raise PolarApiError(
            f"Polar cancel request failed at transport layer: {exc}",
            status_code=None,
            body="",
        ) from exc

    if response.status_code >= 400:
        body_text = response.text[:2000]
        logger.warning(
            "polar_api.cancel_subscription: Polar returned %d for external_ref=%r: %s",
            response.status_code, external_ref, body_text,
        )
        raise PolarApiError(
            f"Polar cancel returned HTTP {response.status_code}",
            status_code=response.status_code,
            body=body_text,
        )

    try:
        return response.json()
    except ValueError:
        # 2xx with a non-JSON body (e.g. 204 No Content) is still a SUCCESS —
        # Polar accepted the cancel.  Return an empty dict rather than failing.
        return {}
