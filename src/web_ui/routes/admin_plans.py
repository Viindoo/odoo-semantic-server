# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin plan tier CRUD (ADR-0039 + ADR-0042).

Routes (admin-only):
- GET   /api/admin/plans               list all tiers (wrapped: {"plans": [...]})
- GET   /api/admin/plans/{slug}        single tier detail
- PATCH /api/admin/plans/{slug}        update quota_calls_per_month + rate_limit_rpm
                                        (require_admin_with_fresh_mfa, audit)
- POST  /api/admin/plans                CREATE NEW TIER — deferred Phase 2, returns 501

The list route returns a ``{"plans": [...]}`` wrapper (NOT a bare array) to
preserve the contract consumed by the admin UI shipped in M10B P0-ext (#206):
``site/src/pages/admin/api-keys.astro`` and ``admin/users.astro`` read
``data.plans``.  The settings plan-tier editor (``admin/settings/plans.astro``)
unwraps the same key.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.requests import Request

from src.db.audit import audit_action
from src.web_ui.auth import require_admin, require_admin_with_fresh_mfa
from src.web_ui.routes._admin_helpers import invalidate_plan_cache

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/plans", tags=["admin-plans"])


class PlanPatch(BaseModel):
    """Update payload — pricing + quota + rpm + seat_limit + display_name editable Phase 1."""
    quota_calls_per_month: int | None = Field(None, ge=0, le=1_000_000_000)
    rate_limit_rpm: int | None = Field(None, ge=1, le=100_000)
    seat_limit: int | None = Field(None, ge=1, le=10_000)
    display_name: str | None = Field(None, min_length=1, max_length=100)
    is_public: bool | None = None
    metadata: dict | None = None
    # Pricing fields (C1 — ADR-0039): all editable at runtime via admin PATCH.
    price_cents: int | None = Field(None, ge=0, le=100_000_000)
    currency: str | None = Field(None, min_length=3, max_length=3)   # ISO-4217
    billing_interval: str | None = Field(None, pattern=r"^(free|monthly|annual|one_time)$")
    trial_days: int | None = Field(None, ge=0, le=365)
    prices: dict | None = None  # per-currency map e.g. {"USD": 1900, "VND": 490000}
    is_archived: bool | None = None
    reason: str = Field(min_length=3, max_length=500)


def _get_pool():
    from src.db.pg import get_pool
    return get_pool()


@router.get("")
async def list_plans(actor_id: int = Depends(require_admin)) -> dict:
    """Return all plan tiers ordered by quota ascending, wrapped as ``{"plans": [...]}``.

    Includes is_public=FALSE tiers (e.g. the 'unlimited' sentinel) so the admin UI
    can render every assignable plan.  The ``{"plans": [...]}`` wrapper shape is
    required by the admin UI shipped in #206 (api-keys.astro / users.astro read
    ``data.plans``); the settings plan-tier editor unwraps the same key.
    """
    pool = _get_pool()
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, slug, display_name, quota_calls_per_month, rate_limit_rpm, "
                "seat_limit, is_public, metadata, created_at, "
                "price_cents, currency, billing_interval, trial_days, is_archived, prices "
                "FROM plans "
                "ORDER BY quota_calls_per_month ASC, id ASC"
            )
            plans = [
                {
                    "id": r[0], "slug": r[1], "display_name": r[2],
                    "quota_calls_per_month": r[3], "rate_limit_rpm": r[4],
                    "seat_limit": r[5], "is_public": r[6], "metadata": r[7],
                    "created_at": r[8].isoformat() if r[8] else None,
                    "price_cents": r[9], "currency": r[10],
                    "billing_interval": r[11], "trial_days": r[12],
                    "is_archived": r[13], "prices": r[14],
                }
                for r in cur.fetchall()
            ]
    return {"plans": plans}


