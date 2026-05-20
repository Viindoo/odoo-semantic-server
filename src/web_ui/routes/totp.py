# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/totp.py
"""TOTP MFA endpoints (M9 W-MF).

Routes (all require valid session):
  POST /api/auth/totp/setup    — generate secret, return QR + provisioning URI
  POST /api/auth/totp/verify   — verify code, enable TOTP, return backup codes
  POST /api/auth/totp/disable  — re-verify password + code, delete row

Design:
  * Secret stored Fernet-encrypted (same key as SSH private keys).
  * Backup codes: 10 × secrets.token_hex(8), HMAC-SHA256 hashed before store.
    Plaintext returned ONCE from /verify. Never persisted in plaintext.
  * valid_window=1 (±30 s drift tolerance).
  * WEBUI_SESSION_SECRET used as HMAC key for backup codes.
"""

import base64
import hashlib
import hmac
import io
import logging
import os
import secrets
from datetime import UTC, datetime

import pyotp
import qrcode
import qrcode.image.pil
from cryptography.fernet import Fernet
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.web_ui._json import _json_safe
from src.web_ui.auth import is_test_bypass_active, verify_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth/totp")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BACKUP_CODE_COUNT = 10
TOTP_VALID_WINDOW = 1  # ±1 step = ±30 s drift tolerance (per pyotp docs)
ISSUER_NAME = "Odoo Semantic MCP"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_fernet() -> Fernet:
    """Return a Fernet instance from FERNET_KEY env var.

    Raises RuntimeError if FERNET_KEY is not set (production guard).
    """
    key = os.environ.get("FERNET_KEY")
    if not key:
        raise RuntimeError("FERNET_KEY environment variable is not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _encrypt_secret(plaintext: str) -> str:
    """Encrypt TOTP base32 secret with Fernet. Returns base64-encoded ciphertext."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def _decrypt_secret(ciphertext: str) -> str:
    """Decrypt Fernet-encrypted TOTP secret. Returns plaintext base32 string."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


def _hmac_backup_code(code: str) -> str:
    """Return HMAC-SHA256 hex of a backup code using WEBUI_SESSION_SECRET."""
    session_secret = os.environ.get("WEBUI_SESSION_SECRET", "dev-fallback-secret")
    return hmac.new(
        session_secret.encode(), code.encode(), hashlib.sha256
    ).hexdigest()


def _generate_backup_codes() -> tuple[list[str], list[dict]]:
    """Generate 10 backup codes.

    Returns:
        (plaintext_list, hashed_list) — plaintext for one-time display,
        hashed_list for DB storage as JSONB array of {hash, used_at}.
    """
    plain = [secrets.token_hex(8) for _ in range(BACKUP_CODE_COUNT)]
    hashed = [{"hash": _hmac_backup_code(c), "used_at": None} for c in plain]
    return plain, hashed


def _make_qr_png_base64(provisioning_uri: str) -> str:
    """Render a TOTP provisioning URI to a QR code PNG and return base64."""
    img = qrcode.make(provisioning_uri, image_factory=qrcode.image.pil.PilImage)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _resolve_user_id(request: Request) -> int | None:
    """Return the integer user_id from the session, or None if not found."""
    from src.db.pg import auth_store

    username = request.session.get("username")
    if not username:
        return None
    pool = auth_store()._pool
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM webui_users WHERE username = %s", (username,))
            row = cur.fetchone()
    if row is None:
        return None
    return row[0]


def _get_totp_row(user_id: int) -> dict | None:
    """Return the totp_secrets row for user_id, or None."""
    from src.db.pg import auth_store

    pool = auth_store()._pool
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, secret_encrypted, enabled, backup_codes_hash, last_used_at "
                "FROM totp_secrets WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "user_id": row[0],
        "secret_encrypted": row[1],
        "enabled": row[2],
        "backup_codes_hash": row[3],
        "last_used_at": row[4],
    }


