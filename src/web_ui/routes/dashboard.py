# src/web_ui/routes/dashboard.py
"""Dashboard route — overview of profiles, repos, and system status."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


def _get_db_conn():
    """Get PostgreSQL connection for Web UI queries."""
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


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render dashboard with profiles + repos overview."""
    templates = request.app.state.templates

    profiles = []
    api_key_count = 0
    ssh_key_count = 0
    error = None

    conn = _get_db_conn()
    if conn:
        try:
            from src.db.auth_registry import list_api_keys, list_ssh_keys
            from src.db.repo_registry import get_repos_for_profile, list_profiles

            profiles_raw = list_profiles(conn)
            for p in profiles_raw:
                repos = get_repos_for_profile(conn, p["name"])
                profiles.append({**p, "repos": repos})
            api_key_count = len(list_api_keys(conn))
            ssh_key_count = len(list_ssh_keys(conn))
        except Exception as e:
            error = str(e)
        finally:
            conn.close()
    else:
        error = "Cannot connect to PostgreSQL. Check PG_DSN configuration."

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "profiles": profiles,
            "api_key_count": api_key_count,
            "ssh_key_count": ssh_key_count,
            "error": error,
        },
    )
