# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public pricing-page plan list — GET /api/plans (no auth required).

Returns the public-facing plan tiers (is_public=TRUE AND is_archived=FALSE)
including commercial pricing columns (price_cents, currency, billing_interval)
added in migration m13_014, pricing_model added in m13_015, and per-plan
min_seats added in m13_016.

Response shape (WI-1 — pricing UX overhaul):
    {
        "plans": [
            {
                ...
                "pricing_model": "flat" | "per_seat",
                "min_seats": <int | null>   # per-plan display SSOT (m13_016)
            }
        ],
        "team_min_seats": <int>   # global setting kept for backward-compat
    }

``min_seats`` per plan is the display SSOT for the pricing page sub-label
("min. N seats — from $X/mo").  It is null for flat plans and for per-seat
plans with no enforced minimum (Pro).  The pricing page uses this column
directly; the ``team_min_seats`` top-level field is kept for backward-compat
but pricing.astro now reads per-plan ``min_seats`` instead of the global.

``billing.team_min_seats`` (the setting) is the enforcement SSOT used at
checkout in activation.py — it is NOT this column.  The two are kept in sync
manually (m13_016 seed sets team.min_seats=3 = catalogue default of the
billing setting).
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
                       pricing_model, min_seats
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
            "min_seats": r[11],      # per-plan display SSOT; null = no minimum (m13_016)
        }
        for r in rows
    ]
    # Read team_min_seats once — kept for backward-compat (enforcement setting in activation.py).
    # Pricing page now reads per-plan min_seats instead of this top-level field.
    # get_setting falls back to the catalogue default (3) if DB unavailable.
    try:
        team_min_seats = int(get_setting("billing.team_min_seats"))
    except Exception:
        team_min_seats = 3  # safe fallback matching catalogue default

    return JSONResponse(_json_safe({"plans": plans, "team_min_seats": team_min_seats}))
