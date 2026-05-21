# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastMCP-layer middleware for usage logging with correct tool_name extraction.

Background
----------
The ASGI layer (AuthMiddleware) cannot read the JSON-RPC body without consuming
the HTTP stream, so it could never reliably extract the MCP tool name.  The only
place where the tool name is guaranteed available is the FastMCP middleware layer,
after the JSON-RPC body has been parsed and the call is dispatched to
``on_call_tool``.

This module provides ``UsageLogMiddleware``, a ``fastmcp.Middleware`` subclass,
that:
  - hooks ``on_call_tool`` (fired for every ``tools/call`` JSON-RPC request)
  - reads ``context.message.name`` for the exact tool name
  - reads ``api_key_id`` from ``request.state`` (set by the ASGI ``AuthMiddleware``)
  - calls ``auth_store().log_usage()`` fire-and-forget (best-effort, never raises)

The ASGI-layer ``_log_usage_async`` in middleware.py was changed to skip the
DB insert (it now only logs to the Python logger for HTTP-level tracing); the
DB insert is handled exclusively here.
"""
import asyncio
import logging
import time
from collections.abc import Sequence

import mcp.types as mt
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.middleware import CallNext, ToolResult
from mcp.server.lowlevel.helper_types import ReadResourceContents

_logger = logging.getLogger(__name__)

# Strong references to background tasks prevent GC-before-completion (mirrors
# the B3 pattern in AuthMiddleware).
_BG_TASKS: set[asyncio.Task] = set()


class UsageLogMiddleware(Middleware):
    """FastMCP middleware that logs MCP tool calls with the correct tool_name.

    Must be registered via ``mcp.add_middleware(UsageLogMiddleware())`` before
    the server starts accepting connections (see server.py module-level setup).

    Also writes api_key_id into server._api_key_id_local so that synchronous
    tool wrappers (model_inspect, module_inspect, entity_lookup, describe_module)
    share the same tenant namespace for ref minting and resolution (fixes HIGH-1
    from Wave C Opus review).
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Hook fired for every ``tools/call`` JSON-RPC request.

        Captures timing around the actual tool invocation, then schedules a
        fire-and-forget log insert.  Never raises — any error is silently
        swallowed so a logging failure never breaks the tool response.

        Sets server._api_key_id_local.value for the duration of the call so
        MCP tool wrappers share the same tenant api_key_id via
        _get_api_key_id().  Cleared in finally to avoid cross-request leakage.
        """
        tool_name: str = context.message.name  # always present per MCP spec

        # api_key_id is set on request.state by AuthMiddleware before the
        # request reaches the FastMCP handler.  Gracefully fall back to None
        # for unauthenticated paths (e.g. /health via custom_route).
        api_key_id: str | None = None
        try:
            req = get_http_request()
            api_key_id = getattr(req.state, "api_key_id", None)
        except Exception:
            pass  # no active HTTP request (e.g. stdio transport) — fine

        # Propagate api_key_id into the thread-local so synchronous tool
        # wrappers can call _get_api_key_id() and get the real tenant key.
        _set_server_api_key(api_key_id)
        start = time.monotonic()
        try:
            result = await call_next(context)
        finally:
            _set_server_api_key(None)  # clear to avoid cross-request leakage
        ms = int((time.monotonic() - start) * 1000)

        task = asyncio.create_task(
            _log_tool_call_async(api_key_id, tool_name, ms)
        )
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)

        return result

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, Sequence[ReadResourceContents]],
    ) -> Sequence[ReadResourceContents]:
        """Hook fired for every ``resources/read`` JSON-RPC request.

        Propagates api_key_id into the thread-local so the session-context
        resolver (_get_api_key_id) returns the real tenant key during resource
        reads, preventing the sticky-session bypass where resource reads always
        resolved to the 'default' sentinel.

        Usage-log insert is intentionally omitted here — resource reads are
        typically bookmark-stable content fetches and are already cached; the
        on_call_tool hook covers tool-call accounting.
        """
        api_key_id: str | None = None
        try:
            req = get_http_request()
            api_key_id = getattr(req.state, "api_key_id", None)
        except Exception:
            pass  # no active HTTP request (e.g. stdio transport) — fine

        _set_server_api_key(api_key_id)
        try:
            return await call_next(context)
        finally:
            _set_server_api_key(None)  # clear to avoid cross-request leakage


def _set_server_api_key(api_key_id: str | None) -> None:
    """Write *api_key_id* into server._api_key_id_local (best-effort, never raises).

    Imported lazily to avoid circular import at module load time.  Called once
    before the tool body and once in the finally block to clear the value.
    """
    try:
        from src.mcp import server as _server  # lazy — avoids circular import
        if api_key_id is not None:
            _server._api_key_id_local.value = api_key_id
        else:
            # Reset to sentinel so _get_api_key_id() falls back to 'default'.
            try:
                del _server._api_key_id_local.value
            except AttributeError:
                pass  # already unset — fine
    except Exception:
        pass  # never raise from middleware — logging failure must not break tool response


async def _log_tool_call_async(
    api_key_id: str | None,
    tool_name: str,
    ms: int,
) -> None:
    """Insert a usage_log row asynchronously — best-effort, never raises."""
    try:
        from src.db.pg import auth_store
        _logger.info(
            "mcp_tool tool=%s key_id=%s ms=%d", tool_name, api_key_id, ms
        )
        await asyncio.to_thread(
            lambda: auth_store().log_usage(api_key_id, tool_name, ms)
        )
    except Exception:
        pass  # best-effort — swallow silently
