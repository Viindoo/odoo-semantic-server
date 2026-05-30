# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public pricing-page plan list — GET /api/plans (no auth required).

Returns the public-facing plan tiers (is_public=TRUE AND is_archived=FALSE)
including commercial pricing columns (price_cents, currency, billing_interval)
added in migration m13_014.

The response shape is ``{"plans": [...]}`` — consistent with the admin plan
catalogue at GET /api/admin/plans so frontend components can share a renderer.
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
    """
    from src.db.pg import get_pool
    pool = get_pool()
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, slug, display_name,
                       quota_calls_per_month, rate_limit_rpm, seat_limit,
                       price_cents, currency, billing_interval, prices
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
        }
        for r in rows
    ]
    return JSONResponse(_json_safe({"plans": plans}))
