# src/web_ui/middleware.py
"""Auth-required middleware for Web UI (M7 W16).

AuthRequiredMiddleware:
    Every request not matching the exempt paths below must have a valid
    session cookie (set by POST /login) whose "session_at" timestamp is
    within SESSION_TTL_SECONDS.

Exempt paths (no auth required):
    /login          GET + POST
    /logout         GET
    /static/*       Static assets
    /health         Health probe (if present on this port)
"""

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from src.web_ui.auth import SESSION_TTL_SECONDS

# Paths exempt from authentication
_EXEMPT_PREFIXES = ("/static/",)
_EXEMPT_EXACT = {"/login", "/logout", "/health"}


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
    return age < SESSION_TTL_SECONDS


class AuthRequiredMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login (except exempt paths)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if _is_exempt(request.url.path):
            return await call_next(request)

        if _session_valid(request):
            return await call_next(request)

        # Preserve original path as ?next= for post-login redirect
        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        from urllib.parse import quote_plus

        return RedirectResponse(
            url=f"/login?next={quote_plus(next_path)}",
            status_code=302,
        )
