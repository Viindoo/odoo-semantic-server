# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Activation API — manual entitlement management (M10B P1, ADR-0039 D3).

Routes (admin-only, prefix /api/admin/entitlements):
    POST   /api/admin/entitlements                  grant a subscription
    POST   /api/admin/entitlements/{ref}/revoke     revoke a subscription
    PATCH  /api/admin/entitlements/{ref}            update plan/status/seats
    GET    /api/admin/entitlements                  list subscriptions (paginated)

Security:
    All routes require the ``require_admin`` dependency (HTTP 401/403).
    All mutating routes are wrapped with ``@audit_action`` (ADR-0021).
"""
import logging
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from psycopg2 import IntegrityError
from psycopg2.errors import CheckViolation
from pydantic import BaseModel, EmailStr, Field  # noqa: F401 (Field used in Pydantic models)
from starlette.requests import Request

from src.billing import activation
from src.billing.activation import EntitlementGrant
from src.db.audit import audit_action
from src.web_ui._json import _json_safe
from src.web_ui.auth import require_admin, require_admin_with_fresh_mfa

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/entitlements", tags=["admin-entitlements"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class GrantBody(BaseModel):
    """Request body for granting a subscription."""

    email: EmailStr
    plan_slug: str
    seats: int = Field(1, ge=1)
    source: Literal["admin", "promo"] = "admin"
    external_ref: str | None = None


# Valid subscriptions.status enum — SSOT is the subscriptions_status_check
# CHECK in migrations/m13_014_billing_p1.sql.  Constraining the request body to
# this Literal makes Pydantic return 422 on a bad value (I10) instead of letting
# it reach the DB CHECK and surface as an unhandled 500.
SubscriptionStatus = Literal[
    "pending", "active", "past_due", "cancelled", "expired", "trialing", "refunded"
]


class UpdateBody(BaseModel):
    """Request body for updating a subscription."""

    plan_slug: str | None = None
    status: SubscriptionStatus | None = None
    seats: int | None = Field(None, ge=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_plan_id(plan_slug: str) -> int:
    """Resolve a plan slug to its integer plans.id.

    Delegates the actual slug→id SQL to the shared, vendor-neutral
    ``src.billing._db.slug_to_plan_id`` helper (the SELECT lives in ONE place, no
    vendor-named module imported just to resolve a slug).  The helper raises
    ``ValueError`` when the slug is unknown; we translate that into HTTP 404 at
    the route boundary so the admin API still answers with a clean 404 instead
    of a 500.
    """
    from src.billing._db import slug_to_plan_id
    from src.db.pg import get_pool
    pool = get_pool()
    with pool.checkout() as conn:
        try:
            return slug_to_plan_id(plan_slug, conn)
        except ValueError as exc:
            raise HTTPException(404, f"Plan {plan_slug!r} not found") from exc


def _list_subscriptions(limit: int = 50, offset: int = 0) -> list[dict]:
    """Fetch subscription rows via subscription_store().list_all() (explicit column
    projection + LEFT JOIN plans for plan_slug/plan_name — no SELECT *)."""
    from src.db.pg import subscription_store
    return subscription_store().list_all(limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", status_code=200)
@audit_action("entitlement.grant")
async def grant(
    body: GrantBody,
    request: Request,
    admin: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Grant a subscription to a user by email.

    Resolves plan_slug → plan_id server-side.  Generates an ``admin-<uuid>``
    external_ref when none is supplied.  Calls :func:`activation.grant_entitlement`
    (idempotent on external_ref).

    Sets ``request.state.audit_target`` to the resulting subscription id so the
    ``@audit_action`` decorator captures it in the audit log.

    Raises 422 when a business-rule constraint is violated (e.g. team plan
    requires >= N seats, per :func:`activation._enforce_team_min_seats`) or when
    the DB CHECK / FK rejects the insert — matches the belt-and-suspenders pattern
    used by the ``update`` route (ADR-0039 I10).
    """
    plan_id = _resolve_plan_id(body.plan_slug)
    external_ref = body.external_ref or f"admin-{uuid4()}"

    grant_obj = EntitlementGrant(
        plan_id=plan_id,
        external_ref=external_ref,
        source=body.source,
        seats=body.seats,
        buyer_email=str(body.email),
    )
    try:
        sub_id = activation.grant_entitlement(grant_obj)
    except ValueError as exc:
        # Business-rule violation (e.g. team min-seats) — surface as 422, not 500.
        raise HTTPException(422, str(exc)) from exc
    except (CheckViolation, IntegrityError) as exc:
        logger.warning(
            "entitlement.grant: DB constraint violation plan_slug=%r email=%s — %s",
            body.plan_slug, body.email, exc,
        )
        raise HTTPException(422, f"Invalid grant: {exc}") from exc

    request.state.audit_target = str(sub_id)

    logger.info(
        "entitlement.grant: sub_id=%d plan_slug=%r email=%s external_ref=%r "
        "seats=%d source=%r admin_id=%d",
        sub_id, body.plan_slug, body.email, external_ref, body.seats, body.source, admin,
    )
    return {"subscription_id": sub_id, "external_ref": external_ref, "status": "active"}


