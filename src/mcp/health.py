"""Health check endpoint for MCP server."""
import asyncio
import importlib.metadata
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from src.constants import ERROR_MSG_MAX_CHARS

logger = logging.getLogger(__name__)


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


async def _get_embeddings_total() -> int | None:
    """Count total rows in the embeddings table.

    Returns:
        int:  Row count on success.
        None: When pgvector is absent, connection fails, or table doesn't exist.

    Defensive pattern mirrors the pg_status check in health_handler — any
    exception produces None rather than propagating (keeps /health always live).
    """
    from src.mcp.server import _checkout_pg

    try:
        def _count() -> int:
            with _checkout_pg() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM embeddings")
                    row = cur.fetchone()
                    return row[0] if row else 0

        return await asyncio.to_thread(_count)
    except Exception as e:
        logger.debug("embeddings_total unavailable: %s", e)
        return None


async def health_handler(request: Request) -> JSONResponse:
    """Check health of Neo4j and PostgreSQL connections, return status + version."""
    from src.mcp.server import _checkout_pg, _get_driver

    neo4j_status = "ok"
    try:
        # verify_connectivity is synchronous — run in thread to avoid blocking event loop
        await asyncio.to_thread(_get_driver().verify_connectivity)
    except Exception as e:
        neo4j_status = f"error:{str(e)[:ERROR_MSG_MAX_CHARS]}"

    pg_status = "ok"
    try:
        def _check_pg():
            with _checkout_pg() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")

        await asyncio.to_thread(_check_pg)
    except Exception as e:
        pg_status = f"error:{str(e)[:ERROR_MSG_MAX_CHARS]}"

    tool_count = await _get_mcp_tool_count()
    embeddings_total = await _get_embeddings_total()

    both_ok = neo4j_status == "ok" and pg_status == "ok"
    one_ok = neo4j_status == "ok" or pg_status == "ok"
    status = "ok" if both_ok else ("degraded" if one_ok else "error")
    http_code = 503 if status == "error" else 200

    try:
        version = importlib.metadata.version("odoo-semantic-mcp")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    body = {
        "status": status,
        "neo4j": neo4j_status,
        "postgres": pg_status,
        "version": version,
        "mcp_tools": tool_count,
        "embeddings_total": embeddings_total,
    }
    return JSONResponse(body, status_code=http_code)
