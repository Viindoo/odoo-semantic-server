# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public pricing-page plan list — GET /api/plans (no auth required).

Returns the public-facing plan tiers (is_public=TRUE AND is_archived=FALSE)
including commercial pricing columns (price_cents, currency, billing_interval)
added in migration m13_014 and pricing_model added in m13_015.

Response shape (WI-1 — pricing UX overhaul):
    {
        "plans": [...],
        "team_min_seats": <int>   # global setting, not a per-plan column
    }

``team_min_seats`` is read once from the settings overlay
(``billing.team_min_seats``, default 3) and placed at the top level of the
response so the pricing page can render "min. N seats" copy without a
second API call.  Frontend consumers (WI-5 pricing.astro) must read
``data.team_min_seats`` from this response — NOT from ``data.plans[i]``.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.web_ui._json import _json_safe

logger = logging.getLogger(__name__)
router = APIRouter(tags=["plans"])


@router.get("/api/plans")
async def list_public_plans() -> dict:
    """Return public, non-archived plan tiers for the pricing page.

    No authentication required — this data is public (pricing page, landing
    page tier comparison table).  Only plans with ``is_public=TRUE`` AND
    ``is_archived=FALSE`` are returned.  Ordered by price_cents ascending so
    the free tier appears first.

    Response includes ``pricing_model`` (flat | per_seat) per plan and a
    top-level ``team_min_seats`` integer read from the settings overlay
    (``billing.team_min_seats``, default 3).
    """
    from src.db.pg import get_pool
    from src.settings import get_setting
    pool = get_pool()
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, slug, display_name,
                       quota_calls_per_month, rate_limit_rpm, seat_limit,
                       price_cents, currency, billing_interval, prices,
                       pricing_model
                FROM plans
                WHERE is_public = TRUE AND is_archived = FALSE
                ORDER BY price_cents ASC, id ASC
                """
            )
            rows = cur.fetchall()

    plans = [
        {
            "id": r[0],
            "slug": r[1],
            "display_name": r[2],
            "quota_calls_per_month": r[3],
            "rate_limit_rpm": r[4],
            "seat_limit": r[5],
            "price_cents": r[6],
            "currency": r[7],
            "billing_interval": r[8],
            "prices": r[9],  # per-currency map e.g. {"USD": 1900} (multi-currency deferred)
            "pricing_model": r[10],  # "flat" or "per_seat" (m13_015)
        }
        for r in rows
    ]
    # Read team_min_seats once — it is a global setting, not a per-plan column.
    # get_setting falls back to the catalogue default (3) if DB unavailable.
    try:
        team_min_seats = int(get_setting("billing.team_min_seats"))
    except Exception:
        team_min_seats = 3  # safe fallback matching catalogue default

    return JSONResponse(_json_safe({"plans": plans, "team_min_seats": team_min_seats}))
