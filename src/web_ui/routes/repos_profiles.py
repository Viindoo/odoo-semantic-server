# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/repos_profiles.py
"""Profile management routes (B3 split from repos.py — pure JSON API).

This sub-router carries the profile CRUD endpoints. It is mounted by
``repos.py`` under the shared ``/api/repos`` prefix via ``include_router`` —
so every path string here is relative and stays byte-identical to the
pre-split paths.

The ledger-first removal helper used by ``delete_profile`` is resolved
through the ``repos`` module namespace at call time (``repos._*``) so the
test patch surface ``src.web_ui.routes.repos._*`` keeps working.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from src.db.audit import audit_action
from src.web_ui._json import _json_safe
from src.web_ui.auth import (
    ALL_TENANTS,
    is_in_scope,
    require_admin,
    resolve_read_scope,
)

_logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/profiles")
async def list_profiles(request: Request):
    """Return all profiles with their repos, filtered to the session's tenant scope.

    W2: tenant-scoped read-side filter. Admin sees all; non-admin sees only profiles
    in their tenant scope (own tenant + shared/null). tenant_id is included in every
    profile and repo entry so the portal can route writes correctly.
    """
    from src.web_ui.routes import repos as repos_module

    is_admin, scope = resolve_read_scope(request)
    profiles = []
    error = None
    all_job_id = None
    all_job_status = None
    try:
        from src.db.pg import job_store, repo_store

        all_repos: list[dict] = []
        for p in repo_store().list_profiles():
            profile_tenant_id = p.get("tenant_id")
            # READ filter: is_in_scope allows null (shared) for all; admin sees all
            if not is_in_scope(scope, profile_tenant_id):
                continue
            repos = repo_store().get_repos_for_profile(p["name"])
            # Attach last_job to each repo for status badge; expose tenant_id
            for repo in repos:
                repo["last_job"] = job_store().get_last_job(p["name"])
            all_repos.extend(repos)
            profiles.append({
                **p,
                "tenant_id": profile_tenant_id,
                "repos": repos,
            })
        repos_module._attach_lifecycle(all_repos)
        repos_module._redact_repo_rows(all_repos, is_admin=is_admin)

        # Fetch most recent bulk "all" job for top-of-page badge (admin-only usage)
        if scope is ALL_TENANTS:
            all_job = job_store().get_last_job("all")
            if all_job:
                all_job_id = all_job["id"]
                all_job_status = all_job["status"]
    except Exception as e:
        _logger.warning("%s failed: %s", request.url.path, e)
        # Raw exception text can name hosts/paths: admin only.
        error = str(e) if is_admin else "Internal error loading repositories."

    return JSONResponse(_json_safe({
        "profiles": profiles,
        "error": error,
        "all_job_id": all_job_id,
        "all_job_status": all_job_status,
    }))


class CreateProfileBody(BaseModel):
    name: str
    version: str
    description: str = ""
    parent_id: int | None = None


@router.post("/profiles")
@audit_action("profile.create")
async def create_profile(
    body: CreateProfileBody, request: Request, _user_id: int = Depends(require_admin)
):
    """Create a new profile.

    Optional ``parent_id`` links this profile under another profile (version
    must match parent; cycle-free + monotonic chain enforced by repo_store).
    """
    try:
        from src.db.pg import repo_store

        repo_store().add_profile(
            name=body.name,
            odoo_version=body.version,
            description=body.description,
            parent_id=body.parent_id,
        )
        # WG-3t T4: a new profile changes the own/shared scope a tenant resolves
        # to → drop the 60s tenant-scope cache so isolation cannot serve stale.
        from src.mcp.session import invalidate_allowed_profiles
        invalidate_allowed_profiles()
    except ValueError as e:
        # Cycle / version-mismatch validation errors → 400.
        _logger.warning("Create profile validation failed: %s", e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=400)
    except Exception as e:
        _logger.warning("Create profile failed: %s", e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    return JSONResponse(_json_safe({"ok": True}))


class SetProfileParentBody(BaseModel):
    parent_id: int | None = None


@router.patch("/profiles/{profile_id}/parent")
@audit_action("profile.set_parent", target_param="profile_id")
async def set_profile_parent(
    profile_id: int,
    body: SetProfileParentBody,
    request: Request,
    _user_id: int = Depends(require_admin),
):
    """Update parent_profile_id for an existing profile.

    JSON body ``parent_id``: integer ID of the new parent, or ``null`` to clear
    the parent (make this profile a root). Validates cycle-free + version match.
    Returns 400 on validation error, 200 on success.
    """
    try:
        from src.db.exceptions import (
            ProfileCycleError,
            ProfileNotFoundError,
            ProfileVersionMismatchError,
        )
        from src.db.pg import repo_store

        changed = repo_store().set_profile_parent(profile_id, body.parent_id)
        # WG-3t T4: re-parenting alters the ancestor chain → shared scope changes.
        if changed:
            from src.mcp.session import invalidate_allowed_profiles
            invalidate_allowed_profiles()
    except ProfileNotFoundError as e:
        _logger.warning("Set profile parent: profile not found: %s", e)
        raise HTTPException(status_code=404, detail="Profile not found")
    except (ProfileCycleError, ProfileVersionMismatchError) as e:
        _logger.warning("Set profile parent validation failed: %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        _logger.warning("Set profile parent failed: %s", e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)

    return JSONResponse(_json_safe({
        "ok": True,
        "profile_id": profile_id,
        "parent_id": body.parent_id,
        "changed": changed,
    }))


class UpdateProfileBody(BaseModel):
    name: str | None = None
    version: str | None = None
    description: str | None = None


@router.patch("/profiles/{profile_id}")
@audit_action("profile.update", target_param="profile_id")
async def update_profile(
    profile_id: int,
    body: UpdateProfileBody,
    request: Request,
    _user_id: int = Depends(require_admin),
):
    """Update name, version, and/or description for an existing profile.

    - 404 if profile not found.
    - 409 if new name conflicts with an existing profile (UNIQUE), or if profile
      has indexed repos and name/version change is requested (re-index required).
    - 422 if new version conflicts with a descendant or ancestor profile version
      (ADR-0016).
    - 409 if a rename cannot take the module lifecycle ledger lock within
      ``WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS`` (nothing changed; the ledger's
      profile_name is rewritten in the same transaction, ADR-0056 L7).
    - 200 + updated_fields list on success.
    """
    try:
        from src.db.exceptions import (
            LifecycleLockTimeout,
            ProfileIndexedError,
            ProfileNameConflictError,
            ProfileNotFoundError,
            ProfileVersionMismatchError,
        )
        from src.db.pg import repo_store

        # Capture before-snapshot for forensic audit detail (non-sensitive fields only)
        existing = repo_store().get_profile_by_id(profile_id)
        if existing is not None:
            try:
                request.state.audit_detail["before"] = {
                    "name": existing.get("name"),
                    "odoo_version": existing.get("odoo_version"),
                    "description": existing.get("description"),
                }
            except Exception:
                pass

        # A rename waits for the ledger lock (up to WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS):
        # off the event loop.
        updated_fields = await run_in_threadpool(
            repo_store().update_profile,
            profile_id,
            name=body.name,
            version=body.version,
            description=body.description,
        )

        # Capture after-snapshot
        try:
            after: dict = {}
            if body.name is not None:
                after["name"] = body.name
            if body.version is not None:
                after["odoo_version"] = body.version
            if body.description is not None:
                after["description"] = body.description
            request.state.audit_detail["after"] = after
            request.state.audit_detail["updated_fields"] = updated_fields
        except Exception:
            pass

        # WG-3t T4: a profile rename changes the names a tenant resolves to via
        # own/shared → drop the 60s tenant-scope cache so isolation cannot serve
        # stale (e.g. an old name still granting visibility).
        if updated_fields:
            from src.mcp.session import invalidate_allowed_profiles
            invalidate_allowed_profiles()

    except ProfileNotFoundError as e:
        _logger.warning("Update profile: not found: %s", e)
        raise HTTPException(status_code=404, detail="Profile not found")
    except ProfileNameConflictError as e:
        _logger.warning("Update profile: name conflict: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    except ProfileIndexedError as e:
        _logger.warning("Update profile: indexed repos block change: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    except ProfileVersionMismatchError as e:
        _logger.warning("Update profile: version mismatch: %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    except LifecycleLockTimeout as e:
        _logger.warning("Update profile %s: ledger lock busy: %s", profile_id, e)
        raise HTTPException(
            status_code=409,
            detail=f"Profile rename deferred: a module lifecycle reconcile is running "
                   f"({e}); retry when the index run finished",
            headers={"Retry-After": "60"},
        )
    except Exception as e:
        _logger.warning("Update profile %s failed: %s", profile_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)

    return JSONResponse(_json_safe({
        "ok": True,
        "profile_id": profile_id,
        "updated_fields": updated_fields,
    }))


@router.delete("/profiles/{profile_id}")
@audit_action("profile.delete", target_param="profile_id")
async def delete_profile(
    request: Request, profile_id: int, _user_id: int = Depends(require_admin)
):
    """Delete a profile and its repos, reconciling their modules through the ledger first.

    Same ledger-first order as the repo delete (ADR-0056, review H3): every
    repo of the profile is flagged ``repo_removed`` and its modules decided
    under the profile, per-repo and per-version locks BEFORE the profile row
    (and, by FK cascade, its repos) is deleted. Modules another profile still
    ships keep their node; the rest are retired with this profile's
    embeddings. 409 (nothing changed) when a lock is not obtained within
    ``WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS``.
    """
    # Resolve the removal helper through the repos namespace at call time so the
    # test patch surface (src.web_ui.routes.repos._*) keeps working.
    from src.web_ui.routes import repos

    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running

        profile = repo_store().get_profile_by_id(profile_id)
        if profile is None:
            return JSONResponse(_json_safe({"error": "Profile not found."}), status_code=404)

        profile_name = profile["name"]

        # Fast guard; the removal itself takes the profile lock race-free.
        with get_pool().checkout() as conn:
            running = indexer_is_running(conn, profile_name)
        if running:
            return JSONResponse(
                _json_safe(
                    {"error": f"Cannot delete: indexer running for profile {profile_name}"}
                ),
                status_code=409,
            )

        repos_for_profile = repo_store().get_repos_for_profile_by_id(profile_id)
        result, summary = await run_in_threadpool(
            repos._remove_repos_through_ledger,
            repos_for_profile,
            profile_name=profile_name,
            pg_delete=lambda: repo_store().delete_profile(profile_id),
        )
        repo_count = len(result["repos"])

        # WG-3t T4: deleting a profile removes it from every tenant's own/shared
        # scope → drop the 60s cache so isolation cannot keep serving it.
        from src.mcp.session import invalidate_allowed_profiles
        invalidate_allowed_profiles()

    except repos.RemovalBusy as e:
        return JSONResponse(
            _json_safe({"error": str(e)}),
            status_code=409,
            headers={"Retry-After": "60"},
        )
    except Exception as e:
        _logger.warning("Delete profile %s failed: %s", profile_id, e)
        return JSONResponse(_json_safe({"error": f"Delete failed: {e}"}), status_code=500)

    return JSONResponse(_json_safe({
        "ok": True,
        "profile_name": profile_name,
        "repo_count": repo_count,
        **summary,
    }))
