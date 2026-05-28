# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/waitlist.py
"""Self-hosted waitlist signup endpoint (ADR-0039 P1 precursor, Issue #203).

Route:
    POST /api/waitlist  — public, no auth required.

Security:
    - Per-IP sliding-window rate limit (5 req/IP/60 s) to prevent email bombing.
    - hCaptcha verification when HCAPTCHA_SECRET is set (skipped in dev mode).
    - Naive email format check (matches signup.py precedent); stricter validation
      deferred to P1 if email-validator dep is added.
    - Source fixed to 'pricing-page' for this MVP; extend when adding new forms.

Response codes:
    201  — successfully subscribed.
    409  — email already on the waitlist (ON CONFLICT DO NOTHING rowcount=0).
    400  — validation error (bad email, bad plan, captcha failure).
    429  — rate limit exceeded (5/min per IP).
    500  — unexpected DB or internal error.
"""

import logging
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.web_ui._json import _json_safe
from src.web_ui.email import send_waitlist_notify_email
from src.web_ui.rate_limit import check_ip_rate_limit, get_client_ip

logger = logging.getLogger(__name__)
router = APIRouter(tags=["waitlist"])

# Allowed plan values for the optional plan field.
_ALLOWED_PLANS: frozenset[str] = frozenset({"free", "pro", "team"})

# Source tag — fixed for this MVP; extend for additional intake forms later.
_SOURCE = "pricing-page"


class WaitlistRequest(BaseModel):
    email: str
    plan: str | None = None
    hcaptcha_token: str | None = None


async def _verify_hcaptcha(token: str | None, remote_ip: str) -> bool:
    """Return True if hCaptcha response is valid.

    Mirrors src/web_ui/routes/signup.py._verify_hcaptcha.
    Skips verification when HCAPTCHA_SECRET is unset (dev mode).
    Skips verification when token is None/empty (also dev/non-captcha path).
    """
    secret = os.getenv("HCAPTCHA_SECRET")
    if not secret:
        logger.warning("HCAPTCHA_SECRET unset — skipping captcha verification (dev mode)")
        return True
    if not token:
        logger.warning("hCaptcha token missing — rejecting (captcha enabled)")
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.hcaptcha.com/siteverify",
                data={"secret": secret, "response": token, "remoteip": remote_ip},
            )
        return resp.json().get("success", False)
    except Exception as exc:
        logger.error("hCaptcha verification error: %s", exc)
        return False


def _validate_email(email: str) -> bool:
    """Naive email format check (mirrors signup.py precedent).

    Accepts addresses with '@' and at least one '.' after '@', capped at 254
    characters (RFC 5321 max). Stricter validation with email-validator can be
    added in P1 without a breaking schema change.
    """
    if not email or len(email) > 254:
        return False
    if "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        return False
    return True


@router.post("/api/waitlist", status_code=201)
async def join_waitlist(body: WaitlistRequest, request: Request):
    """Add an email address to the waitlist.

    Returns 201 on first-time subscribe, 409 if already on the list.
    Rate limited to 5 requests per IP per minute.
    """
    # 1. Rate limit check (per IP, 5/60 s).
    client_ip = await get_client_ip(request)
    allowed = await check_ip_rate_limit(client_ip, limit=5, window_seconds=60)
    if not allowed:
        return JSONResponse(
            _json_safe({"error": "rate_limited", "retry_after": 60}),
            status_code=429,
        )

    # 2. Email validation.
    email = (body.email or "").strip().lower()
    if not _validate_email(email):
        return JSONResponse(
            _json_safe({"error": "invalid_email", "detail": "A valid email address is required."}),
            status_code=400,
        )

    # 3. Plan validation (if provided).
    plan = (body.plan or "").strip().lower() or None
    if plan is not None and plan not in _ALLOWED_PLANS:
        return JSONResponse(
            _json_safe({
                "error": "invalid_plan",
                "detail": f"plan must be one of: {sorted(_ALLOWED_PLANS)} or omitted.",
            }),
            status_code=400,
        )

    # 4. hCaptcha verification (skipped in dev mode when HCAPTCHA_SECRET unset).
    if not await _verify_hcaptcha(body.hcaptcha_token, client_ip):
        logger.warning("Waitlist: captcha failed (IP=%s email=%s)", client_ip, email)
        return JSONResponse(
            _json_safe({"error": "captcha_failed", "detail": "Captcha verification failed."}),
            status_code=400,
        )

    # 5. DB insert — ON CONFLICT DO NOTHING (email UNIQUE).
    try:
        from src.db.pg import get_pool

        pool = get_pool()
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO waitlist_emails (email, plan, source)"
                    " VALUES (%s, %s, %s)"
                    " ON CONFLICT (email) DO NOTHING",
                    (email, plan, _SOURCE),
                )
                rowcount = cur.rowcount
        # rowcount == 0 means ON CONFLICT triggered — email already exists.
        if rowcount == 0:
            logger.info("Waitlist: duplicate email=%s plan=%s", email, plan)
            return JSONResponse(
                _json_safe({
                    "error": "already_subscribed",
                    "detail": "You are already on the waitlist.",
                }),
                status_code=409,
            )
    except Exception as exc:
        logger.error("Waitlist DB error for email=%s: %s", email, exc)
        return JSONResponse(
            _json_safe({
                "error": "internal_error",
                "detail": "Failed to save your signup. Please try again.",
            }),
            status_code=500,
        )

    logger.info(
        "Waitlist: subscribed email=%s plan=%s source=%s ip=%s",
        email, plan, _SOURCE, client_ip,
    )

    # 6. Admin notification — best-effort; never fail the endpoint on SMTP error.
    try:
        ok = send_waitlist_notify_email(submitter_email=email, plan=plan, source=_SOURCE)
        if not ok:
            logger.warning(
                "Waitlist: admin notify failed for email=%s (SMTP error, non-fatal)", email
            )
    except Exception as notify_exc:
        logger.warning(
            "Waitlist: admin notify exception for email=%s: %s (non-fatal)",
            email, notify_exc,
        )

    return JSONResponse(
        _json_safe({"status": "subscribed", "email": email}),
        status_code=201,
    )
