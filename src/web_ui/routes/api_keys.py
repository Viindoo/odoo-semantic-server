# src/web_ui/routes/api_keys.py
"""API key management routes."""
import logging
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

_logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request):
    """Render API keys management page."""
    templates = request.app.state.templates
    keys = []
    error = None
    try:
        from src.db.pg import auth_store

        keys = auth_store().list_api_keys()
    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        request,
        "api_keys.html",
        {"keys": keys, "error": error, "new_raw_key": None},
    )


@router.post("/api-keys", response_class=HTMLResponse)
async def create_api_key(
    request: Request,
    name: Annotated[str, Form()],
):
    """Create a new API key and display raw key once."""
    templates = request.app.state.templates
    keys = []
    error = None
    new_raw_key = None

    try:
        from src.db.pg import auth_store

        raw_key, _, _ = auth_store().create_api_key(name)
        new_raw_key = raw_key
        keys = auth_store().list_api_keys()
    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        request,
        "api_keys.html",
        {"keys": keys, "error": error, "new_raw_key": new_raw_key},
    )


@router.post("/api-keys/{key_id}/deactivate", response_class=RedirectResponse)
async def deactivate_api_key(request: Request, key_id: int):
    """Deactivate an API key."""
    try:
        from src.db.pg import auth_store
        from src.mcp.middleware import _cache_invalidate_by_key_id

        auth_store().deactivate_api_key(key_id)
        _cache_invalidate_by_key_id(key_id)  # B1: immediate in-process cache clear
        _logger.info("API key %s deactivated", key_id)
    except Exception as e:
        _logger.warning("Deactivate key %s failed: %s", key_id, e)
    return RedirectResponse("/api-keys", status_code=303)
