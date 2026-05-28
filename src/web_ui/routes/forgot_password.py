# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/forgot_password.py
"""Self-serve forgot-password endpoint (W1D-1).

Route:
    POST /api/auth/forgot-password  — public, no auth required.

Security:
    - No enumeration: always returns 200 {"status": "ok"} regardless of whether
      the email exists, is verified, etc.  (Only 429 breaks this for rate-limit.)
    - Timing channel closed by BackgroundTasks (WFIX-1 MEDIUM): both the
      verified-user path (DB INSERT + SMTP) and the unknown-user path execute the
      same foreground code; all side-effects run in the background after the
      response is sent, making the two branches indistinguishable by timing.
    - Token stored as sha256(raw_token) in email_verifications (same as signup.py).
    - Raw token is emailed; DB stores only the hash — DB leak cannot be used
      directly for account takeover.
    - Per-IP sliding-window rate limit (3 req/IP/60 s) to prevent email bombing.
    - Email never logged in plain text (only redacted marker logged).
    - Infrastructure failures (DB lookup, DB INSERT, SMTP) are logged with stable
      structured keys and increment Prometheus counters; they do NOT propagate to
      the HTTP response (response is already sent when the background task runs).
"""

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.db.audit import write_audit_log
from src.db.pg import get_pool
from src.metrics import (
    forgot_password_db_failure_total,
    forgot_password_email_send_failure_total,
    forgot_password_success_total,
)
from src.web_ui._json import _json_safe
from src.web_ui.email import send_password_reset_email
from src.web_ui.rate_limit import check_ip_rate_limit, get_client_ip

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth")

# No-enumeration sentinel — same body for every successful call.
_OK_RESPONSE = {"status": "ok"}


class ForgotPasswordBody(BaseModel):
    email: str


def _get_base_url(request: Request) -> str:
    """Infer public base URL from request headers (nginx sets X-Forwarded-Proto/Host)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto}://{host}"


# ---------------------------------------------------------------------------
# Background task — all side-effects run here after response is already sent
# ---------------------------------------------------------------------------


def _process_forgot_password(email: str, base_url: str, client_ip: str) -> None:
    """DB lookup + token INSERT + SMTP send.

    All three side-effects run in the background after the HTTP response has
    been sent.  This ensures the response timing is identical for all input
    branches (timing-channel defence, WFIX-1 MEDIUM).

    Failures are logged with stable structured keys and Prometheus counters.
    The function MUST NOT re-raise — it has no caller that can handle exceptions
    (BackgroundTasks silently drops unhandled exceptions).
    """
    # --- DB lookup + INSERT --------------------------------------------------
    try:
        pool = get_pool()
        with pool.checkout() as conn:
            user_row = pool.fetch_one(
                conn,
                "SELECT id, username, email_verified"
                " FROM webui_users"
                " WHERE email = %s",
                (email,),
            )

        if user_row is None or not user_row.get("email_verified"):
            # Unknown or unverified — no token, no email, no error.
            logger.debug(
                "forgot_password.bg.no_verified_user email_suffix=...%s",
                email[-4:],
            )
            return

        user_id: int = user_row["id"]
        username: str = user_row["username"]

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires_at = datetime.now(UTC) + timedelta(hours=24)

        with pool.checkout() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO email_verifications"
                        " (token, user_id, purpose, expires_at)"
                        " VALUES (%s, %s, 'password_reset', %s)",
                        (token_hash, user_id, expires_at),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True

    except Exception as exc:
        forgot_password_db_failure_total.inc()
        logger.error(
            "forgot_password.bg.db_failure email_suffix=...%s err=%s",
            email[-4:],
            exc,
            exc_info=True,
        )
        return

    # --- SMTP send -----------------------------------------------------------
    try:
        send_password_reset_email(
            to=email,
            username=username,
            token=raw_token,
            base_url=base_url,
        )
        forgot_password_success_total.inc()
    except Exception as exc:
        forgot_password_email_send_failure_total.inc()
        logger.error(
            "forgot_password.bg.email_send_failure"
            " user_id=%s email_suffix=...%s err=%s",
            user_id,
            email[-4:],
            exc,
            exc_info=True,
        )
        return

    # --- Audit log -----------------------------------------------------------
    logger.info(
        "forgot_password.bg.token_issued user_id=%s ip=%s",
        user_id,
        client_ip,
    )
    try:
        write_audit_log(
            actor="anonymous",
            action="auth.forgot_password_request",
            target=str(user_id),
            success=True,
            detail={"ip": client_ip},
        )
    except Exception as exc:
        logger.warning(
            "forgot_password.bg.audit_failure user_id=%s err=%s",
            user_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/forgot-password")
async def forgot_password(
    body: ForgotPasswordBody,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Request a password-reset link.

    Always returns 200 {"status": "ok"} — never reveals whether the email
    exists in the system (no enumeration).  Returns 429 on rate limit.

    All side-effects (DB lookup, token INSERT, SMTP send) are handed off to a
    BackgroundTask so the response is returned immediately on both the
    verified-user and unknown-user branches.  The two branches are therefore
    timing-indistinguishable from the client's perspective (WFIX-1 MEDIUM).

    Flow:
      1. Per-IP rate limit check (3/60 s) — synchronous, can return 429.
      2. Basic email format check — if invalid, return 200 immediately.
         (No DB hit; no background task needed on the invalid-format branch.)
      3. Enqueue background task: DB lookup, optional token INSERT + SMTP send.
      4. Return 200 immediately.
    """
    client_ip = await get_client_ip(request)

    # 1. Rate limit check (synchronous — must return 429 before response).
    allowed = await check_ip_rate_limit(client_ip, limit=3, window_seconds=60)
    if not allowed:
        logger.warning(
            "forgot-password: rate limit exceeded (IP=%s)",
            client_ip,
        )
        return JSONResponse(
            _json_safe({"error": "rate_limited", "retry_after": 60}),
            status_code=429,
            headers={"Retry-After": "60"},
        )

    # 2. Basic email format check — constant-time false branch (no DB hit).
    email = (body.email or "").strip().lower()
    if not email or "@" not in email:
        logger.debug("forgot-password: invalid email format (IP=%s)", client_ip)
        return JSONResponse(_json_safe(_OK_RESPONSE))

    # 3. Enqueue background task; respond immediately regardless of whether
    #    the email belongs to a real verified user.
    base_url = _get_base_url(request)
    background_tasks.add_task(_process_forgot_password, email, base_url, client_ip)

    # 4. Return no-enumeration 200 immediately.
    return JSONResponse(_json_safe(_OK_RESPONSE))
