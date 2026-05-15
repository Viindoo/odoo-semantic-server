# src/web_ui/routes/login.py
"""Auth endpoints for Web UI session auth (M9 W-AC hardening).

Security hardening applied:
  F1  — Dummy-hash unconditional verify (timing oracle fix).
  F2  — Postgres-backed rate-limit via login_attempts table.
  F3  — TRUSTED_PROXY_CIDRS allowlist for X-Forwarded-For.
  F7  — Server-side active_sessions store (opaque session_id in cookie).
  F15 — WEBUI_SECURE_COOKIE opt-out (!= '0' instead of == '1').
  Password min_length=12 + common-password blocklist.
  Audit log INSERT to admin_audit_log.
"""

import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import bcrypt
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import Request

from src.web_ui.auth import hash_password, is_test_bypass_active, verify_password
from src.web_ui.login_attempts import (
    check_rate_limit,
    get_client_ip,
    record_login_attempt,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth")

# ---------------------------------------------------------------------------
# F1 — Dummy hash: unconditional bcrypt verify so timing is constant whether
# the user exists or not.  Prevents username enumeration via timing oracle.
# ---------------------------------------------------------------------------
_DUMMY_HASH: str = bcrypt.hashpw(
    b"dummy-for-timing-defense", bcrypt.gensalt(rounds=12)
).decode("utf-8")

# ---------------------------------------------------------------------------
# Common-password blocklist: lazy-loaded on first login attempt.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_common_passwords() -> frozenset[str]:
    """Load top-100 common passwords from data/common_passwords.txt.

    Tries the file relative to the repo root; returns empty set if not found
    so the application starts cleanly on environments without the file.
    """
    candidates = [
        Path(__file__).parent.parent.parent.parent / "data" / "common_passwords.txt",
        Path("data") / "common_passwords.txt",
    ]
    for path in candidates:
        if path.exists():
            try:
                return frozenset(path.read_text(encoding="utf-8").splitlines())
            except Exception as exc:
                logger.warning("Could not load common_passwords.txt: %s", exc)
    return frozenset()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _lookup_user(username: str) -> dict | None:
    """Return {id, password_hash, is_admin, is_active} for username, or None."""
    from src.db.pg import auth_store

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            return pool.fetch_one(
                conn,
                "SELECT id, password_hash, is_admin,"
                " COALESCE(is_active, TRUE) AS is_active"
                " FROM webui_users WHERE username = %s",
                (username,),
            )
    except Exception as exc:
        logger.error("_lookup_user DB error: %s", exc)
        return None


def _create_session(
    user_id: int,
    ip_address: str | None,
    user_agent: str | None,
) -> str:
    """INSERT into active_sessions and return the new session_id."""
    from src.db.pg import auth_store

    session_id = secrets.token_urlsafe(32)
    expires_at = datetime.now(tz=UTC) + timedelta(seconds=8 * 3600)
    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO active_sessions"
                    " (session_id, user_id, expires_at, ip_address, user_agent)"
                    " VALUES (%s, %s, %s, %s::inet, %s)",
                    (session_id, user_id, expires_at, ip_address, user_agent),
                )
            conn.commit()
    except Exception as exc:
        logger.error("_create_session DB error: %s", exc)
        raise
    return session_id


def _revoke_session(session_id: str) -> None:
    """DELETE session_id from active_sessions (instant revoke)."""
    from src.db.pg import auth_store

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM active_sessions WHERE session_id = %s",
                    (session_id,),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("_revoke_session DB error (non-fatal): %s", exc)


def _revoke_all_user_sessions(user_id: int) -> None:
    """DELETE all sessions for user_id (session rotation on new login)."""
    from src.db.pg import auth_store

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM active_sessions WHERE user_id = %s",
                    (user_id,),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("_revoke_all_user_sessions DB error (non-fatal): %s", exc)


def _update_session_last_seen(session_id: str) -> None:
    """UPDATE last_seen for session_id (sliding window)."""
    from src.db.pg import auth_store

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE active_sessions SET last_seen = NOW() WHERE session_id = %s",
                    (session_id,),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("_update_session_last_seen DB error (non-fatal): %s", exc)


def _lookup_session(session_id: str) -> dict | None:
    """Return {user_id} if session is valid (not expired), else None."""
    from src.db.pg import auth_store

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            return pool.fetch_one(
                conn,
                "SELECT user_id FROM active_sessions"
                " WHERE session_id = %s AND expires_at > NOW()",
                (session_id,),
            )
    except Exception as exc:
        logger.warning("_lookup_session DB error: %s", exc)
        return None


def _insert_audit_log(
    actor: str,
    action: str,
    target: str | None,
    success: bool,
    detail: dict,
) -> None:
    """Write a row to admin_audit_log. Delegates to src.db.audit.write_audit_log.

    Kept as a local wrapper so existing call sites in this module remain stable.
    Never raises — failure is logged as a warning by write_audit_log.
    """
    from src.db.audit import write_audit_log

    write_audit_log(actor, action, target, success, detail)


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------


