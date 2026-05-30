# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/waitlist.py
"""Self-hosted waitlist signup endpoint (ADR-0039 P1 precursor, Issue #203).

Route:
    POST /api/waitlist  — public, no auth required.

Security:
    - Per-IP sliding-window rate limit (5 req/IP/60 s) to prevent email bombing.
    - Naive email format check (matches signup.py precedent); stricter validation
      deferred to P1 if email-validator dep is added.
    - Source fixed to 'pricing-page' for this MVP; extend when adding new forms.

Response codes:
    201  — successfully subscribed.
    409  — email already on the waitlist (ON CONFLICT DO NOTHING rowcount=0).
    400  — validation error (bad email, bad plan).
    429  — rate limit exceeded (5/min per IP).
    500  — unexpected DB or internal error.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.web_ui._json import _json_safe
from src.web_ui.email import send_waitlist_notify_email
from src.web_ui.rate_limit import check_ip_rate_limit, get_client_ip

logger = logging.getLogger(__name__)
router = APIRouter(tags=["waitlist"])

# Source tag — fixed for this MVP; extend for additional intake forms later.
_SOURCE = "pricing-page"


def _public_plan_slugs(conn) -> set[str]:
    """Return the set of slugs for public, non-archived plans (C4 — ADR-0039).

    Derived from the DB so adding a new public plan never requires a code change
    to this allow-list.  Called with the same connection that join_waitlist already
    opens for the INSERT — no extra pool checkout.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT slug FROM plans WHERE is_public = TRUE AND is_archived = FALSE"
        )
        return {r[0] for r in cur.fetchall()}


class WaitlistRequest(BaseModel):
    email: str
    plan: str | None = None


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
            headers={"Retry-After": "60"},
        )

    # 2. Email validation.
    email = (body.email or "").strip().lower()
    if not _validate_email(email):
        return JSONResponse(
            _json_safe({"error": "invalid_email", "detail": "A valid email address is required."}),
            status_code=400,
        )

    # 3. Plan validation (if provided) + DB insert in same connection.
    plan = (body.plan or "").strip().lower() or None

    # 4. DB insert — ON CONFLICT DO NOTHING (email UNIQUE).
    try:
        from src.db.pg import get_pool

        pool = get_pool()
        with pool.checkout() as conn:
            # Derive allowed plans from DB (C4 — ADR-0039): no code change needed
            # when a new public plan is added.
            if plan is not None and plan not in _public_plan_slugs(conn):
                allowed = sorted(_public_plan_slugs(conn))
                return JSONResponse(
                    _json_safe({
                        "error": "invalid_plan",
                        "detail": f"plan must be one of: {allowed} or omitted.",
                    }),
                    status_code=400,
                )

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

    # 5. Admin notification — best-effort; never fail the endpoint on SMTP error.
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
