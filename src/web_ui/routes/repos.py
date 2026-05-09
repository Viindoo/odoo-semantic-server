# src/web_ui/routes/repos.py
"""Profiles & Repos management routes."""
import logging
import subprocess
import sys
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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
            from src.db.repo_registry import get_repos_for_profile, list_profiles

            for p in list_profiles(conn):
                repos = get_repos_for_profile(conn, p["name"])
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
    local_path: Annotated[str, Form()],
):
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
                )
        except Exception as e:
            _logger.warning("Add repo failed: %s", e)
        finally:
            conn.close()
    return RedirectResponse("/repos", status_code=303)


@router.post("/repos/repos/{repo_id}/index", response_class=RedirectResponse)
async def index_repo(request: Request, repo_id: int):
    """Trigger indexer for a specific repo's profile (non-blocking subprocess)."""
    conn = _get_conn()
    if conn:
        try:
            from urllib.parse import quote_plus

            from src.db.repo_registry import list_repos
            from src.indexer.pipeline import indexer_is_running

            repos = list_repos(conn)
            repo = next((r for r in repos if r["id"] == repo_id), None)
            if repo and repo.get("profile_name"):
                if indexer_is_running(conn):
                    flash = (
                        "Indexer already running for profile "
                        f"{repo['profile_name']}. Wait for it to finish."
                    )
                    return RedirectResponse(
                        f"/repos?flash={quote_plus(flash)}",
                        status_code=303,
                    )
                subprocess.Popen(
                    [sys.executable, "-m", "src.indexer", "index-repo",
                     "--profile", repo["profile_name"]],
                    start_new_session=True,
                )
        except Exception as e:
            _logger.warning("Index trigger for repo %s failed: %s", repo_id, e)
        finally:
            conn.close()
    return RedirectResponse("/repos", status_code=303)
