# src/web_ui/routes/api_keys.py
"""API key management routes (M8 W1 — pure JSON API)."""
import datetime
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/api-keys")


def _serialize_keys(keys) -> list[dict]:
    """Convert any datetime fields in key dicts to ISO strings for JSON serialization."""
    result = []
    for key in keys:
        row = {}
        for k, v in key.items():
            row[k] = v.isoformat() if isinstance(v, datetime.datetime) else v
        result.append(row)
    return result


class CreateApiKeyBody(BaseModel):
    name: str


@router.get("")
async def list_api_keys(request: Request):
    """Return list of all API keys as JSON."""
    keys = []
    error = None
    try:
        from src.db.pg import auth_store

        keys = auth_store().list_api_keys()
    except Exception as e:
        error = str(e)

    return JSONResponse({"keys": _serialize_keys(keys), "error": error})


@router.post("")
async def create_api_key(body: CreateApiKeyBody, request: Request):
    """Create a new API key. Returns raw key (shown once)."""
    error = None
    new_raw_key = None
    keys = []

    try:
        from src.db.pg import auth_store

        raw_key, _, _ = auth_store().create_api_key(body.name)
        new_raw_key = raw_key
        keys = auth_store().list_api_keys()
    except Exception as e:
        error = str(e)

    if error:
        return JSONResponse({"error": error}, status_code=500)

    return JSONResponse({"ok": True, "raw_key": new_raw_key, "keys": _serialize_keys(keys)})


@router.post("/{key_id}/deactivate")
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
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})
