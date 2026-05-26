# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/account.py
"""Account self-service routes — tenant membership query (W2, ADR-0038).

Route table:
  GET /api/account/tenants  - list tenants the current user belongs to
                              (admin: all tenants with role='admin';
                               non-admin: memberships only [{tenant_id, name, role}])
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