def _check_totp_enabled(username: str) -> dict | None:
    """Return {user_id} if user has TOTP enabled, else None.

    Queries webui_users JOIN totp_secrets for the given username.
    Returns None on any DB error (graceful degradation — don't block login).
    """
    from src.db.pg import auth_store

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT wu.id
                    FROM webui_users wu
                    JOIN totp_secrets ts ON ts.user_id = wu.id
                    WHERE wu.username = %s AND ts.enabled = TRUE
                    """,
                    (username,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {"user_id": row[0]}
    except Exception:
        return None


class LoginBody(BaseModel):
    """Login request body.

    Password length (12–128) is validated here so that bcrypt is never called
    on arbitrarily long inputs (DoS risk) and excessively short passwords are
    rejected before hitting the DB. Both violations return the generic
    "invalid_credentials" error (no detail leaked to the caller).
    """

    username: str
    password: str = Field(..., min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# Login endpoint
# ---------------------------------------------------------------------------


@router.post("/login")
async def login_post(request: Request, body: LoginBody):
    """Verify credentials, create server-side session, set cookie.

    F1: dummy-hash always runs bcrypt regardless of whether user exists.
    F2: Postgres-backed rate-limit via login_attempts table.
    F3: X-Forwarded-For only trusted from TRUSTED_PROXY_CIDRS peers.
    F7: opaque session_id in signed cookie; session stored in active_sessions.
    """
    # Password complexity check: min_length=12.
    # Done here (not in Pydantic) so we can return generic 401 without leaking
    # validation details (Pydantic would return 422 with field-level error).
    if len(body.password) < 12:
        # Run dummy-hash to keep timing constant regardless of password length.
        # Pad password to 12 chars so bcrypt still processes a non-trivial input.
        verify_password(body.password.ljust(12, "x"), _DUMMY_HASH)
        return JSONResponse({"error": "invalid_credentials"}, status_code=401)

    # F3 — resolve client IP using trusted proxy CIDR list
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("User-Agent", "")

    # F2 — check Postgres-backed rate limit (by username + by IP)
    if check_rate_limit(body.username.strip(), ip_address=client_ip):
        logger.warning("Login rate-limited for user %r / IP %s", body.username, client_ip)
        return JSONResponse(
            {"error": "Too many failed login attempts; please wait before retrying"},
            status_code=429,
        )

    # Lookup user — F1: always run bcrypt regardless of result
    try:
        user = _lookup_user(body.username.strip())
    except Exception as exc:
        logger.error("Login DB error during user lookup: %s", exc)
        user = None

    # F1 — use dummy hash when user not found so timing is constant.
    # OAuth-only users: force dummy-hash compare so timing matches non-existent
    # user (F1 invariant).  password_hash IS NULL for OAuth-only accounts; passing
    # None to verify_password causes bcrypt to raise, which exits early and leaks
    # "this account is OAuth-only" via a timing oracle.
    hash_to_check: str
    if user is None:
        hash_to_check = _DUMMY_HASH
    elif user["password_hash"] is None:
        hash_to_check = _DUMMY_HASH
    else:
        hash_to_check = user["password_hash"]

    ok = verify_password(body.password, hash_to_check)

    # Check common-password blocklist (after bcrypt to keep timing consistent)
    password_is_common = body.password in _load_common_passwords()

    if not user or not ok or not user.get("is_active", True) or password_is_common:
        # F2 — record failed attempt in Postgres
        record_login_attempt(
            identifier=body.username.strip(),
            success=False,
            ip_address=client_ip,
            user_agent=user_agent,
        )
        logger.warning(
            "Failed login attempt for user %r (IP: %s)", body.username, client_ip
        )
        _insert_audit_log(
            actor=body.username.strip(),
            action="user.login",
            target=None,
            success=False,
            detail={
                "ip": client_ip,
                "user_agent": user_agent,
                "reason": "invalid_credentials",
            },
        )
        return JSONResponse({"error": "invalid_credentials"}, status_code=401)

    # Credentials OK
    username_clean = body.username.strip()

    # M9 W-MF: check if TOTP is enabled for this user — if yes, return
    # mfa_required + short-lived mfa_token. Do NOT create the real session
    # until TOTP code is verified in /api/auth/totp/login.
    try:
        totp_row = _check_totp_enabled(username_clean)
    except Exception as exc:
        logger.warning("TOTP status check failed (non-fatal): %s", exc)
        totp_row = None

    if totp_row:
        from src.web_ui.routes.totp import create_mfa_token

        mfa_token = create_mfa_token(totp_row["user_id"], ttl_seconds=300)
        logger.info(
            "MFA required for user %r (IP: %s) — issued mfa_token",
            username_clean,
            client_ip,
        )
        # F2 — record successful password attempt (MFA second step still pending)
        record_login_attempt(
            identifier=username_clean,
            success=True,
            ip_address=client_ip,
            user_agent=user_agent,
        )
        return JSONResponse({"mfa_required": True, "mfa_token": mfa_token})

    # F7 — session rotation: revoke all existing sessions before creating new one
    _revoke_all_user_sessions(user["id"])

    # F7 — create new server-side session
    try:
        session_id = _create_session(
            user_id=user["id"],
            ip_address=client_ip,
            user_agent=user_agent,
        )
    except Exception as exc:
        logger.error("Login: could not create session: %s", exc)
        return JSONResponse({"error": "internal_error"}, status_code=500)

    # F2 — record successful attempt
    record_login_attempt(
        identifier=username_clean,
        success=True,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    # Audit log — success
    _insert_audit_log(
        actor=f"user:{user['id']}",
        action="user.login",
        target=None,
        success=True,
        detail={"ip": client_ip, "user_agent": user_agent},
    )

    # Store opaque session_id in signed cookie (still using SessionMiddleware
    # for cookie transport + integrity; content is just the session_id).
    request.session["session_id"] = session_id
    request.session["username"] = username_clean
    request.session["user_id"] = user["id"]
    request.session["session_at"] = time.time()

    logger.info("Successful login for user %r (IP: %s)", username_clean, client_ip)
    return JSONResponse({"ok": True, "username": username_clean})


@router.post("/logout")
async def logout(request: Request):
    """Revoke server-side session + clear cookie."""
    session_id = request.session.get("session_id")
    if session_id:
        _revoke_session(session_id)
    request.session.clear()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Password reset endpoint (W-UM)
# ---------------------------------------------------------------------------
class ResetPasswordBody(BaseModel):
    token: str
    new_password: str


@router.post("/reset-password")
async def reset_password_consume(body: ResetPasswordBody, request: Request):
    """Consume a password reset token and set a new password.

    Verifies the token (valid, unused, not expired), sets the new bcrypt hash,
    revokes all sessions for that user (forcing re-login), and writes an audit
    log entry.
    """
    try:
        from src.db.pg import auth_store

        store = auth_store()
        try:
            user_id = store.consume_password_reset_token(body.token)
        except ValueError as ve:
            return JSONResponse({"error": str(ve)}, status_code=410)

        if user_id is None:
            return JSONResponse({"error": "not_found"}, status_code=404)

        new_hash = hash_password(body.new_password)
        user = store.get_user_by_id(user_id)
        username = user["username"] if user else str(user_id)

        store.set_user_password(username, new_hash)
        store.revoke_all_sessions(user_id)
        from src.db.audit import write_audit_log
        write_audit_log(
            actor=f"user:{user_id}",
            action="user.reset_password",
            target=str(user_id),
            success=True,
            detail={"method": "token"},
        )
        logger.info("Password reset consumed for user_id=%s (%s)", user_id, username)
        return JSONResponse({"ok": True})

    except Exception as exc:
        logger.error("reset_password_consume error: %s", exc)
        return JSONResponse({"error": "internal_error"}, status_code=500)


# ---------------------------------------------------------------------------
# Verify endpoint (used by Astro middleware)
# ---------------------------------------------------------------------------


@router.get("/verify")
async def verify_session(request: Request):
    """Return 200 + username if session is valid, 401 if not.

    F7: validates session_id against active_sessions table in addition to
    the signed cookie check (defense in depth — server-side revoke).

    Honors the same WEBUI_AUTH_DISABLED + PYTEST_CURRENT_TEST bypass as
    AuthRequiredMiddleware so browser tests can verify admin pages without
    seeding a session cookie.
    """
    from src.web_ui.middleware import _session_valid

    if is_test_bypass_active():
        return JSONResponse({"ok": True, "username": "test-user", "is_admin": True})

    if not _session_valid(request):
        return JSONResponse({"ok": False, "error": "not_authenticated"}, status_code=401)

    # F7 — confirm session_id is still live in DB (instant revoke after logout)
    session_id = request.session.get("session_id")
    if session_id:
        row = _lookup_session(session_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "not_authenticated"}, status_code=401)
        _update_session_last_seen(session_id)

    username = request.session.get("username")

    # W-UM: include is_admin so Astro requireAdmin middleware can gate /admin/users/*
    is_admin = False
    try:
        from src.db.pg import auth_store
        store = auth_store()
        user_id = store.get_user_id_by_username(username) if username else None
        if user_id is not None:
            is_admin = bool(store.get_user_field(user_id, "is_admin"))
    except Exception:
        pass  # is_admin stays False on any DB error
    return JSONResponse({"ok": True, "username": username, "is_admin": is_admin})
