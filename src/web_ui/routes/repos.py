# src/web_ui/routes/repos.py
"""Profiles & Repos management routes."""
import subprocess
import sys
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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
        request, "repos.html", {"profiles": profiles, "error": error, "flash": None}
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
        except Exception:
            pass
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
        except Exception:
            pass
        finally:
            conn.close()
    return RedirectResponse("/repos", status_code=303)


@router.post("/repos/repos/{repo_id}/index", response_class=RedirectResponse)
async def index_repo(request: Request, repo_id: int):
    """Trigger indexer as a non-blocking subprocess."""
    try:
        subprocess.Popen(
            [sys.executable, "-m", "src.indexer", "--all"],
            start_new_session=True,
        )
    except Exception:
        pass
    return RedirectResponse("/repos", status_code=303)