def _upsert_totp_secret(user_id: int, secret_encrypted: str) -> None:
    """INSERT or UPDATE totp_secrets row with enabled=FALSE (enrollment pending)."""
    from src.db.pg import auth_store

    pool = auth_store()._pool
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO totp_secrets (user_id, secret_encrypted, enabled, backup_codes_hash)
                VALUES (%s, %s, FALSE, '[]'::jsonb)
                ON CONFLICT (user_id) DO UPDATE
                    SET secret_encrypted = EXCLUDED.secret_encrypted,
                        enabled = FALSE,
                        backup_codes_hash = '[]'::jsonb,
                        enrolled_at = NOW(),
                        last_used_at = NULL
                """,
                (user_id, secret_encrypted),
            )
        conn.commit()


def _enable_totp(user_id: int, backup_codes_hashed: list[dict]) -> None:
    """Mark TOTP as enabled and store hashed backup codes."""
    import json

    from src.db.pg import auth_store

    pool = auth_store()._pool
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE totp_secrets
                SET enabled = TRUE,
                    backup_codes_hash = %s::jsonb
                WHERE user_id = %s
                """,
                (json.dumps(backup_codes_hashed), user_id),
            )
            cur.execute(
                "UPDATE webui_users SET mfa_enabled = TRUE WHERE id = %s",
                (user_id,),
            )
        conn.commit()


def _delete_totp(user_id: int) -> None:
    """Delete the totp_secrets row for user_id."""
    from src.db.pg import auth_store

    pool = auth_store()._pool
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM totp_secrets WHERE user_id = %s", (user_id,))
            cur.execute(
                "UPDATE webui_users SET mfa_enabled = FALSE WHERE id = %s",
                (user_id,),
            )
        conn.commit()


def _update_backup_codes(user_id: int, backup_codes_hashed: list[dict]) -> None:
    """Update backup_codes_hash in DB after a code is used."""
    import json

    from src.db.pg import auth_store

    pool = auth_store()._pool
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE totp_secrets SET backup_codes_hash = %s::jsonb WHERE user_id = %s",
                (json.dumps(backup_codes_hashed), user_id),
            )
        conn.commit()


def _check_backup_code(code: str, stored: list[dict]) -> tuple[bool, list[dict]]:
    """Check if a backup code is valid (unused + HMAC matches).

    Returns (valid, updated_list) where updated_list has used_at set on the
    matched entry. If invalid, returns (False, stored) unchanged.
    """
    code_hash = _hmac_backup_code(code.strip())
    updated = []
    found = False
    for entry in stored:
        if not found and entry["used_at"] is None and hmac.compare_digest(
            entry["hash"], code_hash
        ):
            found = True
            updated.append({"hash": entry["hash"], "used_at": datetime.now(tz=UTC).isoformat()})
        else:
            updated.append(entry)
    return found, updated


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class VerifyBody(BaseModel):
    code: str


class DisableBody(BaseModel):
    password: str
    code: str


class MfaLoginBody(BaseModel):
    mfa_token: str
    code: str | None = None
    backup_code: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/setup")
async def totp_setup(request: Request):
    """Generate a TOTP secret, encrypt it, return QR code + provisioning URI.

    Marks any existing row as not-enabled (re-enrollment resets everything).
    Does NOT enable TOTP — user must call /verify to confirm.
    """
    if not is_test_bypass_active() and not request.session.get("username"):
        return JSONResponse(_json_safe({"error": "not_authenticated"}), status_code=401)

    user_id = _resolve_user_id(request)
    if user_id is None:
        return JSONResponse(_json_safe({"error": "user_not_found"}), status_code=404)

    username = request.session.get("username", "user")

    # Generate TOTP secret (RFC 4648 base32)
    secret = pyotp.random_base32()
    secret_encrypted = _encrypt_secret(secret)

    # Persist as pending (enabled=FALSE)
    _upsert_totp_secret(user_id, secret_encrypted)

    # Build provisioning URI + QR code
    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=username, issuer_name=ISSUER_NAME)
    qr_png_base64 = _make_qr_png_base64(provisioning_uri)

    logger.info("TOTP setup initiated for user_id=%d", user_id)
    return JSONResponse(
        _json_safe(
            {
                "secret": secret,
                "provisioning_uri": provisioning_uri,
                "qr_png_base64": qr_png_base64,
            }
        )
    )