@router.get("/{slug}")
async def get_plan(slug: str, actor_id: int = Depends(require_admin)) -> dict:
    """Return a single plan tier by slug."""
    pool = _get_pool()
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, slug, display_name, quota_calls_per_month, rate_limit_rpm, "
                "seat_limit, is_public, metadata, created_at, "
                "price_cents, currency, billing_interval, trial_days, is_archived, prices "
                "FROM plans WHERE slug = %s",
                (slug,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"Plan {slug!r} not found")
    return {
        "id": row[0], "slug": row[1], "display_name": row[2],
        "quota_calls_per_month": row[3], "rate_limit_rpm": row[4],
        "seat_limit": row[5], "is_public": row[6], "metadata": row[7],
        "created_at": row[8].isoformat() if row[8] else None,
        "price_cents": row[9], "currency": row[10],
        "billing_interval": row[11], "trial_days": row[12],
        "is_archived": row[13], "prices": row[14],
    }


# WI-RV F-B: shared :func:`invalidate_plan_cache` consolidated in
# ``src/web_ui/routes/_admin_helpers.py``.  The previous in-line helper here
# was correct (uppercase ``_PLAN_CACHE``) but its siblings in
# admin_settings.py / tenant_settings.py used lowercase ``_plan_cache`` and
# silently swallowed AttributeError — fixed by routing all three call sites
# through the same module.
_invalidate_plan_cache = invalidate_plan_cache


@router.patch("/{slug}")
@audit_action("plan.update", target_param="slug")
async def update_plan(
    slug: str,
    payload: PlanPatch,
    request: Request,
    actor_id: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Update plan tier fields.

    Requires fresh MFA (5-min window). Drops _PLAN_CACHE after commit.
    A >50% reduction in quota or RPM is logged at WARNING level — the
    UI should also prompt a confirmation dialog client-side before submitting.
    """
    updates = payload.model_dump(exclude_none=True, exclude={"reason"})
    if not updates:
        raise HTTPException(400, "No fields to update")

    pool = _get_pool()
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = %s", (slug,))
            if cur.fetchone() is None:
                raise HTTPException(404, f"Plan {slug!r} not found")

            # Warn on >50% quota/RPM drop (backend telemetry; UI confirms separately)
            if "quota_calls_per_month" in updates or "rate_limit_rpm" in updates:
                cur.execute(
                    "SELECT quota_calls_per_month, rate_limit_rpm FROM plans WHERE slug = %s",
                    (slug,),
                )
                old_q, old_r = cur.fetchone()
                new_q = updates.get("quota_calls_per_month", old_q)
                new_r = updates.get("rate_limit_rpm", old_r)
                if old_q > 0 and new_q < old_q * 0.5:
                    log.warning(
                        "Plan %s quota dropped >50%% (%d -> %d) by user %d (%s)",
                        slug, old_q, new_q, actor_id, payload.reason,
                    )
                if old_r > 0 and new_r < old_r * 0.5:
                    log.warning(
                        "Plan %s rpm dropped >50%% (%d -> %d) by user %d (%s)",
                        slug, old_r, new_r, actor_id, payload.reason,
                    )

            set_clauses = []
            values = []
            for col, val in updates.items():
                set_clauses.append(f"{col} = %s")
                # JSON-encode JSONB columns (metadata, prices) before sending to psycopg2.
                values.append(json.dumps(val) if col in ("metadata", "prices") else val)
            values.append(slug)
            conn.autocommit = False
            cur.execute(
                f"UPDATE plans SET {', '.join(set_clauses)} WHERE slug = %s "
                f"RETURNING id, slug, display_name, quota_calls_per_month, rate_limit_rpm",
                tuple(values),
            )
            row = cur.fetchone()
        conn.commit()
        conn.autocommit = True

    _invalidate_plan_cache()
    return {
        "id": row[0], "slug": row[1], "display_name": row[2],
        "quota_calls_per_month": row[3], "rate_limit_rpm": row[4],
        "propagation_eta_seconds": 60,
    }


@router.post("")
@audit_action("plan.create_attempt", target_param=None)
async def create_plan_not_implemented(
    request: Request,
    actor_id: int = Depends(require_admin),
):
    """Plan tier creation is deferred to Phase 2.

    Decorated with :func:`audit_action` per ADR-0021: every mutating
    admin-gated route MUST emit an audit log entry — even when the
    handler immediately rejects with 501.  The decorator records the
    attempt (actor + ip + user-agent) before re-raising the HTTPException,
    so an admin probing for plan-create endpoints is captured in the
    audit trail.  When Phase 2 lands, swap the action name to ``plan.create``.
    """
    raise HTTPException(501, "Plan tier creation deferred to Phase 2")
