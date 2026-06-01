# SPDX-License-Identifier: AGPL-3.0-or-later
"""Health check endpoints for MCP server.

Two endpoints are defined here:

* ``/health``  — **liveness** probe: ALWAYS fast and 200 if the event loop can
  serve the request.  Performs **no** database I/O (no Neo4j / PG pool
  checkout) and never scans large tables.  Under DB-pool exhaustion (the #227
  failure mode — an ~11 h silent wedge in production) a DB-coupled health check
  would falsely report 503 and trigger needless restarts; liveness must reflect
  only "the process is responsive".  All DB connectivity belongs to ``/ready``.

* ``/ready``   — **readiness** probe: includes the heavyweight embedding counts
  (``SELECT COUNT(*) FROM embeddings`` and the ``GROUP BY chunk_type``
  breakdown) that are too expensive to run on every liveness hit.  Results are
  cached in-memory for ``_READY_CACHE_TTL_S`` seconds so even a burst of
  probes only triggers one DB scan per TTL window.

Backward compat: ``/health`` still exposes ``embeddings_total`` and
``embeddings_by_chunk_type`` but the values are a **non-scanning peek** at the
shared ``/ready`` cache — populated only by ``/ready`` hits, ``None``/empty
until then, and **never** scanned (or fetched) on the liveness path.
"""
import asyncio
import importlib.metadata
import logging
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from src.constants import ERROR_MSG_MAX_CHARS

logger = logging.getLogger(__name__)

# Module-level version constant — computed once at import time, shared by all handlers.
try:
    _APP_VERSION: str = importlib.metadata.version("odoo-semantic-mcp")
except importlib.metadata.PackageNotFoundError:
    _APP_VERSION = "unknown"

# ---------------------------------------------------------------------------
# Readiness cache — shared between /ready and /health (backward compat)
# ---------------------------------------------------------------------------

# TTL for the heavy embedding-count cache (seconds).
_READY_CACHE_TTL_S: int = 60

# Cache state — module-level so it survives across requests within a process.
# Keys: embeddings_total (int|None), embeddings_by_chunk_type (dict|None), cached_at (float)
_ready_cache: dict[str, object] | None = None
_ready_cache_lock = asyncio.Lock()


async def _get_ready_data() -> dict[str, object]:
    """Return cached readiness data, refreshing if the TTL has expired.

    Returns a dict with keys:
        embeddings_total (int | None)
        embeddings_by_chunk_type (dict[str, int] | None)
        cached_at (float)           — time.monotonic() of last refresh

    The cache is process-local and in-memory.  A cold miss (or expired entry)
    triggers a single DB scan; concurrent callers that arrive during the scan
    all wait on ``_ready_cache_lock`` and then read the freshly-written entry.
    """
    global _ready_cache

    now = time.monotonic()

    # Fast path — cache is valid (check outside lock first to avoid contention)
    if (
        _ready_cache is not None
        and now - _ready_cache["cached_at"] < _READY_CACHE_TTL_S  # type: ignore[operator]
    ):
        return _ready_cache  # type: ignore[return-value]

    async with _ready_cache_lock:
        # Re-check under lock in case another coroutine refreshed while we waited
        now = time.monotonic()
        if (
            _ready_cache is not None
            and now - _ready_cache["cached_at"] < _READY_CACHE_TTL_S  # type: ignore[operator]
        ):
            return _ready_cache  # type: ignore[return-value]

        # Cache is missing or expired — refresh
        embeddings_total, embeddings_by_chunk_type = await asyncio.gather(
            _fetch_embeddings_total(),
            _fetch_embeddings_by_chunk_type(),
        )

        _ready_cache = {
            "embeddings_total": embeddings_total,
            "embeddings_by_chunk_type": embeddings_by_chunk_type,
            "cached_at": time.monotonic(),
        }
        return _ready_cache  # type: ignore[return-value]


def _peek_ready_cache() -> dict[str, object] | None:
    """Return the current readiness cache WITHOUT triggering a DB scan.

    Used by the liveness probe to surface last-known counts for backward
    compat without ever populating the cache (which would scan the 591 k-row
    ``embeddings`` table).  Returns ``None`` when no ``/ready`` hit has
    populated it yet, or the (possibly stale) cached dict otherwise.  Reading a
    module global is non-blocking and pool-independent — safe on the liveness
    hot path.
    """
    return _ready_cache


# ---------------------------------------------------------------------------
# Private DB fetch helpers (each called AT MOST once per TTL via _get_ready_data)
# ---------------------------------------------------------------------------


