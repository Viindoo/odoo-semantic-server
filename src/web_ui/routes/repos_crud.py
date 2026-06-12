# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/repos_crud.py
"""Repo CRUD + SSH key listing + core-symbol-counts routes (B3 split from repos.py).

Mounted by ``repos.py`` under the shared ``/api/repos`` prefix via
``include_router`` — path strings stay byte-identical to the pre-split routes.

``subprocess`` is imported at module level so the SSH-clone spawn path in
``add_repo`` keeps working; note the test patch surface
``src.web_ui.routes.repos.subprocess.Popen`` still reaches this module because
``subprocess`` is a shared module singleton (patching ``Popen`` on it is global).

Neo4j + pgvector cleanup helpers used by ``delete_repo`` / ``core_symbol_counts``
are resolved through the ``repos`` namespace at call time (``repos._*``) so the
existing test patch surface ``src.web_ui.routes.repos._*`` keeps working.
"""
import logging
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.db.audit import audit_action
from src.web_ui._json import _json_safe
from src.web_ui.auth import (
    is_admin_session,
    read_access_allowed,
    require_authenticated,
    resolve_read_scope,
    resolve_tenant_scope_web,
    tenant_write_allowed,
)

_logger = logging.getLogger(__name__)
router = APIRouter()


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
    body: AddRepoBody, request: Request, _user_id: int = Depends(require_authenticated)
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
        # SSH key selection is NOT a free-form user choice. The deployment uses a
        # single shared admin-managed access keypair: the admin publishes its
        # PUBLIC key and every user adds that public key to their own git host —
        # users never select or install a private key in OSM (only the server-side
        # cloner ever decrypts it). An admin may still target a specific stored key
        # explicitly (key-management surface); a non-admin's client-supplied
        # ssh_key_id is IGNORED and the shared key is resolved server-side. This
        # closes the cross-tenant arbitrary-key-selection hole (code review #183,
        # ADR-0034) — a tenant member can no longer clone via someone else's key.
        if is_admin_session(request):
            if not body.ssh_key_id or not body.ssh_key_id.strip().isdigit():
                return JSONResponse(
                    _json_safe(
                        {"error": "SSH URL requires an SSH key. Select one from the dropdown."}
                    ),
                    status_code=400,
                )
            ssh_key_id_int = int(body.ssh_key_id.strip())
        else:
            from src.db.pg import auth_store
            shared_keys = auth_store().list_ssh_keys()  # access_key rows, ordered by id
            if not shared_keys:
                return JSONResponse(
                    _json_safe({
                        "error": "No shared SSH key configured. Ask an admin to set up an "
                                 "SSH key, then add its public key to your git host.",
                    }),
                    status_code=400,
                )
            ssh_key_id_int = shared_keys[0]["id"]
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
async def ssh_keys_list(request: Request, _user_id: int = Depends(require_authenticated)):
    """Return JSON array of SSH key pairs (id + name) for dropdowns.

    Security (IDOR sweep #237): SSH key names are admin-managed and globally shared
    (no per-tenant rows), but revealing them to unauthenticated callers is unnecessary.
    Require authentication — non-admin users legitimately need the list to attach keys
    to their repos, so admin-only would be too restrictive.
    """
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
    _user_id: int = Depends(require_authenticated),
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

        # SSH key is server-managed for non-admins (ADR-0038 D13): a non-admin's
        # client-supplied ssh_key_id / clear_ssh_key is IGNORED so they cannot point
        # a repo at an arbitrary stored key — e.g. another tenant's deploy_key that
        # list_ssh_keys() deliberately hides. This mirrors the add_repo guard above
        # and closes the same cross-tenant arbitrary-key-selection hole on the PATCH
        # path (code review #183). An admin may still target a specific stored key.
        if is_admin_session(request):
            patch_ssh_key_id = body.ssh_key_id
            patch_clear_ssh_key = body.clear_ssh_key
        else:
            patch_ssh_key_id = None  # preserve existing key; non-admin cannot change it
            patch_clear_ssh_key = False
            if is_ssh_url(effective_url) and not existing.get("ssh_key_id"):
                # URL is (now) SSH but the repo has no key yet — resolve the shared
                # admin-managed access key server-side, exactly like add_repo.
                from src.db.pg import auth_store

                shared_keys = auth_store().list_ssh_keys()  # access_key rows, ordered by id
                if not shared_keys:
                    return JSONResponse(
                        _json_safe({
                            "error": "No shared SSH key configured. Ask an admin to set up "
                                     "an SSH key, then add its public key to your git host.",
                        }),
                        status_code=400,
                    )
                patch_ssh_key_id = shared_keys[0]["id"]

        effective_ssh_key_id = existing.get("ssh_key_id")
        if patch_clear_ssh_key:
            effective_ssh_key_id = None
        elif patch_ssh_key_id is not None:
            effective_ssh_key_id = patch_ssh_key_id

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
            ssh_key_id=patch_ssh_key_id,
            clear_ssh_key=patch_clear_ssh_key,
        )

        # Capture after-snapshot — only fields that changed (resolved values, not the
        # raw client request, so the audit row reflects what actually changed).
        try:
            after: dict = {}
            if body.url is not None:
                after["url"] = body.url
            if body.branch is not None:
                after["branch"] = body.branch
            if patch_clear_ssh_key:
                after["ssh_key_id"] = None
            elif patch_ssh_key_id is not None:
                after["ssh_key_id"] = patch_ssh_key_id
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
    request: Request, repo_id: int, _user_id: int = Depends(require_authenticated)
):
    """Delete a single repo, then clean Neo4j + pgvector scoped to that repo.

    W2: open to authenticated non-admin users within their tenant scope.
    Non-admin may only delete repos belonging to their tenant (shared/null is admin-only).
    """
    # Resolve the cleanup helpers through the repos namespace at call time so the
    # test patch surface (src.web_ui.routes.repos._delete_*) keeps working.
    from src.web_ui.routes import repos

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
    module_names_by_version = repos._collect_module_names_for_repos(cleanup_pairs)
    total_modules, total_children = repos._delete_neo4j_for_repos(cleanup_pairs)
    total_embeddings = repos._delete_embeddings_for_repos(
        cleanup_pairs, module_names_by_version
    )

    return JSONResponse(_json_safe({
        "ok": True,
        "basename": basename,
        "neo4j_modules": total_modules,
        "neo4j_children": total_children,
        "embeddings": total_embeddings,
    }))


