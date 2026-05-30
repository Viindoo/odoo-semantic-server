# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/admin_users.py
"""User management routes for Web UI admin (M9 W-UM).

Routes
------
GET   /api/admin/users                             list all users (admin only)
POST  /api/admin/users/{user_id}/deactivate        deactivate + revoke sessions
POST  /api/admin/users/{user_id}/reactivate        reactivate user
POST  /api/admin/users/{user_id}/reset-password-link  generate + send reset link
PATCH /api/admin/users/{user_id}/admin             promote/demote admin flag
PATCH /api/admin/api-keys/{key_id}/owner           reassign API key ownership
PATCH /api/admin/api-keys/{key_id}/plan            set plan + per-key overrides (W-3)
PATCH /api/admin/users/{user_id}/plan              cascade plan to all user keys (W-3)

Auth
----
Most routes require require_admin Depends (raises 401/403 if not admin).
The two plan-assignment routes (PATCH .../plan) require
require_admin_with_fresh_mfa instead — assigning a paid plan is an
entitlement-sensitive op, symmetric with the entitlement grant/revoke and
plan price/quota edit routes (issue #220; freshness window per ADR-0043).
Self-deactivation is blocked (403).
"""

import logging
import os
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import Request

from src.db.audit import audit_action
from src.db.auth_registry import LastAdminProtectedError, UserNotFoundError
from src.web_ui._json import _json_safe
from src.web_ui.auth import hash_password, require_admin, require_admin_with_fresh_mfa

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_@.\-]{1,64}$")

_logger = logging.getLogger(__name__)

router = APIRouter()


def _auth_store():
    from src.db.pg import auth_store as _store
    return _store()


# ---------------------------------------------------------------------------
# Pydantic body models
# ---------------------------------------------------------------------------


class SetAdminBody(BaseModel):
    is_admin: bool


class AssignOwnerBody(BaseModel):
    user_id: int | None


class CreateUserBody(BaseModel):
    username: str
    email: str | None = None
    is_admin: bool = False
    password: str | None = None  # if omitted, a temp password is generated


class SetApiKeyPlanRequest(BaseModel):
    """Body for PATCH /api/admin/api-keys/{key_id}/plan (W-3, ADR-0041)."""
    plan_id: int
    rate_limit_override: int | None = Field(
        default=None, ge=0,
        description=(
            "Per-key RPM override. NULL = use plan default. "
            "CHECK >=0 (DB defense-in-depth)."
        ),
    )
    quota_override: int | None = Field(
        default=None, ge=0,
        description="Per-key monthly quota override. NULL = use plan default. CHECK >=0.",
    )


class CascadeSetPlanRequest(BaseModel):
    """Body for PATCH /api/admin/users/{user_id}/plan (W-3, ADR-0041)."""
    plan_id: int


# ---------------------------------------------------------------------------
# List users
# ---------------------------------------------------------------------------