async def _fetch_embeddings_total() -> int | None:
    """Run ``SELECT COUNT(*) FROM embeddings`` once; return None on any error.

    Renamed from the old ``_get_embeddings_total`` — callers must go through
    ``_get_ready_data()`` so the result is always cached.
    """
    from src.mcp.server import _checkout_pg, _rls_read_tx

    try:
        def _count() -> int:
            with _checkout_pg() as conn:
                # RLS GAP1 (ADR-0034): admin sentinel (GUC '*') so COUNT(*)
                # sees all rows, not just the NULL-profile subset.
                with _rls_read_tx(conn, None):
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM embeddings")
                        row = cur.fetchone()
                        return row[0] if row else 0

        return await asyncio.to_thread(_count)
    except Exception as e:
        logger.debug("embeddings_total unavailable: %s", e)
        return None


async def _fetch_embeddings_by_chunk_type() -> dict[str, int] | None:
    """Run ``SELECT chunk_type, COUNT(*) … GROUP BY chunk_type``; return None on error.

    Renamed from the old ``_get_embeddings_by_chunk_type`` — callers must go
    through ``_get_ready_data()`` so the result is always cached.
    """
    from src.mcp.server import _checkout_pg, _rls_read_tx

    try:
        def _count_by_type() -> dict[str, int]:
            with _checkout_pg() as conn:
                # RLS GAP1 (ADR-0034): same admin-sentinel wrap as above.
                with _rls_read_tx(conn, None):
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT chunk_type, COUNT(*) FROM embeddings GROUP BY chunk_type"
                        )
                        rows = cur.fetchall()
                        return {row[0]: row[1] for row in rows} if rows else {}

        return await asyncio.to_thread(_count_by_type)
    except Exception as e:
        logger.debug("embeddings_by_chunk_type unavailable: %s", e)
        return None


# ---------------------------------------------------------------------------
# Backward-compat aliases used by existing tests
# ---------------------------------------------------------------------------


async def _get_embeddings_total() -> int | None:
    """Cached wrapper — backward-compat alias for tests that call this directly.

    Data comes from the shared TTL cache; the heavy DB scan runs at most once
    per ``_READY_CACHE_TTL_S`` seconds regardless of call frequency.
    """
    data = await _get_ready_data()
    return data["embeddings_total"]  # type: ignore[return-value]


