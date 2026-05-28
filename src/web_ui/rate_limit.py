# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/rate_limit.py
"""Generic per-IP sliding-window rate limiter for Web UI public endpoints.

Extracted from the mcp/middleware.py _check_rate_limit pattern, adapted for
per-IP keying (public endpoints have no API key) instead of per-api_key_id.

Usage:
    from src.web_ui.rate_limit import check_ip_rate_limit, get_client_ip

    client_ip = await get_client_ip(request)
    allowed = await check_ip_rate_limit(client_ip)
    if not allowed:
        return JSONResponse({"error": "rate_limited"}, status_code=429)

Thread-safety: all mutations to _per_ip_buckets are serialised via _lock
(asyncio.Lock). This is safe for single-process async servers (FastAPI/uvicorn
with a single event loop). For multi-process deployments a Redis-backed
limiter would be needed — deferred to M10B P1.
"""

import asyncio
import logging
import time
from collections import deque

_logger = logging.getLogger(__name__)

# Module-level state: per-IP deques of monotonic timestamps.
# Key: str client IP, Value: deque of float (monotonic timestamps within window).
_per_ip_buckets: dict[str, deque] = {}

# Monotonic time of last prune pass for stale IP eviction.
_last_prune: float = 0.0

# asyncio.Lock — safe within a single event loop; must NOT be shared across threads.
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazily create the asyncio.Lock (avoids issues with module import before loop start)."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _prune_stale(window_seconds: int) -> None:
    """Remove IP buckets idle for longer than 2x window — keeps memory bounded.

    Called opportunistically on every check_ip_rate_limit call; actual prune
    only runs when at least 2*window_seconds have elapsed since last run.
    """
    global _last_prune
    now = time.monotonic()
    stale_threshold = 2 * window_seconds
    if now - _last_prune < stale_threshold:
        return
    _last_prune = now
    cutoff = now - stale_threshold
    stale_ips = [ip for ip, bucket in _per_ip_buckets.items()
                 if not bucket or bucket[-1] < cutoff]
    for ip in stale_ips:
        _per_ip_buckets.pop(ip, None)
    if stale_ips:
        _logger.debug("rate_limit: pruned %d stale IP bucket(s)", len(stale_ips))


async def check_ip_rate_limit(
    client_ip: str,
    *,
    limit: int = 5,
    window_seconds: int = 60,
) -> bool:
    """Sliding-window rate check. Returns True if request is within limit.

    Args:
        client_ip:      The resolved client IP string (use get_client_ip()).
        limit:          Max requests allowed within the window (default 5).
        window_seconds: Rolling window duration in seconds (default 60).

    Returns:
        True  — request allowed (bucket updated).
        False — rate limit exceeded (bucket NOT updated).
    """
    now = time.monotonic()
    lock = _get_lock()
    async with lock:
        await _prune_stale(window_seconds)
        bucket = _per_ip_buckets.setdefault(client_ip, deque())
        # Evict timestamps outside the rolling window.
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            _logger.debug(
                "rate_limit: IP %s exceeded %d req/%ds",
                client_ip, limit, window_seconds,
            )
            return False
        bucket.append(now)
        return True


async def get_client_ip(request) -> str:
    """Resolve the best-available client IP from request headers.

    Prefers X-Forwarded-For (first item, set by nginx) over X-Real-IP over
    the raw ASGI remote address. Returns 'unknown' if all sources are absent.

    Args:
        request: Starlette/FastAPI Request object.

    Returns:
        IP string, e.g. '203.0.113.42' or '::1'.
    """
    xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if xff:
        return xff
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    if request.client:
        return request.client.host
    return "unknown"