@router.post("/api/admin/users")
@audit_action("user.create", target_param=None)
async def create_user(
    body: CreateUserBody,
    request: Request,
    actor_id: int = Depends(require_admin),
):
    """Create a new web UI user (admin only — W3 B).

    Body: {username, email?, is_admin?, password?}

    - If ``password`` is provided: use it (hashed with bcrypt cost=12).
    - If ``password`` is omitted: generate a cryptographically random 20-char
      temp password, return it **once** in the response body. It is never
      persisted in plaintext and not logged.

    Duplicate username or email -> 409. Invalid username format -> 422.
    The created user has ``email_verified=TRUE`` (admin-created accounts need
    no verification step). If the user was created with a temp-password, the
    admin must convey it to the user out-of-band.
    """
    username = (body.username or "").strip()
    if not _USERNAME_RE.match(username):
        raise HTTPException(
            status_code=422,
            detail="Username invalid: 1-64 chars, alphanumeric + _ @ . -",
        )

    # Generate or use provided password (never log plaintext)
    temp_password: str | None = None
    if body.password:
        pw_hash = hash_password(body.password)
    else:
        temp_password = secrets.token_urlsafe(15)  # 120 bits entropy, ~20 chars
        pw_hash = hash_password(temp_password)

    try:
        from src.db.pg import get_pool
        with get_pool().checkout() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    # Check username uniqueness
                    cur.execute(
                        "SELECT id FROM webui_users WHERE username = %s",
                        (username,),
                    )
                    if cur.fetchone():
                        raise HTTPException(
                            status_code=409,
                            detail=f"Username '{username}' already exists",
                        )
                    # Check email uniqueness if provided
                    email = (body.email or "").strip() or None
                    if email:
                        cur.execute(
                            "SELECT id FROM webui_users WHERE email = %s",
                            (email,),
                        )
                        if cur.fetchone():
                            raise HTTPException(
                                status_code=409,
                                detail=f"Email '{email}' already registered",
                            )
                    # Insert user — admin-created accounts are email_verified by default
                    cur.execute(
                        "INSERT INTO webui_users"
                        " (username, password_hash, email, email_verified, is_admin, is_active)"
                        " VALUES (%s, %s, %s, TRUE, %s, TRUE)"
                        " RETURNING id",
                        (username, pw_hash, email, body.is_admin),
                    )
                    new_id = cur.fetchone()[0]
                conn.commit()
            except HTTPException:
                conn.rollback()
                raise
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
    except HTTPException:
        raise
    except Exception as exc:
        _logger.error("create_user DB error: %s", exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)

    # Audit row is written once by @audit_action("user.create"); enrich it here
    # with the generated user id (target) + forensic detail — no second write.
    try:
        request.state.audit_target = str(new_id)
        request.state.audit_detail.update({
            "username": username,
            "is_admin": body.is_admin,
            "temp_password": temp_password is not None,
        })
    except Exception:
        pass
    _logger.info(
        "Admin %s created user %s (id=%s is_admin=%s temp_pw=%s)",
        actor_id, username, new_id, body.is_admin, temp_password is not None,
    )

    response_body: dict = {"ok": True, "user_id": new_id, "username": username}
    if temp_password is not None:
        # Return once — caller must convey this out-of-band. Not persisted or logged.
        response_body["temp_password"] = temp_password
    return JSONResponse(_json_safe(response_body))


@router.get("/api/admin/users")
async def list_users(request: Request, actor_id: int = Depends(require_admin)):
    """Return all webui_users (no password hashes) as JSON array.

    Requires admin session. Each user dict includes api_key_count (active keys).
    """
    try:
        store = _auth_store()
        users = store.list_webui_users()
        counts = store.count_api_keys_per_user()
        for u in users:
            u["api_key_count"] = counts.get(u["id"], 0)
    except Exception as exc:
        _logger.error("list_users DB error: %s", exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)
    return JSONResponse(_json_safe({"users": users}))


# ---------------------------------------------------------------------------
# Deactivate
# ---------------------------------------------------------------------------


@router.post("/api/admin/users/{user_id}/deactivate")
@audit_action("user.deactivate", target_param="user_id")
async def deactivate_user(
    user_id: int, request: Request, actor_id: int = Depends(require_admin)
):
    """Deactivate a user and revoke all their sessions (instant logout).

    Self-deactivation is blocked — admin cannot lock themselves out.
    Deactivating the last active admin is blocked (422).
    """
    if user_id == actor_id:
        raise HTTPException(status_code=403, detail="Cannot deactivate your own account")

    store = _auth_store()
    user = store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        store.set_user_active(user_id, is_active=False)
        store.revoke_all_sessions(user_id)
        # Single audit row via @audit_action (target=user_id from path param);
        # enrich detail only — no second write_audit_log.
        try:
            request.state.audit_detail.update({"username": user["username"]})
        except Exception:
            pass
        _logger.info("Admin %s deactivated user %s (%s)", actor_id, user_id, user["username"])
    except LastAdminProtectedError:
        return JSONResponse(_json_safe({"error": "last_admin_protected"}), status_code=422)
    except HTTPException:
        raise
    except Exception as exc:
        _logger.error("deactivate_user DB error: %s", exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)

    return JSONResponse(_json_safe({"ok": True}))


