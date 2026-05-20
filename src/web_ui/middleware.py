# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/middleware.py
"""Auth-required middleware for Web UI (M9 W-AC hardening).

AuthRequiredMiddleware:
    Every request not matching the exempt paths below must have a valid
    session cookie (set by POST /api/auth/login) whose "session_at" timestamp is
    within SESSION_TTL_SECONDS.

    F7: additionally validates session_id against the active_sessions table so
    that logout / server-side revoke takes effect immediately on the next request.

Exempt paths (no auth required):
    /api/auth/login        POST
    /api/auth/logout       POST
    /api/auth/verify       GET
    /api/auth/totp/login   POST (second-factor MFA step — no full session yet)
    /api/health            Uptime monitoring (pre-launch checklist §10.5)
    /health                Health probe (if present on this port)
    /openapi.json          FastAPI schema (intentionally public — used by
                           tests/browser/conftest.py api_server fixture for
                           readiness polling, and by external API consumers)
    /docs, /redoc          Interactive docs render the same schema

MFA enforcement (M9 W-MF):
    Admin users (is_admin=TRUE) with TOTP not yet enabled and account older
    than MFA_GRACE_DAYS are redirected to a 403 JSON response with
    {"error": "mfa_required", "redirect": "/admin/security?force_mfa=1"}.
    Front-end Astro middleware handles the actual redirect.
"""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.web_ui._json import _json_safe
from src.web_ui.auth import SESSION_TTL_SECONDS, is_test_bypass_active

logger = logging.getLogger(__name__)

# MFA grace period for admin users (days before enforcement kicks in)
MFA_GRACE_DAYS = 7

# Paths exempt from authentication. /openapi.json is unauthenticated by design
# (FastAPI's schema is meant to be introspected); /docs and /redoc are the
# matching interactive UIs. Keeping these out of the auth scope is required for
# the api_server fixture in tests/browser/conftest.py to poll readiness — the
# wait would otherwise see 401 and time out before the subprocess is healthy.
# /api/health is exempt for uptime monitoring (pre-launch checklist §10.5).
_EXEMPT_PREFIXES = ("/api/auth/",)
_EXEMPT_EXACT = {"/health", "/api/health", "/openapi.json", "/docs", "/redoc"}

# API paths that deliver MFA setup — exempt from MFA enforcement check
# to avoid a redirect loop when the admin is trying to enroll.
_MFA_SETUP_PATHS = {"/api/auth/totp/setup", "/api/auth/totp/verify", "/api/auth/totp/status"}


def _is_exempt(path: str) -> bool:
    if path in _EXEMPT_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _session_valid(request: Request) -> bool:
    """Return True if session contains a non-expired username.

    Checks signed cookie timestamp only (no DB call — fast path for middleware).
    F7 server-side DB validation is done by verify_session endpoint and in
    AuthRequiredMiddleware.dispatch when session_id is present.
    """
    session = request.session
    username = session.get("username")
    if not username:
        return False
    session_at = session.get("session_at")
    if not session_at:
        return False
    try:
        age = time.time() - float(session_at)
    except (TypeError, ValueError):
        return False
    # Require non-negative age: a future session_at (age < 0) is invalid.
    # This closes the edge case where a tampered or clock-skewed session_at
    # in the far future would satisfy `age < SESSION_TTL_SECONDS` indefinitely.
    return 0 <= age < SESSION_TTL_SECONDS


def _server_session_valid(session_id: str) -> bool:
    """Return True if session_id exists and is not expired in active_sessions.

    F7: server-side validation provides instant revoke on logout.
    Fails open (returns True) if the DB is unreachable, to avoid blocking
    legitimate requests during DB maintenance. Logs a warning on failure.
    """
    from src.web_ui.routes.login import _lookup_session, _update_session_last_seen

    try:
        row = _lookup_session(session_id)
        if row is None:
            return False
        # Update last_seen (sliding window) — best-effort
        _update_session_last_seen(session_id)
        return True
    except Exception as exc:
        logger.warning("_server_session_valid DB error (fail-open): %s", exc)
        return True  # fail-open


def _check_mfa_enforcement(username: str) -> bool:
    """Return True if admin user MUST enroll TOTP (grace period expired).

    Conditions (all must be true):
      1. webui_users.is_admin = TRUE for the username.
      2. No enabled totp_secrets row (MFA not yet enrolled).
      3. Account created more than MFA_GRACE_DAYS ago.

    Returns False on any DB error (fail-open — don't block the admin).
    """
    try:
        from src.db.pg import auth_store

        pool = auth_store()._pool
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                # Check is_admin column — gracefully handle missing column
                # (pre-M9 schema that hasn't run oauth migration yet).
                cur.execute(
                    """
                    SELECT
                        COALESCE(wu.is_admin, FALSE),
                        wu.created_at,
                        (SELECT COUNT(*) FROM totp_secrets ts
                         WHERE ts.user_id = wu.id AND ts.enabled = TRUE) > 0
                    FROM webui_users wu
                    WHERE wu.username = %s
                    """,
                    (username,),
                )
                row = cur.fetchone()
        if row is None:
            return False
        is_admin, created_at, totp_enabled = row
        if not is_admin or totp_enabled:
            return False
        # Check grace period
        if created_at is None:
            return False
        age_days = (time.time() - created_at.timestamp()) / 86400
        return age_days >= MFA_GRACE_DAYS
    except Exception as exc:
        logger.debug("MFA enforcement check failed (fail-open): %s", exc)
        return False


class AuthRequiredMiddleware(BaseHTTPMiddleware):
    """Return 401 JSON for unauthenticated requests to non-exempt paths.

    After session validation, admin users who have not enrolled TOTP and whose
    account is older than MFA_GRACE_DAYS receive a 403 with mfa_required flag.
    Front-end redirects them to /admin/security?force_mfa=1.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Test-only bypass: BOTH env vars must be set so a misconfigured
        # production deployment (or copy-pasted .env) cannot accidentally
        # disable auth. See auth.is_test_bypass_active() for rationale.
        if is_test_bypass_active():
            return await call_next(request)
        if _is_exempt(request.url.path):
            return await call_next(request)

        if not _session_valid(request):
            return JSONResponse(
                _json_safe({"error": "not_authenticated", "detail": "Valid session required"}),
                status_code=401,
            )

        # F7: server-side session validation (instant revoke after logout).
        # Only enforced when session_id is present in the cookie (post-M9 login).
        # Legacy sessions without session_id still pass cookie check above.
        session_id = request.session.get("session_id")
        if session_id and not _server_session_valid(session_id):
            return JSONResponse(
                _json_safe({"error": "not_authenticated", "detail": "Session revoked"}),
                status_code=401,
            )

        # MFA enforcement for admin users (W-MF grace period check).
        # Skip enforcement for MFA setup/verify paths (avoid redirect loop).
        username = request.session.get("username")
        if username and request.url.path not in _MFA_SETUP_PATHS:
            if _check_mfa_enforcement(username):
                return JSONResponse(
                    _json_safe(
                        {
                            "error": "mfa_required",
                            "detail": "Admin MFA enrollment required",
                            "redirect": "/admin/security?force_mfa=1",
                        }
                    ),
                    status_code=403,
                )

        return await call_next(request)
