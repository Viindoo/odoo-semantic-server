"""Health check endpoint for MCP server."""
import asyncio
import importlib.metadata
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


async def _get_mcp_tool_count() -> int:
    """Count registered MCP tools, with defensive fallback if introspection fails.

    Returns:
        int: Positive count of tools, or -1 if introspection failed.

    Approach:
        1. Try public API mcp.get_tools() (async).
        2. Fallback to private _tool_manager._tools if public API unavailable.
        3. Return -1 and log warning on any exception.
    """
    from src.mcp.server import mcp

    try:
        # Prefer public API (FastMCP 2.3+)
        if hasattr(mcp, "get_tools") and callable(mcp.get_tools):
            try:
                tools_dict = await mcp.get_tools()
                return len(tools_dict) if isinstance(tools_dict, dict) else -1
            except Exception as e:
                logger.warning(f"get_tools() call failed: {e}")
                raise  # Fall through to private API below

        # Fallback: private API (FastMCP internals)
        if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
            return len(mcp._tool_manager._tools)

        logger.warning("No tool introspection method available (get_tools or _tool_manager)")
        return -1

    except Exception as e:
        logger.warning(f"Tool count introspection failed: {type(e).__name__}: {str(e)[:100]}")
        return -1


async def health_handler(request: Request) -> JSONResponse:
    """Check health of Neo4j and PostgreSQL connections, return status + version."""
    from src.mcp.middleware import _PG_LOCK
    from src.mcp.server import _get_driver, _get_pg_conn

    neo4j_status = "ok"
    try:
        # verify_connectivity is synchronous — run in thread to avoid blocking event loop
        await asyncio.to_thread(_get_driver().verify_connectivity)
    except Exception as e:
        neo4j_status = f"error:{str(e)[:100]}"

    pg_status = "ok"
    try:
        def _check_pg():
            with _PG_LOCK:  # B2: serialise with auth middleware DB calls
                conn = _get_pg_conn()
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")

        await asyncio.to_thread(_check_pg)
    except Exception as e:
        pg_status = f"error:{str(e)[:100]}"

    tool_count = await _get_mcp_tool_count()

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
    }
    return JSONResponse(body, status_code=http_code)