@router.get("/repos/{repo_id}/core-symbol-counts")
async def core_symbol_counts(request: Request, repo_id: int):
    """Return CoreSymbol counts per version for a single repo.

    Queries Neo4j for all :CoreSymbol nodes grouped by ``odoo_version``.
    The version(s) relevant to this repo come from its profile.  We return
    counts for every version present in the graph so the UI can show zero-
    vs-nonzero status badges.

    JSON response: ``{"counts": {"17.0": 1234, "16.0": 0, ...}}``

    Returns 404 when the repo is not found or out-of-scope (no oracle).
    Returns 503 when Postgres / repo-lookup fails.
    Returns 200 with an empty ``counts`` dict when Neo4j is unavailable or
    the Neo4j query fails (graceful degradation - no 503 on graph errors).

    Security (IDOR sweep #237): repos.tenant_id scopes visibility.
    """
    # Resolve _get_neo4j_writer through the repos namespace at call time so the
    # test patch surface (src.web_ui.routes.repos._get_neo4j_writer) keeps working.
    from src.web_ui.routes import repos

    try:
        from src.db.pg import repo_store

        repo = repo_store().get_repo_by_id(repo_id)
    except Exception as e:
        return JSONResponse(_json_safe({"error": str(e)}), status_code=503)

    if repo is None:
        return JSONResponse(_json_safe({"error": "not found"}), status_code=404)

    # Single resolution: is_admin is derived from the same scope (no double DB read).
    is_admin, scope = resolve_read_scope(request)
    if not read_access_allowed(is_admin, scope, repo.get("tenant_id")):
        return JSONResponse(_json_safe({"error": "not found"}), status_code=404)

    odoo_version: str | None = repo.get("odoo_version")

    try:
        writer = repos._get_neo4j_writer()
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
