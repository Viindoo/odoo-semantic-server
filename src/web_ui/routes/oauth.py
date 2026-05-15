# src/web_ui/routes/oauth.py
"""OAuth login endpoint for Web UI (M9 W-OA — Google + GitHub via arctic+oslo).

POST /api/auth/oauth-login
    Called by Astro callback routes after token exchange with the provider.
    Upserts the user in webui_users, issues a session cookie, writes audit log.

Account-linking policy (F5 security gate):
    1. Match by (oauth_provider, oauth_id)   → same account, update last_seen.
    2. Match by email + email_verified=TRUE  → merge (set oauth columns).
    3. Match by email + email_verified=FALSE → reject 409 (prevents takeover
       via unverified email at the provider).
    4. No match                              → create new user (is_admin=FALSE).

OAuth-only users have password_hash = NULL.  The login.py password path forces
a dummy-hash bcrypt compare for NULL password_hash so timing matches a
non-existent user (F1 invariant — timing oracle closed in login.py).
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from starlette.requests import Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth")

_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"google", "github"})
_SESSION_TTL_HOURS = 8


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------


class OAuthLoginBody(BaseModel):
    """Payload sent by Astro callback routes after successful token exchange."""

    provider: str
    oauth_id: str
    email: str
    email_verified: bool
    name: str = ""

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        if v not in _ALLOWED_PROVIDERS:
            raise ValueError(f"Unsupported provider: {v!r}. Allowed: {sorted(_ALLOWED_PROVIDERS)}")
        return v

    @field_validator("oauth_id")
    @classmethod
    def _validate_oauth_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("oauth_id must not be empty")
        return v.strip()

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or "@" not in v:
            raise ValueError("Invalid email address")
        return v


# ---------------------------------------------------------------------------
# DB helpers — auth_store wrappers
# ---------------------------------------------------------------------------


def _lookup_user_by_oauth(provider: str, oauth_id: str) -> dict | None:
    """Return webui_users row matching (oauth_provider, oauth_id), or None."""
    from src.db.pg import auth_store

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            return pool.fetch_one(
                conn,
                "SELECT id, username, email, email_verified, is_admin, is_active, password_hash"
                " FROM webui_users"
                " WHERE oauth_provider = %s AND oauth_id = %s",
                (provider, oauth_id),
            )
    except Exception as exc:
        logger.error("_lookup_user_by_oauth DB error: %s", exc)
        return None


def _lookup_user_by_email(email: str) -> dict | None:
    """Return webui_users row matching email (case-insensitive), or None."""
    from src.db.pg import auth_store

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            return pool.fetch_one(
                conn,
                "SELECT id, username, email, email_verified, is_admin, is_active,"
                " oauth_provider, oauth_id, password_hash"
                " FROM webui_users"
                " WHERE lower(email) = lower(%s)",
                (email,),
            )
    except Exception as exc:
        logger.error("_lookup_user_by_email DB error: %s", exc)
        return None


def _merge_oauth_into_user(user_id: int, provider: str, oauth_id: str) -> None:
    """Update existing user: set oauth_provider + oauth_id (email merge path)."""
    from src.db.pg import auth_store

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE webui_users SET oauth_provider = %s, oauth_id = %s"
                    " WHERE id = %s",
                    (provider, oauth_id, user_id),
                )
            conn.commit()
    except Exception as exc:
        logger.error("_merge_oauth_into_user DB error: %s", exc)
        raise


def _create_oauth_user(
    provider: str,
    oauth_id: str,
    email: str,
    email_verified: bool,
    name: str,
) -> dict:
    """INSERT new OAuth-only user (password_hash = NULL) and return the row."""
    from src.db.pg import auth_store

    # Derive a safe username: email local-part + random suffix to avoid collisions
    local = email.split("@")[0][:40]
    suffix = secrets.token_hex(4)
    username = f"{local}_{suffix}"

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO webui_users"
                    " (username, password_hash, oauth_provider, oauth_id,"
                    "  email, email_verified, is_admin, is_active)"
                    " VALUES (%s, NULL, %s, %s, %s, %s, FALSE, TRUE)"
                    " RETURNING id, username, email, email_verified, is_admin, is_active",
                    (username, provider, oauth_id, email, email_verified),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("INSERT returned no row")
            conn.commit()
            return {
                "id": row[0],
                "username": row[1],
                "email": row[2],
                "email_verified": row[3],
                "is_admin": row[4],
                "is_active": row[5],
            }
    except Exception as exc:
        logger.error("_create_oauth_user DB error: %s", exc)
        raise


def _create_session(user_id: int, ip_address: str | None, user_agent: str | None) -> str:
    """INSERT into active_sessions, return opaque session_id."""
    from src.db.pg import auth_store

    session_id = secrets.token_urlsafe(32)
    expires_at = datetime.now(tz=UTC) + timedelta(hours=_SESSION_TTL_HOURS)
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
        return session_id
    except Exception as exc:
        logger.error("_create_session DB error: %s", exc)
        raise


def _insert_audit_log(
    actor: str,
    action: str,
    target: str | None,
    success: bool,
    detail: dict,
) -> None:
    """Write to admin_audit_log. Delegates to src.db.audit.write_audit_log.

    Kept as a local wrapper so existing call sites in this module remain stable.
    Never raises — failure is logged as a warning by write_audit_log.
    """
    from src.db.audit import write_audit_log

    write_audit_log(actor, action, target, success, detail)


# ---------------------------------------------------------------------------
# OAuth login endpoint
# ---------------------------------------------------------------------------


@router.post("/oauth-login")
async def oauth_login(body: OAuthLoginBody, request: Request) -> JSONResponse:
    """Upsert OAuth user + issue session cookie.

    Called only by Astro callback routes (loopback — enforced by
    _LoopbackOnlyMiddleware in app.py).

    Returns 200 with Set-Cookie on success.
    Returns 409 if email collision with unverified existing account.
    Returns 400 if body validation fails (caught by Pydantic).
    """
    client_ip: str = (
        request.headers.get("x-real-ip")
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    user_agent: str | None = request.headers.get("user-agent")

    # -----------------------------------------------------------------------
    # 1. Check existing record by (provider, oauth_id) — same account, fast path
    # -----------------------------------------------------------------------
    user = _lookup_user_by_oauth(body.provider, body.oauth_id)

    # -----------------------------------------------------------------------
    # 2. No oauth match — try email lookup for account linking
    # -----------------------------------------------------------------------
    if user is None:
        existing_by_email = _lookup_user_by_email(body.email)
        if existing_by_email is not None:
            if not body.email_verified:
                # Reject: unverified email at provider — could be a takeover attempt
                logger.warning(
                    "OAuth login rejected: email %r exists but provider email_verified=False"
                    " (provider=%s, oauth_id=%s)",
                    body.email,
                    body.provider,
                    body.oauth_id,
                )
                _insert_audit_log(
                    actor=f"oauth:{body.provider}:{body.oauth_id}",
                    action="user.oauth_login",
                    target=body.email,
                    success=False,
                    detail={
                        "ip": client_ip,
                        "reason": "email_not_verified_at_provider",
                        "provider": body.provider,
                    },
                )
                return JSONResponse(
                    {
                        "error": "email_conflict",
                        "detail": (
                            "An account with this email already exists. "
                            "Your provider has not verified this email address. "
                            "Please verify your email at the provider and try again."
                        ),
                    },
                    status_code=409,
                )
            # Email verified at provider → safe to merge oauth credentials
            try:
                _merge_oauth_into_user(
                    existing_by_email["id"], body.provider, body.oauth_id
                )
            except Exception as exc:
                logger.error("OAuth email-merge failed: %s", exc)
                return JSONResponse({"error": "internal_error"}, status_code=500)
            user = existing_by_email

        else:
            # ---------------------------------------------------------------
            # 3. No match at all → create new OAuth-only user
            # ---------------------------------------------------------------
            try:
                user = _create_oauth_user(
                    provider=body.provider,
                    oauth_id=body.oauth_id,
                    email=body.email,
                    email_verified=body.email_verified,
                    name=body.name,
                )
            except Exception as exc:
                logger.error("OAuth user creation failed: %s", exc)
                return JSONResponse({"error": "internal_error"}, status_code=500)

    # -----------------------------------------------------------------------
    # 4. Check account active
    # -----------------------------------------------------------------------
    if not user.get("is_active", True):
        logger.warning(
            "OAuth login rejected: account inactive (provider=%s, oauth_id=%s)",
            body.provider,
            body.oauth_id,
        )
        _insert_audit_log(
            actor=f"oauth:{body.provider}:{body.oauth_id}",
            action="user.oauth_login",
            target=body.email,
            success=False,
            detail={"ip": client_ip, "reason": "account_inactive", "provider": body.provider},
        )
        return JSONResponse({"error": "account_inactive"}, status_code=403)

    # -----------------------------------------------------------------------
    # 5. Issue session
    # -----------------------------------------------------------------------
    try:
        session_id = _create_session(
            user_id=user["id"],
            ip_address=client_ip,
            user_agent=user_agent,
        )
    except Exception as exc:
        logger.error("OAuth login: session creation failed: %s", exc)
        return JSONResponse({"error": "internal_error"}, status_code=500)

    # -----------------------------------------------------------------------
    # 6. Audit log — success
    # -----------------------------------------------------------------------
    _insert_audit_log(
        actor=f"user:{user['id']}",
        action="user.oauth_login",
        target=body.email,
        success=True,
        detail={"ip": client_ip, "provider": body.provider},
    )

    # -----------------------------------------------------------------------
    # 7. Set session in signed cookie (same transport as password login)
    # -----------------------------------------------------------------------
    username = user.get("username", "")
    request.session["session_id"] = session_id
    request.session["username"] = username
    request.session["session_at"] = time.time()

    logger.info(
        "OAuth login success: user %r (provider=%s, IP=%s)",
        username,
        body.provider,
        client_ip,
    )
    return JSONResponse({"ok": True, "username": username})
