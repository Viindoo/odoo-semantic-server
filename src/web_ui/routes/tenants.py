# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/tenants.py
"""Tenant CRUD + member assignment + resource assignment routes (W1, ADR-0038).

All routes are admin-only (Depends(require_admin)).
Prefix: none (absolute paths used, as routes span /api/tenants/* and
/api/profiles/*/tenant and /api/repos/*/tenant).

Route table:
  GET    /api/tenants                              - list all tenants
  POST   /api/tenants                              - create tenant
  PATCH  /api/tenants/{tenant_id}                  - update tenant
  DELETE /api/tenants/{tenant_id}                  - delete tenant (blocked if has resources)
  GET    /api/tenants/{tenant_id}/members          - list members
  POST   /api/tenants/{tenant_id}/members          - add/update member
  DELETE /api/tenants/{tenant_id}/members/{user_id} - remove member
  PATCH  /api/profiles/{profile_id}/tenant         - assign profile <-> tenant
  PATCH  /api/repos/{repo_id}/tenant               - assign repo <-> tenant
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.db.audit import audit_action
from src.web_ui._json import _json_safe
from src.web_ui.auth import require_admin

_logger = logging.getLogger(__name__)

# No prefix — paths are absolute to mix /api/tenants/* with /api/{profiles,repos}/*/tenant
router = APIRouter()


# ---------------------------------------------------------------------------
# Request body models
# ---------------------------------------------------------------------------


class CreateTenantBody(BaseModel):
    name: str


class UpdateTenantBody(BaseModel):
    name: str | None = None
    active: bool | None = None


class AddMemberBody(BaseModel):
    user_id: int
    role: str = "member"


class AssignTenantBody(BaseModel):
    tenant_id: int | None = None


class AssignProfileTenantBody(BaseModel):
    tenant_id: int | None = None
    # null = publish the profile to every key: must be confirmed explicitly.
    confirm_shared: bool = False


# ---------------------------------------------------------------------------
# Tenant CRUD
# ---------------------------------------------------------------------------


@router.get("/api/tenants")
async def list_tenants(request: Request, _user_id: int = Depends(require_admin)):
    """List all tenants with member/repo/profile counts (admin-only)."""
    try:
        from src.db.pg import auth_store
        tenants = auth_store().list_tenants()
    except Exception as e:
        _logger.warning("list_tenants failed: %s", e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    return JSONResponse(_json_safe({"tenants": tenants}))


@router.post("/api/tenants")
@audit_action("tenant.create")
async def create_tenant(
    body: CreateTenantBody, request: Request, _user_id: int = Depends(require_admin)
):
    """Create a new tenant (admin-only).

    Returns:
        201 {ok: true, tenant_id: int}
        400 if name is empty or contains ','
        409 if name already exists
    """
    if not body.name or not body.name.strip():
        return JSONResponse(_json_safe({"error": "Tenant name must not be empty"}), status_code=400)
    if "," in body.name:
        return JSONResponse(
            _json_safe({"error": "Tenant name must not contain ','"}), status_code=400
        )
    try:
        from src.db.pg import auth_store
        tenant_id = auth_store().create_tenant(body.name)
    except ValueError as e:
        return JSONResponse(_json_safe({"error": str(e)}), status_code=400)
    except Exception as e:
        err_str = str(e)
        if "unique" in err_str.lower() or "duplicate" in err_str.lower():
            return JSONResponse(
                _json_safe({"error": f"Tenant name '{body.name}' already exists"}),
                status_code=409,
            )
        _logger.warning("create_tenant failed: %s", e)
        return JSONResponse(_json_safe({"error": err_str}), status_code=500)
    return JSONResponse(_json_safe({"ok": True, "tenant_id": tenant_id}), status_code=201)


@router.patch("/api/tenants/{tenant_id}")
@audit_action("tenant.update", target_param="tenant_id")
async def update_tenant(
    tenant_id: int,
    body: UpdateTenantBody,
    request: Request,
    _user_id: int = Depends(require_admin),
):
    """Update a tenant's name and/or active flag (admin-only)."""
    if body.name is not None and "," in body.name:
        return JSONResponse(
            _json_safe({"error": "Tenant name must not contain ','"}), status_code=400
        )
    try:
        from src.db.pg import auth_store
        found = auth_store().update_tenant(tenant_id, name=body.name, active=body.active)
    except ValueError as e:
        return JSONResponse(_json_safe({"error": str(e)}), status_code=400)
    except Exception as e:
        err_str = str(e)
        if "unique" in err_str.lower() or "duplicate" in err_str.lower():
            return JSONResponse(
                _json_safe({"error": f"Tenant name '{body.name}' already exists"}),
                status_code=409,
            )
        _logger.warning("update_tenant %d failed: %s", tenant_id, e)
        return JSONResponse(_json_safe({"error": err_str}), status_code=500)
    if not found:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
    return JSONResponse(_json_safe({"ok": True}))


