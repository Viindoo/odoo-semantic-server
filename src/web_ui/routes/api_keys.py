# src/web_ui/routes/api_keys.py
"""API key management routes (M8 W1 — pure JSON API).

M9 W-AK changes:
  - POST / now sets user_id = current_user_id(request) on new keys.
  - GET / filters keys by user_id (admin sees all; regular user sees own keys).
  - Accepts optional ``expires_at`` in POST body (ISO-8601 string, or null).
"""
import datetime
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.db.audit import audit_action
from src.web_ui._json import _json_safe

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/api-keys")


def _serialize_keys(keys) -> list[dict]:
    """Convert any datetime/date fields in key dicts to ISO strings for JSON serialization."""
    result = []
    for key in keys:
        row = {}
        for k, v in key.items():
            row[k] = v.isoformat() if isinstance(v, (datetime.datetime, datetime.date)) else v
        result.append(row)
    return result


class CreateApiKeyBody(BaseModel):
    name: str
    expires_at: str | None = None  # ISO-8601 string or null (eternal)


@router.get("")
async def list_api_keys(request: Request):
    """Return list of API keys as JSON.

    Scoping rules (M9 §3.3):
      - Admin session (or no session): returns all keys.
      - Regular user session: returns only keys owned by that user.
    """
    keys = []
    error = None
    try:
        from src.db.pg import auth_store
        from src.web_ui.auth import current_user_id, is_admin_session

        uid = current_user_id(request)
        is_admin = is_admin_session(request)
        keys = auth_store().list_api_keys(user_id=uid, admin=is_admin)
    except Exception as e:
        error = str(e)

    return JSONResponse(_json_safe({"keys": _serialize_keys(keys), "error": error}))


@router.post("")
@audit_action("api_key.create")
async def create_api_key(body: CreateApiKeyBody, request: Request):
    """Create a new API key. Returns raw key (shown once).

    user_id is set from the current session (M9 §3.3):
      - Web UI session: user_id = session user's integer id.
      - No session (CLI / backward-compat): user_id = NULL (global/admin key).
    """
    error = None
    new_raw_key = None
    keys = []

    try:
        from src.db.pg import auth_store
        from src.web_ui.auth import current_user_id, is_admin_session

        uid = current_user_id(request)

        # Parse optional expires_at
        expires_dt: datetime.datetime | None = None
        if body.expires_at:
            try:
                expires_dt = datetime.datetime.fromisoformat(body.expires_at)
            except ValueError as exc:
                return JSONResponse(
                    _json_safe({"error": f"Invalid expires_at format: {exc}"}),
                    status_code=400,
                )

        raw_key, _, _ = auth_store().create_api_key(
            body.name, user_id=uid, expires_at=expires_dt
        )
        new_raw_key = raw_key

        is_admin = is_admin_session(request)
        keys = auth_store().list_api_keys(user_id=uid, admin=is_admin)
    except Exception as e:
        error = str(e)

    if error:
        return JSONResponse(_json_safe({"error": error}), status_code=500)

    return JSONResponse(_json_safe({
        "ok": True,
        "raw_key": new_raw_key,
        "keys": _serialize_keys(keys),
    }))


@router.post("/{key_id}/deactivate")
@audit_action("api_key.deactivate", target_param="key_id")
async def deactivate_api_key(request: Request, key_id: int):
    """Deactivate an API key.

    Admin: unconditional deactivate (any key).
    Non-admin: ownership-guarded — only keys owned by the caller (403 otherwise).
    Unauthenticated (uid=None in non-admin context): 401.
    """
    from src.db.pg import auth_store
    from src.mcp.middleware import _cache_invalidate_by_key_id
    from src.web_ui.auth import current_user_id, is_admin_session

    uid = current_user_id(request)
    store = auth_store()

    try:
        if is_admin_session(request):
            store.deactivate_api_key(key_id)  # admin → unconditional
        else:
            if uid is None:
                return JSONResponse(_json_safe({"error": "not_authenticated"}), status_code=401)
            rows = store.deactivate_api_key_for_user(key_id, uid)
            if rows == 0:
                return JSONResponse(_json_safe({"error": "not_owner"}), status_code=403)
        _cache_invalidate_by_key_id(key_id)
        _logger.info("API key %s deactivated", key_id)
    except Exception as e:
        _logger.warning("Deactivate key %s failed: %s", key_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    return JSONResponse(_json_safe({"ok": True}))