@router.post("/verify")
async def totp_verify(body: VerifyBody, request: Request):
    """Verify TOTP code from authenticator app, enable TOTP, return backup codes.

    Backup codes are returned ONCE here — never retrievable again.
    """
    if not is_test_bypass_active() and not request.session.get("username"):
        return JSONResponse(_json_safe({"error": "not_authenticated"}), status_code=401)

    user_id = _resolve_user_id(request)
    if user_id is None:
        return JSONResponse(_json_safe({"error": "user_not_found"}), status_code=404)

    row = _get_totp_row(user_id)
    if row is None:
        return JSONResponse(_json_safe({"error": "totp_not_setup"}), status_code=400)

    secret = _decrypt_secret(row["secret_encrypted"])
    totp = pyotp.TOTP(secret)

    if not totp.verify(body.code.strip(), valid_window=TOTP_VALID_WINDOW):
        return JSONResponse(_json_safe({"error": "invalid_code"}), status_code=400)

    # Code valid — generate backup codes and enable
    plain_codes, hashed_codes = _generate_backup_codes()
    _enable_totp(user_id, hashed_codes)

    logger.info("TOTP enabled for user_id=%d", user_id)
    return JSONResponse(
        _json_safe(
            {
                "ok": True,
                "backup_codes": plain_codes,
                "message": "TOTP enabled. Save these backup codes — they will not be shown again.",
            }
        )
    )


@router.post("/disable")
async def totp_disable(body: DisableBody, request: Request):
    """Disable TOTP for the current user.

    Requires both current password AND a valid TOTP code (prevents CSRF
    from disabling MFA with just a stolen session cookie).
    """
    if not is_test_bypass_active() and not request.session.get("username"):
        return JSONResponse(_json_safe({"error": "not_authenticated"}), status_code=401)

    user_id = _resolve_user_id(request)
    if user_id is None:
        return JSONResponse(_json_safe({"error": "user_not_found"}), status_code=404)

    # Re-verify password
    from src.db.pg import auth_store

    pool = auth_store()._pool
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT password_hash FROM webui_users WHERE id = %s", (user_id,)
            )
            pw_row = cur.fetchone()

    if pw_row is None or not verify_password(body.password, pw_row[0]):
        return JSONResponse(_json_safe({"error": "invalid_credentials"}), status_code=401)

    # Verify TOTP code
    row = _get_totp_row(user_id)
    if row is None or not row["enabled"]:
        return JSONResponse(_json_safe({"error": "totp_not_enabled"}), status_code=400)

    secret = _decrypt_secret(row["secret_encrypted"])
    totp = pyotp.TOTP(secret)

    if not totp.verify(body.code.strip(), valid_window=TOTP_VALID_WINDOW):
        return JSONResponse(_json_safe({"error": "invalid_code"}), status_code=400)

    _delete_totp(user_id)
    logger.info("TOTP disabled for user_id=%d", user_id)
    return JSONResponse(_json_safe({"ok": True, "message": "TOTP has been disabled."}))


# ---------------------------------------------------------------------------
# Public helper: check TOTP status (used by security page)
# ---------------------------------------------------------------------------


@router.get("/status")
async def totp_status(request: Request):
    """Return current TOTP enrollment status for the logged-in user."""
    if not is_test_bypass_active() and not request.session.get("username"):
        return JSONResponse(_json_safe({"error": "not_authenticated"}), status_code=401)

    user_id = _resolve_user_id(request)
    if user_id is None:
        return JSONResponse(_json_safe({"error": "user_not_found"}), status_code=404)

    row = _get_totp_row(user_id)
    return JSONResponse(
        _json_safe(
            {
                "enabled": row is not None and row["enabled"],
                "enrolled": row is not None,
            }
        )
    )


# ---------------------------------------------------------------------------
# MFA login endpoint (second step after password)
# ---------------------------------------------------------------------------


