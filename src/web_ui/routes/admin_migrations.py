# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/admin_migrations.py
"""Admin migrations read-only display (M9 W-UO §3.8)."""
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.web_ui._json import _json_safe
from src.web_ui.auth import require_admin

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/migrations")
async def list_migrations(request: Request, _: int = Depends(require_admin)):
    """List applied yoyo migrations from _yoyo_migrations table.

    Returns list of dicts with: id (migration_id), applied_at (applied_at_utc).
    The yoyo backend uses _yoyo_migrations (plural) with columns
    migration_id (VARCHAR) and applied_at_utc (TIMESTAMP).
    Returns empty list when table exists but has no rows.
    Returns count=0 when table does not yet exist (pre-first-run).
    """
    from src.db.pg import get_pool

    pool = get_pool()
    try:
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                # Check table exists first — table may not exist on fresh installs
                # that have not yet run migrations.
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = '_yoyo_migrations' LIMIT 1"
                )
                table_exists = cur.fetchone() is not None

                if not table_exists:
                    return JSONResponse(_json_safe({
                        "ok": True,
                        "migrations": [],
                        "count": 0,
                    }))

                cur.execute(
                    "SELECT migration_id, applied_at_utc "
                    "FROM _yoyo_migrations ORDER BY applied_at_utc DESC"
                )
                rows = cur.fetchall()
    except Exception as exc:
        _logger.error("list_migrations DB error: %s", exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)

    return JSONResponse(_json_safe({
        "ok": True,
        "migrations": [
            {"id": r[0], "applied_at": r[1]}
            for r in rows
        ],
        "count": len(rows),
    }))
