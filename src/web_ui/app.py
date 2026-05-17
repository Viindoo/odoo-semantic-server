# src/web_ui/app.py
"""FastAPI Web UI application — pure JSON API, port 8003, localhost-only (M8 W1)."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.web_ui._json import _json_safe

_logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app):
    """FastAPI lifespan: startup cleanup for stale indexer jobs."""
    try:
        from src import config
        from src.constants import PG_POOL_MAX_CONN, PG_POOL_MIN_CONN
        from src.db.pg import init_pool, job_store

        dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
        if dsn:
            init_pool(dsn, min_conn=PG_POOL_MIN_CONN, max_conn=PG_POOL_MAX_CONN)
            try:
                cleaned = job_store().mark_dead_jobs()
                if cleaned:
                    _logger.warning(
                        "Startup cleanup: marked %d stale indexer job(s) as error", cleaned
                    )
            except Exception as exc:
                _logger.warning("Startup job cleanup failed (non-fatal): %s", exc)
    except Exception as exc:
        _logger.warning("Startup job cleanup failed (non-fatal): %s", exc)
    yield


class _LoopbackOnlyMiddleware(BaseHTTPMiddleware):
    """Reject requests from non-loopback addresses (I6 — CSRF mitigation)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1"):
            return JSONResponse(_json_safe({"error": "forbidden"}), status_code=403)
        return await call_next(request)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject security headers on every FastAPI response (M9 CSP hardening).

    FastAPI is a JSON-only API (ADR-0015) — strictest CSP applies:
    `default-src 'none'` forbids all resource loading; browsers never
    render HTML from this layer.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        # https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        # https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Permissions-Policy
        response.headers["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()"
        )
        return response


class _MaintenanceModeMiddleware(BaseHTTPMiddleware):
    """Return 503 for non-restore requests while a restore is in progress.

    M9 W-RS OWASP item 7: maintenance mode blocks all non-restore API calls
    with a 503 + Retry-After: 60 header to allow clients to back off.
    The restore endpoint itself (/api/operations/restore) is allowed through
    so the 409 concurrent-restore guard can respond correctly.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        from src.web_ui.routes.operations import _RESTORE_IN_PROGRESS

        if _RESTORE_IN_PROGRESS.is_set():
            # Allow the restore endpoint through (for 409 concurrent guard)
            if request.url.path == "/api/operations/restore":
                return await call_next(request)
            # Allow health checks through
            if request.url.path in ("/health", "/openapi.json"):
                return await call_next(request)
            return JSONResponse(
                _json_safe(
                    {
                        "error": "service_in_maintenance",
                        "detail": "Restore in progress — try again shortly",
                    }
                ),
                status_code=503,
                headers={"Retry-After": "60"},
            )
        return await call_next(request)


def create_app() -> FastAPI:
    """Create and configure the Web UI FastAPI app (pure JSON API)."""
    app = FastAPI(
        title="Odoo Semantic MCP — Admin",
        description="Admin JSON API for managing profiles, repos, API keys, and SSH keys.",
        # Disabled per ADR-0015 (pure JSON API). Astro renders all admin UI;
        # FastAPI never serves HTML docs. /docs and /redoc must remain None.
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # Middleware ordering (FastAPI/Starlette add_middleware is LIFO):
    # Last added = outermost (runs first on request, last on response).
    # Required order (outermost → innermost):
    #   1. SecurityHeaders    — inject CSP/Permissions-Policy on every response.
    #   2. LoopbackOnly       — reject non-loopback (I6 CSRF mitigation) before anything else.
    #   3. MaintenanceMode    — block all non-restore requests during restore (M9 W-RS).
    #   4. SessionMiddleware  — populate request.session from signed cookie.
    #   5. AuthRequiredMiddleware — check request.session for valid login.
    #   6. CORSMiddleware     — documented no-op (see comment below).
    # Add in REVERSE order so the last add_middleware call = outermost.

    # Innermost: CORS (added first → innermost).
    # Documented no-op: Astro SSR proxies all /api/* calls through nginx,
    # so browsers never make direct cross-origin requests to FastAPI :8003.
    # CORSMiddleware here makes intent explicit — no cross-origin access allowed.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],  # empty = no cross-origin allowed
        allow_credentials=False,
        allow_methods=[],
        allow_headers=[],
    )

    # Auth check (added second → runs just outside CORSMiddleware)
    from src.web_ui.middleware import AuthRequiredMiddleware

    app.add_middleware(AuthRequiredMiddleware)

    # Middle: session cookie parsing
    from src.web_ui.auth import get_session_secret

    # F15: WEBUI_SECURE_COOKIE opt-out — cookie is Secure by default.
    # Set WEBUI_SECURE_COOKIE=0 to disable (local dev over plain HTTP only).
    # WARNING: setting to 0 in production allows session hijacking over plain HTTP.
    https_only = os.environ.get("WEBUI_SECURE_COOKIE", "1") != "0"
    app.add_middleware(
        SessionMiddleware,
        secret_key=get_session_secret(),
        session_cookie="osm_session",
        same_site="strict",
        https_only=https_only,
        max_age=None,  # Session cookie (browser-close expiry); TTL enforced by session_at
    )

    # Middle: maintenance mode (blocks non-restore requests during restore)
    app.add_middleware(_MaintenanceModeMiddleware)

    # Outermost-1: loopback IP check
    app.add_middleware(_LoopbackOnlyMiddleware)

    # Outermost: security headers — added last so they wrap every response
    app.add_middleware(_SecurityHeadersMiddleware)

    # Auth endpoints (exempt from AuthRequiredMiddleware via /api/auth/ prefix)
    from src.web_ui.routes import login, oauth, signup

    app.include_router(login.router)
    app.include_router(oauth.router)
    app.include_router(signup.router)

    from src.web_ui.routes import dashboard

    app.include_router(dashboard.router)

    from src.web_ui.routes import api_keys, repos, ssh_keys

    app.include_router(repos.router)
    app.include_router(api_keys.router)
    app.include_router(ssh_keys.router)

    # Jobs router extracted from repos.py per Phase 8 review:
    # client polls /api/jobs/{id}/status but repos prefix was /api/repos.
    from src.web_ui.routes import jobs

    app.include_router(jobs.router)

    from src.web_ui.routes import feedback

    app.include_router(feedback.router)

    from src.web_ui.routes import operations

    app.include_router(operations.router)

    # TOTP MFA routes (M9 W-MF)
    from src.web_ui.routes import totp

    app.include_router(totp.router)

    # M9 W-UM: User management (list, deactivate, reactivate, reset-password-link)
    from src.web_ui.routes import admin_users

    app.include_router(admin_users.router)

    # M9 W-UO: Migrations read-only display
    from src.web_ui.routes import admin_migrations

    app.include_router(admin_migrations.router)

    # Health endpoint — auth-exempt (pre-launch checklist §10.5)
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """Auth-exempt health endpoint for uptime monitoring (pre-launch checklist §10.5)."""
        try:
            from src._version import __version__
        except ImportError:
            __version__ = "unknown"
        return {"status": "ok", "version": __version__}

    return app
