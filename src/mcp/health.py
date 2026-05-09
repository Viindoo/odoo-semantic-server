"""Health check endpoint for MCP server."""
import asyncio
import importlib.metadata

from starlette.requests import Request
from starlette.responses import JSONResponse


async def health_handler(request: Request) -> JSONResponse:
    """Check health of Neo4j and PostgreSQL connections, return status + version."""
    from src.mcp.middleware import _PG_LOCK
    from src.mcp.server import _get_driver, _get_pg_conn, mcp

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

    try:
        tool_count = len(mcp._tool_manager._tools)
    except Exception:
        tool_count = -1

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
