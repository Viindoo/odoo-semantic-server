# src/web_ui/routes/repos.py
"""Profiles & Repos management routes."""
import logging
import subprocess
import sys
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

_logger = logging.getLogger(__name__)
router = APIRouter()


def _get_conn():
    import psycopg2

    from src import config

    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        return None
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception:
        return None


@router.get("/repos", response_class=HTMLResponse)
async def repos_page(request: Request):
    templates = request.app.state.templates
    flash = request.query_params.get("flash")
    profiles = []
    error = None
    conn = _get_conn()
    if conn:
        try:
            from src.db import job_registry
            from src.db.repo_registry import get_repos_for_profile, list_profiles

            for p in list_profiles(conn):
                repos = get_repos_for_profile(conn, p["name"])
                # Attach last_job to each repo for status badge
                for repo in repos:
                    repo["last_job"] = job_registry.get_last_job(conn, p["name"])
                profiles.append({**p, "repos": repos})
        except Exception as e:
            error = str(e)
        finally:
            conn.close()
    else:
        error = "Cannot connect to PostgreSQL. Check PG_DSN configuration."

    return templates.TemplateResponse(
        request, "repos.html", {"profiles": profiles, "error": error, "flash": flash}
    )


@router.post("/repos/profiles", response_class=RedirectResponse)
async def create_profile(
    request: Request,
    name: Annotated[str, Form()],
    version: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
):
    conn = _get_conn()
    if conn:
        try:
            from src.db.repo_registry import add_profile

            add_profile(conn, name=name, odoo_version=version, description=description)
        except Exception as e:
            _logger.warning("Create profile failed: %s", e)
        finally:
            conn.close()
    return RedirectResponse("/repos", status_code=303)


@router.post("/repos/repos", response_class=RedirectResponse)
async def add_repo(
    request: Request,
    profile: Annotated[str, Form()],
    url: Annotated[str, Form()],
    branch: Annotated[str, Form()],
    local_path: Annotated[str, Form()] = "",
    ssh_key_id: Annotated[str, Form()] = "",
):
    from urllib.parse import quote_plus

    from src.git_utils import default_clone_dir, is_ssh_url

    templates = request.app.state.templates

    if is_ssh_url(url):
        if not ssh_key_id or not ssh_key_id.strip().isdigit():
            conn = _get_conn()
            profiles = []
            if conn:
                try:
                    from src.db.repo_registry import list_profiles
                    profiles = list_profiles(conn)
                finally:
                    conn.close()
            return templates.TemplateResponse(
                request,
                "repos.html",
                {
                    "profiles": profiles,
                    "error": "SSH URL requires an SSH key. Select one from the dropdown.",
                    "flash": None,
                },
                status_code=400,
            )
        ssh_key_id_int = int(ssh_key_id.strip())
        repo_id: int | None = None
        conn = _get_conn()
        if conn:
            try:
                from src.db.repo_registry import add_repo as _add_repo
                from src.db.repo_registry import list_profiles

                profiles = [p for p in list_profiles(conn) if p["name"] == profile]
                if profiles:
                    target_dir = default_clone_dir(profile, url)
                    repo_id = _add_repo(
                        conn,
                        profile_id=profiles[0]["id"],
                        url=url,
                        branch=branch,
                        local_path=str(target_dir),
                        ssh_key_id=ssh_key_id_int,
                        clone_status="manual",
                    )
            except Exception as e:
                _logger.warning("Add SSH repo failed: %s", e)
            finally:
                conn.close()

        if repo_id is not None:
            subprocess.Popen(
                [sys.executable, "-m", "src.cloner", "--repo-id", str(repo_id)],
                start_new_session=True,
            )
            flash = quote_plus("Clone started — refresh to see status")
            return RedirectResponse(f"/repos?flash={flash}", status_code=303)
        return RedirectResponse("/repos", status_code=303)

    # HTTPS / file:// / manual path — legacy behavior unchanged
    conn = _get_conn()
    if conn:
        try:
            from src.db.repo_registry import add_repo as _add_repo
            from src.db.repo_registry import list_profiles

            profiles = [p for p in list_profiles(conn) if p["name"] == profile]
            if profiles:
                _add_repo(
                    conn,
                    profile_id=profiles[0]["id"],
                    url=url,
                    branch=branch,
                    local_path=local_path,
                    ssh_key_id=None,
                    clone_status="manual",
                )
        except Exception as e:
            _logger.warning("Add repo failed: %s", e)
        finally:
            conn.close()
    return RedirectResponse("/repos", status_code=303)


