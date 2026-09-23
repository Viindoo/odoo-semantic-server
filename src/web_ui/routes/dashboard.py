# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/dashboard.py
"""Dashboard route — overview of profiles, repos, and system status (M8 W1 pure JSON)."""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.web_ui._json import _json_safe

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard")


def _count_embeddings(profile_names: list[str] | None = None) -> int | None:
    """Return the embeddings row count, or None on any error.

    ``profile_names`` None = the global total (admin); a list = only the rows
    of those profiles (a tenant member's visible profiles; empty -> 0).

    Handles the case where pgvector is absent (table doesn't exist yet) or
    the connection is unavailable — returns None so the response can show
    null instead of crashing the dashboard.

    RLS GAP2 (ADR-0034): the web UI keeps the owner DSN, so this COUNT(*)
    intentionally bypasses RLS (the owner is exempt even under FORCE) and
    returns the true global total for an admin; a non-admin gets the explicit
    ``profile_name`` filter instead (the owner bypasses RLS, so the filter is
    the tenant boundary here). Do NOT flip the web UI to the osm_reader read
    role; only the MCP :8002 read tier moves to the non-owner role. (The MCP
    /health count wraps in _rls_read_tx(conn, None) instead.)
    """
    if profile_names is not None and not profile_names:
        return 0
    try:
        from src.db.pg import get_pool

        with get_pool().checkout() as conn:
            if profile_names is None:
                row = get_pool().fetch_one(conn, "SELECT COUNT(*) FROM embeddings")
            else:
                row = get_pool().fetch_one(
                    conn,
                    "SELECT COUNT(*) FROM embeddings WHERE profile_name = ANY(%s)",
                    (list(profile_names),),
                )
            return row["count"] if row else 0
    except Exception:
        return None


@router.get("/stats")
async def dashboard_stats(request: Request):
    """Return dashboard stats as JSON: profiles, repo counts, api_key_count, ssh_key_count.

    Any signed-in user reaches this route (AuthRequiredMiddleware), so the
    profile list is tenant-scoped exactly like ``/api/repos/profiles`` (admin
    sees all; a tenant member sees its own and shared profiles) and repo error
    text is redacted for non-admins (``repos._redact_repo_rows``). The counts
    follow the same scoping as the lists they summarise: a non-admin gets
    ``api_key_count`` of its own keys (as ``GET /api/api-keys`` lists them),
    ``embeddings_total`` of the listed profiles only; ``ssh_key_count`` is the
    shared admin access keys, which ``/api/repos/ssh-keys-list`` shows every
    signed-in user. An admin gets global counts. ``is_admin`` is DB-sourced
    via ``resolve_read_scope``.
    """
    from src.web_ui.auth import current_user_id, is_in_scope, resolve_read_scope
    from src.web_ui.routes import repos as repos_module

    is_admin, scope = resolve_read_scope(request)
    profiles = []
    api_key_count = 0
    ssh_key_count = 0
    embeddings_total: int | None = None
    error = None

    try:
        from src.db.pg import auth_store, repo_store

        profiles_raw = repo_store().list_profiles()
        for p in profiles_raw:
            if not is_in_scope(scope, p.get("tenant_id")):
                continue
            repos = repo_store().get_repos_for_profile(p["name"])
            repos_module._redact_repo_rows(repos, is_admin=is_admin)
            profiles.append({**p, "repos": repos})
        if is_admin:
            api_key_count = len(auth_store().list_api_keys(admin=True))
            embeddings_total = _count_embeddings()
        else:
            # list_api_keys(user_id=None) means "all keys": never for a non-admin.
            uid = current_user_id(request)
            api_key_count = (
                len(auth_store().list_api_keys(user_id=uid, admin=False))
                if uid is not None else 0
            )
            embeddings_total = _count_embeddings([p["name"] for p in profiles])
        ssh_key_count = len(auth_store().list_ssh_keys())
    except Exception as e:
        _logger.warning("%s failed: %s", request.url.path, e)
        # Raw exception text can name hosts/paths: admin only.
        error = str(e) if is_admin else "Internal error loading repositories."

    return JSONResponse(_json_safe({
        "profiles": profiles,
        "api_key_count": api_key_count,
        "ssh_key_count": ssh_key_count,
        "embeddings_total": embeddings_total,
        "error": error,
    }))
