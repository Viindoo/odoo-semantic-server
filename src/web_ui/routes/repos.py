# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/repos.py
"""Profiles & Repos management routes (M8 W1 — pure JSON API).

Note: job status/reset routes were moved to src/web_ui/routes/jobs.py
(Phase 8 review) so that clients polling /api/jobs/{id}/status resolve
correctly. The original prefix "/api/repos" caused 404s for those paths.
"""
import logging
import subprocess
import sys
import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.db.audit import audit_action
from src.web_ui._json import _json_safe
from src.web_ui.auth import (
    ALL_TENANTS,
    is_in_scope,
    require_admin,
    resolve_tenant_scope_web,
    tenant_write_allowed,
)

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/repos")


async def _require_authenticated(request: Request) -> int:
    """FastAPI dependency: require an authenticated session (not necessarily admin).

    Returns user_id. Raises 401 if not authenticated.
    Used by W2 routes that are open to non-admin tenant members.
    """
    from src.web_ui.auth import current_user_id
    user_id = current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user_id


@router.get("/profiles")
async def list_profiles(request: Request):
    """Return all profiles with their repos, filtered to the session's tenant scope.

    W2: tenant-scoped read-side filter. Admin sees all; non-admin sees only profiles
    in their tenant scope (own tenant + shared/null). tenant_id is included in every
    profile and repo entry so the portal can route writes correctly.
    """
    scope = resolve_tenant_scope_web(request)
    profiles = []
    error = None
    all_job_id = None
    all_job_status = None
    try:
        from src.db.pg import job_store, repo_store

        for p in repo_store().list_profiles():
            profile_tenant_id = p.get("tenant_id")
            # READ filter: is_in_scope allows null (shared) for all; admin sees all
            if not is_in_scope(scope, profile_tenant_id):
                continue
            repos = repo_store().get_repos_for_profile(p["name"])
            # Attach last_job to each repo for status badge; expose tenant_id
            for repo in repos:
                repo["last_job"] = job_store().get_last_job(p["name"])
            profiles.append({
                **p,
                "tenant_id": profile_tenant_id,
                "repos": repos,
            })

        # Fetch most recent bulk "all" job for top-of-page badge (admin-only usage)
        if scope is ALL_TENANTS:
            all_job = job_store().get_last_job("all")
            if all_job:
                all_job_id = all_job["id"]
                all_job_status = all_job["status"]
    except Exception as e:
        error = str(e)

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
    - 200 + updated_fields list on success.
    """
    try:
        from src.db.exceptions import (
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

        updated_fields = repo_store().update_profile(
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
    """Delete a profile (and cascade-delete its repos), then clean Neo4j + pgvector."""
    from pathlib import Path

    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running

        # Lookup profile name
        profiles = repo_store().list_profiles()
        profile = next((p for p in profiles if p["id"] == profile_id), None)
        if profile is None:
            return JSONResponse(_json_safe({"error": "Profile not found."}), status_code=404)

        profile_name = profile["name"]

        # Guard: reject if indexer is running for this profile
        with get_pool().checkout() as conn:
            running = indexer_is_running(conn, profile_name)
        if running:
            return JSONResponse(
                _json_safe(
                    {"error": f"Cannot delete: indexer running for profile {profile_name}"}
                ),
                status_code=409,
            )

        # Snapshot repos BEFORE PG delete (for Neo4j + pgvector cleanup)
        repos = repo_store().get_repos_for_profile(profile_name)
        repo_cleanup_pairs = [
            {
                "basename": Path(r["local_path"]).name,
                "version": r["odoo_version"],
            }
            for r in repos
        ]

        # PG delete (CASCADE removes child repos automatically)
        result = repo_store().delete_profile(profile_id)
        repo_count = len(result["repos"])

        # WG-3t T4: deleting a profile removes it from every tenant's own/shared
        # scope → drop the 60s cache so isolation cannot keep serving it.
        from src.mcp.session import invalidate_allowed_profiles
        invalidate_allowed_profiles()

    except Exception as e:
        _logger.warning("Delete profile %s failed: %s", profile_id, e)
        return JSONResponse(_json_safe({"error": f"Delete failed: {e}"}), status_code=500)

    # Neo4j + pgvector cleanup (outside PG conn)
    module_names_by_version = _collect_module_names_for_repos(repo_cleanup_pairs)
    total_modules, total_children = _delete_neo4j_for_repos(repo_cleanup_pairs)
    total_embeddings = _delete_embeddings_for_repos(repo_cleanup_pairs, module_names_by_version)

    return JSONResponse(_json_safe({
        "ok": True,
        "profile_name": profile_name,
        "repo_count": repo_count,
        "neo4j_modules": total_modules,
        "neo4j_children": total_children,
        "embeddings": total_embeddings,
    }))


def _get_neo4j_writer():
    """Build a Neo4jWriter from config, or None if password is missing."""
    from src import config
    from src.indexer.writer_neo4j import Neo4jWriter

    uri = config.from_env_or_ini(
        "NEO4J_URI", "database", "neo4j_uri",
        fallback="bolt://localhost:7687",
    )
    user = config.from_env_or_ini(
        "NEO4J_USER", "database", "neo4j_user", fallback="neo4j",
    )
    password = config.from_env_or_ini(
        "NEO4J_PASSWORD", "database", "neo4j_password", fallback=None,
    )
    if not password:
        return None
    return Neo4jWriter(uri=uri, user=user, password=password)


def _delete_neo4j_for_repos(repo_cleanup_pairs: list[dict]) -> tuple[int, int]:
    """Delete Neo4j Module nodes + children for each (basename, version) pair.

    Returns (total_modules_deleted, total_children_deleted).
    """
    total_modules = 0
    total_children = 0
    for pair in repo_cleanup_pairs:
        basename = pair["basename"]
        version = pair["version"]
        try:
            writer = _get_neo4j_writer()
            if writer is None:
                continue
            try:
                counts = writer.delete_modules_scoped(basename, version)
                total_modules += counts.get("modules", 0)
                total_children += counts.get("children", 0)
            finally:
                writer.close()
        except Exception as e:
            _logger.warning(
                "Neo4j cleanup failed for repo %s version %s: %s", basename, version, e
            )
    return total_modules, total_children


def _collect_module_names_for_repos(
    repo_cleanup_pairs: list[dict],
) -> dict[str, list[str]]:
    """Query Neo4j for Odoo module names belonging to each (basename, version) pair.

    Returns a dict mapping version → list of module names.
    Must be called BEFORE _delete_neo4j_for_repos so the Module nodes still exist.
    """
    by_version: dict[str, list[str]] = {}
    for pair in repo_cleanup_pairs:
        version = pair["version"]
        basename = pair["basename"]
        try:
            writer = _get_neo4j_writer()
            if writer is None:
                _logger.warning(
                    "Neo4j unavailable — cannot resolve module names for repo %s v%s",
                    basename,
                    version,
                )
                continue
            try:
                with writer.driver.session() as session:
                    result = session.run(
                        "MATCH (m:Module {repo: $repo, odoo_version: $v}) "
                        "RETURN m.name AS module_name",
                        repo=basename,
                        v=version,
                    )
                    names = [row["module_name"] for row in result]
            finally:
                writer.close()
            by_version.setdefault(version, []).extend(names)
        except Exception as e:
            _logger.warning(
                "Failed to collect module names for repo %s v%s: %s", basename, version, e
            )
    return by_version


def _delete_embeddings_for_repos(
    repo_cleanup_pairs: list[dict],
    module_names_by_version: dict[str, list[str]] | None = None,
) -> int:
    """Delete pgvector embeddings for each (basename, version) repo pair.

    Resolves the correct Odoo module names from ``module_names_by_version`` (a dict
    produced by ``_collect_module_names_for_repos`` called BEFORE the Neo4j delete).
    The embeddings table stores Odoo module names (e.g. ``sale``, ``account``), NOT
    repo basenames — using basenames was a production bug that made every DELETE a
    no-op.

    If ``module_names_by_version`` is None or empty for a version, the DELETE is a
    correct no-op (repo was never indexed → no embeddings to clean).

    Returns total embeddings rows deleted.
    """
    if module_names_by_version is None:
        module_names_by_version = {}

    total = 0

    # Collect all versions we need to clean (deduplicated)
    versions_seen: set[str] = {pair["version"] for pair in repo_cleanup_pairs}
    if not any(module_names_by_version.get(v) for v in versions_seen):
        return 0  # nothing to delete

    try:
        from src.db.pg import get_pool

        for version in versions_seen:
            module_list = module_names_by_version.get(version, [])
            if not module_list:
                continue  # repo never indexed → no embeddings to delete
            try:
                with get_pool().checkout() as conn:
                    rowcount = get_pool().execute(
                        conn,
                        "DELETE FROM embeddings "
                        "WHERE odoo_version = %s AND module = ANY(%s)",
                        (version, module_list),
                    )
                    total += rowcount
            except Exception as e:
                _logger.warning(
                    "pgvector cleanup failed for version %s modules %s: %s",
                    version,
                    module_list,
                    e,
                )
    except Exception as e:
        _logger.warning("PG connection unavailable — skipping embeddings cleanup: %s", e)

    return total


class AddRepoBody(BaseModel):
    profile: str
    url: str
    branch: str
    # local_path is server-managed: always derived via default_clone_dir(profile, url).
    # Any client-supplied local_path field is silently ignored (WI-G).
    ssh_key_id: str = ""


@router.post("/repos")
@audit_action("repo.create")
async def add_repo(
    body: AddRepoBody, request: Request, _user_id: int = Depends(_require_authenticated)
):
    """Add a repo to a profile. Triggers async clone for SSH URLs.

    W2: open to authenticated non-admin users within their tenant scope.
    Non-admin may only add repos to profiles belonging to their tenant
    (shared/null profiles are admin-only; cross-tenant is 403).

    local_path is always server-derived via default_clone_dir(profile, url) —
    user-supplied local_path values are not accepted (WI-G server-managed paths).
    """
    from src.git_utils import default_clone_dir, is_ssh_url

    # Bug (i) fix: early 404 guard for both SSH and HTTPS paths (W1, ADR-0038).
    try:
        from src.db.pg import repo_store
        profiles_list = [p for p in repo_store().list_profiles() if p["name"] == body.profile]
    except Exception as e:
        _logger.warning("Add repo: could not list profiles: %s", e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)

    if not profiles_list:
        return JSONResponse(
            _json_safe({"error": f"Profile '{body.profile}' not found."}),
            status_code=404,
        )
    profile_row = profiles_list[0]
    profile_id = profile_row["id"]
    profile_tenant_id = profile_row.get("tenant_id")

    # W2: write-scope check — tenant_write_allowed is stricter than is_in_scope
    scope = resolve_tenant_scope_web(request)
    if not tenant_write_allowed(scope, profile_tenant_id):
        raise HTTPException(
            status_code=403,
            detail="Write access denied: outside your tenant scope",
        )

    if is_ssh_url(body.url):
        if not body.ssh_key_id or not body.ssh_key_id.strip().isdigit():
            return JSONResponse(
                _json_safe(
                    {"error": "SSH URL requires an SSH key. Select one from the dropdown."}
                ),
                status_code=400,
            )
        ssh_key_id_int = int(body.ssh_key_id.strip())
        repo_id: int | None = None
        try:
            target_dir = default_clone_dir(body.profile, body.url)
            repo_id = repo_store().add_repo(
                profile_id=profile_id,
                url=body.url,
                branch=body.branch,
                local_path=str(target_dir),
                ssh_key_id=ssh_key_id_int,
                # clone_status is the git-clone lifecycle (manual/pending/cloned/error),
                # set by set_clone_status. Distinct from `status` (indexer lifecycle:
                # pending/running/done/error, set by update_repo_status). The repo starts
                # with clone_status='manual' (not yet cloned); a background clone process
                # immediately transitions it to 'pending'. `status` defaults to 'pending'
                # (indexer lifecycle) — repo is freshly added, not yet indexed.
                clone_status="manual",
                tenant_id=profile_tenant_id,  # W2: inherit tenant from profile
            )
        except Exception as e:
            _logger.warning("Add SSH repo failed: %s", e)

        if repo_id is not None:
            with open(f"/tmp/osm-clone-{repo_id}.log", "wb") as _clone_log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "src.cloner", "--repo-id", str(repo_id)],
                    start_new_session=True,
                    stdout=_clone_log,
                    stderr=_clone_log,
                )
            threading.Thread(target=proc.wait, daemon=True).start()
            return JSONResponse(_json_safe({
                "ok": True,
                "repo_id": repo_id,
                "clone_status": "pending",
            }))
        return JSONResponse(
            _json_safe({"ok": False, "error": "Failed to add SSH repo"}),
            status_code=500,
        )

    # HTTPS / file:// — derive local_path server-side (WI-G: no user-supplied path)
    try:
        target_dir = default_clone_dir(body.profile, body.url)
        repo_store().add_repo(
            profile_id=profile_id,
            url=body.url,
            branch=body.branch,
            local_path=str(target_dir),
            ssh_key_id=None,
            # clone_status is the git-clone lifecycle (not the indexer lifecycle).
            # 'manual' means "no auto-clone triggered"; the indexer will pick it up
            # via the normal index flow. `status` (indexer lifecycle) defaults to
            # 'pending' — correct, repo is new and has not been indexed yet.
            clone_status="manual",
            tenant_id=profile_tenant_id,  # W2: inherit tenant from profile
        )
    except Exception as e:
        _logger.warning("Add repo failed: %s", e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)
    return JSONResponse(_json_safe({"ok": True}))


@router.get("/ssh-keys-list")
async def ssh_keys_list(request: Request):
    """Return JSON array of SSH key pairs (id + name) for dropdowns."""
    try:
        from src.db.pg import auth_store

        keys = auth_store().list_ssh_keys()
    except Exception as e:
        return JSONResponse(_json_safe({"error": str(e)}), status_code=503)
    return JSONResponse(_json_safe([{"id": k["id"], "name": k["name"]} for k in keys]))


class UpdateRepoBody(BaseModel):
    url: str | None = None
    branch: str | None = None
    # ssh_key_id: None = do not change; use clear_ssh_key=True to set NULL explicitly.
    ssh_key_id: int | None = None
    clear_ssh_key: bool = False
    # local_path is server-managed and cannot be changed via PATCH (WI-G).
    # Any client-supplied local_path is silently ignored.


@router.patch("/repos/{repo_id}")
@audit_action("repo.update", target_param="repo_id")
async def update_repo(
    repo_id: int,
    body: UpdateRepoBody,
    request: Request,
    _user_id: int = Depends(_require_authenticated),
):
    """Update URL / branch / SSH key of an existing repo.

    W2: open to authenticated non-admin users within their tenant scope.
    Non-admin may only patch repos belonging to their tenant (shared/null is admin-only).

    head_sha is intentionally preserved so the incremental indexer can still
    use the stored sha — avoiding a costly full reindex after a metadata edit.

    local_path is server-managed and cannot be changed via this endpoint (WI-G).
    """
    from src.git_utils import is_ssh_url

    try:
        from src.db.exceptions import RepoConflictError, RepoNotFoundError
        from src.db.pg import repo_store

        # SSH URL validation: if the effective URL is SSH, a key must be set.
        # Resolve effective URL (may come from body or from existing row).
        existing = repo_store().get_repo_by_id(repo_id)
        if existing is None:
            return JSONResponse(_json_safe({"error": "Repo not found."}), status_code=404)

        # W2: write-scope check on repo's tenant_id
        scope = resolve_tenant_scope_web(request)
        if not tenant_write_allowed(scope, existing.get("tenant_id")):
            raise HTTPException(
                status_code=403,
                detail="Write access denied: outside your tenant scope",
            )

        # Capture before-snapshot for forensic audit detail (non-sensitive fields only)
        try:
            request.state.audit_detail["before"] = {
                "url": existing.get("url"),
                "branch": existing.get("branch"),
                "ssh_key_id": existing.get("ssh_key_id"),
            }
        except Exception:
            pass

        effective_url = body.url if body.url is not None else existing["url"]
        effective_ssh_key_id = existing.get("ssh_key_id")
        if body.clear_ssh_key:
            effective_ssh_key_id = None
        elif body.ssh_key_id is not None:
            effective_ssh_key_id = body.ssh_key_id

        if is_ssh_url(effective_url) and not effective_ssh_key_id:
            return JSONResponse(
                _json_safe(
                    {"error": "SSH URL requires an SSH key. Provide ssh_key_id or select one."}
                ),
                status_code=400,
            )

        # local_path is NOT passed — server-managed only (WI-G)
        updated_fields = repo_store().update_repo(
            repo_id,
            url=body.url,
            branch=body.branch,
            ssh_key_id=body.ssh_key_id,
            clear_ssh_key=body.clear_ssh_key,
        )

        # Capture after-snapshot — only fields that changed
        try:
            after: dict = {}
            if body.url is not None:
                after["url"] = body.url
            if body.branch is not None:
                after["branch"] = body.branch
            if body.clear_ssh_key:
                after["ssh_key_id"] = None
            elif body.ssh_key_id is not None:
                after["ssh_key_id"] = body.ssh_key_id
            request.state.audit_detail["after"] = after
            request.state.audit_detail["updated_fields"] = updated_fields
        except Exception:
            pass

    except HTTPException:
        raise  # W2: re-raise 403 scope denials before generic catch
    except RepoNotFoundError:
        return JSONResponse(_json_safe({"error": "Repo not found."}), status_code=404)
    except RepoConflictError as e:
        _logger.warning("Update repo %s conflict: %s", repo_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=409)
    except Exception as e:
        _logger.warning("Update repo %s failed: %s", repo_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)

    return JSONResponse(_json_safe({
        "ok": True,
        "repo_id": repo_id,
        "updated_fields": updated_fields,
    }))


@router.delete("/repos/{repo_id}")
@audit_action("repo.delete", target_param="repo_id")
async def delete_repo(
    request: Request, repo_id: int, _user_id: int = Depends(_require_authenticated)
):
    """Delete a single repo, then clean Neo4j + pgvector scoped to that repo.

    W2: open to authenticated non-admin users within their tenant scope.
    Non-admin may only delete repos belonging to their tenant (shared/null is admin-only).
    """
    from pathlib import Path

    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running

        repo = repo_store().get_repo_by_id(repo_id)
        if repo is None:
            return JSONResponse(_json_safe({"error": "Repo not found."}), status_code=404)

        # W2: write-scope check on repo's tenant_id
        scope = resolve_tenant_scope_web(request)
        if not tenant_write_allowed(scope, repo.get("tenant_id")):
            raise HTTPException(
                status_code=403,
                detail="Write access denied: outside your tenant scope",
            )

        profile_name = repo["profile_name"]
        odoo_version = repo["odoo_version"]
        basename = Path(repo["local_path"]).name

        # Guard: reject if indexer is running for the containing profile
        with get_pool().checkout() as conn:
            running = indexer_is_running(conn, profile_name)
        if running:
            return JSONResponse(
                _json_safe(
                    {"error": f"Cannot delete: indexer running for profile {profile_name}"}
                ),
                status_code=409,
            )

        # PG delete
        repo_store().delete_repo(repo_id)

    except HTTPException:
        raise  # W2: re-raise 403 scope denials before generic catch
    except Exception as e:
        _logger.warning("Delete repo %s failed: %s", repo_id, e)
        return JSONResponse(_json_safe({"error": f"Delete failed: {e}"}), status_code=500)

    # Neo4j + pgvector cleanup
    cleanup_pairs = [{"basename": basename, "version": odoo_version}]
    module_names_by_version = _collect_module_names_for_repos(cleanup_pairs)
    total_modules, total_children = _delete_neo4j_for_repos(cleanup_pairs)
    total_embeddings = _delete_embeddings_for_repos(cleanup_pairs, module_names_by_version)

    return JSONResponse(_json_safe({
        "ok": True,
        "basename": basename,
        "neo4j_modules": total_modules,
        "neo4j_children": total_children,
        "embeddings": total_embeddings,
    }))


@router.post("/profiles/{profile_id}/clone-all")
@audit_action("profile.clone_all", target_param="profile_id")
async def clone_all_pending(
    profile_id: int, request: Request, _user_id: int = Depends(require_admin)
):
    """Bulk-clone all pending/manual/error repos for a profile.

    file:// URLs pointing to existing local directories are short-circuited
    (marked 'cloned' inline without spawning a subprocess). All other repos
    have a ``src.cloner`` subprocess spawned in a background thread, mirroring
    the single-repo clone flow.

    Returns JSON: { ok, profile_id, spawned, short_circuited, total }.
    """
    from pathlib import Path
    from urllib.parse import urlparse

    # F22: distinguish 404 (profile does not exist) from 200 (profile exists,
    # no repos pending). Check profile existence before listing repos.
    try:
        from src.db.pg import repo_store

        profile = repo_store().get_profile_by_id(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found")

        pending_statuses = {"manual", "pending", "error"}

        all_repos = repo_store().get_repos_for_profile_by_id(profile_id)
        repos = [
            r for r in all_repos if r.get("clone_status", "manual") in pending_statuses
        ]

        if not repos:
            return JSONResponse(_json_safe({
                "ok": True,
                "profile_id": profile_id,
                "spawned": 0,
                "short_circuited": 0,
                "total": 0,
                "message": "No pending repos to clone.",
            }))

        short_circuited = 0
        spawned = 0

        for r in repos:
            repo_id: int = r["id"]
            url: str = r.get("url", "")

            # Short-circuit file:// URLs with existing local directory
            parsed = urlparse(url)
            if parsed.scheme == "file":
                local_path = (
                    parsed.netloc + parsed.path if parsed.netloc else parsed.path
                )
                if Path(local_path).is_dir():
                    try:
                        repo_store().update_repo_local_path(repo_id, local_path)
                        repo_store().set_clone_status(repo_id, "cloned")
                        short_circuited += 1
                    except Exception as e:
                        _logger.warning(
                            "clone-all: short-circuit failed for repo id=%s: %s",
                            repo_id,
                            e,
                        )
                    continue

            # Spawn cloner subprocess (detached, logged to /tmp)
            try:
                with open(f"/tmp/osm-clone-{repo_id}.log", "wb") as _clone_log:
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "src.cloner", "--repo-id", str(repo_id)],
                        start_new_session=True,
                        stdout=_clone_log,
                        stderr=_clone_log,
                    )
                threading.Thread(target=proc.wait, daemon=True).start()
                spawned += 1
            except Exception as e:
                _logger.warning(
                    "clone-all: spawn failed for repo id=%s: %s", repo_id, e
                )
    except HTTPException:
        raise
    except Exception as e:
        _logger.warning("clone-all failed for profile %s: %s", profile_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)

    return JSONResponse(_json_safe({
        "ok": True,
        "profile_id": profile_id,
        "spawned": spawned,
        "short_circuited": short_circuited,
        "total": spawned + short_circuited,
    }))


@router.get("/repos/{repo_id}/clone-status")
async def clone_status(request: Request, repo_id: int):
    """Return JSON clone_status for a single repo (used by badge polling)."""
    try:
        from src.db.pg import repo_store

        repo = repo_store().get_repo_by_id(repo_id)
    except Exception as e:
        return JSONResponse(_json_safe({"error": str(e)}), status_code=503)
    if repo is None:
        return JSONResponse(_json_safe({"error": "repo not found"}), status_code=404)
    return JSONResponse(_json_safe({
        "id": repo["id"],
        "clone_status": repo.get("clone_status", "manual"),
        "error_msg": repo.get("clone_error_msg"),
    }))


class IndexRepoBody(BaseModel):
    no_embed: str = ""
    full: str = ""
    gc: str = ""
    max_workers: str = "1"


@router.post("/repos/{repo_id}/index")
@audit_action("operations.index_repo", target_param="repo_id")
async def index_repo(
    request: Request,
    repo_id: int,
    body: IndexRepoBody,
    _user_id: int = Depends(_require_authenticated),
):
    """Trigger indexer for a specific repo's profile (non-blocking subprocess).

    W2: open to authenticated non-admin users within their tenant scope.
    Non-admin may only trigger index for repos in their tenant (shared/null is admin-only).
    """
    # Validate max_workers before acquiring a DB connection
    try:
        max_workers_int = int(body.max_workers)
    except (ValueError, TypeError):
        return JSONResponse(
            _json_safe(
                {
                    "error": f"Invalid max_workers value '{body.max_workers}': "
                    "must be an integer between 1 and 8."
                }
            ),
            status_code=422,
        )

    if not (1 <= max_workers_int <= 8):
        return JSONResponse(
            _json_safe(
                {"error": f"max_workers must be between 1 and 8 (got {max_workers_int})."}
            ),
            status_code=422,
        )

    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running
        from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

        repos = repo_store().list_repos()
        repo = next((r for r in repos if r["id"] == repo_id), None)
        if repo is None:
            return JSONResponse(_json_safe({"error": "Repo not found."}), status_code=404)
        if not repo.get("profile_name"):
            return JSONResponse(
                _json_safe({"error": "Repo is not attached to a profile."}),
                status_code=400,
            )

        # W2: write-scope check on repo's tenant_id
        scope = resolve_tenant_scope_web(request)
        if not tenant_write_allowed(scope, repo.get("tenant_id")):
            raise HTTPException(
                status_code=403,
                detail="Write access denied: outside your tenant scope",
            )

        with get_pool().checkout() as conn:
            running = indexer_is_running(conn, repo["profile_name"])
        if running:
            return JSONResponse(
                _json_safe({
                    "error": (
                        f"Indexer already running for profile "
                        f"{repo['profile_name']}. Wait for it to finish."
                    )
                }),
                status_code=409,
            )

        argv = ["index-repo", "--profile", repo["profile_name"]]
        if body.no_embed:
            argv += ["--no-embed"]
        if body.full:
            argv += ["--full"]
        if body.gc:
            argv += ["--gc"]
        if max_workers_int != 1:
            argv += ["--max-workers", str(max_workers_int)]

        job_id = spawn_indexer_subcommand(argv, job_label=repo["profile_name"])
        return JSONResponse(_json_safe({"ok": True, "job_id": job_id}))
    except HTTPException:
        raise  # W2: re-raise 403 scope denials before generic catch
    except Exception as e:
        _logger.warning("Index trigger for repo %s failed: %s", repo_id, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)


@router.post("/repos/{repo_id}/reset-embed")
@audit_action("operations.reset_embed", target_param="repo_id")
async def reset_embed(
    request: Request, repo_id: int, _user_id: int = Depends(require_admin)
):
    """Reset head_sha to NULL and spawn index-repo (with embeddings) for the repo's profile."""
    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running
        from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

        repo = repo_store().get_repo_by_id(repo_id)
        if repo is None:
            return JSONResponse(_json_safe({"error": "Repo not found."}), status_code=404)

        profile_name = repo["profile_name"]

        with get_pool().checkout() as conn:
            running = indexer_is_running(conn, profile_name)
        if running:
            return JSONResponse(
                _json_safe({
                    "error": (
                        f"Cannot reset embed state: indexer already running for profile "
                        f"{profile_name}. Wait for it to finish."
                    )
                }),
                status_code=409,
            )

        # Wipe head_sha → forces full re-scan
        repo_store().reset_repo_head_sha(repo_id)

        argv = ["index-repo", "--profile", profile_name]
        job_id = spawn_indexer_subcommand(argv, job_label=profile_name)

        return JSONResponse(_json_safe({
            "ok": True,
            "profile_name": profile_name,
            "job_id": job_id,
        }))

    except Exception as e:
        _logger.warning("Reset embed for repo %s failed: %s", repo_id, e)
        return JSONResponse(_json_safe({"error": f"Reset embed failed: {e}"}), status_code=500)