@router.delete("/api/tenants/{tenant_id}")
@audit_action("tenant.delete", target_param="tenant_id")
async def delete_tenant(
    tenant_id: int, request: Request, _user_id: int = Depends(require_admin)
):
    """Delete a tenant (admin-only).

    Blocked with 409 if the tenant still has repos or profiles assigned.
    Membership rows CASCADE on delete (safe, they are just permission grants).
    """
    try:
        from src.db.pg import auth_store
        found = auth_store().delete_tenant(tenant_id)
    except ValueError as e:
        # Still has resources assigned
        return JSONResponse(_json_safe({"error": str(e)}), status_code=409)
    except Exception as e:
        _logger.warning("delete_tenant %d failed: %s", tenant_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    if not found:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
    return JSONResponse(_json_safe({"ok": True}))


# ---------------------------------------------------------------------------
# Tenant member management
# ---------------------------------------------------------------------------


@router.get("/api/tenants/{tenant_id}/members")
async def list_tenant_members(
    tenant_id: int, request: Request, _user_id: int = Depends(require_admin)
):
    """List members of a tenant (admin-only)."""
    try:
        from src.db.pg import auth_store
        # Verify tenant exists
        tenant = auth_store().get_tenant_by_id(tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
        members = auth_store().list_members_of_tenant(tenant_id)
    except HTTPException:
        raise
    except Exception as e:
        _logger.warning("list_tenant_members %d failed: %s", tenant_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    return JSONResponse(_json_safe({"members": members}))


@router.post("/api/tenants/{tenant_id}/members")
@audit_action("tenant.add_member", target_param="tenant_id")
async def add_tenant_member(
    tenant_id: int,
    body: AddMemberBody,
    request: Request,
    _user_id: int = Depends(require_admin),
):
    """Add or update a user's membership in a tenant (admin-only).

    Idempotent: re-posting with different role upserts the role.
    Returns 404 if user or tenant not found.
    """
    try:
        from src.db.pg import auth_store
        # Verify tenant exists
        tenant = auth_store().get_tenant_by_id(tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
        # Verify user exists
        user = auth_store().get_user_by_id(body.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail=f"User id={body.user_id} not found")
        auth_store().add_tenant_member(body.user_id, tenant_id, body.role)
    except HTTPException:
        raise
    except ValueError as e:
        return JSONResponse(_json_safe({"error": str(e)}), status_code=400)
    except Exception as e:
        _logger.warning(
            "add_tenant_member tenant=%d user=%d failed: %s", tenant_id, body.user_id, e
        )
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    return JSONResponse(_json_safe({"ok": True}))


@router.delete("/api/tenants/{tenant_id}/members/{user_id}")
@audit_action("tenant.remove_member", target_param="tenant_id")
async def remove_tenant_member(
    tenant_id: int,
    user_id: int,
    request: Request,
    _user_id: int = Depends(require_admin),
):
    """Remove a user's membership from a tenant (admin-only)."""
    try:
        from src.db.pg import auth_store
        # Verify tenant exists
        tenant = auth_store().get_tenant_by_id(tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
        auth_store().remove_tenant_member(user_id, tenant_id)
    except HTTPException:
        raise
    except Exception as e:
        _logger.warning("remove_tenant_member tenant=%d user=%d failed: %s", tenant_id, user_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    return JSONResponse(_json_safe({"ok": True}))


# ---------------------------------------------------------------------------
# Resource-to-tenant assignment (profile and repo)
# ---------------------------------------------------------------------------


@router.patch("/api/profiles/{profile_id}/tenant")
@audit_action("profile.assign_tenant", target_param="profile_id")
async def assign_profile_tenant(
    profile_id: int,
    body: AssignProfileTenantBody,
    request: Request,
    _user_id: int = Depends(require_admin),
):
    """Assign or clear the tenant for a profile (admin-only) - fail-closed.

    Changing a profile's tenant_id changes which tenant's members can access it
    via the read-side isolation, so the allowed_profiles cache is invalidated.
    ``RepoStore.set_profile_tenant`` enforces, in one transaction:

    - tenant_id null (shared, visible to every key) needs ``confirm_shared=true``
      (422 otherwise);
    - the profile is never more visible than its parent, and no child more
      visible than the profile (422, the conflicting profile is named - this
      route is admin-only);
    - the profile's repos follow: ``repos.tenant_id`` is set to the new tenant.
    """
    # Validate tenant_id exists when not null
    if body.tenant_id is not None:
        try:
            from src.db.pg import auth_store
            tenant = auth_store().get_tenant_by_id(body.tenant_id)
            if tenant is None:
                raise HTTPException(
                    status_code=404, detail=f"Tenant {body.tenant_id} not found"
                )
        except HTTPException:
            raise
        except Exception as e:
            _logger.warning("assign_profile_tenant tenant lookup failed: %s", e)
            return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    from src.db.exceptions import (
        ProfileInputError,
        ProfileNotFoundError,
        ProfileTenantRequiredError,
        ProfileVisibilityError,
    )
    try:
        from src.db.pg import repo_store
        result = repo_store().set_profile_tenant(
            profile_id, body.tenant_id, confirm_shared=body.confirm_shared,
        )
        # Changing tenant_id changes profile->tenant mapping -> drop RLS cache
        from src.mcp.session import invalidate_allowed_profiles
        invalidate_allowed_profiles()
        try:
            request.state.audit_detail.update(
                {"tenant_id": body.tenant_id, "repos_updated": result["repos_updated"]},
            )
        except Exception:
            pass
    except ProfileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")
    except (ProfileTenantRequiredError, ProfileVisibilityError, ProfileInputError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        _logger.warning("assign_profile_tenant profile=%d failed: %s", profile_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    return JSONResponse(_json_safe({"ok": True, **result}))


@router.patch("/api/repos/{repo_id}/tenant")
@audit_action("repo.assign_tenant", target_param="repo_id")
async def assign_repo_tenant(
    repo_id: int,
    body: AssignTenantBody,
    request: Request,
    _user_id: int = Depends(require_admin),
):
    """Re-sync a repo's tenant to its profile's (admin-only).

    ``repos.tenant_id`` follows the profile: any other value is rejected with
    422 (change the profile's tenant instead - PATCH /api/profiles/{id}/tenant
    propagates it to every repo of the profile).
    """
    # Validate tenant_id exists when not null
    if body.tenant_id is not None:
        try:
            from src.db.pg import auth_store
            tenant = auth_store().get_tenant_by_id(body.tenant_id)
            if tenant is None:
                raise HTTPException(
                    status_code=404, detail=f"Tenant {body.tenant_id} not found"
                )
        except HTTPException:
            raise
        except Exception as e:
            _logger.warning("assign_repo_tenant tenant lookup failed: %s", e)
            return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    from src.db.exceptions import ProfileVisibilityError
    try:
        from src.db.pg import repo_store
        found = repo_store().set_repo_tenant(repo_id, body.tenant_id)
        if not found:
            raise HTTPException(status_code=404, detail=f"Repo {repo_id} not found")
    except HTTPException:
        raise
    except ProfileVisibilityError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        _logger.warning("assign_repo_tenant repo=%d failed: %s", repo_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    return JSONResponse(_json_safe({"ok": True}))