@router.post("/{external_ref}/revoke", status_code=200)
@audit_action("entitlement.revoke", target_param="external_ref")
async def revoke(
    external_ref: str,
    request: Request,
    admin: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Revoke (cancel) a subscription by external_ref.

    Calls :func:`activation.revoke_entitlement` which marks the sub cancelled
    and downgrades the linked API key to the free plan (if claimed).
    Returns 404 if the external_ref is unknown.
    """
    from src.db.pg import subscription_store
    subs = subscription_store()
    sub = subs.get_by_external_ref(external_ref)
    if sub is None:
        raise HTTPException(404, f"Subscription {external_ref!r} not found")

    activation.revoke_entitlement(external_ref, reason="admin-revoke")
    logger.info(
        "entitlement.revoke: external_ref=%r sub_id=%d admin_id=%d",
        external_ref, sub["id"], admin,
    )
    return {"external_ref": external_ref, "status": "cancelled"}


@router.patch("/{external_ref}", status_code=200)
@audit_action("entitlement.update", target_param="external_ref")
async def update(
    external_ref: str,
    body: UpdateBody,
    request: Request,
    admin: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Update plan, status, or seats of an existing subscription.

    Resolves plan_slug → plan_id when provided.  If the plan changes on a
    claimed subscription, the linked API key is re-pointed and the middleware
    plan cache is flushed.

    Raises 404 if the external_ref is unknown; 400 if no fields are supplied.
    """
    if body.plan_slug is None and body.status is None and body.seats is None:
        raise HTTPException(400, "At least one of plan_slug, status, seats must be provided")

    plan_id: int | None = None
    if body.plan_slug is not None:
        plan_id = _resolve_plan_id(body.plan_slug)

    try:
        sub_id = activation.update_entitlement(
            external_ref,
            plan_id=plan_id,
            status=body.status,
            seats=body.seats,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (CheckViolation, IntegrityError) as exc:
        # Belt-and-suspenders (I10): the Literal on UpdateBody.status already
        # blocks bad enum values at the Pydantic layer, but any other CHECK /
        # FK violation that slips through must surface as a clean 422, never an
        # unhandled 500.
        logger.warning(
            "entitlement.update: DB constraint violation for external_ref=%r — %s",
            external_ref, exc,
        )
        raise HTTPException(422, f"Invalid update: {exc}") from exc

    logger.info(
        "entitlement.update: external_ref=%r sub_id=%d plan_id=%s status=%s "
        "seats=%s admin_id=%d",
        external_ref, sub_id, plan_id, body.status, body.seats, admin,
    )
    return {"subscription_id": sub_id, "external_ref": external_ref}


@router.get("", status_code=200)
async def list_subs(
    request: Request,
    admin: int = Depends(require_admin),
) -> dict:
    """List subscriptions, newest first. Paginated via limit/offset query params."""
    # Read limit and offset from query params with defaults + bounds
    try:
        limit_val = min(max(int(request.query_params.get("limit", 50)), 1), 500)
    except (ValueError, TypeError):
        limit_val = 50
    try:
        offset_val = max(int(request.query_params.get("offset", 0)), 0)
    except (ValueError, TypeError):
        offset_val = 0

    rows = _list_subscriptions(limit=limit_val, offset=offset_val)
    # I16: use the shared _json_safe so datetime AND Decimal/UUID/bytes (incl.
    # nested) are all serialised, not just top-level datetimes.
    return _json_safe(
        {"subscriptions": rows, "limit": limit_val, "offset": offset_val}
    )
