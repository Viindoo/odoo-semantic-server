# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/account.py
"""Account self-service routes — tenant membership + usage quota (W2, M10B P0).

Route table:
  GET  /api/account/tenants            - list tenants the current user belongs to
                                         (admin: all tenants with role='admin';
                                          non-admin: memberships only
                                          [{tenant_id, name, role}])
  GET  /api/account/usage              - current API key plan + quota usage +
                                         6-period history (ADR-0039 control-plane
                                         API, WI-B3)
  GET  /api/account/subscription       - the user's subscriptions + renewal date +
                                         cancel state + Polar manage URL (M10B P1)
  POST /api/account/subscription/cancel - self-service cancel-at-period-end via the
                                         Polar API (no refund, access to period end)
"""
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.db.audit import audit_action
from src.settings import get_setting
from src.web_ui._json import _json_safe
from src.web_ui.auth import ALL_TENANTS, current_user_id, resolve_tenant_scope_web

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/account")


def _polar_portal_url() -> str:
    """Return the admin-configured Polar customer-portal URL (manage/cancel link)."""
    return get_setting("billing.polar_portal_url") or "https://polar.sh/"


@router.get("/tenants")
async def list_my_tenants(request: Request):
    """Return the tenant memberships visible to the current user.

    - Admin: returns all tenants (full list from tenants table), role='admin'.
    - Non-admin: returns [{tenant_id, name, role}] from tenant_members JOIN tenants.
    - Unauthenticated: 401.

    Used by the portal header to show which org(s) the user belongs to,
    and by the repos portal to offer tenant-scoped profile selection.
    """
    uid = current_user_id(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    scope = resolve_tenant_scope_web(request)

    try:
        from src.db.pg import auth_store

        if scope is ALL_TENANTS:
            # Admin: return all tenants with synthesised role='admin'
            tenants_raw = auth_store().list_tenants()
            result = [
                {
                    "tenant_id": t["id"],
                    "name": t["name"],
                    "role": "admin",
                }
                for t in tenants_raw
            ]
        else:
            # Non-admin: own memberships only (name included via JOIN)
            result = auth_store().list_tenant_memberships_for_user(uid)
    except Exception as e:
        _logger.warning("list_my_tenants failed for user %d: %s", uid, e)
        return JSONResponse(_json_safe({"error": str(e)}), status_code=500)

    return JSONResponse(_json_safe({"tenants": result}))


@router.get("/usage")
async def get_account_usage(request: Request):
    """Return current API key's plan + quota usage + last 6 months of history.

    Auth: requires authenticated user session (cookie). 401 if absent.
    Auth check uses current_user_id (not is_admin — per ADR-0026 / ADR-0039):
    this endpoint is open to any logged-in user, not just admins.

    API-key resolution (M10B P0 limitation — Wave 2 integration review
    ISSUE-3): the "primary" key for a multi-key user is defined as the
    OLDEST active key (``ORDER BY k.id ASC LIMIT 1``). Users with more
    than one API key see usage for that single primary key only — the
    portal surfaces an explicit hint so this is not silent. Multi-key
    aggregation is deferred to M10B P1 (per-key breakdown + selector).
    If the user has no API key yet (new account), returns 200 with
    nulls (graceful empty).

    Response shape:
      {
        "plan": {
          "slug": "free",
          "name": "Free",
          "quota_calls_per_month": 100,
          "rate_limit_rpm": 30
        },
        "current_period": {
          "yyyymm": "202605",
          "used": 87,
          "remaining": 913,
          "percent": 8.7        -- null when quota_calls_per_month == 0 (unlimited)
        },
        "history": [
          {"period": "202605", "used": 87},
          ...up to 6 periods, ordered DESC
        ]
      }
    """
    uid = current_user_id(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        from src.db.pg import get_pool

        pool = get_pool()

        with pool.checkout() as conn:
            # 1. Resolve the primary API key for this user.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT k.id, p.slug, p.display_name,"
                    "       p.quota_calls_per_month, p.rate_limit_rpm"
                    "  FROM api_keys k"
                    "  JOIN plans p ON p.id = k.plan_id"
                    " WHERE k.user_id = %s"
                    " ORDER BY k.id ASC LIMIT 1",
                    (uid,),
                )
                row = cur.fetchone()

            if row is None:
                # User has no API key yet — graceful empty.
                return JSONResponse(
                    _json_safe({"plan": None, "current_period": None, "history": []})
                )

            key_id, slug, display_name, quota, rate_limit_rpm = row

            # 2. Current period (UTC).
            with conn.cursor() as cur:
                cur.execute("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYYMM')")
                current_yyyymm: str = cur.fetchone()[0]

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT call_count FROM usage_counter"
                    " WHERE api_key_id = %s AND period_yyyymm = %s",
                    (key_id, current_yyyymm),
                )
                uc_row = cur.fetchone()
            used: int = uc_row[0] if uc_row else 0

            if quota == 0:
                # quota_calls_per_month == 0 means unlimited (admin-only plan).
                remaining: int | None = None
                percent: float | None = None
            else:
                remaining = max(0, quota - used)
                percent = round(used / quota * 100, 1)

            current_period = {
                "yyyymm": current_yyyymm,
                "used": used,
                "remaining": remaining,
                "percent": percent,
            }

            # 3. Last 6 periods history (DESC).
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT period_yyyymm, call_count"
                    "  FROM usage_counter"
                    " WHERE api_key_id = %s"
                    " ORDER BY period_yyyymm DESC"
                    " LIMIT 6",
                    (key_id,),
                )
                history = [{"period": r[0], "used": r[1]} for r in cur.fetchall()]

        return JSONResponse(
            _json_safe(
                {
                    "plan": {
                        "slug": slug,
                        "name": display_name,
                        "quota_calls_per_month": quota,
                        "rate_limit_rpm": rate_limit_rpm,
                    },
                    "current_period": current_period,
                    "history": history,
                }
            )
        )

    except HTTPException:
        raise
    except Exception as exc:
        _logger.warning("get_account_usage failed for user %d: %s", uid, exc)
        return JSONResponse(
            _json_safe({"error": str(exc)}), status_code=500
        )


