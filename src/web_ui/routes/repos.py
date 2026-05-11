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
    all_job_id = None
    all_job_status = None
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

            # Fetch most recent bulk "all" job for top-of-page badge
            all_job = job_registry.get_last_job(conn, "all")
            if all_job:
                all_job_id = all_job["id"]
                all_job_status = all_job["status"]
        except Exception as e:
            error = str(e)
        finally:
            conn.close()
    else:
        error = "Cannot connect to PostgreSQL. Check PG_DSN configuration."

    return templates.TemplateResponse(
        request,
        "repos.html",
        {
            "profiles": profiles,
            "error": error,
            "flash": flash,
            "all_job_id": all_job_id,
            "all_job_status": all_job_status,
        },
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


@router.post("/repos/profiles/{profile_id}/delete", response_class=RedirectResponse)
async def delete_profile(request: Request, profile_id: int):
    """Delete a profile (and cascade-delete its repos), then clean Neo4j + pgvector."""
    from pathlib import Path
    from urllib.parse import quote_plus

    conn = _get_conn()
    if not conn:
        return RedirectResponse(
            "/repos?flash=" + quote_plus("Cannot connect to database."),
            status_code=303,
        )

    try:
        from src.db.repo_registry import delete_profile as _delete_profile
        from src.db.repo_registry import get_repos_for_profile, list_profiles
        from src.indexer.pipeline import indexer_is_running

        # Lookup profile name for flash message + job guard
        profiles = list_profiles(conn)
        profile = next((p for p in profiles if p["id"] == profile_id), None)
        if profile is None:
            return RedirectResponse(
                "/repos?flash=" + quote_plus("Profile not found."),
                status_code=303,
            )

        profile_name = profile["name"]

        # Guard: reject if indexer is running for this profile
        if indexer_is_running(conn, profile_name):
            flash = f"Cannot delete: indexer running for profile {profile_name}"
            return RedirectResponse(
                f"/repos?flash={quote_plus(flash)}",
                status_code=303,
            )

        # Snapshot repos BEFORE PG delete (for Neo4j + pgvector cleanup)
        repos = get_repos_for_profile(conn, profile_name)
        repo_cleanup_pairs = [
            {
                "basename": Path(r["local_path"]).name,
                "version": r["odoo_version"],
            }
            for r in repos
        ]

        # PG delete (CASCADE removes child repos automatically)
        result = _delete_profile(conn, profile_id)
        repo_count = len(result["repos"])

    except Exception as e:
        _logger.warning("Delete profile %s failed: %s", profile_id, e)
        return RedirectResponse(
            "/repos?flash=" + quote_plus(f"Delete failed: {e}"),
            status_code=303,
        )
    finally:
        conn.close()

    # Neo4j + pgvector cleanup (outside PG conn — Neo4j driver manages its own connections)
    total_modules, total_children = _delete_neo4j_for_repos(repo_cleanup_pairs)
    total_embeddings = _delete_embeddings_for_repos(repo_cleanup_pairs)

    flash = (
        f"Profile '{profile_name}' deleted "
        f"({repo_count} repo{'s' if repo_count != 1 else ''}, "
        f"{total_modules} Neo4j modules, {total_children} child nodes, "
        f"{total_embeddings} embeddings)"
    )
    return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)


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


def _delete_embeddings_for_repos(repo_cleanup_pairs: list[dict]) -> int:
    """Delete pgvector embeddings rows for each (basename, version) repo pair.

    Uses basename as the module name: in Odoo, the module name equals the checkout
    directory name (basename). This is exact for single-module repos. Multi-module
    repos (uncommon) may leave orphan rows — covered by future `--gc` pass.

    Returns total embeddings rows deleted.
    """
    import psycopg2

    from src import config

    total = 0
    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        return 0

    for pair in repo_cleanup_pairs:
        version = pair["version"]
        basename = pair["basename"]
        try:
            pg_conn = psycopg2.connect(dsn)
            pg_conn.autocommit = True
            try:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM embeddings WHERE odoo_version = %s AND module = %s",
                        (version, basename),
                    )
                    total += cur.rowcount
            finally:
                pg_conn.close()
        except Exception as e:
            _logger.warning(
                "pgvector cleanup failed for repo %s version %s: %s", basename, version, e
            )

    return total


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


