# src/web_ui/app.py
"""FastAPI Web UI application — admin interface, port 8003, localhost-only."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import Response

_logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def _lifespan(app):
    """FastAPI lifespan: startup cleanup for stale indexer jobs."""
    try:
        import psycopg2

        from src import config
        from src.db import job_registry

        dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
        if dsn:
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            try:
                cleaned = job_registry.mark_dead_jobs(conn)
                if cleaned:
                    _logger.warning(
                        "Startup cleanup: marked %d stale indexer job(s) as error", cleaned
                    )
            except Exception as exc:
                _logger.warning("Startup job cleanup failed (non-fatal): %s", exc)
            finally:
                conn.close()
    except Exception as exc:
        _logger.warning("Startup job cleanup failed (non-fatal): %s", exc)
    yield


class _LoopbackOnlyMiddleware(BaseHTTPMiddleware):
    """Reject requests from non-loopback addresses (I6 — CSRF mitigation)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)


def create_app() -> FastAPI:
    """Create and configure the Web UI FastAPI app."""
    app = FastAPI(
        title="Odoo Semantic MCP — Admin",
        description="Admin interface for managing profiles, repos, API keys, and SSH keys.",
        docs_url=None,  # Disable OpenAPI docs in admin UI
        redoc_url=None,
        lifespan=_lifespan,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    # Middleware ordering (FastAPI/Starlette add_middleware is LIFO):
    # Last added = outermost (runs first in request chain).
    # Required order (outermost → innermost):
    #   1. LoopbackOnly  — reject non-loopback (I6 CSRF mitigation) before anything else.
    #   2. SessionMiddleware — populate request.session from signed cookie.
    #   3. AuthRequiredMiddleware — check request.session for valid login.
    # Add in REVERSE order so the last add_middleware call = outermost.

    # Innermost: auth check (added first → innermost)
    from src.web_ui.middleware import AuthRequiredMiddleware

    app.add_middleware(AuthRequiredMiddleware)

    # Middle: session cookie parsing
    from src.web_ui.auth import get_session_secret

    # WEBUI_SECURE_COOKIE=1 (default) → Secure flag; set to 0 for local dev over plain HTTP.
    # WARNING: setting to 0 in production allows session hijacking over plain HTTP.
    https_only = os.environ.get("WEBUI_SECURE_COOKIE", "1") == "1"
    app.add_middleware(
        SessionMiddleware,
        secret_key=get_session_secret(),
        session_cookie="osm_session",
        same_site="strict",
        https_only=https_only,
        max_age=None,  # Session cookie (browser-close expiry); TTL enforced by session_at
    )

    # Outermost: loopback IP check (added last → runs first)
    app.add_middleware(_LoopbackOnlyMiddleware)

    # Login/logout routes (exempt from auth by AuthRequiredMiddleware)
    from src.web_ui.routes import login

    app.include_router(login.router)

    from src.web_ui.routes import dashboard

    app.include_router(dashboard.router)

    from src.web_ui.routes import api_keys, repos, ssh_keys

    app.include_router(repos.router)
    app.include_router(api_keys.router)
    app.include_router(ssh_keys.router)

    from src.web_ui.routes import feedback

    app.include_router(feedback.router)

    from src.web_ui.routes import operations

    app.include_router(operations.router)

    return app
