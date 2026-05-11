# src/web_ui/routes/login.py
"""Login / logout endpoints for Web UI session auth (M7 W16)."""

import logging
import time
from typing import Annotated
from urllib.parse import unquote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from src.web_ui.auth import verify_password

logger = logging.getLogger(__name__)
router = APIRouter()

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
    """Return True if *ip* has ≥ _RATE_MAX_FAILURES failures in the last window."""
    now = time.time()
    failures = _LOGIN_FAILURES.get(ip, [])
    recent = [t for t in failures if now - t < _RATE_WINDOW_SECONDS]
    _LOGIN_FAILURES[ip] = recent
    return len(recent) >= _RATE_MAX_FAILURES


def _get_conn():
    import psycopg2

    from src import config

    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        return None
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception:
        return None


def _lookup_user(conn, username: str) -> str | None:
    """Return password_hash for username, or None if not found."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT password_hash FROM webui_users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
        return row[0] if row else None


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    """Render the login form."""
    # If already authenticated, redirect to dashboard
    from src.web_ui.middleware import _session_valid

    if _session_valid(request):
        return RedirectResponse("/", status_code=302)

    templates = request.app.state.templates
    error = request.query_params.get("error")
    next_url = request.query_params.get("next", "/")
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "next": next_url},
    )


@router.post("/login")
async def login_post(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    """Verify credentials, set session, redirect."""
    from urllib.parse import quote_plus

    # Per-IP rate limiting: reject after too many consecutive failures.
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(client_ip):
        logger.warning("Login rate-limited for IP %s", client_ip)
        return JSONResponse(
            {"error": "Too many failed login attempts; wait 60s"},
            status_code=429,
        )

    # Validate redirect target (only allow relative paths)
    safe_next = "/"
    if next and next.startswith("/") and not next.startswith("//"):
        safe_next = unquote_plus(next)

    conn = _get_conn()
    if conn is None:
        logger.error("Login: cannot connect to PostgreSQL")
        error_url = f"/login?error=db_unavailable&next={quote_plus(safe_next)}"
        return RedirectResponse(error_url, status_code=302)

    try:
        pw_hash = _lookup_user(conn, username.strip())
    except Exception as exc:
        logger.error("Login DB error: %s", exc)
        pw_hash = None
    finally:
        conn.close()

    if pw_hash is None or not verify_password(password, pw_hash):
        logger.warning("Failed login attempt for user %r (IP: %s)", username, client_ip)
        _record_failure(client_ip)
        error_url = f"/login?error=invalid_credentials&next={quote_plus(safe_next)}"
        return RedirectResponse(error_url, status_code=302)

    # Credentials OK — clear failure counter + set session
    _clear_failures(client_ip)
    request.session["username"] = username.strip()
    request.session["session_at"] = time.time()
    logger.info("Successful login for user %r (IP: %s)", username, client_ip)
    return RedirectResponse(safe_next, status_code=302)


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
