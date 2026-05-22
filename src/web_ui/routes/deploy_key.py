# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/deploy_key.py
"""Tenant self-service deploy-key endpoint (ADR-0034 D7, WI-I).

GET /api/tenant/deploy-key

Authenticated by X-API-Key (AuthMiddleware on the MCP server).  The tenant_id
is read exclusively from request.state.tenant_id (set by WI-D middleware) —
never from a path or query parameter.  This makes cross-tenant leakage
structurally impossible.

Response:
  {
    "public_key": "ssh-ed25519 AAAA...",
    "instructions": "Add the public_key value as a read-only deploy key on your
                     repository (GitHub: Settings → Deploy keys → Add deploy key).
                     Keep the checkbox 'Allow write access' UNCHECKED."
  }

Error responses:
  401 — no valid X-API-Key (enforced by AuthMiddleware before this handler runs).
  403 — API key is a legacy/global key (tenant_id IS NULL):
          { "error": "no_tenant_bound",
            "detail": "This API key is not bound to a tenant. Only tenant-scoped
                       keys can access the deploy-key endpoint." }
  500 — FERNET_KEY not set or DB error.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.web_ui._json import _json_safe

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tenant")

_INSTRUCTIONS = (
    "Add the public_key value as a read-only deploy key on your repository "
    "(GitHub: Settings → Deploy keys → Add deploy key; "
    "GitLab: Settings → Repository → Deploy keys). "
    "Keep 'Allow write access' UNCHECKED. "
    "Viindoo's private key never leaves the server — only the public half is shown here."
)


@router.get("/deploy-key")
async def get_tenant_deploy_key(request: Request):
    """Return (or lazily generate) the tenant's Ed25519 deploy-key public key.

    The tenant_id is taken from request.state (populated by AuthMiddleware /
    WI-D plumbing).  No path or query parameter is accepted for the tenant
    identity — cross-tenant fetch is structurally impossible.

    Returns 403 if the authenticated key is a legacy/global key (tenant_id IS NULL).
    """
    tenant_id = getattr(request.state, "tenant_id", None)

    if tenant_id is None:
        return JSONResponse(
            _json_safe({
                "error": "no_tenant_bound",
                "detail": (
                    "This API key is not bound to a tenant. "
                    "Only tenant-scoped keys can access the deploy-key endpoint."
                ),
            }),
            status_code=403,
        )

    try:
        from src.db.pg import auth_store

        store = auth_store()
        with store._pool.checkout() as conn:
            public_key = store.get_or_create_tenant_deploy_key(conn, tenant_id)
            conn.commit()

    except RuntimeError as exc:
        # FERNET_KEY not set — fail-fast per ADR-0020
        _logger.error("deploy-key: FERNET_KEY not set: %s", exc)
        return JSONResponse(
            _json_safe({"error": "fernet_missing", "detail": str(exc)}),
            status_code=500,
        )
    except Exception as exc:
        _logger.error("deploy-key: unexpected error for tenant_id=%s: %s", tenant_id, exc)
        return JSONResponse(
            _json_safe({"error": "internal_error", "detail": "Failed to retrieve deploy key."}),
            status_code=500,
        )

    return JSONResponse(
        _json_safe({
            "public_key": public_key,
            "instructions": _INSTRUCTIONS,
        })
    )