# ---------------------------------------------------------------------------
# Reactivate
# ---------------------------------------------------------------------------


@router.post("/api/admin/users/{user_id}/reactivate")
@audit_action("user.reactivate", target_param="user_id")
async def reactivate_user(
    user_id: int, request: Request, actor_id: int = Depends(require_admin)
):
    """Reactivate a previously deactivated user."""
    store = _auth_store()
    user = store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        store.set_user_active(user_id, is_active=True)
        # Single audit row via @audit_action (target=user_id from path param);
        # enrich detail only — no second write_audit_log.
        try:
            request.state.audit_detail.update({"username": user["username"]})
        except Exception:
            pass
        _logger.info("Admin %s reactivated user %s (%s)", actor_id, user_id, user["username"])
    except HTTPException:
        raise
    except Exception as exc:
        _logger.error("reactivate_user DB error: %s", exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)

    return JSONResponse(_json_safe({"ok": True}))


# ---------------------------------------------------------------------------
# Password reset link
# ---------------------------------------------------------------------------


@router.post("/api/admin/users/{user_id}/reset-password-link")
@audit_action("user.reset_password_link", target_param="user_id")
async def reset_password_link(
    user_id: int, request: Request, actor_id: int = Depends(require_admin)
):
    """Generate a password reset token and send (or log) a reset email.

    Token entropy: 256-bit. TTL: 1 hour. Stored as SHA-256 hash in
    email_verifications (purpose='password_reset').

    If SMTP_HOST is not configured, the link is logged at WARNING level instead
    of being emailed — useful for dev/staging environments.
    """
    store = _auth_store()
    user = store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        raw_token = store.create_password_reset_token(user_id, ttl_seconds=3600)

        # Build reset link
        host = os.environ.get("PUBLIC_HOST", "http://localhost:4321")
        reset_link = f"{host}/reset-password?token={raw_token}"

        email_sent = _send_reset_email(
            to_email=user.get("email") or user["username"],
            username=user["username"],
            reset_link=reset_link,
        )

        # Single audit row via @audit_action("user.reset_password_link"); enrich
        # detail only. The prior manual write used the wrong action name
        # ("user.reset_password") and wrote a duplicate row — both removed.
        try:
            request.state.audit_detail.update({
                "username": user["username"],
                "email_sent": email_sent,
            })
        except Exception:
            pass
        _logger.info(
            "Admin %s generated password reset link for user %s (%s), email_sent=%s",
            actor_id, user_id, user["username"], email_sent,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _logger.error("reset_password_link error: %s", exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)

    return JSONResponse(_json_safe({"ok": True, "email_sent": email_sent}))


def _send_reset_email(*, to_email: str, username: str, reset_link: str) -> bool:
    """Send password reset email via SMTP. Returns True if sent, False if SMTP unset.

    Uses env vars: SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
    SMTP_FROM (default SMTP_USER).

    If SMTP_HOST is not set, logs the reset link at WARNING level and returns False.
    """
    smtp_host = os.environ.get("SMTP_HOST", "")
    if not smtp_host:
        _logger.warning(
            "SMTP_HOST not set — password reset link for %s: %s", username, reset_link
        )
        return False

    import smtplib
    from email.mime.text import MIMEText

    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)

    body = (
        f"Hello {username},\n\n"
        f"Click the link below to reset your odoo-semantic-mcp password.\n"
        f"This link expires in 1 hour and can only be used once.\n\n"
        f"{reset_link}\n\n"
        f"If you did not request this reset, ignore this email.\n"
    )
    msg = MIMEText(body)
    msg["Subject"] = "Reset your odoo-semantic-mcp password"
    msg["From"] = smtp_from
    msg["To"] = to_email

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return True
    except Exception as exc:
        _logger.error("SMTP send failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Promote / demote admin
# ---------------------------------------------------------------------------


@router.patch("/api/admin/users/{user_id}/admin")
@audit_action("user.set_admin", target_param="user_id")
async def set_user_admin_route(
    user_id: int,
    body: SetAdminBody,
    request: Request,
    _admin: int = Depends(require_admin),
):
    """Promote or demote a user's admin flag.

    Demoting the last active admin is blocked (422).
    Returns the updated user dict on success.
    """
    try:
        _auth_store().set_user_admin(user_id, body.is_admin)
    except LastAdminProtectedError:
        return JSONResponse(_json_safe({"error": "last_admin_protected"}), status_code=422)
    except UserNotFoundError:
        return JSONResponse(_json_safe({"error": "user_not_found"}), status_code=404)
    user = _auth_store().get_user_by_id(user_id)
    return JSONResponse(_json_safe({"ok": True, "user": user}))


# ---------------------------------------------------------------------------
# Assign API key owner
# ---------------------------------------------------------------------------


@router.patch("/api/admin/api-keys/{key_id}/owner")
@audit_action("api_key.assign_owner", target_param="key_id")
async def assign_key_owner_route(
    key_id: int,
    body: AssignOwnerBody,
    request: Request,
    _admin: int = Depends(require_admin),
):
    """Reassign ownership of an API key to a different user (or clear it).

    Pass ``user_id: null`` in the body to clear ownership (system key).
    Returns 404 if the target user does not exist.

    Audit detail includes old_user_id → new_user_id for forensic traceability.
    """
    store = _auth_store()

    # Fetch current owner before reassigning — for audit trail.
    existing_keys = store.list_api_keys(admin=True)
    old_user_id: int | None = None
    for k in existing_keys:
        if k["id"] == key_id:
            old_user_id = k.get("user_id")
            break

    try:
        store.assign_key_owner(key_id, body.user_id)
    except UserNotFoundError:
        return JSONResponse(_json_safe({"error": "user_not_found"}), status_code=404)

    # Attach old→new owner detail so @audit_action merges it into the audit row.
    try:
        request.state.audit_detail.update({
            "old_user_id": old_user_id,
            "new_user_id": body.user_id,
        })
    except Exception:
        pass

    return JSONResponse(_json_safe({"ok": True}))


# ---------------------------------------------------------------------------
# W-3: Set plan + per-key overrides on a single API key
# ---------------------------------------------------------------------------


@router.patch("/api/admin/api-keys/{key_id}/plan")
@audit_action("api_key.set_plan", target_param="key_id")
async def set_api_key_plan_route(
    key_id: int,
    body: SetApiKeyPlanRequest,
    request: Request,
    _admin: int = Depends(require_admin_with_fresh_mfa),
):
    """Set plan + per-key rate_limit / quota overrides on a single API key.

    Admin-only. Validates plan_id exists (422 if not). Accepts NULL overrides
    to reset to plan default. Negative values rejected by pydantic (ge=0) with
    422 before the DB constraint fires.

    Response 200: {key_id, plan: {id, slug, display_name}, rate_limit_override, quota_override}
    Response 404: key_id not found.
    Response 422: plan_id not found or override value negative.
    """
    from src.db.auth_registry import get_plan_by_id, set_api_key_plan_and_overrides
    from src.db.pg import get_pool
    from src.mcp.middleware import _cache_invalidate_by_key_id

    pg_pool = get_pool()

    # Validate plan exists
    plan = get_plan_by_id(pg_pool, body.plan_id)
    if plan is None:
        raise HTTPException(status_code=422, detail=f"plan_id={body.plan_id} not found")

    # BLOCK-1 fix: use Pydantic model_fields_set to detect which fields were
    # explicitly present in the request body.  Fields absent from the JSON body
    # get Pydantic defaults (None) but are NOT in model_fields_set, so we must
    # NOT overwrite those columns in the DB.
    #
    # Behaviour matrix:
    #   Body {plan_id}                    → fields_set={plan_id}
    #                                       → SET plan_id only → overrides PRESERVED
    #   Body {plan_id, rate_override: N}  → fields_set includes rate_override
    #                                       → SET plan_id + rate_override only
    #   Body {plan_id, ..., quota: null}  → fields_set includes quota_override
    #                                       → SET plan_id + quota_override=NULL (clears override)
    update_rate = "rate_limit_override" in body.model_fields_set
    update_quota = "quota_override" in body.model_fields_set

    # Apply update (also fetches old values for audit)
    try:
        snapshot = set_api_key_plan_and_overrides(
            pg_pool,
            key_id,
            body.plan_id,
            body.rate_limit_override,
            body.quota_override,
            update_rate_limit_override=update_rate,
            update_quota_override=update_quota,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"API key id={key_id} not found")
    except Exception as exc:
        _logger.error("set_api_key_plan DB error key_id=%s: %s", key_id, exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)

    # Enrich audit log with old/new snapshot
    try:
        request.state.audit_detail.update({
            "key_id": key_id,
            "old_plan_id": snapshot["old_plan_id"],
            "new_plan_id": snapshot["new_plan_id"],
            "old_rate_override": snapshot["old_rate_limit_override"],
            "new_rate_override": snapshot["new_rate_limit_override"],
            "old_quota_override": snapshot["old_quota_override"],
            "new_quota_override": snapshot["new_quota_override"],
        })
    except Exception:
        pass

    # Invalidate in-process MCP middleware cache for this key
    _cache_invalidate_by_key_id(key_id)

    # Return the effective values after the update (from DB snapshot), not the
    # raw body values — when a field was absent from the request body the body
    # value is the Pydantic default (None), while the DB column was preserved.
    return JSONResponse(_json_safe({
        "key_id": key_id,
        "plan": {
            "id": plan["id"],
            "slug": plan["slug"],
            "display_name": plan["display_name"],
        },
        "rate_limit_override": snapshot["new_rate_limit_override"],
        "quota_override": snapshot["new_quota_override"],
    }))


# ---------------------------------------------------------------------------
# W-3: Cascade plan to all keys of a user
# ---------------------------------------------------------------------------


@router.patch("/api/admin/users/{user_id}/plan")
@audit_action("user.set_plan_cascade", target_param="user_id")
async def cascade_set_user_plan_route(
    user_id: int,
    body: CascadeSetPlanRequest,
    request: Request,
    _admin: int = Depends(require_admin_with_fresh_mfa),
):
    """Set plan_id on ALL api_keys (active + inactive) owned by user_id.

    Per D3 decision: cascade covers ALL keys regardless of active status.
    Per-key overrides are NOT touched (use PATCH .../api-keys/{key_id}/plan
    to manage per-key overrides individually).

    Response 200: {user_id, plan_id, keys_updated: N}  — N=0 is valid (user has no keys).
    Response 404: user_id not found.
    Response 422: plan_id not found.
    """
    from src.db.auth_registry import bulk_set_plan_for_user, get_plan_by_id
    from src.db.pg import auth_store, get_pool
    from src.mcp.middleware import _cache_invalidate_by_key_id

    pg_pool = get_pool()

    # Validate plan exists
    plan = get_plan_by_id(pg_pool, body.plan_id)
    if plan is None:
        raise HTTPException(status_code=422, detail=f"plan_id={body.plan_id} not found")

    # Validate user exists
    store = auth_store()
    user = store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"User id={user_id} not found")

    try:
        affected_key_ids = bulk_set_plan_for_user(pg_pool, user_id, body.plan_id)
    except Exception as exc:
        _logger.error("cascade_set_user_plan DB error user_id=%s: %s", user_id, exc)
        return JSONResponse(_json_safe({"error": str(exc)}), status_code=500)

    keys_updated = len(affected_key_ids)

    # Enrich audit log
    try:
        request.state.audit_detail.update({
            "user_id": user_id,
            "plan_id": body.plan_id,
            "keys_updated": keys_updated,
        })
    except Exception:
        pass

    # Invalidate per-key MCP cache for every affected key
    for kid in affected_key_ids:
        _cache_invalidate_by_key_id(kid)

    return JSONResponse(_json_safe({
        "user_id": user_id,
        "plan_id": body.plan_id,
        "keys_updated": keys_updated,
    }))
