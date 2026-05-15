"""API key utilities — HMAC-SHA256 hashing and generation.

HMAC key source: WEBUI_SESSION_SECRET environment variable.
If unset in production, key generation is aborted with ValueError.
In dev/test (WEBUI_SESSION_SECRET absent), a process-local fallback is used
with a warning — same behaviour as web_ui/auth.py::get_session_secret().

SHA-256 legacy fallback (backward compatibility):
  Keys created before M9 are hashed with plain SHA-256.  verify_api_key() in
  auth_registry.py tries HMAC first, then falls back to SHA-256 with a warning.
  The fallback expires on LEGACY_HASH_DEADLINE — after that date, SHA-256 keys
  should be rotated by the admin.
"""
import hashlib
import hmac
import logging
import os
import secrets

logger = logging.getLogger(__name__)

# Deadline after which legacy SHA-256 keys should be rotated.
LEGACY_HASH_DEADLINE = "2026-06-15"

_DEV_FALLBACK_SECRET: bytes | None = None


def _get_hmac_secret() -> bytes:
    """Return HMAC secret bytes from WEBUI_SESSION_SECRET env var.

    Returns:
        Secret bytes for HMAC computation.

    Raises:
        ValueError: If WEBUI_SESSION_SECRET is unset and we're in a context
            where a production-quality secret is required (i.e. not dev/test).
    """
    secret_str = os.environ.get("WEBUI_SESSION_SECRET", "")
    if secret_str:
        return secret_str.encode()

    # Dev/test: generate a process-local fallback (never use in production).
    global _DEV_FALLBACK_SECRET
    if _DEV_FALLBACK_SECRET is None:
        _DEV_FALLBACK_SECRET = secrets.token_bytes(32)
        logger.warning(
            "WEBUI_SESSION_SECRET not set — using a generated dev-only HMAC secret. "
            "API keys created now will not verify after process restart. "
            "Set WEBUI_SESSION_SECRET=<32-byte-hex> in webui.env for production."
        )
    return _DEV_FALLBACK_SECRET


def hash_key(raw: str) -> str:
    """Return HMAC-SHA256 hex digest of raw API key, keyed with WEBUI_SESSION_SECRET.

    This replaces the previous SHA-256 (unkeyed) hash.  HMAC prevents offline
    brute-force attacks on the stored hash even if the database is leaked.

    Args:
        raw: The full raw API key string (e.g. 'osm_abc...').

    Returns:
        64-character lowercase hex string (HMAC-SHA256).
    """
    secret = _get_hmac_secret()
    return hmac.new(secret, raw.encode(), "sha256").hexdigest()


def hash_key_legacy_sha256(raw: str) -> str:
    """Return plain SHA-256 hex digest (legacy, pre-M9 keys).

    Used only in the backward-compatibility fallback path in verify_api_key().
    Do NOT use for new keys.

    Args:
        raw: The full raw API key string.

    Returns:
        64-character lowercase hex string (SHA-256).
    """
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Generate new API key. Return (raw_key, key_hash).

    raw_key: shown to user once (osm_ prefix + 32-byte urlsafe token).
    key_hash: HMAC-SHA256 hex stored in DB.

    Returns:
        Tuple of (raw_key_string, hmac_sha256_hash_string).
    """
    raw = "osm_" + secrets.token_urlsafe(32)
    return raw, hash_key(raw)
