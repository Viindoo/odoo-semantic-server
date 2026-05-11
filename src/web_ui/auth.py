# src/web_ui/auth.py
"""Password hashing + session secret for Web UI auth (M7 W16).

Usage:
    hash_password(pw)        → bcrypt hash (cost=12)
    verify_password(pw, h)   → bool
    get_session_secret()     → 32-byte hex string (from env or dev fallback)

Session middleware:
    Use starlette.middleware.sessions.SessionMiddleware with the secret returned
    by get_session_secret(). TTL enforced by storing "session_at" epoch in the
    session dict and checking inside AuthRequiredMiddleware.
"""

import logging
import os
import secrets

import bcrypt

logger = logging.getLogger(__name__)

# Session TTL: 8 hours in seconds
SESSION_TTL_SECONDS = 8 * 3600

_DEV_FALLBACK_SECRET: str | None = None


def get_session_secret() -> str:
    """Return WEBUI_SESSION_SECRET or a generated dev-only fallback.

    Production: set WEBUI_SESSION_SECRET to a 32-byte random hex string in webui.env.
    Dev: if env var unset, a random secret is generated per process start (sessions
    invalidated on restart — acceptable for dev).
    """
    secret = os.environ.get("WEBUI_SESSION_SECRET")
    if secret:
        return secret

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
