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


def _count_embeddings(conn) -> int | None:
    """Return total row count from the embeddings table, or None on any error.

    Handles the case where pgvector is absent (table doesn't exist yet) or
    the connection is unavailable — returns None so the template can show
    'N/A' instead of crashing the dashboard.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM embeddings")
            row = cur.fetchone()
            return row[0] if row else 0
    except Exception:
        # Roll back the aborted transaction so subsequent queries on the same
        # connection are not poisoned (e.g. when pgvector is absent → table
        # doesn't exist → ProgrammingError → conn enters aborted-tx state).
        try:
            conn.rollback()
        except Exception:
            pass
        return None


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render dashboard with profiles + repos overview."""
    templates = request.app.state.templates

    profiles = []
    api_key_count = 0
    ssh_key_count = 0
    embeddings_total: int | None = None
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
            embeddings_total = _count_embeddings(conn)
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
            "embeddings_total": embeddings_total,
            "error": error,
        },
    )
