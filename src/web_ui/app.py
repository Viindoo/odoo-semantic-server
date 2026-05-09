# src/web_ui/app.py
"""FastAPI Web UI application — admin interface, port 8003, localhost-only."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app() -> FastAPI:
    """Create and configure the Web UI FastAPI app."""
    app = FastAPI(
        title="Odoo Semantic MCP — Admin",
        description="Admin interface for managing profiles, repos, API keys, and SSH keys.",
        docs_url=None,  # Disable OpenAPI docs in admin UI
        redoc_url=None,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    from src.web_ui.routes import dashboard

    app.include_router(dashboard.router)

    # Wave 3 routers — uncomment as they land:
    # from src.web_ui.routes import repos; app.include_router(repos.router)
    # from src.web_ui.routes import api_keys; app.include_router(api_keys.router)
    # from src.web_ui.routes import ssh_keys; app.include_router(ssh_keys.router)

    return app