@router.post("/repos/repos/{repo_id}/delete", response_class=RedirectResponse)
async def delete_repo(request: Request, repo_id: int):
    """Delete a single repo, then clean Neo4j + pgvector scoped to that repo."""
    from pathlib import Path
    from urllib.parse import quote_plus

    conn = _get_conn()
    if not conn:
        return RedirectResponse(
            "/repos?flash=" + quote_plus("Cannot connect to database."),
            status_code=303,
        )

    try:
        from src.db.repo_registry import delete_repo as _delete_repo
        from src.db.repo_registry import get_repo_by_id
        from src.indexer.pipeline import indexer_is_running

        # Lookup repo + profile info; 404-style redirect if missing
        repo = get_repo_by_id(conn, repo_id)
        if repo is None:
            return RedirectResponse(
                "/repos?flash=" + quote_plus("Repo not found."),
                status_code=303,
            )

        profile_name = repo["profile_name"]
        odoo_version = repo["odoo_version"]
        basename = Path(repo["local_path"]).name

        # Guard: reject if indexer is running for the containing profile
        if indexer_is_running(conn, profile_name):
            flash = f"Cannot delete: indexer running for profile {profile_name}"
            return RedirectResponse(
                f"/repos?flash={quote_plus(flash)}",
                status_code=303,
            )

        # PG delete (snapshot already done above)
        _delete_repo(conn, repo_id)

    except Exception as e:
        _logger.warning("Delete repo %s failed: %s", repo_id, e)
        return RedirectResponse(
            "/repos?flash=" + quote_plus(f"Delete failed: {e}"),
            status_code=303,
        )
    finally:
        conn.close()

    # Neo4j + pgvector cleanup (outside PG conn)
    cleanup_pairs = [{"basename": basename, "version": odoo_version}]
    total_modules, total_children = _delete_neo4j_for_repos(cleanup_pairs)
    total_embeddings = _delete_embeddings_for_repos(cleanup_pairs)

    flash = (
        f"Repo '{basename}' deleted "
        f"({total_modules} Neo4j modules, {total_children} child nodes, "
        f"{total_embeddings} embeddings)"
    )
    return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)


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
async def index_repo(
    request: Request,
    repo_id: int,
    no_embed: Annotated[str, Form()] = "",
    full: Annotated[str, Form()] = "",
    gc: Annotated[str, Form()] = "",
    max_workers: Annotated[str, Form()] = "1",
):
    """Trigger indexer for a specific repo's profile (non-blocking subprocess).

    Accepts optional form fields:
    - no_embed: if truthy, appends --no-embed
    - full: if truthy, appends --full
    - gc: if truthy, appends --gc
    - max_workers: integer 1-8, appends --max-workers N when != 1
    """
    from urllib.parse import quote_plus

    # Validate max_workers before acquiring a DB connection
    try:
        max_workers_int = int(max_workers)
    except (ValueError, TypeError):
        flash = f"Invalid max_workers value '{max_workers}': must be an integer between 1 and 8."
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)

    if not (1 <= max_workers_int <= 8):
        flash = f"max_workers must be between 1 and 8 (got {max_workers_int})."
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)

    conn = _get_conn()
    if conn:
        try:
            from src.db.repo_registry import list_repos
            from src.indexer.pipeline import indexer_is_running
            from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

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

                argv = ["index-repo", "--profile", repo["profile_name"]]
                if no_embed:
                    argv += ["--no-embed"]
                if full:
                    argv += ["--full"]
                if gc:
                    argv += ["--gc"]
                if max_workers_int != 1:
                    argv += ["--max-workers", str(max_workers_int)]

                spawn_indexer_subcommand(conn, argv, job_label=repo["profile_name"])
        except Exception as e:
            _logger.warning("Index trigger for repo %s failed: %s", repo_id, e)
        finally:
            conn.close()
    return RedirectResponse("/repos", status_code=303)


