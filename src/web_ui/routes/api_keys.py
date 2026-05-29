# SPDX-License-Identifier: AGPL-3.0-or-later
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


def _mint_default_api_key(user_id: int, username: str) -> str | None:
    """Auto-mint one free-plan API key for a newly-created user.

    Reuses AuthStore.create_api_key() — plan_id is NOT specified so the DB
    column DEFAULT (= free plan) applies automatically.  The raw key is
    returned so callers can surface it in the signup response if desired;
    callers that do not need it may discard the return value.

    Idempotent-friendly: callers are responsible for only calling this when
    the user has zero existing keys (or unconditionally on first-login if
    idempotency at the caller is acceptable).

    Never raises into the auth flow: any exception is caught, logged as a
    warning, and None is returned.  A failed mint must NOT break login or
    signup.

    Args:
        user_id: webui_users.id for the new account.
        username: Human-readable label component for the key name.

    Returns:
        Raw key string (shown once) on success, or None on failure.
    """
    try:
        from src.db.pg import auth_store

        label = f"Default key ({username})"
        raw_key, _prefix, _key_id = auth_store().create_api_key(
            name=label,
            user_id=user_id,
            expires_at=None,
            tenant_id=None,
        )
        _logger.info(
            "Auto-minted default API key for user_id=%d (username=%r)", user_id, username
        )
        return raw_key
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "Failed to auto-mint default API key for user_id=%d: %s", user_id, exc
        )
        return None


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

    Lazy-mint (WI-7): if an authenticated non-admin user has zero keys, one
    free-plan key is minted before the list is returned.  This ensures that
    existing key-less accounts (e.g. early OAuth signups before WI-7 shipped)
    see a usable key on their first visit to the API keys page without any
    manual action.  The mint is idempotent: only fires when count == 0.
    """
    keys = []
    error = None
    try:
        from src.db.pg import auth_store
        from src.web_ui.auth import current_user_id, is_admin_session

        uid = current_user_id(request)
        is_admin = is_admin_session(request)
        store = auth_store()
        keys = store.list_api_keys(user_id=uid, admin=is_admin)

        # Lazy-mint: non-admin authenticated user with zero keys → mint one now.
        if uid is not None and not is_admin and len(keys) == 0:
            username = request.session.get("username", f"user{uid}")
            _mint_default_api_key(uid, username)
            # Re-fetch so the response includes the newly-minted key.
            keys = store.list_api_keys(user_id=uid, admin=is_admin)
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


@router.post("/{key_id}/reactivate")
@audit_action("api_key.reactivate", target_param="key_id")
async def reactivate_api_key_route(request: Request, key_id: int):
    """Reactivate an API key (symmetric counterpart of /deactivate).

    Admin: unconditional reactivate (any key).
    Non-admin: ownership-guarded — only keys owned by the caller (403 otherwise).
    Unauthenticated (uid=None in non-admin context): 401.
    404 if key_id does not exist.
    Idempotent — reactivating an already-active key returns 200.
    """
    from src.db.auth_registry import reactivate_api_key
    from src.db.pg import get_pool
    from src.mcp.middleware import _cache_invalidate_by_key_id
    from src.web_ui.auth import current_user_id, is_admin_session

    uid = current_user_id(request)
    pool = get_pool()

    try:
        is_admin = is_admin_session(request)

        if not is_admin:
            if uid is None:
                return JSONResponse(_json_safe({"error": "not_authenticated"}), status_code=401)
            # Fetch key row to check existence and ownership in one query.
            with pool.checkout() as conn:
                key_row = pool.fetch_one(
                    conn,
                    "SELECT id, user_id FROM api_keys WHERE id = %s",
                    (key_id,),
                )
            if key_row is None:
                return JSONResponse(_json_safe({"error": "not_found"}), status_code=404)
            if key_row["user_id"] != uid:
                return JSONResponse(_json_safe({"error": "not_owner"}), status_code=403)

        # Perform the reactivation (idempotent UPDATE RETURNING).
        # Returns None when key_id does not exist (admin path, or race condition).
        row = reactivate_api_key(pool, key_id)
        if row is None:
            return JSONResponse(_json_safe({"error": "not_found"}), status_code=404)

        _cache_invalidate_by_key_id(key_id)
        _logger.info("API key %s reactivated", key_id)
    except Exception as e:
        _logger.warning("Reactivate key %s failed: %s", key_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)

    return JSONResponse(_json_safe({
        "ok": True,
        "key_id": row["id"],
        "active": row["active"],
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "user_id": row["user_id"],
    }))
