# src/web_ui/routes/api_keys.py
"""API key management routes."""
import logging
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

_logger = logging.getLogger(__name__)
router = APIRouter()


def _get_conn():
    """Get PostgreSQL connection for Web UI queries."""
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


@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request):
    """Render API keys management page."""
    templates = request.app.state.templates
    keys = []
    error = None
    conn = _get_conn()
    if conn:
        try:
            from src.db.auth_registry import list_api_keys

            keys = list_api_keys(conn)
        except Exception as e:
            error = str(e)
        finally:
            conn.close()
    else:
        error = "Cannot connect to PostgreSQL."

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

    conn = _get_conn()
    if conn:
        try:
            from src.db.auth_registry import create_api_key as _create
            from src.db.auth_registry import list_api_keys

            raw_key, _, _ = _create(conn, name)
            new_raw_key = raw_key
            keys = list_api_keys(conn)
        except Exception as e:
            error = str(e)
        finally:
            conn.close()
    else:
        error = "Cannot connect to PostgreSQL."

    return templates.TemplateResponse(
        request,
        "api_keys.html",
        {"keys": keys, "error": error, "new_raw_key": new_raw_key},
    )


@router.post("/api-keys/{key_id}/deactivate", response_class=RedirectResponse)
async def deactivate_api_key(request: Request, key_id: int):
    """Deactivate an API key."""
    conn = _get_conn()
    if conn:
        try:
            from src.db.auth_registry import deactivate_api_key as _deactivate
            from src.mcp.middleware import _cache_invalidate_by_key_id

            _deactivate(conn, key_id)
            _cache_invalidate_by_key_id(key_id)  # B1: immediate in-process cache clear
            _logger.info("API key %s deactivated", key_id)
        except Exception as e:
            _logger.warning("Deactivate key %s failed: %s", key_id, e)
        finally:
            conn.close()
    return RedirectResponse("/api-keys", status_code=303)