@router.get("/repos/ssh-keys-list")
async def ssh_keys_list(request: Request):
    """Return JSON array of SSH key pairs (id + name) for the add-repo form dropdown."""
    conn = _get_conn()
    if not conn:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    try:
        from src.db.auth_registry import list_ssh_keys

        keys = list_ssh_keys(conn)
    finally:
        conn.close()
    return JSONResponse([{"id": k["id"], "name": k["name"]} for k in keys])


@router.get("/repos/repos/{repo_id}/clone-status")
async def clone_status(request: Request, repo_id: int):
    """Return JSON clone_status for a single repo (used by badge polling)."""
    conn = _get_conn()
    if not conn:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    try:
        from src.db.repo_registry import get_repo_by_id

        repo = get_repo_by_id(conn, repo_id)
    finally:
        conn.close()
    if repo is None:
        return JSONResponse({"error": "repo not found"}, status_code=404)
    return JSONResponse({
        "id": repo["id"],
        "clone_status": repo.get("clone_status", "manual"),
        # Return clone_error_msg under the key "error_msg" to preserve API contract.
        # clone_error_msg is written exclusively by the cloner; repos.error_msg is
        # written exclusively by the indexer (update_repo_status). Keeping them separate
        # prevents a cloner success from clearing an indexer error and vice versa.
        "error_msg": repo.get("clone_error_msg"),
    })


@router.post("/repos/repos/{repo_id}/index", response_class=RedirectResponse)
async def index_repo(request: Request, repo_id: int):
    """Trigger indexer for a specific repo's profile (non-blocking subprocess)."""
    conn = _get_conn()
    if conn:
        try:
            from urllib.parse import quote_plus

            from src.db import job_registry
            from src.db.repo_registry import list_repos
            from src.indexer.pipeline import indexer_is_running

            repos = list_repos(conn)
            repo = next((r for r in repos if r["id"] == repo_id), None)
            if repo and repo.get("profile_name"):
                if indexer_is_running(conn, repo["profile_name"]):
                    flash = (
                        "Indexer already running for profile "
                        f"{repo['profile_name']}. Wait for it to finish."
                    )
                    return RedirectResponse(
                        f"/repos?flash={quote_plus(flash)}",
                        status_code=303,
                    )
                job_id = job_registry.create_job(conn, repo["profile_name"])
                subprocess.Popen(
                    [sys.executable, "-m", "src.indexer", "index-repo",
                     "--profile", repo["profile_name"],
                     "--job-id", str(job_id)],
                    start_new_session=True,
                )
        except Exception as e:
            _logger.warning("Index trigger for repo %s failed: %s", repo_id, e)
        finally:
            conn.close()
    return RedirectResponse("/repos", status_code=303)


@router.get("/repos/jobs/{job_id}/status")
async def job_status(request: Request, job_id: int):
    """Return JSON status of a single indexer job."""
    from src.db import job_registry

    conn = _get_conn()
    if not conn:
        return JSONResponse({"error": "database unavailable"}, status_code=503)
    try:
        job = job_registry.get_job(conn, job_id)
    finally:
        conn.close()
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return JSONResponse({
        "id": job["id"],
        "profile_name": job["profile_name"],
        "status": job["status"],
        "pid": job["pid"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "error_msg": job["error_msg"],
        "created_at": job["created_at"],
    })