@router.post("/login")
async def totp_login(body: MfaLoginBody, request: Request):
    """Second-factor login: verify mfa_token + TOTP code (or backup code).

    Called after /api/auth/login returns {mfa_required: true, mfa_token: ...}.
    On success: promotes the pending session to a full session (sets username +
    session_at in cookie, matching the existing W-AC session cookie format).

    mfa_token is a signed string: "<user_id>:<expires_epoch>" signed via HMAC-SHA256
    with WEBUI_SESSION_SECRET as key.
    """
    import time

    # Validate mfa_token
    session_secret = os.environ.get("WEBUI_SESSION_SECRET", "dev-fallback-secret")
    try:
        payload, sig = body.mfa_token.rsplit(".", 1)
        expected_sig = hmac.new(
            session_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return JSONResponse(_json_safe({"error": "invalid_mfa_token"}), status_code=401)
        user_id_str, expires_str = payload.split(":", 1)
        user_id = int(user_id_str)
        expires_at = float(expires_str)
    except Exception:
        return JSONResponse(_json_safe({"error": "invalid_mfa_token"}), status_code=401)

    if time.time() > expires_at:
        return JSONResponse(_json_safe({"error": "mfa_token_expired"}), status_code=401)

    # Fetch user info
    from src.db.pg import auth_store

    pool = auth_store()._pool
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username FROM webui_users WHERE id = %s", (user_id,)
            )
            user_row = cur.fetchone()

    if user_row is None:
        return JSONResponse(_json_safe({"error": "user_not_found"}), status_code=404)

    username = user_row[0]

    # Verify TOTP code or backup code
    row = _get_totp_row(user_id)
    if row is None or not row["enabled"]:
        return JSONResponse(_json_safe({"error": "totp_not_enabled"}), status_code=400)

    secret = _decrypt_secret(row["secret_encrypted"])
    totp = pyotp.TOTP(secret)

    if body.code is not None:
        if not totp.verify(body.code.strip(), valid_window=TOTP_VALID_WINDOW):
            return JSONResponse(_json_safe({"error": "invalid_code"}), status_code=401)
    elif body.backup_code is not None:
        stored = row["backup_codes_hash"]
        if isinstance(stored, str):
            import json
            stored = json.loads(stored)
        valid, updated = _check_backup_code(body.backup_code, stored)
        if not valid:
            return JSONResponse(_json_safe({"error": "invalid_backup_code"}), status_code=401)
        _update_backup_codes(user_id, updated)
    else:
        return JSONResponse(_json_safe({"error": "code_or_backup_code_required"}), status_code=400)

    # Promote to full session — F7: create active_sessions row so server-side
    # revoke (revoke_all_sessions / deactivate) can kick this session immediately.
    from src.web_ui.routes.login import _create_session

    client_ip: str = (
        request.headers.get("x-real-ip")
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    user_agent: str | None = request.headers.get("user-agent")
    try:
        session_id = _create_session(
            user_id=user_id,
            ip_address=client_ip,
            user_agent=user_agent,
        )
    except Exception as exc:
        logger.error("totp_login: could not create session: %s", exc)
        return JSONResponse(_json_safe({"error": "internal_error"}), status_code=500)

    request.session["session_id"] = session_id
    request.session["username"] = username
    request.session["user_id"] = user_id
    request.session["session_at"] = time.time()
    logger.info("MFA login success for user_id=%d (%r)", user_id, username)
    return JSONResponse(_json_safe({"ok": True, "username": username}))


# ---------------------------------------------------------------------------
# Helper to build a signed MFA token (used by login.py)
# ---------------------------------------------------------------------------


def create_mfa_token(user_id: int, ttl_seconds: int = 300) -> str:
    """Create a signed short-lived MFA token.

    Format: "<user_id>:<expires_epoch>.<hmac_hex>"
    TTL default: 5 minutes.
    """
    import time

    session_secret = os.environ.get("WEBUI_SESSION_SECRET", "dev-fallback-secret")
    expires_at = time.time() + ttl_seconds
    payload = f"{user_id}:{expires_at}"
    sig = hmac.new(session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"