class IndexAllBody(BaseModel):
    no_embed: str = ""
    full: str = ""
    gc: str = ""
    max_workers: str = "1"
    profile_workers: str = "1"


@router.post("/index-all")
@audit_action("operations.index_all")
async def index_all(
    request: Request, body: IndexAllBody, _user_id: int = Depends(require_admin)
):
    """Trigger bulk index-repo --all for every registered profile."""
    # Validate max_workers in [1, 8]
    try:
        max_workers_int = int(body.max_workers)
    except (ValueError, TypeError):
        return JSONResponse(
            _json_safe(
                {
                    "error": f"Invalid max_workers '{body.max_workers}': "
                    "must be an integer between 1 and 8."
                }
            ),
            status_code=422,
        )
    if not (1 <= max_workers_int <= 8):
        return JSONResponse(
            _json_safe(
                {"error": f"max_workers must be between 1 and 8 (got {max_workers_int})."}
            ),
            status_code=422,
        )

    # Validate profile_workers in [1, 4]
    try:
        profile_workers_int = int(body.profile_workers)
    except (ValueError, TypeError):
        return JSONResponse(
            _json_safe(
                {
                    "error": f"Invalid profile_workers '{body.profile_workers}': "
                    "must be an integer between 1 and 4."
                }
            ),
            status_code=422,
        )
    if not (1 <= profile_workers_int <= 4):
        return JSONResponse(
            _json_safe(
                {"error": f"profile_workers must be between 1 and 4 (got {profile_workers_int})."}
            ),
            status_code=422,
        )

    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running
        from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

        all_profiles = repo_store().list_profiles()
        blocked = []
        with get_pool().checkout() as conn:
            blocked = [p["name"] for p in all_profiles if indexer_is_running(conn, p["name"])]
        if blocked:
            names = ", ".join(blocked)
            return JSONResponse(
                _json_safe(
                    {"error": f"Cannot start index-all: indexer running for: {names}"}
                ),
                status_code=409,
            )

        argv = ["index-repo", "--all"]
        if body.no_embed:
            argv += ["--no-embed"]
        if body.full:
            argv += ["--full"]
        if body.gc:
            argv += ["--gc"]
        if max_workers_int != 1:
            argv += ["--max-workers", str(max_workers_int)]
        if profile_workers_int != 1:
            argv += ["--profile-workers", str(profile_workers_int)]

        job_id = spawn_indexer_subcommand(argv, job_label="all")
        return JSONResponse(_json_safe({"ok": True, "job_id": job_id}))

    except Exception as e:
        _logger.warning("index-all trigger failed: %s", e)
        return JSONResponse(_json_safe({"error": f"index-all failed: {e}"}), status_code=500)


