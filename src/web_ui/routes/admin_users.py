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

Auth
----
All routes require require_admin Depends (raises 401/403 if not admin).
Self-deactivation is blocked (403).
"""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.db.audit import audit_action
from src.db.auth_registry import LastAdminProtectedError, UserNotFoundError
from src.web_ui._json import _json_safe
from src.web_ui.auth import require_admin

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


# ---------------------------------------------------------------------------
# List users
# ---------------------------------------------------------------------------


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
        from src.db.audit import write_audit_log
        write_audit_log(
            actor=f"user:{actor_id}",
            action="user.deactivate",
            target=str(user_id),
            success=True,
            detail={"username": user["username"]},
        )
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
        from src.db.audit import write_audit_log
        write_audit_log(
            actor=f"user:{actor_id}",
            action="user.reactivate",
            target=str(user_id),
            success=True,
            detail={"username": user["username"]},
        )
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

        from src.db.audit import write_audit_log
        write_audit_log(
            actor=f"user:{actor_id}",
            action="user.reset_password",
            target=str(user_id),
            success=True,
            detail={
                "username": user["username"],
                "email_sent": email_sent,
            },
        )
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
