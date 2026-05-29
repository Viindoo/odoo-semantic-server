# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/admin_plans.py
"""Plan catalogue route for Web UI admin (M10B P0-ext, W-3).

Routes
------
GET /api/admin/plans   List all plans (including non-public ones) ordered by id.

Auth
----
Requires require_admin Depends (raises 401/403 if not admin).

ADR-0039: plans table added in m13_006; unlimited sentinel added in m13_009.
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.web_ui._json import _json_safe
from src.web_ui.auth import require_admin

_logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/admin/plans")
async def list_plans_route(
    request: Request,
    _admin: int = Depends(require_admin),
):
    """Return all plans (including is_public=FALSE) ordered by id ASC.

    Admin endpoint — used by the admin UI to populate plan dropdowns,
    including the 'unlimited' sentinel plan that is not visible to users.

    Response 200:
        {plans: [{id, slug, display_name, quota_calls_per_month,
                  rate_limit_rpm, seat_limit, is_public}]}
    """
    try:
        from src.db.pg import get_pool
        pg_pool = get_pool()
        with pg_pool.checkout() as conn:
            rows = pg_pool.fetch_all(
                conn,
                "SELECT id, slug, display_name, quota_calls_per_month, "
                "rate_limit_rpm, seat_limit, is_public "
                "FROM plans ORDER BY id ASC",
            )
    except Exception as exc:
        _logger.error("list_plans DB error: %s", exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)

    plans = [
        {
            "id": row["id"],
            "slug": row["slug"],
            "display_name": row["display_name"],
            "quota_calls_per_month": row["quota_calls_per_month"],
            "rate_limit_rpm": row["rate_limit_rpm"],
            "seat_limit": row["seat_limit"],
            "is_public": bool(row["is_public"]),
        }
        for row in rows
    ]
    return JSONResponse(_json_safe({"plans": plans}))


# REGISTER: src/web_ui/app.py needs `app.include_router(router)` import — main agent will wire.
# (Out of W-3 ownership.)
