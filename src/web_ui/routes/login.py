# src/web_ui/routes/login.py
"""Login / logout endpoints for Web UI session auth (M7 W16)."""

import logging
import time
from typing import Annotated
from urllib.parse import unquote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web_ui.auth import verify_password

logger = logging.getLogger(__name__)
router = APIRouter()


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
        logger.warning("Failed login attempt for user %r", username)
        error_url = f"/login?error=invalid_credentials&next={quote_plus(safe_next)}"
        return RedirectResponse(error_url, status_code=302)

    # Credentials OK — set session
    request.session["username"] = username.strip()
    request.session["session_at"] = time.time()
    logger.info("Successful login for user %r", username)
    return RedirectResponse(safe_next, status_code=302)


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