@router.get("/repos/{repo_id}/core-symbol-counts")
async def core_symbol_counts(request: Request, repo_id: int):
    """Return CoreSymbol counts per version for a single repo.

    Queries Neo4j for all :CoreSymbol nodes grouped by ``odoo_version``.
    The version(s) relevant to this repo come from its profile.  We return
    counts for every version present in the graph so the UI can show zero-
    vs-nonzero status badges.

    JSON response: ``{"counts": {"17.0": 1234, "16.0": 0, ...}}``

    Returns 404 when the repo is not found.
    Returns 503 when Postgres / repo-lookup fails.
    Returns 200 with an empty ``counts`` dict when Neo4j is unavailable or
    the Neo4j query fails (graceful degradation - no 503 on graph errors).
    """
    try:
        from src.db.pg import repo_store

        repo = repo_store().get_repo_by_id(repo_id)
    except Exception as e:
        return JSONResponse(_json_safe({"error": str(e)}), status_code=503)

    if repo is None:
        return JSONResponse(_json_safe({"error": "repo not found"}), status_code=404)

    odoo_version: str | None = repo.get("odoo_version")

    try:
        writer = _get_neo4j_writer()
        if writer is None:
            return JSONResponse(_json_safe({"counts": {}}))
        try:
            with writer.driver.session() as session:
                if odoo_version:
                    # Fast path: only count for the repo's own version
                    result = session.run(
                        "MATCH (cs:CoreSymbol {odoo_version: $v}) "
                        "RETURN $v AS version, COUNT(cs) AS cnt",
                        v=odoo_version,
                    )
                    counts = {row["version"]: row["cnt"] for row in result}
                else:
                    # Fallback: group all versions (repo has no version attached)
                    result = session.run(
                        "MATCH (cs:CoreSymbol) "
                        "RETURN cs.odoo_version AS version, COUNT(cs) AS cnt "
                        "ORDER BY toFloat(version)"
                    )
                    counts = {row["version"]: row["cnt"] for row in result}
        finally:
            try:
                writer.close()
            except Exception:
                pass
    except Exception as e:
        _logger.warning("core_symbol_counts Neo4j query failed for repo %s: %s", repo_id, e)
        return JSONResponse(_json_safe({"counts": {}}))

    return JSONResponse(_json_safe({"counts": counts}))


# Job status and reset routes have been moved to src/web_ui/routes/jobs.py
# (prefix="/api/jobs") per Phase 8 review — see that module for job_status
# and reset_stuck_job handlers.
