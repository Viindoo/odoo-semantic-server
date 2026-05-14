# src/web_ui/middleware.py
"""Auth-required middleware for Web UI (M8 W1 — pure JSON API).

AuthRequiredMiddleware:
    Every request not matching the exempt paths below must have a valid
    session cookie (set by POST /api/auth/login) whose "session_at" timestamp is
    within SESSION_TTL_SECONDS.

Exempt paths (no auth required):
    /api/auth/login     POST
    /api/auth/logout    POST
    /api/auth/verify    GET
    /health             Health probe (if present on this port)
    /openapi.json       FastAPI schema (intentionally public — used by
                        tests/browser/conftest.py api_server fixture for
                        readiness polling, and by external API consumers)
    /docs, /redoc       Interactive docs render the same schema
"""

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.web_ui.auth import SESSION_TTL_SECONDS, is_test_bypass_active

# Paths exempt from authentication. /openapi.json is unauthenticated by design
# (FastAPI's schema is meant to be introspected); /docs and /redoc are the
# matching interactive UIs. Keeping these out of the auth scope is required for
# the api_server fixture in tests/browser/conftest.py to poll readiness — the
# wait would otherwise see 401 and time out before the subprocess is healthy.
_EXEMPT_PREFIXES = ("/api/auth/",)
_EXEMPT_EXACT = {"/health", "/openapi.json", "/docs", "/redoc"}


def _is_exempt(path: str) -> bool:
    if path in _EXEMPT_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _session_valid(request: Request) -> bool:
    """Return True if session contains a non-expired username."""
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


class AuthRequiredMiddleware(BaseHTTPMiddleware):
    """Return 401 JSON for unauthenticated requests to non-exempt paths."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Test-only bypass: BOTH env vars must be set so a misconfigured
        # production deployment (or copy-pasted .env) cannot accidentally
        # disable auth. See auth.is_test_bypass_active() for rationale.
        if is_test_bypass_active():
            return await call_next(request)
        if _is_exempt(request.url.path):
            return await call_next(request)

        if _session_valid(request):
            return await call_next(request)

        return JSONResponse(
            {"error": "not_authenticated", "detail": "Valid session required"},
            status_code=401,
        )
