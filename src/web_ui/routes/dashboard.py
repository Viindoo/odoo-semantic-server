# src/web_ui/routes/dashboard.py
"""Dashboard route — overview of profiles, repos, and system status."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


def _count_embeddings() -> int | None:
    """Return total row count from the embeddings table, or None on any error.

    Handles the case where pgvector is absent (table doesn't exist yet) or
    the connection is unavailable — returns None so the template can show
    'N/A' instead of crashing the dashboard.
    """
    try:
        from src.db.pg import get_pool

        with get_pool().checkout() as conn:
            row = get_pool().fetch_one(conn, "SELECT COUNT(*) FROM embeddings")
            return row["count"] if row else 0
    except Exception:
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
