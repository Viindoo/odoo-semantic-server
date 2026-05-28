# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/account.py
"""Account self-service routes — tenant membership + usage quota (W2, M10B P0).

Route table:
  GET /api/account/tenants  - list tenants the current user belongs to
                              (admin: all tenants with role='admin';
                               non-admin: memberships only [{tenant_id, name, role}])
  GET /api/account/usage    - current API key plan + quota usage + 6-period history
                              (ADR-0039 control-plane API, WI-B3)
"""
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.web_ui._json import _json_safe
from src.web_ui.auth import ALL_TENANTS, current_user_id, resolve_tenant_scope_web

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/account")


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

    API-key resolution: primary key belonging to this user
    (SELECT ... WHERE user_id = %s LIMIT 1). If the user has no API key
    yet (new account), returns 200 with nulls (graceful empty).

    Response shape:
      {
        "plan": {
          "slug": "free-grandfathered",
          "name": "Free (Grandfathered)",
          "quota_calls_per_month": 1000,
          "rate_limit_rpm": 60
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
                    {"plan": None, "current_period": None, "history": []}
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
