# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/repos_indexing.py
"""Clone + index trigger routes (B3 split from repos.py — pure JSON API).

Mounted by ``repos.py`` under the shared ``/api/repos`` prefix via
``include_router`` — path strings stay byte-identical to the pre-split routes.

``subprocess`` is imported at module level so the ``src.cloner`` spawn path in
``clone_all_pending`` keeps working (mirrors ``repos_crud.add_repo``).
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
    read_access_allowed,
    require_admin,
    require_authenticated,
    resolve_read_scope,
    resolve_tenant_scope_web,
    tenant_write_allowed,
)

_logger = logging.getLogger(__name__)
router = APIRouter()


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
    """Return JSON clone_status for a single repo (used by badge polling).

    Security (IDOR sweep #237):
    - repos.tenant_id scopes visibility; out-of-scope → 404 (no oracle).
    - clone_error_msg redacted for non-admin (may contain filesystem paths / SSH errors).
    """
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

    return JSONResponse(_json_safe({
        "id": repo["id"],
        "clone_status": repo.get("clone_status", "manual"),
        "error_msg": repo.get("clone_error_msg") if is_admin else None,
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
    _user_id: int = Depends(require_authenticated),
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