async def _get_embeddings_by_chunk_type() -> dict[str, int] | None:
    """Cached wrapper — backward-compat alias for tests that call this directly.

    Data comes from the shared TTL cache; the heavy DB scan runs at most once
    per ``_READY_CACHE_TTL_S`` seconds regardless of call frequency.
    """
    data = await _get_ready_data()
    return data["embeddings_by_chunk_type"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# MCP tool-count helper (unchanged)
# ---------------------------------------------------------------------------


async def _get_mcp_tool_count() -> int:
    """Count registered MCP tools, with defensive fallback if introspection fails.

    Returns:
        int: Positive count of tools, or -1 if introspection failed.

    Approach:
        1. Try public API mcp.get_tools() (async, FastMCP 2.3+).
        2. Fallback to private _tool_manager._tools if public API unavailable or raises.
        3. Return -1 and log warning if both methods fail.
    """
    from src.mcp.server import mcp

    # Try public API first (FastMCP 2.3+)
    if hasattr(mcp, "get_tools") and callable(mcp.get_tools):
        try:
            tools_dict = await mcp.get_tools()
            if isinstance(tools_dict, dict):
                return len(tools_dict)
        except Exception as e:
            logger.warning(f"get_tools() call failed: {e}; falling back to private API")

    # Fallback: private API (FastMCP internals)
    try:
        if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
            return len(mcp._tool_manager._tools)
    except Exception as e:
        logger.warning(f"Private API introspection failed: {e}")

    logger.warning("No tool introspection method available (get_tools or _tool_manager)")
    return -1


# ---------------------------------------------------------------------------
# Timeout for individual liveness sub-checks
# ---------------------------------------------------------------------------

# Each DB sub-check in /health is wrapped with this timeout (seconds).
# Large enough to survive a transient hiccup; small enough that /health
# can never hang more than 2× this value (gather runs both in parallel).
_LIVENESS_CHECK_TIMEOUT_S: float = 5.0


async def _timed_check(thunk, *, timeout: float = _LIVENESS_CHECK_TIMEOUT_S) -> str:
    """Run *thunk* (a zero-arg callable) in a thread with a timeout.

    Returns ``'ok'`` on success, or ``'error:<msg>'`` on timeout / exception.
    The error message is capped at ``ERROR_MSG_MAX_CHARS`` characters.

    Both ``_check_neo4j`` and ``_check_pg`` delegate here so the wait_for +
    error-formatting boilerplate lives in a single place.
    """
    try:
        await asyncio.wait_for(asyncio.to_thread(thunk), timeout=timeout)
        return "ok"
    except TimeoutError:
        return f"error:timeout after {timeout}s"
    except Exception as e:
        return f"error:{str(e)[:ERROR_MSG_MAX_CHARS]}"


async def _check_neo4j() -> str:
    """Run Neo4j verify_connectivity with a timeout.  Returns 'ok' or 'error:...'."""
    from src.mcp.server import _get_driver

    return await _timed_check(_get_driver().verify_connectivity)


async def _check_pg() -> str:
    """Run PG ``SELECT 1`` with a timeout.  Returns 'ok' or 'error:...'."""
    from src.mcp.server import _checkout_pg

    def _ping():
        with _checkout_pg() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

    return await _timed_check(_ping)


# ---------------------------------------------------------------------------
# /health — liveness handler
# ---------------------------------------------------------------------------


async def health_handler(request: Request) -> JSONResponse:
    """Liveness probe: ALWAYS 200 if the event loop can serve the request.

    Performs **no** database I/O — no Neo4j / PG pool checkout, no table scan,
    no cache refresh.  This is deliberate: under DB-pool exhaustion (the #227
    production wedge) a DB-coupled liveness check reports 503 and provokes
    needless restarts.  Liveness here means only "the process is responsive";
    DB connectivity + readiness counts live on ``/ready``.

    ``embeddings_total`` / ``embeddings_by_chunk_type`` are surfaced for
    backward compat as a **non-scanning peek** at the ``/ready`` cache — they
    are ``None`` / empty until a ``/ready`` hit populates the cache.
    """
    tool_count = await _get_mcp_tool_count()  # in-memory FastMCP registry — no DB
    cached = _peek_ready_cache()  # non-blocking peek; never scans
    embeddings_total = cached["embeddings_total"] if cached else None
    embeddings_by_chunk_type = cached["embeddings_by_chunk_type"] if cached else None

    body = {
        "status": "alive",
        "version": _APP_VERSION,
        "mcp_tools": tool_count,
        # Non-scanning peek at the /ready cache — backward compat only.
        "embeddings_total": embeddings_total,
        "embeddings_by_chunk_type": (
            embeddings_by_chunk_type if embeddings_by_chunk_type is not None else {}
        ),
    }
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# /ready — readiness handler (heavy counts, cached)
# ---------------------------------------------------------------------------


async def ready_handler(request: Request) -> JSONResponse:
    """Readiness probe: includes heavy embedding counts from the TTL cache.

    Results are cached for ``_READY_CACHE_TTL_S`` seconds so a burst of
    probes only triggers one DB scan per TTL window.

    Route registration (WI-C / main agent): add to ``src/mcp/server.py``
    immediately after the ``/health`` registration::

        @mcp.custom_route("/ready", methods=["GET"])
        async def ready_check(request: Request):
            from src.mcp.health import ready_handler
            return await ready_handler(request)

    Until that line is added, this handler is callable directly (e.g. from
    tests or the web-UI layer) but is not reachable via HTTP.
    """
    neo4j_status, pg_status, ready_data, tool_count = await asyncio.gather(
        _check_neo4j(),
        _check_pg(),
        _get_ready_data(),
        _get_mcp_tool_count(),
    )

    both_ok = neo4j_status == "ok" and pg_status == "ok"
    one_ok = neo4j_status == "ok" or pg_status == "ok"
    status = "ok" if both_ok else ("degraded" if one_ok else "error")
    http_code = 503 if status == "error" else 200

    embeddings_total = ready_data["embeddings_total"]
    embeddings_by_chunk_type = ready_data["embeddings_by_chunk_type"]

    body = {
        "status": status,
        "neo4j": neo4j_status,
        "postgres": pg_status,
        "version": _APP_VERSION,
        "mcp_tools": tool_count,
        "embeddings_total": embeddings_total,
        "embeddings_by_chunk_type": (
            embeddings_by_chunk_type if embeddings_by_chunk_type is not None else {}
        ),
        "cache_ttl_s": _READY_CACHE_TTL_S,
        "cache_age_s": round(time.monotonic() - ready_data["cached_at"], 1),  # type: ignore[operator]
    }
    return JSONResponse(body, status_code=http_code)