@router.post("/repos/repos/{repo_id}/reset-embed", response_class=RedirectResponse)
async def reset_embed(request: Request, repo_id: int):
    """Reset head_sha to NULL and spawn index-repo (with embeddings) for the repo's profile.

    This fixes the HIGH-severity gap where `index-repo --no-embed` advanced head_sha,
    causing subsequent incremental runs to skip permanently — embeddings never written.
    Setting head_sha=NULL forces a full re-scan on the next run (bypasses incremental skip).
    No --no-embed, no --full flags: head_sha=NULL alone triggers full embed pass.
    """
    from urllib.parse import quote_plus

    conn = _get_conn()
    if not conn:
        return RedirectResponse(
            "/repos?flash=" + quote_plus("Cannot connect to database."),
            status_code=303,
        )

    try:
        from src.db.repo_registry import get_repo_by_id, reset_repo_head_sha
        from src.indexer.pipeline import indexer_is_running
        from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

        repo = get_repo_by_id(conn, repo_id)
        if repo is None:
            return RedirectResponse(
                "/repos?flash=" + quote_plus("Repo not found."),
                status_code=404,
            )

        profile_name = repo["profile_name"]

        # Guard: reject if indexer is already running for this profile
        if indexer_is_running(conn, profile_name):
            flash = (
                f"Cannot reset embed state: indexer already running for profile "
                f"{profile_name}. Wait for it to finish."
            )
            return RedirectResponse(
                f"/repos?flash={quote_plus(flash)}",
                status_code=303,
            )

        # Wipe head_sha → forces full re-scan (bypasses incremental skip)
        reset_repo_head_sha(conn, repo_id)

        # Spawn index-repo without --no-embed / --full
        argv = ["index-repo", "--profile", profile_name]
        job_id = spawn_indexer_subcommand(conn, argv, job_label=profile_name)

        flash = f"Re-embedding started for '{profile_name}' (job {job_id})"
        return RedirectResponse(
            f"/repos?flash={quote_plus(flash)}",
            status_code=303,
        )

    except Exception as e:
        _logger.warning("Reset embed for repo %s failed: %s", repo_id, e)
        return RedirectResponse(
            "/repos?flash=" + quote_plus(f"Reset embed failed: {e}"),
            status_code=303,
        )
    finally:
        conn.close()


@router.post("/repos/index-all", response_class=RedirectResponse)
async def index_all(
    request: Request,
    no_embed: Annotated[str, Form()] = "",
    full: Annotated[str, Form()] = "",
    max_workers: Annotated[str, Form()] = "1",
    profile_workers: Annotated[str, Form()] = "1",
):
    """Trigger bulk index-repo --all for every registered profile.

    Accepts optional form fields:
    - no_embed: if truthy, appends --no-embed
    - full: if truthy, appends --full
    - max_workers: integer 1-8 (per-profile thread count)
    - profile_workers: integer 1-4 (parallel profile count)
    """
    from urllib.parse import quote_plus

    # Validate max_workers ∈ [1, 8]
    try:
        max_workers_int = int(max_workers)
    except (ValueError, TypeError):
        flash = f"Invalid max_workers '{max_workers}': must be an integer between 1 and 8."
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)
    if not (1 <= max_workers_int <= 8):
        flash = f"max_workers must be between 1 and 8 (got {max_workers_int})."
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)

    # Validate profile_workers ∈ [1, 4]
    try:
        profile_workers_int = int(profile_workers)
    except (ValueError, TypeError):
        flash = f"Invalid profile_workers '{profile_workers}': must be an integer between 1 and 4."
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)
    if not (1 <= profile_workers_int <= 4):
        flash = f"profile_workers must be between 1 and 4 (got {profile_workers_int})."
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)

    conn = _get_conn()
    if not conn:
        flash = "Cannot connect to database."
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)

    try:
        from src.db.repo_registry import list_profiles
        from src.indexer.pipeline import indexer_is_running
        from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

        # Guard: reject if any profile has a running indexer job
        all_profiles = list_profiles(conn)
        blocked = [p["name"] for p in all_profiles if indexer_is_running(conn, p["name"])]
        if blocked:
            names = ", ".join(blocked)
            flash = f"Cannot start index-all: indexer running for: {names}"
            return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)

        # Build argv
        argv = ["index-repo", "--all"]
        if no_embed:
            argv += ["--no-embed"]
        if full:
            argv += ["--full"]
        if max_workers_int != 1:
            argv += ["--max-workers", str(max_workers_int)]
        if profile_workers_int != 1:
            argv += ["--profile-workers", str(profile_workers_int)]

        job_id = spawn_indexer_subcommand(conn, argv, job_label="all")
        flash = f"Index all started (job {job_id})"
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)

    except Exception as e:
        _logger.warning("index-all trigger failed: %s", e)
        flash = f"index-all failed: {e}"
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)
    finally:
        conn.close()


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
