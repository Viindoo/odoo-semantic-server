"""API key authentication middleware for MCP server."""
import asyncio
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# In-memory LRU cache: raw_key -> (api_key_id | None, timestamp)
_KEY_CACHE: dict[str, int | None] = {}
_CACHE_TS: dict[str, float] = {}
_CACHE_TTL = 300.0  # 5 minutes

# Paths that bypass auth entirely
_PUBLIC_PATHS = frozenset({"/health"})


def _cache_get(raw_key: str) -> tuple[bool, int | None]:
    """Return (hit, api_key_id). hit=False means cache miss or expired."""
    ts = _CACHE_TS.get(raw_key)
    if ts is not None and time.monotonic() - ts < _CACHE_TTL:
        return True, _KEY_CACHE[raw_key]
    return False, None


def _cache_set(raw_key: str, key_id: int | None) -> None:
    """Store key_id for raw_key in the in-memory cache."""
    _KEY_CACHE[raw_key] = key_id
    _CACHE_TS[raw_key] = time.monotonic()


def _cache_invalidate(raw_key: str) -> None:
    """Remove a key from cache (call after deactivate)."""
    _KEY_CACHE.pop(raw_key, None)
    _CACHE_TS.pop(raw_key, None)


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

            conn = _get_pg_conn()
            key_id = await asyncio.to_thread(verify_api_key, conn, raw_key)
            _cache_set(raw_key, key_id)

        if key_id is None:
            return Response("Invalid or inactive API key", status_code=401)

        request.state.api_key_id = key_id
        start = time.monotonic()
        response = await call_next(request)
        ms = int((time.monotonic() - start) * 1000)

        # Fire-and-forget usage log — do not block response
        asyncio.create_task(_log_usage_async(key_id, request, ms))
        return response


async def _log_usage_async(key_id: int, request: Request, ms: int) -> None:
    """Log tool usage asynchronously — best-effort, never raises."""
    try:
        from src.db.auth_registry import log_usage
        from src.mcp.server import _get_pg_conn

        tool = request.headers.get("X-Tool-Name", "unknown")
        conn = _get_pg_conn()
        await asyncio.to_thread(log_usage, conn, key_id, tool, ms)
    except Exception:
        pass
