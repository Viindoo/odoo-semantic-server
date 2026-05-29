# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin EE Modules CRUD (ADR-0042).

Routes (admin-only):
- GET    /api/admin/ee-modules                list active modules
- GET    /api/admin/ee-modules/{id}           single
- POST   /api/admin/ee-modules                create new
- PATCH  /api/admin/ee-modules/{id}           update + cache invalidate
- DELETE /api/admin/ee-modules/{id}           soft-delete (deprecated=true)
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.requests import Request

from src.data.ee_modules import invalidate_ee_modules_cache
from src.db.audit import audit_action
from src.web_ui.auth import require_admin, require_admin_with_fresh_mfa
from src.web_ui.routes._admin_helpers import coerce_actor_id

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/ee-modules", tags=["admin-ee-modules"])


def _get_pool():
    from src.db.pg import get_pool
    return get_pool()


class EEModuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    since_version: str | None = None
    vt_equivalent: str | None = None
    description: str | None = None
    reason: str = Field(min_length=3, max_length=500)


class EEModulePatch(BaseModel):
    since_version: str | None = None
    vt_equivalent: str | None = None
    description: str | None = None
    deprecated: bool | None = None
    reason: str = Field(min_length=3, max_length=500)


@router.get("")
async def list_ee_modules(
    include_deprecated: bool = False,
    actor_id: int = Depends(require_admin),
) -> list[dict]:
    """Return EE Module guard list.  Excludes deprecated entries by default."""
    pool = _get_pool()
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            if include_deprecated:
                cur.execute(
                    "SELECT id, name, since_version, vt_equivalent, description, "
                    "deprecated, created_at, updated_at FROM ee_modules ORDER BY name"
                )
            else:
                cur.execute(
                    "SELECT id, name, since_version, vt_equivalent, description, "
                    "deprecated, created_at, updated_at FROM ee_modules "
                    "WHERE deprecated = FALSE ORDER BY name"
                )
            return [
                {
                    "id": r[0], "name": r[1], "since_version": r[2],
                    "vt_equivalent": r[3], "description": r[4], "deprecated": r[5],
                    "created_at": r[6].isoformat() if r[6] else None,
                    "updated_at": r[7].isoformat() if r[7] else None,
                }
                for r in cur.fetchall()
            ]


@router.get("/{module_id}")
async def get_ee_module(
    module_id: int,
    actor_id: int = Depends(require_admin),
) -> dict:
    """Return a single EE Module entry by id."""
    pool = _get_pool()
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, since_version, vt_equivalent, description, "
                "deprecated, created_at, updated_at FROM ee_modules WHERE id = %s",
                (module_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "EE module not found")
    return {
        "id": row[0], "name": row[1], "since_version": row[2],
        "vt_equivalent": row[3], "description": row[4], "deprecated": row[5],
        "created_at": row[6].isoformat() if row[6] else None,
        "updated_at": row[7].isoformat() if row[7] else None,
    }


@router.post("")
@audit_action("ee_module.create", target_param=None)
async def create_ee_module(
    payload: EEModuleCreate,
    request: Request,
    actor_id: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Create a new EE Module guard entry.

    Returns 409 if a module with the same name already exists (including
    deprecated ones — name is unique in the table).
    """
    pool = _get_pool()
    with pool.checkout() as conn:
        # FK ee_modules.updated_by -> webui_users(id) ON DELETE SET NULL;
        # in test bypass the sentinel actor_id may not exist → NULL is the
        # consistent fallback (see coerce_actor_id docstring).
        updated_by = coerce_actor_id(actor_id, conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ee_modules "
                "(name, since_version, vt_equivalent, description, updated_by) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (name) DO NOTHING RETURNING id",
                (payload.name, payload.since_version, payload.vt_equivalent,
                 payload.description, updated_by),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(409, f"EE module {payload.name!r} already exists")
            new_id = row[0]
        conn.commit()
    invalidate_ee_modules_cache()
    # Expose generated id as audit target (ADR-0021 create-style pattern)
    try:
        request.state.audit_target = str(new_id)
    except Exception:
        pass
    return {"id": new_id, "name": payload.name, "created": True}


@router.patch("/{module_id}")
@audit_action("ee_module.update", target_param="module_id")
async def update_ee_module(
    module_id: int,
    payload: EEModulePatch,
    request: Request,
    actor_id: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Update an EE Module entry.  Invalidates the in-process guard cache."""
    updates = payload.model_dump(exclude_none=True, exclude={"reason"})
    if not updates:
        raise HTTPException(400, "No fields to update")
    set_clauses = [f"{col} = %s" for col in updates]
    set_clauses.append("updated_at = now()")
    set_clauses.append("updated_by = %s")
    pool = _get_pool()
    with pool.checkout() as conn:
        # Test bypass sentinel may not exist in webui_users → NULL fallback.
        updated_by = coerce_actor_id(actor_id, conn)
        values = list(updates.values()) + [updated_by, module_id]
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE ee_modules SET {', '.join(set_clauses)} "
                "WHERE id = %s RETURNING id, name",
                tuple(values),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "EE module not found")
        conn.commit()
    invalidate_ee_modules_cache()
    return {"id": row[0], "name": row[1], "updated": True}


@router.delete("/{module_id}")
@audit_action("ee_module.delete", target_param="module_id")
async def delete_ee_module(
    module_id: int,
    request: Request,
    actor_id: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Soft-delete an EE Module entry (sets deprecated=TRUE).

    Returns 404 if the module does not exist or is already deprecated.
    Invalidates the in-process guard cache so MCP consumers see the change
    within the 60 s cache window.
    """
    pool = _get_pool()
    with pool.checkout() as conn:
        # Test bypass sentinel may not exist in webui_users → NULL fallback.
        updated_by = coerce_actor_id(actor_id, conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ee_modules "
                "SET deprecated = TRUE, updated_at = now(), updated_by = %s "
                "WHERE id = %s AND deprecated = FALSE RETURNING name",
                (updated_by, module_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "EE module not found or already deprecated")
        conn.commit()
    invalidate_ee_modules_cache()
    return {"id": module_id, "name": row[0], "soft_deleted": True}
