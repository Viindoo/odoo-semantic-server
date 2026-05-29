# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tenant Settings per-tenant override CRUD endpoints (ADR-0042).

Routes (require tenant_admin role or system admin):
- GET    /api/tenants/{tenant_id}/settings                  list tenant-scopable settings
- GET    /api/tenants/{tenant_id}/settings/{key}            single setting (effective value)
- PATCH  /api/tenants/{tenant_id}/settings/{key}            create/update tenant override
- POST   /api/tenants/{tenant_id}/settings/{key}/reset      delete tenant override (-> system)
- GET    /api/tenants/{tenant_id}/settings/{key}/history    last 50 changes for this tenant

Only keys with tenant_scopable=True in SETTINGS_CATALOGUE are accessible here.
"""
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.requests import Request

from src.db.audit import audit_action
from src.settings import get_setting
from src.settings_registry import SETTINGS_CATALOGUE
from src.web_ui.auth import (
    _check_mfa_freshness,
    current_user_id,
    is_admin_session,
    is_test_bypass_active,
    require_admin_with_fresh_mfa,  # noqa: F401 — re-export anchor for tests
)
from src.web_ui.routes._admin_helpers import (
    catalogue_by_key,
    coerce_actor_id,
    post_write_hook,
    validate_value_http,
)

# WI-RV F-F: shared helpers consolidated from in-line duplicates.  The local
# underscore aliases preserve the previous call shape used throughout the
# route bodies (and tests that import them) without bloating the route code.
_catalogue_by_key = catalogue_by_key
_post_write_hook = post_write_hook
_validate_value = validate_value_http

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tenants/{tenant_id}/settings", tags=["tenant-settings"])


# ---------------------------------------------------------------------------
# RBAC helper — inline (WI-9 owns auth.py; we keep this local)
# ---------------------------------------------------------------------------


def _get_db_pool():
    from src.db.pg import get_pool
    return get_pool()


async def _require_tenant_owner_or_admin(tenant_id: int, request: Request) -> int:
    """Return actor_id if user is admin OR tenant_admin of tenant_id; else 403.

    tenant_members.role check: ('member', 'tenant_admin') per m13_005.
    Only 'tenant_admin' role grants write access to tenant settings.

    WI-RV F-I: uses :func:`current_user_id` (auth.py) instead of reading
    ``request.session["user_id"]`` directly so the test-bypass + legacy
    signed-cookie paths are honoured uniformly across the codebase.
    """
    user_id = current_user_id(request)
    if user_id is None:
        raise HTTPException(401, "Not authenticated")
    if is_admin_session(request):
        return int(user_id)

    with _get_db_pool().checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM tenant_members WHERE user_id = %s AND tenant_id = %s",
                (user_id, tenant_id),
            )
            row = cur.fetchone()

    if row is None or row[0] != "tenant_admin":
        raise HTTPException(403, "Requires tenant_admin role for this tenant")
    return int(user_id)


async def _require_tenant_owner_or_admin_with_mfa(tenant_id: int, request: Request) -> int:
    """Same as above but also verifies fresh MFA (for destructive ops).

    Freshness check delegates to :func:`src.web_ui.auth._check_mfa_freshness`
    so the window is runtime-configurable via ``auth.mfa_freshness_seconds``
    (ADR-0042) and the logic stays DRY with the system-admin path.
    """
    # First check role
    actor_id = await _require_tenant_owner_or_admin(tenant_id, request)

    if is_test_bypass_active():
        return actor_id

    # Delegate to shared freshness helper (raises HTTPException 403 on failure)
    _check_mfa_freshness(request)
    return actor_id


# ---------------------------------------------------------------------------
# Catalogue helpers (shared duplicates removed in WI-RV F-F; only the
# tenant-specific filter remains local)
# ---------------------------------------------------------------------------


def _scopable_keys() -> set[str]:
    return {sdef.key for sdef in SETTINGS_CATALOGUE if sdef.tenant_scopable}


class SettingPatch(BaseModel):
    value: Any
    reason: str = Field(min_length=3, max_length=500)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_tenant_settings(tenant_id: int, request: Request) -> dict:
    """List all tenant-scopable settings with effective values for this tenant."""
    await _require_tenant_owner_or_admin(tenant_id, request)

    catalogue = _catalogue_by_key()
    scopable = _scopable_keys()
    result: list[dict] = []

    with _get_db_pool().checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, value_json, updated_at, updated_by, change_reason "
                "FROM app_settings WHERE scope = 'tenant' AND tenant_id = %s "
                "ORDER BY key",
                (tenant_id,),
            )
            overrides = {row[0]: row for row in cur.fetchall()}

    for key in sorted(scopable):
        sdef = catalogue[key]
        effective = get_setting(key, tenant_id=tenant_id)
        override_row = overrides.get(key)
        override_val = None
        if override_row:
            raw = override_row[1]
            override_val = raw.get("v") if isinstance(raw, dict) else raw

        result.append({
            "key": key,
            "effective_value": effective,
            "effective_source": "tenant_override" if key in overrides else "system_or_default",
            "tenant_override": override_val,
            "system_default": get_setting(key),  # system row (no tenant context)
            "category": sdef.category,
            "data_type": sdef.data_type,
            "validation": sdef.validation,
            "description": sdef.description,
            "updated_at": override_row[2].isoformat() if override_row and override_row[2] else None,
            "updated_by": override_row[3] if override_row else None,
            "change_reason": override_row[4] if override_row else None,
        })

    return {"tenant_id": tenant_id, "settings": result}


@router.get("/{key}")
async def get_tenant_setting(tenant_id: int, key: str, request: Request) -> dict:
    """Single setting with effective value for this tenant."""
    await _require_tenant_owner_or_admin(tenant_id, request)

    catalogue = _catalogue_by_key()
    if key not in catalogue:
        raise HTTPException(404, f"Setting {key!r} not in catalogue")
    if key not in _scopable_keys():
        raise HTTPException(403, f"Setting {key!r} is not tenant-scopable")

    sdef = catalogue[key]
    effective = get_setting(key, tenant_id=tenant_id)
    system_val = get_setting(key)  # system row (no tenant context)

    with _get_db_pool().checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value_json, updated_at, updated_by, change_reason "
                "FROM app_settings WHERE key = %s AND scope = 'tenant' AND tenant_id = %s",
                (key, tenant_id),
            )
            row = cur.fetchone()

    override_val = None
    if row:
        raw = row[0]
        override_val = raw.get("v") if isinstance(raw, dict) else raw

    return {
        "key": key,
        "tenant_id": tenant_id,
        "effective_value": effective,
        "effective_source": "tenant_override" if row else "system_or_default",
        "tenant_override": override_val,
        "system_value": system_val,
        "code_default": sdef.default_value,
        "category": sdef.category,
        "data_type": sdef.data_type,
        "validation": sdef.validation,
        "description": sdef.description,
        "updated_at": row[1].isoformat() if row and row[1] else None,
        "updated_by": row[2] if row else None,
        "change_reason": row[3] if row else None,
    }


@router.patch("/{key}")
@audit_action("setting.tenant_update", target_param="key")
async def update_tenant_setting(
    tenant_id: int,
    key: str,
    payload: SettingPatch,
    request: Request,
) -> dict:
    """Create or update tenant-scoped override for a setting."""
    actor_id = await _require_tenant_owner_or_admin_with_mfa(tenant_id, request)

    catalogue = _catalogue_by_key()
    if key not in catalogue:
        raise HTTPException(404, f"Setting {key!r} not in catalogue")
    if key not in _scopable_keys():
        raise HTTPException(403, f"Setting {key!r} is not tenant-scopable")

    sdef = catalogue[key]
    _validate_value(sdef, payload.value)

    new_value_json = json.dumps({"v": payload.value})

    with _get_db_pool().checkout() as conn:
        # Test bypass sentinel may not exist in webui_users → NULL fallback.
        updated_by = coerce_actor_id(actor_id, conn)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                # Get old value for history
                cur.execute(
                    "SELECT value_json FROM app_settings "
                    "WHERE key = %s AND scope = 'tenant' AND tenant_id = %s",
                    (key, tenant_id),
                )
                old_row = cur.fetchone()
                old_value_json = old_row[0] if old_row else None

                cur.execute(
                    """
                    INSERT INTO app_settings (key, value_json, category, scope, tenant_id,
                                              data_type, validation_json, default_value,
                                              requires_restart, requires_reseed, is_secret,
                                              description, updated_by, change_reason)
                    VALUES (%s, %s::jsonb, %s, 'tenant', %s, %s, %s::jsonb, %s::jsonb,
                            %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (key, tenant_id) WHERE scope = 'tenant' AND tenant_id IS NOT NULL
                    DO UPDATE SET
                        value_json = EXCLUDED.value_json,
                        updated_at = now(),
                        updated_by = EXCLUDED.updated_by,
                        change_reason = EXCLUDED.change_reason
                    """,
                    (key, new_value_json, sdef.category, tenant_id, sdef.data_type,
                     json.dumps(sdef.validation), json.dumps({"v": sdef.default_value}),
                     sdef.requires_restart, sdef.requires_reseed, sdef.is_secret,
                     sdef.description, updated_by, payload.reason),
                )

                cur.execute(
                    "INSERT INTO app_settings_history "
                    "(setting_key, tenant_id, old_value, new_value, changed_by, change_reason) "
                    "VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s)",
                    (key, tenant_id,
                     json.dumps(old_value_json) if old_value_json else None,
                     new_value_json, updated_by, payload.reason),
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    _post_write_hook(key, tenant_id=tenant_id)
    return {
        "key": key,
        "tenant_id": tenant_id,
        "value": payload.value,
        "propagation_eta_seconds": 60,
    }


@router.post("/{key}/reset")
@audit_action("setting.tenant_reset", target_param="key")
async def reset_tenant_setting(
    tenant_id: int,
    key: str,
    request: Request,
) -> dict:
    """Delete tenant override row; fallback to system row / code default on next read."""
    actor_id = await _require_tenant_owner_or_admin_with_mfa(tenant_id, request)

    catalogue = _catalogue_by_key()
    if key not in catalogue:
        raise HTTPException(404, f"Setting {key!r} not in catalogue")
    if key not in _scopable_keys():
        raise HTTPException(403, f"Setting {key!r} is not tenant-scopable")

    with _get_db_pool().checkout() as conn:
        # Test bypass sentinel may not exist in webui_users → NULL fallback.
        changed_by = coerce_actor_id(actor_id, conn)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value_json FROM app_settings "
                    "WHERE key = %s AND scope = 'tenant' AND tenant_id = %s",
                    (key, tenant_id),
                )
                old_row = cur.fetchone()
                if old_row is None:
                    raise HTTPException(404, "No tenant override row to reset")

                cur.execute(
                    "DELETE FROM app_settings "
                    "WHERE key = %s AND scope = 'tenant' AND tenant_id = %s",
                    (key, tenant_id),
                )
                # Reset rows record the system default as new_value so the
                # history shows what consumers will see (app_settings_history.new_value
                # is NOT NULL, and "value fell back to default" is more useful than NULL).
                cur.execute(
                    "INSERT INTO app_settings_history "
                    "(setting_key, tenant_id, old_value, new_value, changed_by, change_reason) "
                    "VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, 'reset tenant override')",
                    (key, tenant_id, json.dumps(old_row[0]),
                     json.dumps({"v": catalogue[key].default_value}), changed_by),
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    _post_write_hook(key, tenant_id=tenant_id)
    return {"key": key, "tenant_id": tenant_id, "reset": True}


@router.get("/{key}/history")
async def get_tenant_setting_history(
    tenant_id: int,
    key: str,
    request: Request,
    limit: int = 50,
) -> list[dict]:
    """Last N changes for a key scoped to this tenant."""
    await _require_tenant_owner_or_admin(tenant_id, request)

    if limit > 100:
        limit = 100

    catalogue = _catalogue_by_key()
    if key not in catalogue:
        raise HTTPException(404, f"Setting {key!r} not in catalogue")
    if key not in _scopable_keys():
        raise HTTPException(403, f"Setting {key!r} is not tenant-scopable")

    with _get_db_pool().checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, old_value, new_value, changed_by, changed_at, change_reason "
                "FROM app_settings_history "
                "WHERE setting_key = %s AND tenant_id = %s "
                "ORDER BY changed_at DESC LIMIT %s",
                (key, tenant_id, limit),
            )
            rows = cur.fetchall()

    return [
        {
            "id": r[0],
            "old_value": r[1],
            "new_value": r[2],
            "changed_by": r[3],
            "changed_at": r[4].isoformat() if r[4] else None,
            "change_reason": r[5],
        }
        for r in rows
    ]
