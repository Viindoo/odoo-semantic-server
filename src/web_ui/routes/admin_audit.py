# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/admin_audit.py
"""Admin audit log viewer route (W3 C).

Routes
------
GET /api/admin/audit-log   list audit entries (admin only, filterable, paginated)

Auth
----
All routes require require_admin Depends (raises 401/403 if not admin).
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.web_ui._json import _json_safe
from src.web_ui.auth import require_admin

_logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


@router.get("/api/admin/audit-log")
async def list_audit_log(
    request: Request,
    action: str = "",
    actor: str = "",
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
    _admin: int = Depends(require_admin),
):
    """Return admin_audit_log entries (admin only, paginated).

    Query params:
      action  (str)  filter by exact action name substring match (ILIKE %action%)
      actor   (str)  filter by actor prefix (ILIKE actor%)
      limit   (int)  max rows (default 50, capped at 200)
      offset  (int)  pagination offset (default 0)

    Response:
      {
        "entries": [{"id", "ts", "actor", "action", "target", "success", "detail"}],
        "total": int
      }
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)

    try:
        from src.db.pg import get_pool

        conditions = []
        params: list = []

        if action:
            conditions.append("action ILIKE %s")
            params.append(f"%{action}%")
        if actor:
            conditions.append("actor ILIKE %s")
            params.append(f"{actor}%")

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with get_pool().checkout() as conn:
            with conn.cursor() as cur:
                # Count total matching rows (for pagination UI)
                cur.execute(
                    f"SELECT COUNT(*) FROM admin_audit_log {where_clause}",
                    params,
                )
                total: int = cur.fetchone()[0]

                # Fetch page
                cur.execute(
                    f"SELECT id, created_at, actor, action, target, success, detail"
                    f" FROM admin_audit_log {where_clause}"
                    f" ORDER BY id DESC LIMIT %s OFFSET %s",
                    [*params, limit, offset],
                )
                rows = cur.fetchall()

    except Exception as exc:
        _logger.error("list_audit_log DB error: %s", exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)

    entries = []
    for row in rows:
        row_id, created_at, row_actor, row_action, target, success, detail = row
        entries.append({
            "id": row_id,
            "ts": str(created_at) if created_at else None,
            "actor": row_actor,
            "action": row_action,
            "target": target,
            "success": bool(success) if success is not None else None,
            "detail": detail,
        })

    return JSONResponse(_json_safe({"entries": entries, "total": total}))
