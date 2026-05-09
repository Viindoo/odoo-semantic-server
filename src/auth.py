"""API key utilities — hashing and generation."""
import functools
import hashlib
import secrets


def generate_api_key() -> tuple[str, str]:
    """Generate new API key. Return (raw_key, key_hash).

    raw_key: shown to user once (osm_ prefix + 32-byte urlsafe token)
    key_hash: SHA-256 hex stored in DB
    """
    raw = "osm_" + secrets.token_urlsafe(32)
    return raw, hash_key(raw)


def hash_key(raw: str) -> str:
    """Return SHA-256 hex digest of raw API key."""
    return hashlib.sha256(raw.encode()).hexdigest()


@functools.lru_cache(maxsize=256)
def _cached_hash(raw: str) -> str:
    """Cached hash for hot path (verify calls). Same result as hash_key."""
    return hash_key(raw)
