# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Settings system-wide CRUD endpoints (ADR-0042).

Routes (all require admin):
- GET    /api/admin/settings                       list by category
- GET    /api/admin/settings/{key}                 single setting
- PATCH  /api/admin/settings/{key}                 update (require_admin_with_fresh_mfa)
- POST   /api/admin/settings/{key}/reset           revert to default
- GET    /api/admin/settings/{key}/history         last 50 changes
- POST   /api/admin/settings/{key}/undo            revert to prev (require_admin_with_fresh_mfa)
"""
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.db.audit import audit_action
from src.settings import get_setting
from src.settings_registry import SETTINGS_CATALOGUE
from src.web_ui.auth import require_admin, require_admin_with_fresh_mfa
from src.web_ui.routes._admin_helpers import (
    catalogue_by_key,
    coerce_actor_id,
    post_write_hook,
    validate_value_http,
)

# WI-RV F-F: shared helpers consolidated from in-line duplicates.  Aliases
# preserve the previous call shape used in the route bodies + tests.
_catalogue_by_key = catalogue_by_key
_post_write_hook = post_write_hook
_validate_value = validate_value_http

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/settings", tags=["admin-settings"])


class SettingPatch(BaseModel):
    value: Any
    reason: str = Field(min_length=3, max_length=500)


@router.get("")
async def list_settings(actor_id: int = Depends(require_admin)) -> dict:
    """Group settings by category. Returns dict with effective values + sources.

    WI-R F-002 fix: the response is the UNION of (a) all system rows in
    ``app_settings`` and (b) every entry in :data:`SETTINGS_CATALOGUE`.
    Catalogue-only keys (rows that ``bootstrap_settings_safe`` failed to
    insert, e.g., because m13_010 had not yet been applied) appear with
    ``effective_source="code_default"`` and a null ``updated_at`` so the
    admin UI can still render every Tier-1 setting and offer a
    "first write" PATCH instead of leaving the operator with a blank page.
    """
    catalogue = _catalogue_by_key()
    by_category: dict[str, list[dict]] = {}
    seen_keys: set[str] = set()

    from src.db.pg import get_pool
    with get_pool().checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, value_json, category, scope, tenant_id, data_type, "
                "validation_json, default_value, requires_restart, requires_reseed, "
                "is_secret, description, updated_at, updated_by, change_reason "
                "FROM app_settings WHERE scope = 'system' AND tenant_id IS NULL "
                "ORDER BY category, key"
            )
            rows = cur.fetchall()

    for row in rows:
        key = row[0]
        sdef = catalogue.get(key)
        raw = row[1]
        value = raw.get("v") if isinstance(raw, dict) else raw
        category = row[2]
        raw_default = row[7]
        entry = {
            "key": key,
            "value": value,
            "effective_source": "system_db",
            "category": category,
            "scope": row[3],
            "data_type": row[5],
            "validation": row[6],
            "default_value": raw_default.get("v") if isinstance(raw_default, dict) else raw_default,
            "requires_restart": row[8],
            "requires_reseed": row[9],
            "is_secret": row[10],
            "description": row[11],
            "tenant_scopable": sdef.tenant_scopable if sdef else False,
            # WI-RV F-C: advisory flag tells the UI that this key's runtime
            # value lives in a different table — admin PATCH updates the
            # overlay (cache flush still happens) but does NOT change live
            # gating until the canonical source is updated.
            "advisory": sdef.advisory if sdef else False,
            "advisory_canonical_source": (
                sdef.advisory_canonical_source if sdef else ""
            ),
            "updated_at": row[12].isoformat() if row[12] else None,
            "updated_by": row[13],
            "change_reason": row[14],
        }
        by_category.setdefault(category, []).append(entry)
        seen_keys.add(key)

    # F-002: backfill any catalogue entry missing from the DB so the admin UI
    # always renders all 15 Tier-1 settings — bootstrap may have failed
    # silently and we must not hide the row from the operator.
    for sdef in SETTINGS_CATALOGUE:
        if sdef.key in seen_keys:
            continue
        entry = {
            "key": sdef.key,
            "value": sdef.default_value,
            "effective_source": "code_default",
            "category": sdef.category,
            "scope": "system",
            "data_type": sdef.data_type,
            "validation": sdef.validation,
            "default_value": sdef.default_value,
            "requires_restart": sdef.requires_restart,
            "requires_reseed": sdef.requires_reseed,
            "is_secret": sdef.is_secret,
            "description": sdef.description,
            "tenant_scopable": sdef.tenant_scopable,
            "advisory": sdef.advisory,
            "advisory_canonical_source": sdef.advisory_canonical_source,
            "updated_at": None,
            "updated_by": None,
            "change_reason": None,
        }
        by_category.setdefault(sdef.category, []).append(entry)

    # Re-sort each category by key for deterministic UI ordering after the union.
    for cat in by_category:
        by_category[cat].sort(key=lambda e: e["key"])

    return {"categories": by_category}


@router.get("/{key}")
async def get_single_setting(key: str, actor_id: int = Depends(require_admin)) -> dict:
    """Single setting + current + default + drift flag."""
    catalogue = _catalogue_by_key()
    if key not in catalogue:
        raise HTTPException(404, f"Setting {key!r} not in catalogue")
    sdef = catalogue[key]
    current = get_setting(key)
    return {
        "key": key,
        "current_value": current,
        "code_default": sdef.default_value,
        "drift_from_default": current != sdef.default_value,
        "category": sdef.category,
        "data_type": sdef.data_type,
        "validation": sdef.validation,
        "requires_restart": sdef.requires_restart,
        "requires_reseed": sdef.requires_reseed,
        "is_secret": sdef.is_secret,
        "description": sdef.description,
        "tenant_scopable": sdef.tenant_scopable,
        "advisory": sdef.advisory,
        "advisory_canonical_source": sdef.advisory_canonical_source,
    }


@router.patch("/{key}")
@audit_action("setting.update", target_param="key")
async def update_setting(
    key: str,
    payload: SettingPatch,
    actor_id: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Update system-scope setting. Tenant override goes through tenant_settings router."""
    catalogue = _catalogue_by_key()
    if key not in catalogue:
        raise HTTPException(404, f"Setting {key!r} not in catalogue")
    sdef = catalogue[key]
    _validate_value(sdef, payload.value)

    new_value_json = json.dumps({"v": payload.value})

    from src.db.pg import get_pool
    with get_pool().checkout() as conn:
        # Test bypass sentinel may not exist in webui_users → NULL fallback.
        updated_by = coerce_actor_id(actor_id, conn)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                # Get old value for history
                cur.execute(
                    "SELECT value_json FROM app_settings "
                    "WHERE key = %s AND scope = 'system' AND tenant_id IS NULL",
                    (key,),
                )
                old_row = cur.fetchone()
                old_value_json = old_row[0] if old_row else None

                # UPSERT to system row
                cur.execute(
                    """
                    INSERT INTO app_settings (key, value_json, category, scope, data_type,
                                              validation_json, default_value, requires_restart,
                                              requires_reseed, is_secret, description,
                                              updated_by, change_reason)
                    VALUES (%s, %s::jsonb, %s, 'system', %s, %s::jsonb, %s::jsonb,
                            %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (key) WHERE scope = 'system' AND tenant_id IS NULL
                    DO UPDATE SET
                        value_json = EXCLUDED.value_json,
                        updated_at = now(),
                        updated_by = EXCLUDED.updated_by,
                        change_reason = EXCLUDED.change_reason
                    """,
                    (key, new_value_json, sdef.category, sdef.data_type,
                     json.dumps(sdef.validation), json.dumps({"v": sdef.default_value}),
                     sdef.requires_restart, sdef.requires_reseed, sdef.is_secret,
                     sdef.description, updated_by, payload.reason),
                )

                # History insert
                cur.execute(
                    "INSERT INTO app_settings_history "
                    "(setting_key, tenant_id, old_value, new_value, changed_by, change_reason) "
                    "VALUES (%s, NULL, %s::jsonb, %s::jsonb, %s, %s)",
                    (key, json.dumps(old_value_json) if old_value_json else None,
                     new_value_json, updated_by, payload.reason),
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    _post_write_hook(key, tenant_id=None)
    return {"key": key, "value": payload.value, "propagation_eta_seconds": 60}


@router.post("/{key}/reset")
@audit_action("setting.reset", target_param="key")
async def reset_setting(
    key: str,
    actor_id: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Delete system row; fallback to code default on next read."""
    catalogue = _catalogue_by_key()
    if key not in catalogue:
        raise HTTPException(404, f"Setting {key!r} not in catalogue")

    from src.db.pg import get_pool
    with get_pool().checkout() as conn:
        # Test bypass sentinel may not exist in webui_users → NULL fallback.
        changed_by = coerce_actor_id(actor_id, conn)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value_json FROM app_settings "
                    "WHERE key = %s AND scope = 'system' AND tenant_id IS NULL",
                    (key,),
                )
                old_row = cur.fetchone()
                if old_row is None:
                    raise HTTPException(404, "No system row to reset")
                cur.execute(
                    "DELETE FROM app_settings "
                    "WHERE key = %s AND scope = 'system' AND tenant_id IS NULL",
                    (key,),
                )
                cur.execute(
                    "INSERT INTO app_settings_history "
                    "(setting_key, tenant_id, old_value, new_value, changed_by, change_reason) "
                    "VALUES (%s, NULL, %s::jsonb, %s::jsonb, %s, 'reset to default')",
                    (key, json.dumps(old_row[0]),
                     json.dumps({"v": catalogue[key].default_value}), changed_by),
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    _post_write_hook(key, tenant_id=None)
    return {"key": key, "reset": True, "default_value": catalogue[key].default_value}


@router.get("/{key}/history")
async def get_history(
    key: str,
    limit: int = 50,
    actor_id: int = Depends(require_admin),
) -> list[dict]:
    """Last N changes for a key (system rows only)."""
    if limit > 100:
        limit = 100

    from src.db.pg import get_pool
    with get_pool().checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, old_value, new_value, changed_by, changed_at, change_reason "
                "FROM app_settings_history WHERE setting_key = %s AND tenant_id IS NULL "
                "ORDER BY changed_at DESC LIMIT %s",
                (key, limit),
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


@router.post("/{key}/undo")
@audit_action("setting.undo", target_param="key")
async def undo_setting(
    key: str,
    actor_id: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Revert to value from N-1 history entry."""
    catalogue = _catalogue_by_key()
    if key not in catalogue:
        raise HTTPException(404, f"Setting {key!r} not in catalogue")

    from src.db.pg import get_pool
    with get_pool().checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT old_value FROM app_settings_history "
                "WHERE setting_key = %s AND tenant_id IS NULL "
                "ORDER BY changed_at DESC LIMIT 1",
                (key,),
            )
            row = cur.fetchone()
            if row is None or row[0] is None:
                raise HTTPException(404, "Nothing to undo")
            prev = row[0]
            target_value = prev.get("v") if isinstance(prev, dict) else prev

    sdef = catalogue[key]
    _validate_value(sdef, target_value)

    new_value_json = json.dumps({"v": target_value})
    with get_pool().checkout() as conn:
        # Test bypass sentinel may not exist in webui_users → NULL fallback.
        updated_by = coerce_actor_id(actor_id, conn)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_settings (key, value_json, category, scope, data_type,
                                              validation_json, default_value, requires_restart,
                                              requires_reseed, is_secret, description,
                                              updated_by, change_reason)
                    VALUES (%s, %s::jsonb, %s, 'system', %s, %s::jsonb, %s::jsonb,
                            %s, %s, %s, %s, %s, 'undo')
                    ON CONFLICT (key) WHERE scope = 'system' AND tenant_id IS NULL
                    DO UPDATE SET value_json = EXCLUDED.value_json,
                                  updated_at = now(), updated_by = EXCLUDED.updated_by,
                                  change_reason = EXCLUDED.change_reason
                    """,
                    (key, new_value_json, sdef.category, sdef.data_type,
                     json.dumps(sdef.validation), json.dumps({"v": sdef.default_value}),
                     sdef.requires_restart, sdef.requires_reseed, sdef.is_secret,
                     sdef.description, updated_by),
                )
                cur.execute(
                    "INSERT INTO app_settings_history "
                    "(setting_key, tenant_id, old_value, new_value, changed_by, change_reason) "
                    "VALUES (%s, NULL, NULL, %s::jsonb, %s, 'undo')",
                    (key, new_value_json, updated_by),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    _post_write_hook(key, tenant_id=None)
    return {"key": key, "undone_to": target_value}
