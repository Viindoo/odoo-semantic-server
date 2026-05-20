# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/auth.py
"""Password hashing + session secret for Web UI auth (M7 W16).

Usage:
    hash_password(pw)        → bcrypt hash (cost=12)
    verify_password(pw, h)   → bool
    get_session_secret()     → 32-byte hex string (from env or dev fallback)
    require_admin(request)   → user_id (raises HTTPException 401/403 if not)
    require_admin_with_fresh_mfa(request) → user_id (raises 403 if MFA stale)

Session middleware:
    Use starlette.middleware.sessions.SessionMiddleware with the secret returned
    by get_session_secret(). TTL enforced by storing "session_at" epoch in the
    session dict and checking inside AuthRequiredMiddleware.

MFA freshness:
    For destructive operations (restore), require MFA to have been completed
    within the last MFA_FRESHNESS_SECONDS (default 5 minutes). MFA timestamp
    is stored in session["mfa_verified_at"]. If MFA is not enrolled, the
    operation is blocked (403).
"""

import logging
import os
import secrets
import time

import bcrypt
from fastapi import HTTPException
from starlette.requests import Request

logger = logging.getLogger(__name__)

# Session TTL: 8 hours in seconds
SESSION_TTL_SECONDS = 8 * 3600

# MFA freshness window for destructive operations (restore): 5 minutes
MFA_FRESHNESS_SECONDS = 5 * 60

_DEV_FALLBACK_SECRET: str | None = None


def get_session_secret() -> str:
    """Return WEBUI_SESSION_SECRET or a generated dev-only fallback.

    Production: set WEBUI_SESSION_SECRET to a 32-byte random hex string in webui.env.
    Dev: if env var unset, a random secret is generated per process start (sessions
    invalidated on restart — acceptable for dev).

    Startup assertion: if ENVIRONMENT=production and WEBUI_SESSION_SECRET is not set,
    the process will refuse to start (SystemExit 1) to prevent insecure deployment.
    """
    secret = os.environ.get("WEBUI_SESSION_SECRET")
    if secret:
        return secret

    # Production guard: refuse to start with an insecure ephemeral secret.
    if os.environ.get("ENVIRONMENT") == "production":
        raise SystemExit(
            "FATAL: WEBUI_SESSION_SECRET is required in production. "
            "Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""
            " and set it in webui.env or the systemd EnvironmentFile."
        )

    global _DEV_FALLBACK_SECRET
    if _DEV_FALLBACK_SECRET is None:
        _DEV_FALLBACK_SECRET = secrets.token_hex(32)
        logger.warning(
            "WEBUI_SESSION_SECRET not set — using a generated dev-only secret. "
            "Sessions will be invalidated on process restart. "
            "Set WEBUI_SESSION_SECRET=<32-byte-hex> in webui.env for production."
        )
    return _DEV_FALLBACK_SECRET


def hash_password(pw: str) -> str:
    """Hash a plaintext password with bcrypt cost=12.

    Returns a UTF-8 string suitable for storage in webui_users.password_hash.
    """
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(pw.encode(), salt)
    return hashed.decode()


def verify_password(pw: str, hash_: str) -> bool:
    """Return True if pw matches the bcrypt hash_, False otherwise.

    Never raises — returns False on any error (malformed hash, etc.).
    """
    try:
        return bcrypt.checkpw(pw.encode(), hash_.encode())
    except Exception:
        return False


def current_user_id(request: Request) -> int | None:
    """Return the integer webui_users.id for the current session, or None.

    Resolution order:
      1. ``request.session["user_id"]`` — set by W-AC (active_sessions path).
      2. ``request.session["username"]`` — legacy signed-cookie path.
      3. Test bypass mode → sentinel id=1.
    """
    if is_test_bypass_active():
        return 1

    try:
        session = request.session
    except AssertionError:
        return None

    uid = session.get("user_id")
    if uid is not None:
        return int(uid)

    username = session.get("username")
    if not username:
        return None

    try:
        from src.db.pg import auth_store
        return auth_store().get_user_id_by_username(username)
    except Exception:
        return None


def is_admin_session(request: Request) -> bool:
    """DB-sourced admin check (per ADR-0011 — never trust session for privilege).

    Fails CLOSED: returns False when uid is None (unauthenticated or malformed
    session cookie).  This function is called only from HTTP handlers; CLI paths
    never call it.  The prior True-on-None was a backward-compat backdoor — if
    SessionMiddleware crashed or the cookie was malformed, callers would receive
    admin privilege silently.  Fail-closed eliminates that path.
    """
    from src.db.pg import auth_store

    uid = current_user_id(request)
    if uid is None:
        return False
    try:
        return bool(auth_store().get_user_field(uid, "is_admin"))
    except Exception:
        return False


async def require_admin(request: Request) -> int:
    """FastAPI Depends: return user_id if session user is an admin.

    Raises:
        HTTPException 401: session not authenticated.
        HTTPException 403: user authenticated but not an admin.
    """
    user_id = current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    if is_test_bypass_active():
        return user_id

    try:
        from src.db.pg import auth_store
        is_admin = auth_store().get_user_field(user_id, "is_admin")
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to verify admin status") from exc

    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin privilege required")
    return user_id


async def require_admin_with_fresh_mfa(request: Request) -> int:
    """FastAPI Depends: require admin + fresh MFA verification (within 5 min).

    Used for destructive operations like restore. Adds MFA freshness check
    on top of require_admin.
    """
    user_id = await require_admin(request)

    if is_test_bypass_active():
        return user_id

    mfa_verified_at = request.session.get("mfa_verified_at")
    if mfa_verified_at is None:
        raise HTTPException(
            status_code=403,
            detail="Fresh MFA required — MFA not enrolled or not recently verified",
        )
    try:
        age = time.time() - float(mfa_verified_at)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=403,
            detail="Fresh MFA required — invalid MFA timestamp",
        )
    if age < 0 or age > MFA_FRESHNESS_SECONDS:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Fresh MFA required — re-verify within last "
                f"{MFA_FRESHNESS_SECONDS // 60} minutes"
            ),
        )
    return user_id


def is_test_bypass_active() -> bool:
    """Return True only when BOTH test-bypass env vars are set.

    Why both are required: a production deployment with a stale .env
    containing WEBUI_AUTH_DISABLED=1 must NOT bypass auth. Pairing with
    PYTEST_CURRENT_TEST (set automatically by pytest itself) ensures the
    bypass is impossible outside an active pytest process — even if ops
    accidentally leaves WEBUI_AUTH_DISABLED=1 in the production env file.
    """
    return (
        os.environ.get("WEBUI_AUTH_DISABLED") == "1"
        and os.environ.get("PYTEST_CURRENT_TEST") is not None
    )
