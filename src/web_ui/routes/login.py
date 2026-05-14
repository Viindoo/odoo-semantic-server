# src/web_ui/routes/login.py
"""Auth endpoints for Web UI session auth (M8 W1 — pure JSON API)."""

import logging
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.web_ui.auth import is_test_bypass_active, verify_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth")

# ---------------------------------------------------------------------------
# Per-IP login rate limiting (M7 Opus review finding #17)
# ---------------------------------------------------------------------------
# Module-level dict: {ip: [timestamps of recent failed login attempts]}
# Evict entries older than _RATE_WINDOW_SECONDS on each access.
_LOGIN_FAILURES: dict[str, list[float]] = {}
_RATE_WINDOW_SECONDS = 60.0
_RATE_MAX_FAILURES = 5


def _record_failure(ip: str) -> None:
    """Record a failed login attempt for *ip* and evict old entries."""
    now = time.time()
    failures = _LOGIN_FAILURES.get(ip, [])
    failures = [t for t in failures if now - t < _RATE_WINDOW_SECONDS]
    failures.append(now)
    _LOGIN_FAILURES[ip] = failures


def _clear_failures(ip: str) -> None:
    """Clear recorded failures for *ip* after a successful login."""
    _LOGIN_FAILURES.pop(ip, None)


def _is_rate_limited(ip: str) -> bool:
    """Return True if *ip* has >= _RATE_MAX_FAILURES failures in the last window."""
    now = time.time()
    failures = _LOGIN_FAILURES.get(ip, [])
    recent = [t for t in failures if now - t < _RATE_WINDOW_SECONDS]
    _LOGIN_FAILURES[ip] = recent
    return len(recent) >= _RATE_MAX_FAILURES


def _lookup_user(username: str) -> str | None:
    """Return password_hash for username, or None if not found."""
    from src.db.pg import auth_store

    return auth_store().get_user_password_hash(username)


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login_post(body: LoginBody, request: Request):
    """Verify credentials, set session cookie, return JSON."""
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(client_ip):
        logger.warning("Login rate-limited for IP %s", client_ip)
        return JSONResponse(
            {"error": "Too many failed login attempts; wait 60s"},
            status_code=429,
        )

    try:
        pw_hash = _lookup_user(body.username.strip())
    except Exception as exc:
        logger.error("Login DB error: %s", exc)
        pw_hash = None

    if pw_hash is None or not verify_password(body.password, pw_hash):
        logger.warning("Failed login attempt for user %r (IP: %s)", body.username, client_ip)
        _record_failure(client_ip)
        return JSONResponse({"error": "invalid_credentials"}, status_code=401)

    # Credentials OK — clear failure counter + set session
    _clear_failures(client_ip)
    request.session["username"] = body.username.strip()
    request.session["session_at"] = time.time()
    logger.info("Successful login for user %r (IP: %s)", body.username, client_ip)
    return JSONResponse({"ok": True, "username": body.username.strip()})


@router.post("/logout")
async def logout(request: Request):
    """Clear session cookie."""
    request.session.clear()
    return JSONResponse({"ok": True})


@router.get("/verify")
async def verify_session(request: Request):
    """Return 200 + username if session is valid, 401 if not.

    Used by Astro middleware to check session before serving protected pages.

    Honors the same WEBUI_AUTH_DISABLED + PYTEST_CURRENT_TEST bypass as
    AuthRequiredMiddleware so browser tests can verify admin pages without
    seeding a session cookie. The double-env guard makes the bypass safe in
    production (PYTEST_CURRENT_TEST is never set by ops).
    """
    from src.web_ui.middleware import _session_valid

    if is_test_bypass_active():
        return JSONResponse({"ok": True, "username": "test-user"})
    if _session_valid(request):
        return JSONResponse({"ok": True, "username": request.session.get("username")})
    return JSONResponse({"ok": False, "error": "not_authenticated"}, status_code=401)
