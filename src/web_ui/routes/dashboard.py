# src/web_ui/routes/dashboard.py
"""Dashboard route — overview of profiles, repos, and system status (M8 W1 pure JSON)."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.web_ui._json import _json_safe

router = APIRouter(prefix="/api/dashboard")


def _count_embeddings() -> int | None:
    """Return total row count from the embeddings table, or None on any error.

    Handles the case where pgvector is absent (table doesn't exist yet) or
    the connection is unavailable — returns None so the response can show
    null instead of crashing the dashboard.
    """
    try:
        from src.db.pg import get_pool

        with get_pool().checkout() as conn:
            row = get_pool().fetch_one(conn, "SELECT COUNT(*) FROM embeddings")
            return row["count"] if row else 0
    except Exception:
        return None


@router.get("/stats")
async def dashboard_stats(request: Request):
    """Return dashboard stats as JSON: profiles, repo counts, api_key_count, ssh_key_count."""
    profiles = []
    api_key_count = 0
    ssh_key_count = 0
    embeddings_total: int | None = None
    error = None

    try:
        from src.db.pg import auth_store, repo_store

        profiles_raw = repo_store().list_profiles()
        for p in profiles_raw:
            repos = repo_store().get_repos_for_profile(p["name"])
            profiles.append({**p, "repos": repos})
        api_key_count = len(auth_store().list_api_keys())
        ssh_key_count = len(auth_store().list_ssh_keys())
        embeddings_total = _count_embeddings()
    except Exception as e:
        error = str(e)

    return JSONResponse(_json_safe({
        "profiles": profiles,
        "api_key_count": api_key_count,
        "ssh_key_count": ssh_key_count,
        "embeddings_total": embeddings_total,
        "error": error,
    }))
