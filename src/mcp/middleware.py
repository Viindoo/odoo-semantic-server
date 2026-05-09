"""API key authentication middleware for MCP server."""
import asyncio
import threading
import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.auth import hash_key as _hash_key

# In-memory cache: hash(raw_key) -> (api_key_id | None, timestamp)
# Keys stored as SHA-256 hashes — never plaintext in RAM (I2).
_KEY_CACHE: dict[str, int | None] = {}
_CACHE_TS: dict[str, float] = {}
_CACHE_TTL = 300.0  # 5 minutes

# ---------------------------------------------------------------------------
# Per-API-key sliding-window rate limiter (WI-8)
# ---------------------------------------------------------------------------
# Maps api_key_id → deque of monotonic timestamps for requests in the last 60s.
_rate_buckets: dict[int, deque] = {}
_rate_lock = threading.Lock()


def _check_rate_limit(api_key_id: int, limit_rpm: int = 120) -> tuple[bool, int]:
    """Sliding-window rate limiter. Returns (allowed, remaining).

    Thread-safe: guarded by _rate_lock.

    Args:
        api_key_id: The authenticated API key id.
        limit_rpm:  Maximum requests per 60-second window (default: 120).

    Returns:
        (True, remaining)  if the request is within the limit.
        (False, 0)         if the window is exhausted.
    """
    now = time.monotonic()
    window = 60.0
    with _rate_lock:
        bucket = _rate_buckets.setdefault(api_key_id, deque())
        # Prune timestamps older than the window
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        remaining = max(0, limit_rpm - len(bucket))
        if len(bucket) >= limit_rpm:
            return False, 0
        bucket.append(now)
        return True, remaining - 1

# Strong references to background tasks prevent GC-before-completion (B3).
_BG_TASKS: set[asyncio.Task] = set()

# Serialise all psycopg2 calls that run inside asyncio.to_thread (B2).
# psycopg2 connections are not thread-safe; this lock ensures only one
# thread uses _pg_conn at a time. Acceptable for <30 concurrent users.
_PG_LOCK = threading.Lock()

# Paths that bypass auth entirely
_PUBLIC_PATHS = frozenset({"/health"})


def _cache_get(raw_key: str) -> tuple[bool, int | None]:
    """Return (hit, api_key_id). hit=False means cache miss or expired."""
    h = _hash_key(raw_key)
    ts = _CACHE_TS.get(h)
    if ts is not None and time.monotonic() - ts < _CACHE_TTL:
        return True, _KEY_CACHE[h]
    return False, None


def _cache_set(raw_key: str, key_id: int | None) -> None:
    """Store key_id for raw_key (stored as hash) in the in-memory cache."""
    h = _hash_key(raw_key)
    _KEY_CACHE[h] = key_id
    _CACHE_TS[h] = time.monotonic()


def _cache_invalidate(raw_key: str) -> None:
    """Remove a key from cache (call after deactivate with raw_key known)."""
    h = _hash_key(raw_key)
    _KEY_CACHE.pop(h, None)
    _CACHE_TS.pop(h, None)


def _cache_invalidate_by_key_id(key_id: int) -> None:
    """Remove all cache entries mapping to key_id (call after deactivate).

    Used when only key_id is available (e.g. Web UI deactivate route).
    O(n) scan is fine — cache holds at most a few hundred entries.
    Works in-process; cross-process invalidation is bounded by _CACHE_TTL.
    """
    stale = [h for h, v in _KEY_CACHE.items() if v == key_id]
    for h in stale:
        _KEY_CACHE.pop(h, None)
        _CACHE_TS.pop(h, None)


class AuthMiddleware(BaseHTTPMiddleware):
    """Verify X-API-Key header on every request except public paths."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Public paths bypass auth
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        raw_key = request.headers.get("X-API-Key")
        if not raw_key:
            return Response("Missing X-API-Key header", status_code=401)

        # Check cache first to avoid DB round-trip per request
        hit, key_id = _cache_get(raw_key)
        if not hit:
            from src.db.auth_registry import verify_api_key
            from src.mcp.server import _get_pg_conn

            def _do_verify():
                with _PG_LOCK:
                    return verify_api_key(_get_pg_conn(), raw_key)

            key_id = await asyncio.to_thread(_do_verify)
            _cache_set(raw_key, key_id)

        if key_id is None:
            return Response("Invalid or inactive API key", status_code=401)

        # Rate limiting — applied after successful auth (WI-8)
        from src import config as _config
        limit_rpm = int(_config.get("auth", "rate_limit_rpm", fallback="120"))
        allowed, remaining = _check_rate_limit(key_id, limit_rpm)
        if not allowed:
            return Response(
                "Rate limit exceeded — try again in 60 seconds",
                status_code=429,
                headers={"X-RateLimit-Remaining": "0"},
            )

        request.state.api_key_id = key_id
        start = time.monotonic()
        response = await call_next(request)
        ms = int((time.monotonic() - start) * 1000)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        # Fire-and-forget usage log — hold strong ref to prevent GC (B3)
        task = asyncio.create_task(_log_usage_async(key_id, request, ms))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        return response


async def _log_usage_async(key_id: int, request: Request, ms: int) -> None:
    """Log tool usage asynchronously — best-effort, never raises."""
    try:
        from src.db.auth_registry import log_usage
        from src.mcp.server import _get_pg_conn

        tool = request.headers.get("X-Tool-Name", "unknown")

        def _do_log():
            with _PG_LOCK:
                log_usage(_get_pg_conn(), key_id, tool, ms)

        await asyncio.to_thread(_do_log)
    except Exception:
        pass
