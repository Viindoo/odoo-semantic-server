# src/web_ui/routes/repos.py
"""Profiles & Repos management routes."""
import logging
import os
import subprocess
import sys
import threading
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

_logger = logging.getLogger(__name__)
router = APIRouter()


def _is_pid_alive(pid: int) -> bool:
    """Return True if process pid is still running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, different UID — assume alive


@router.get("/repos", response_class=HTMLResponse)
async def repos_page(request: Request):
    templates = request.app.state.templates
    flash = request.query_params.get("flash")
    profiles = []
    error = None
    all_job_id = None
    all_job_status = None
    try:
        from src.db.pg import job_store, repo_store

        for p in repo_store().list_profiles():
            repos = repo_store().get_repos_for_profile(p["name"])
            # Attach last_job to each repo for status badge
            for repo in repos:
                repo["last_job"] = job_store().get_last_job(p["name"])
            profiles.append({**p, "repos": repos})

        # Fetch most recent bulk "all" job for top-of-page badge
        all_job = job_store().get_last_job("all")
        if all_job:
            all_job_id = all_job["id"]
            all_job_status = all_job["status"]
    except Exception as e:
        error = str(e)

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
    parent_id: Annotated[str, Form()] = "",
):
    from urllib.parse import quote_plus

    # Parse parent_id: empty string = None (root profile), digit string = int.
    _parent_id: int | None = None
    if parent_id and parent_id.strip().isdigit():
        _parent_id = int(parent_id.strip())

    try:
        from src.db.pg import repo_store

        repo_store().add_profile(
            name=name,
            odoo_version=version,
            description=description,
            parent_id=_parent_id,
        )
    except ValueError as e:
        # Cycle / version-mismatch validation errors → redirect with flash.
        _logger.warning("Create profile validation failed: %s", e)
        return RedirectResponse(
            "/repos?flash=" + quote_plus(f"Create profile failed: {e}"),
            status_code=303,
        )
    except Exception as e:
        _logger.warning("Create profile failed: %s", e)
    return RedirectResponse("/repos", status_code=303)


@router.post("/repos/profiles/{profile_id}/parent", response_class=RedirectResponse)
async def set_profile_parent(
    request: Request,
    profile_id: int,
    parent_id: Annotated[str, Form()] = "",
):
    """Update parent_profile_id for an existing profile.

    POST form field ``parent_id``: integer ID of the new parent, or empty string
    to clear the parent (make this profile a root). Validates cycle-free + version
    match; returns HTTP 303 redirect to /repos with flash on success or failure.
    """
    from urllib.parse import quote_plus

    _parent_id: int | None = None
    if parent_id and parent_id.strip().isdigit():
        _parent_id = int(parent_id.strip())

    try:
        from src.db.pg import repo_store

        changed = repo_store().set_profile_parent(profile_id, _parent_id)
        if changed:
            flash = f"Profile id={profile_id} parent updated."
        else:
            flash = f"Profile id={profile_id} parent already set to requested value."
    except ValueError as e:
        _logger.warning("Set profile parent validation failed: %s", e)
        flash = f"Set parent failed: {e}"
    except Exception as e:
        _logger.warning("Set profile parent failed: %s", e)
        flash = f"Set parent failed: {e}"

    return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)


@router.post("/repos/profiles/{profile_id}/delete", response_class=RedirectResponse)
async def delete_profile(request: Request, profile_id: int):
    """Delete a profile (and cascade-delete its repos), then clean Neo4j + pgvector."""
    from pathlib import Path
    from urllib.parse import quote_plus

    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running

        # Lookup profile name for flash message + job guard
        profiles = repo_store().list_profiles()
        profile = next((p for p in profiles if p["id"] == profile_id), None)
        if profile is None:
            return RedirectResponse(
                "/repos?flash=" + quote_plus("Profile not found."),
                status_code=303,
            )

        profile_name = profile["name"]

        # Guard: reject if indexer is running for this profile
        with get_pool().checkout() as conn:
            running = indexer_is_running(conn, profile_name)
        if running:
            flash = f"Cannot delete: indexer running for profile {profile_name}"
            return RedirectResponse(
                f"/repos?flash={quote_plus(flash)}",
                status_code=303,
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

    except Exception as e:
        _logger.warning("Delete profile %s failed: %s", profile_id, e)
        return RedirectResponse(
            "/repos?flash=" + quote_plus(f"Delete failed: {e}"),
            status_code=303,
        )

    # Neo4j + pgvector cleanup (outside PG conn — Neo4j driver manages its own connections)
    # Collect module names FIRST (while Module nodes still exist in Neo4j)
    module_names_by_version = _collect_module_names_for_repos(repo_cleanup_pairs)
    total_modules, total_children = _delete_neo4j_for_repos(repo_cleanup_pairs)
    total_embeddings = _delete_embeddings_for_repos(repo_cleanup_pairs, module_names_by_version)

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
            profiles = []
            try:
                from src.db.pg import repo_store
                profiles = repo_store().list_profiles()
            except Exception:
                pass
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
        try:
            from src.db.pg import repo_store

            profiles_list = [p for p in repo_store().list_profiles() if p["name"] == profile]
            if profiles_list:
                target_dir = default_clone_dir(profile, url)
                repo_id = repo_store().add_repo(
                    profile_id=profiles_list[0]["id"],
                    url=url,
                    branch=branch,
                    local_path=str(target_dir),
                    ssh_key_id=ssh_key_id_int,
                    clone_status="manual",
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
            flash = quote_plus("Clone started — refresh to see status")
            return RedirectResponse(f"/repos?flash={flash}", status_code=303)
        return RedirectResponse("/repos", status_code=303)

    # HTTPS / file:// / manual path — legacy behavior unchanged
    try:
        from src.db.pg import repo_store

        profiles_list = [p for p in repo_store().list_profiles() if p["name"] == profile]
        if profiles_list:
            repo_store().add_repo(
                profile_id=profiles_list[0]["id"],
                url=url,
                branch=branch,
                local_path=local_path,
                ssh_key_id=None,
                clone_status="manual",
            )
    except Exception as e:
        _logger.warning("Add repo failed: %s", e)
    return RedirectResponse("/repos", status_code=303)


@router.get("/repos/ssh-keys-list")
async def ssh_keys_list(request: Request):
    """Return JSON array of SSH key pairs (id + name) for the add-repo form dropdown."""
    try:
        from src.db.pg import auth_store

        keys = auth_store().list_ssh_keys()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse([{"id": k["id"], "name": k["name"]} for k in keys])


@router.post("/repos/repos/{repo_id}/delete", response_class=RedirectResponse)
async def delete_repo(request: Request, repo_id: int):
    """Delete a single repo, then clean Neo4j + pgvector scoped to that repo."""
    from pathlib import Path
    from urllib.parse import quote_plus

    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running

        # Lookup repo + profile info; 404-style redirect if missing
        repo = repo_store().get_repo_by_id(repo_id)
        if repo is None:
            return RedirectResponse(
                "/repos?flash=" + quote_plus("Repo not found."),
                status_code=303,
            )

        profile_name = repo["profile_name"]
        odoo_version = repo["odoo_version"]
        basename = Path(repo["local_path"]).name

        # Guard: reject if indexer is running for the containing profile
        with get_pool().checkout() as conn:
            running = indexer_is_running(conn, profile_name)
        if running:
            flash = f"Cannot delete: indexer running for profile {profile_name}"
            return RedirectResponse(
                f"/repos?flash={quote_plus(flash)}",
                status_code=303,
            )

        # PG delete (snapshot already done above)
        repo_store().delete_repo(repo_id)

    except Exception as e:
        _logger.warning("Delete repo %s failed: %s", repo_id, e)
        return RedirectResponse(
            "/repos?flash=" + quote_plus(f"Delete failed: {e}"),
            status_code=303,
        )

    # Neo4j + pgvector cleanup (outside PG conn)
    cleanup_pairs = [{"basename": basename, "version": odoo_version}]
    # Collect module names FIRST (while Module nodes still exist in Neo4j)
    module_names_by_version = _collect_module_names_for_repos(cleanup_pairs)
    total_modules, total_children = _delete_neo4j_for_repos(cleanup_pairs)
    total_embeddings = _delete_embeddings_for_repos(cleanup_pairs, module_names_by_version)

    flash = (
        f"Repo '{basename}' deleted "
        f"({total_modules} Neo4j modules, {total_children} child nodes, "
        f"{total_embeddings} embeddings)"
    )
    return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)


@router.post("/repos/profiles/{profile_id}/clone-all", response_class=RedirectResponse)
async def clone_all_pending(request: Request, profile_id: int):
    """Bulk-clone all pending/manual/error repos for a profile.

    file:// URLs pointing to existing local directories are short-circuited
    (marked 'cloned' inline without spawning a subprocess).  All other repos
    have a ``src.cloner`` subprocess spawned in a background thread, mirroring
    the single-repo clone flow.
    """
    from pathlib import Path
    from urllib.parse import quote_plus, urlparse

    from src.db.pg import repo_store

    pending_statuses = {"manual", "pending", "error"}

    all_repos = repo_store().get_repos_for_profile_by_id(profile_id)
    repos = [r for r in all_repos if r.get("clone_status", "manual") in pending_statuses]

    if not repos:
        flash = quote_plus("No pending repos to clone.")
        return RedirectResponse(f"/repos?flash={flash}", status_code=303)

    short_circuited = 0
    spawned = 0

    for r in repos:
        repo_id: int = r["id"]
        url: str = r.get("url", "")

        # Short-circuit file:// URLs with existing local directory
        parsed = urlparse(url)
        if parsed.scheme == "file":
            local_path = parsed.netloc + parsed.path if parsed.netloc else parsed.path
            if Path(local_path).is_dir():
                try:
                    repo_store().update_repo_local_path(repo_id, local_path)
                    repo_store().set_clone_status(repo_id, "cloned")
                    short_circuited += 1
                except Exception as e:
                    _logger.warning(
                        "clone-all: short-circuit failed for repo id=%s: %s", repo_id, e
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
            _logger.warning("clone-all: spawn failed for repo id=%s: %s", repo_id, e)

    msg = f"Clone started: {spawned} spawned, {short_circuited} short-circuited (file://)."
    return RedirectResponse(f"/repos?flash={quote_plus(msg)}", status_code=303)


@router.get("/repos/repos/{repo_id}/clone-status")
async def clone_status(request: Request, repo_id: int):
    """Return JSON clone_status for a single repo (used by badge polling)."""
    try:
        from src.db.pg import repo_store

        repo = repo_store().get_repo_by_id(repo_id)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
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

    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running
        from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

        repos = repo_store().list_repos()
        repo = next((r for r in repos if r["id"] == repo_id), None)
        if repo and repo.get("profile_name"):
            with get_pool().checkout() as conn:
                running = indexer_is_running(conn, repo["profile_name"])
            if running:
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

            spawn_indexer_subcommand(argv, job_label=repo["profile_name"])
    except Exception as e:
        _logger.warning("Index trigger for repo %s failed: %s", repo_id, e)
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

    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running
        from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

        repo = repo_store().get_repo_by_id(repo_id)
        if repo is None:
            return RedirectResponse(
                "/repos?flash=" + quote_plus("Repo not found."),
                status_code=404,
            )

        profile_name = repo["profile_name"]

        # Guard: reject if indexer is already running for this profile
        with get_pool().checkout() as conn:
            running = indexer_is_running(conn, profile_name)
        if running:
            flash = (
                f"Cannot reset embed state: indexer already running for profile "
                f"{profile_name}. Wait for it to finish."
            )
            return RedirectResponse(
                f"/repos?flash={quote_plus(flash)}",
                status_code=303,
            )

        # Wipe head_sha → forces full re-scan (bypasses incremental skip)
        repo_store().reset_repo_head_sha(repo_id)

        # Spawn index-repo without --no-embed / --full
        argv = ["index-repo", "--profile", profile_name]
        job_id = spawn_indexer_subcommand(argv, job_label=profile_name)

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

    try:
        from src.db.pg import get_pool, repo_store
        from src.indexer.pipeline import indexer_is_running
        from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

        # Guard: reject if any profile has a running indexer job
        all_profiles = repo_store().list_profiles()
        blocked = []
        with get_pool().checkout() as conn:
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

        job_id = spawn_indexer_subcommand(argv, job_label="all")
        flash = f"Index all started (job {job_id})"
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)

    except Exception as e:
        _logger.warning("index-all trigger failed: %s", e)
        flash = f"index-all failed: {e}"
        return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)


@router.get("/repos/jobs/{job_id}/status")
async def job_status(request: Request, job_id: int):
    """Return JSON status of a single indexer job.

    Includes ``is_alive`` field: True/False when status='running' and a PID is recorded,
    None otherwise. A False value indicates a stuck job (process not found).
    """
    try:
        from src.db.pg import job_store

        job = job_store().get_job(job_id)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)

    pid = job.get("pid")
    is_alive: bool | None = None
    if pid is not None and job.get("status") == "running":
        is_alive = _is_pid_alive(pid)

    return JSONResponse({
        "id": job["id"],
        "profile_name": job["profile_name"],
        "status": job["status"],
        "pid": job["pid"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "error_msg": job["error_msg"],
        "created_at": job["created_at"],
        "is_alive": is_alive,
    })


@router.post("/repos/jobs/{job_id}/reset", response_class=RedirectResponse)
async def reset_stuck_job(request: Request, job_id: int):
    """Force-mark a stuck running job as error when its PID is dead."""
    import datetime as _dt
    from urllib.parse import quote_plus

    try:
        from src.db.pg import job_store

        job = job_store().get_job(job_id)
        if job is None:
            flash = f"Job {job_id} not found."
        elif job["status"] != "running":
            flash = f"Job {job_id} is not in 'running' state (current: {job['status']})."
        else:
            pid = job.get("pid")
            if pid is not None and _is_pid_alive(pid):
                flash = f"Job {job_id} process (PID {pid}) is still alive — cannot reset."
            else:
                job_store().update_job(
                    job_id,
                    status="error",
                    finished_at=_dt.datetime.now(_dt.UTC),
                    error_msg="Reset by admin (process not found)",
                )
                flash = f"Job {job_id} has been reset to error state."
    except Exception as e:
        flash = f"Reset failed: {e}"
        _logger.warning("Reset job %d failed: %s", job_id, e)
    return RedirectResponse(f"/repos?flash={quote_plus(flash)}", status_code=303)
