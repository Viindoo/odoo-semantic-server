# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/signup.py
"""Public signup, email verification, and resend endpoints (M9 W-SG).

Routes (all exempt from AuthRequiredMiddleware via /api/auth/ prefix):
    POST /api/auth/register          — create unverified account + send verify email
    POST /api/auth/verify-email      — consume token → mark verified + auto-login
    POST /api/auth/resend-verification — re-send verify email (rate: 3/hour per email)

Security decisions:
    • Token: secrets.token_urlsafe(32) = 256-bit entropy.
    • TTL: 24h for email_verify.
    • Single-use: used_at IS NULL guard + FOR UPDATE pessimistic lock.
    • Password: min_length=12 + top-100 common-password blocklist.
    • Duplicate check: generic 409 message to prevent username/email enumeration.
    • HTML email: user input escaped via html.escape before embedding in body.
    • hCaptcha: skipped (dev mode) when HCAPTCHA_SECRET unset.
    • Audit: every action logged at INFO (success) or WARNING (failure).
    • SIGNUP_ENABLED gate: register is disabled by default (invite-only model,
      ADR-0034 / Wave 0). POST /api/auth/register returns 403 until an operator
      sets SIGNUP_ENABLED=1 — see src/web_ui/config.py.
"""

import hashlib
import logging
import os
import secrets
import time
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.web_ui._json import _json_safe
from src.web_ui.auth import PASSWORD_MIN_LENGTH, hash_password, validate_password
from src.web_ui.config import (
    SIGNUP_ENABLED,  # noqa: F401 — kept as legacy monkeypatch target
    signup_enabled,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth")

# ---------------------------------------------------------------------------
# Password complexity constants
# ---------------------------------------------------------------------------
#
# WI-9 (ADR-0042): :data:`_MIN_PASSWORD_LENGTH` is a backwards-compat alias
# for :data:`src.web_ui.auth.PASSWORD_MIN_LENGTH` (the canonical SSOT).
# The constant is preserved for ``tests/test_constants_fallback.py`` which
# imports it by this name.  New code should import PASSWORD_MIN_LENGTH from
# ``src.web_ui.auth`` directly.
_MIN_PASSWORD_LENGTH = PASSWORD_MIN_LENGTH

# Email-verification token validity (hours).  Likewise admin-tunable via
# ``auth.email_verification_ttl_hours`` (see WI-9 / ADR-0042).
_EMAIL_VERIFY_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# hCaptcha verification
# ---------------------------------------------------------------------------

async def _verify_hcaptcha(token: str, remote_ip: str) -> bool:
    """Return True if hCaptcha response is valid.

    Skips verification in dev mode (HCAPTCHA_SECRET unset) — logs a warning
    so the operator knows captcha is disabled.
    """
    secret = os.getenv("HCAPTCHA_SECRET")
    if not secret:
        logger.warning("HCAPTCHA_SECRET unset — skipping captcha verification (dev mode)")
        return True
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.hcaptcha.com/siteverify",
                data={"secret": secret, "response": token, "remoteip": remote_ip},
            )
        return resp.json().get("success", False)
    except Exception as exc:
        logger.error("hCaptcha verification error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------
# Delegated to the shared helper in src.web_ui.auth so both register and
# reset-password use exactly the same policy (ADR-0042 / WI-1 fix).
# ``validate_password`` is already imported at the top of this module.
_validate_password = validate_password  # backwards-compat alias for tests


# ---------------------------------------------------------------------------
# DB helpers (use pool from pg module)
# ---------------------------------------------------------------------------

def _get_pool():
    from src.db.pg import get_pool
    return get_pool()


def _get_email_verify_ttl_hours() -> int:
    """Return the live email-verification token TTL in hours (WI-9).

    Reads ``auth.email_verification_ttl_hours`` through the settings overlay
    (DB > catalogue > module default).  Falls back to :data:`_EMAIL_VERIFY_TTL_HOURS`
    on any overlay failure so a DB outage never produces a zero-length window.
    """
    try:
        from src.settings import get_setting
        return int(get_setting("auth.email_verification_ttl_hours"))
    except Exception:
        return _EMAIL_VERIFY_TTL_HOURS


def _get_client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return (
        request.headers.get("x-real-ip")
        or forwarded
        or (request.client.host if request.client else "unknown")
    )


def _get_base_url(request: Request) -> str:
    """Infer public base URL from request headers (nginx sets X-Forwarded-Proto/Host)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto}://{host}"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RegisterBody(BaseModel):
    email: str
    username: str
    password: str
    confirm_password: str
    hcaptcha_token: str = ""
    # D4 consent gate: must be True to proceed (PDPL 91/2025 + card-network requirement).
    consent: bool = False


class VerifyEmailBody(BaseModel):
    token: str


class ResendBody(BaseModel):
    email: str


# ---------------------------------------------------------------------------
# POST /api/auth/register
# ---------------------------------------------------------------------------

@router.post("/register")
async def register(body: RegisterBody, request: Request):
    """Create an unverified user account and send a verification email.

    Returns 201 on success. Returns 409 if username/email already taken (generic
    message to prevent enumeration). Returns 400 for validation failures.
    Skips hCaptcha when HCAPTCHA_SECRET is unset (dev mode).
    Returns 403 when signup is disabled (invite-only mode, default).

    WI-RV F-A: gate is read via :func:`signup_enabled` which honours a live
    ``app_settings`` overlay row (set via PATCH
    /api/admin/settings/signup.enabled) BEFORE falling back to the
    ``SIGNUP_ENABLED`` import-time constant.  The constant is still imported
    so existing tests that monkeypatch
    ``src.web_ui.routes.signup.SIGNUP_ENABLED`` continue to work via the
    legacy path; new tests should patch ``src.web_ui.config.SIGNUP_ENABLED``
    (the source of truth) or write a DB overlay row.
    """
    if not signup_enabled():
        logger.warning(
            "Signup blocked: signup_enabled()=False (invite-only) — email=%s username=%s",
            body.email,
            body.username,
        )
        return JSONResponse(
            _json_safe({"error": "signup_disabled", "detail": "Public signup is not enabled."}),
            status_code=403,
        )

    # D4 consent gate — PDPL 91/2025 + card-network requirement.
    # Must be True; absence/False → 422 (client error, not auth error).
    if not body.consent:
        logger.warning(
            "Signup rejected: consent not given — email=%s username=%s",
            body.email,
            body.username,
        )
        return JSONResponse(
            _json_safe({
                "error": "consent_required",
                "detail": (
                    "You must agree to the Terms of Service and Privacy Policy"
                    " to create an account."
                ),
            }),
            status_code=422,
        )

    client_ip = _get_client_ip(request)

    # Basic field validation
    email = body.email.strip().lower()
    username = body.username.strip()

    if not email or "@" not in email:
        return JSONResponse(_json_safe({"error": "Invalid email address."}), status_code=400)
    if not username or len(username) < 2 or len(username) > 64:
        return JSONResponse(
            _json_safe({"error": "Username must be between 2 and 64 characters."}),
            status_code=400,
        )
    if body.password != body.confirm_password:
        return JSONResponse(_json_safe({"error": "Passwords do not match."}), status_code=400)

    pw_error = _validate_password(body.password)
    if pw_error:
        return JSONResponse(_json_safe({"error": pw_error}), status_code=400)

    # hCaptcha
    if not await _verify_hcaptcha(body.hcaptcha_token, client_ip):
        logger.warning("Signup rejected: invalid captcha (IP=%s email=%s)", client_ip, email)
        return JSONResponse(_json_safe({"error": "Captcha verification failed."}), status_code=400)

    pool = _get_pool()
    password_hash = hash_password(body.password)

    with pool.checkout() as conn:
        # Check uniqueness
        existing_user = pool.fetch_one(
            conn,
            "SELECT 1 FROM webui_users WHERE username = %s",
            (username,),
        )
        existing_email = pool.fetch_one(
            conn,
            "SELECT 1 FROM webui_users WHERE email = %s",
            (email,),
        )
        if existing_user or existing_email:
            logger.warning(
                "Signup rejected: duplicate username=%r email=%r (IP=%s)",
                username,
                email,
                client_ip,
            )
            return JSONResponse(
                _json_safe(
                    {
                        "error": (
                            "Email or username already registered. "
                            "If this is yours, try logging in."
                        )
                    }
                ),
                status_code=409,
            )

        # Insert unverified user; capture integer id for FK in email_verifications.
        # terms_accepted_at = NOW() records auditable proof-of-consent (D4 / m13_014_billing_p1 §7).
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO webui_users"
                    " (username, password_hash, email, email_verified, is_admin, terms_accepted_at)"
                    " VALUES (%s, %s, %s, FALSE, FALSE, NOW()) RETURNING id",
                    (username, password_hash, email),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    user_id: int = row[0]

    # Generate token + insert verification record.
    # Defense-in-depth (F10): raw token is emailed to user; only sha256(token)
    # is stored in DB so a DB leak cannot be used directly for account takeover.
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    # WI-9: TTL admin-tunable via auth.email_verification_ttl_hours.
    expires_at = datetime.now(UTC) + timedelta(hours=_get_email_verify_ttl_hours())

    with pool.checkout() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO email_verifications (token, user_id, purpose, expires_at)"
                    " VALUES (%s, %s, 'email_verify', %s)",
                    (token_hash, user_id, expires_at),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    # Send email with raw token (dev mode: logs token instead)
    base_url = _get_base_url(request)
    try:
        from src.web_ui.email import send_verification_email
        send_verification_email(to=email, username=username, token=raw_token, base_url=base_url)
    except Exception as exc:
        logger.error("Failed to send verification email to %s: %s", email, exc)
        # Non-fatal: user can use resend endpoint

    logger.info("Signup: user %r registered (IP=%s)", username, client_ip)
    return JSONResponse(_json_safe({"status": "verification_email_sent"}), status_code=201)


# ---------------------------------------------------------------------------
# POST /api/auth/verify-email
# ---------------------------------------------------------------------------

@router.post("/verify-email")
async def verify_email(body: VerifyEmailBody, request: Request):
    """Consume a verification token, mark user verified, and issue a session.

    Returns 200 + Set-Cookie on success.
    Returns 410 Gone if token is expired, invalid, or already used.
    """
    raw_token = body.token.strip()
    if not raw_token:
        return JSONResponse(_json_safe({"error": "Token required."}), status_code=400)

    # Hash the incoming raw token before DB lookup (mirroring password-reset pattern).
    # DB stores sha256(token) so a DB leak cannot be used directly for account takeover.
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    pool = _get_pool()

    with pool.checkout() as conn:
        conn.autocommit = False
        try:
            # Pessimistic lock on verification row
            row = pool.fetch_one(
                conn,
                "SELECT user_id, expires_at, used_at"
                " FROM email_verifications"
                " WHERE token = %s AND purpose = 'email_verify'"
                " FOR UPDATE",
                (token_hash,),
            )

            if row is None:
                conn.rollback()
                logger.warning("verify-email: unknown token (len=%d)", len(raw_token))
                return JSONResponse(_json_safe({"error": "expired_or_invalid"}), status_code=410)

            now = datetime.now(UTC)
            expires_at = row["expires_at"]
            # Ensure offset-aware comparison
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)

            if row["used_at"] is not None or expires_at < now:
                conn.rollback()
                logger.warning(
                    "verify-email: expired/used token for user=%r used_at=%s expires=%s",
                    row["user_id"],
                    row["used_at"],
                    expires_at,
                )
                return JSONResponse(_json_safe({"error": "expired_or_invalid"}), status_code=410)

            user_id = row["user_id"]  # integer FK

            # Resolve username for session (needed for auto-login)
            user_row = pool.fetch_one(
                conn,
                "SELECT username, email, is_admin FROM webui_users WHERE id = %s",
                (user_id,),
            )
            if user_row is None:
                conn.rollback()
                logger.error("verify-email: user id=%d not found", user_id)
                return JSONResponse(_json_safe({"error": "expired_or_invalid"}), status_code=410)

            username = user_row["username"]
            user_email = user_row["email"]

            # Mark verified + consume token in one transaction
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE webui_users SET email_verified = TRUE WHERE id = %s",
                    (user_id,),
                )
                cur.execute(
                    "UPDATE email_verifications SET used_at = NOW() WHERE token = %s",
                    (token_hash,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    # Auto-login: issue session cookie — F7: create active_sessions row so
    # admin-driven revoke_all_sessions / deactivate can kick this session.
    from src.web_ui.routes.login import _create_session

    client_ip = _get_client_ip(request)
    user_agent: str | None = request.headers.get("user-agent")
    try:
        session_id = _create_session(
            user_id=user_id,
            ip_address=client_ip,
            user_agent=user_agent,
        )
    except Exception as exc:
        logger.error("verify-email: could not create session for user %r: %s", username, exc)
        return JSONResponse(_json_safe({"error": "internal_error"}), status_code=500)

    request.session["session_id"] = session_id
    request.session["username"] = username
    request.session["user_id"] = user_id
    request.session["session_at"] = time.time()

    # Auto-mint a free-plan API key for the newly-verified user (WI-7).
    # Failure is non-fatal — auth must succeed even if onboarding-key minting
    # blows up.  The helper has its own internal try/except, but we also wrap
    # the call site defensively so that a failure at the boundary (helper
    # unavailable, import error, or any exception that escapes the helper)
    # can never turn a successful verification into a 500.
    try:
        from src.web_ui.routes.api_keys import _mint_default_api_key
        _mint_default_api_key(user_id, username)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "verify-email: default API key mint failed for user %r (id=%d): %s"
            " — continuing (non-fatal)",
            username,
            user_id,
            exc,
        )

    # Claim any unclaimed paid subscriptions for this now-verified email (WI-6).
    # Best-effort: never breaks the login flow.
    try:
        from src.billing.provisioning import claim_subscription_for_user
        claim_subscription_for_user(user_id, user_email)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "verify-email: claim_subscription_for_user failed for user %r (id=%d): %s"
            " — continuing (non-fatal)",
            username,
            user_id,
            exc,
        )

    logger.info("verify-email: user %r (id=%d) verified and logged in", username, user_id)
    return JSONResponse(
        _json_safe({"ok": True, "username": username, "is_admin": bool(user_row["is_admin"])})
    )


# ---------------------------------------------------------------------------
# POST /api/auth/resend-verification
# ---------------------------------------------------------------------------

@router.post("/resend-verification")
async def resend_verification(body: ResendBody, request: Request):
    """Resend a verification email. Rate-limited to 3 sends per hour per email.

    Returns 200 always (does not reveal whether email exists — prevents enumeration).
    Returns 429 if rate limit exceeded.
    Note: old tokens remain valid until their expiry; the new token is additive.
    """
    email = body.email.strip().lower()
    if not email or "@" not in email:
        return JSONResponse(_json_safe({"error": "Invalid email address."}), status_code=400)

    pool = _get_pool()

    with pool.checkout() as conn:
        user_row = pool.fetch_one(
            conn,
            "SELECT id, username FROM webui_users WHERE email = %s AND email_verified = FALSE",
            (email,),
        )
        if user_row is None:
            # User does not exist or is already verified — return 200 silently
            logger.info("resend-verification: no unverified user for email=%s", email)
            return JSONResponse(_json_safe({"status": "ok"}))

        user_id: int = user_row["id"]
        username = user_row["username"]

        # Rate limit: max 3 sends per hour per email (use integer user_id)
        count_row = pool.fetch_one(
            conn,
            "SELECT COUNT(*) AS cnt FROM email_verifications"
            " WHERE user_id = %s AND purpose = 'email_verify'"
            "   AND created_at > NOW() - INTERVAL '1 hour'",
            (user_id,),
        )
        if count_row and count_row["cnt"] >= 3:
            logger.warning("resend-verification: rate limit for email=%s", email)
            return JSONResponse(
                _json_safe({"error": "Too many verification emails sent. Try again later."}),
                status_code=429,
            )

    # Generate new token (old tokens remain valid until expiry).
    # Store sha256(token) in DB; send raw token to user (same pattern as register).
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    # WI-9: TTL admin-tunable via auth.email_verification_ttl_hours.
    expires_at = datetime.now(UTC) + timedelta(hours=_get_email_verify_ttl_hours())

    with pool.checkout() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO email_verifications (token, user_id, purpose, expires_at)"
                    " VALUES (%s, %s, 'email_verify', %s)",
                    (token_hash, user_id, expires_at),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    base_url = _get_base_url(request)
    try:
        from src.web_ui.email import send_verification_email
        send_verification_email(to=email, username=username, token=raw_token, base_url=base_url)
    except Exception as exc:
        logger.error("Failed to resend verification email to %s: %s", email, exc)

    logger.info("resend-verification: new token sent for email=%s", email)
    return JSONResponse(_json_safe({"status": "ok"}))