@router.get("/subscription")
async def get_my_subscription(request: Request):
    """Return the current user's subscriptions + the Polar manage/cancel URL.

    Auth: requires an authenticated user session (cookie). 401 if absent — open
    to any logged-in user (current_user_id, not is_admin; ADR-0026 / ADR-0039).

    Each subscription includes the human-readable plan name/slug (LEFT JOINed in
    ``list_by_user``), the renewal date (``current_period_end``), seats, billing
    interval, amount/currency, and the cancel state (``cancel_at_period_end`` +
    ``cancelled_at``) so the billing dashboard can show "Cancels on {date}".

    Response:
      {
        "subscriptions": [ {id, plan_id, plan_slug, plan_name, status, seats,
                            billing_interval, current_period_start,
                            current_period_end, trial_ends_at,
                            cancel_at_period_end, cancelled_at, amount_cents,
                            currency, source}, ... ],
        "manage_url": "<billing.polar_portal_url>"
      }
    """
    uid = current_user_id(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        from src.db.pg import subscription_store

        rows = subscription_store().list_by_user(uid)
        subscriptions = [
            {
                "id": s["id"],
                "plan_id": s["plan_id"],
                "plan_slug": s.get("plan_slug"),
                "plan_name": s.get("plan_name"),
                "status": s["status"],
                "seats": s["seats"],
                "billing_interval": s["billing_interval"],
                "current_period_start": s["current_period_start"],
                "current_period_end": s["current_period_end"],
                "trial_ends_at": s["trial_ends_at"],
                "cancel_at_period_end": s["cancel_at_period_end"],
                "cancelled_at": s["cancelled_at"],
                "amount_cents": s["amount_cents"],
                "currency": s["currency"],
                "source": s["source"],
            }
            for s in rows
        ]
    except HTTPException:
        raise
    except Exception as exc:
        _logger.warning("get_my_subscription failed for user %d: %s", uid, exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)

    return JSONResponse(
        _json_safe(
            {"subscriptions": subscriptions, "manage_url": _polar_portal_url()}
        )
    )


@router.post("/subscription/cancel")
@audit_action("account.subscription.cancel")
async def cancel_my_subscription(request: Request):
    """Self-service cancel-at-period-end for the user's active subscription.

    Owner decision (overrides plan B4): this CALLS the Polar API so the cancel is
    authoritative at the seller-of-record.  Policy: no refund; the user keeps
    access until ``current_period_end`` (owner decision #1).

    Flow (fail-closed — money logic):
      1. 401 if not authenticated.
      2. Find the user's active, not-yet-cancelling sub (status='active' AND NOT
         cancel_at_period_end).  404 if none.
      3. ``await polar_api.cancel_subscription(external_ref, at_period_end=True)``.
         - PolarApiNotConfigured → 503 + the portal link (user can cancel there).
         - PolarApiError         → 502 + the portal link as fallback.
         In BOTH failure cases the LOCAL schedule flag is NOT set — we never tell
         a paying user "cancelled" while Polar would still charge them.
      4. On Polar success → ``schedule_cancellation`` flips the local flag for
         instant UI feedback; the eventual period-end webhook performs the real
         downgrade.

    Returns:
      200 {"status": "cancellation_scheduled", "access_until": <iso|None>,
           "manage_url": <portal>}
    """
    uid = current_user_id(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from src.billing import activation, polar_api
    from src.db.pg import subscription_store

    subs = subscription_store()
    rows = subs.list_by_user(uid)
    active = next(
        (
            s
            for s in rows
            if s["status"] == "active" and not s["cancel_at_period_end"]
        ),
        None,
    )
    if active is None:
        return JSONResponse(
            _json_safe({"error": "no_active_subscription"}), status_code=404
        )

    external_ref = active["external_ref"]
    portal = _polar_portal_url()

    # CR8: set audit_target BEFORE any side-effect so partial failures are logged
    # with the correct subscription id in the audit log.
    request.state.audit_target = str(active["id"])

    try:
        await polar_api.cancel_subscription(external_ref, at_period_end=True)
    except polar_api.PolarApiNotConfigured:
        _logger.error(
            "cancel_my_subscription: POLAR_API_KEY not configured; cannot cancel "
            "sub_id=%s for user_id=%d (local flag NOT set)",
            active["id"], uid,
        )
        return JSONResponse(
            _json_safe(
                {
                    "error": "cancel_unavailable",
                    "detail": (
                        "Online cancellation is temporarily unavailable. Please "
                        "cancel from the customer portal."
                    ),
                    "manage_url": portal,
                }
            ),
            status_code=503,
        )
    except polar_api.PolarApiError as exc:
        _logger.error(
            "cancel_my_subscription: Polar cancel failed for sub_id=%s "
            "user_id=%d (status=%s); local flag NOT set: %s",
            active["id"], uid, exc.status_code, exc,
        )
        return JSONResponse(
            _json_safe(
                {
                    "error": "cancel_failed_upstream",
                    "detail": (
                        "We could not complete the cancellation with our payment "
                        "provider. Please try the customer portal."
                    ),
                    "manage_url": portal,
                }
            ),
            status_code=502,
        )

    # Polar confirmed the cancel → use revoke_entitlement(voluntary=True) as the
    # sole-writer path (CR5 / ADR-0039).  voluntary=True schedules cancel-at-period-end
    # so the user keeps access until current_period_end (owner decision #1).
    # This is equivalent to subs.schedule_cancellation() but goes through the
    # canonical entitlement write-path (activation layer) for consistency.
    activation.revoke_entitlement(external_ref, reason="user-cancel", voluntary=True)
    _logger.info(
        "cancel_my_subscription: sub_id=%s scheduled cancel-at-period-end for "
        "user_id=%d (Polar confirmed)",
        active["id"], uid,
    )

    return JSONResponse(
        _json_safe(
            {
                "status": "cancellation_scheduled",
                "access_until": active["current_period_end"],
                "manage_url": portal,
            }
        )
    )
