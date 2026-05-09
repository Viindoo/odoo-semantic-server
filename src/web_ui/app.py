# src/web_ui/app.py
"""FastAPI Web UI application — admin interface, port 8003, localhost-only."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app() -> FastAPI:
    """Create and configure the Web UI FastAPI app."""
    app = FastAPI(
        title="Odoo Semantic MCP — Admin",
        description="Admin interface for managing profiles, repos, API keys, and SSH keys.",
        docs_url=None,  # Disable OpenAPI docs in admin UI
        redoc_url=None,
    )

    @app.middleware("http")
    async def _loopback_only(request: Request, call_next):
        """Reject requests from non-loopback addresses (I6 — CSRF mitigation)."""
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    from src.web_ui.routes import dashboard

    app.include_router(dashboard.router)

    from src.web_ui.routes import api_keys, repos, ssh_keys

    app.include_router(repos.router)
    app.include_router(api_keys.router)
    app.include_router(ssh_keys.router)

    from src.web_ui.routes import feedback

    app.include_router(feedback.router)

    return app
