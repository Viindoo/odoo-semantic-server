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

    Also writes api_key_id + tenant_id into server._api_key_id_var / _tenant_id_var
    (ContextVars) so synchronous tool wrappers (model_inspect, module_inspect,
    entity_lookup, describe_module, set_active_version, set_active_profile) share
    the correct tenant namespace for ref minting and session persistence.
    ContextVars are used (not threading.local) to prevent cross-request leakage
    when concurrent asyncio coroutines share the same event-loop thread.
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

        Sets server._api_key_id_var ContextVar for the duration of the call so
        MCP tool wrappers share the correct tenant api_key_id via
        _get_api_key_id().  Token-reset in finally prevents cross-request
        leakage between concurrent asyncio coroutines.
        """
        tool_name: str = context.message.name  # always present per MCP spec

        # api_key_id and tenant_id are set on request.state by AuthMiddleware
        # before the request reaches the FastMCP handler.  Gracefully fall back
        # to None for unauthenticated paths (e.g. /health via custom_route).
        api_key_id: str | None = None
        tenant_id: int | None = None
        key_prefix: str | None = None
        try:
            req = get_http_request()
            api_key_id = getattr(req.state, "api_key_id", None)
            tenant_id = getattr(req.state, "tenant_id", None)
            key_prefix = getattr(req.state, "key_prefix", None)
            if api_key_id is None:
                # #248: on stateful streamable-HTTP the AuthMiddleware scope-state
                # mutation does not survive into the request_ctx Request the loop
                # exposes here — recover the PK from the X-API-Key header (which
                # does survive) via AuthMiddleware's warm cache.
                api_key_id, tenant_id, key_prefix = _recover_identity_from_header(
                    req, api_key_id, tenant_id, key_prefix
                )
        except Exception:
            pass  # no active HTTP request (e.g. stdio transport) — fine

        # Propagate api_key_id and tenant_id into ContextVars so synchronous
        # tool wrappers can call _get_api_key_id() / _get_tenant_id().
        # ContextVar tokens allow atomic reset to the previous value in finally,
        # preventing cross-request leakage even when concurrent coroutines run
        # in the same event-loop thread (the old threading.local() approach
        # suffered a race where one request's finally would wipe another's value).
        key_token = _set_server_api_key(api_key_id)
        tid_token = _set_server_tenant_id(tenant_id)
        start = time.monotonic()
        try:
            result = await call_next(context)
        finally:
            _reset_server_api_key(key_token)   # reset to pre-call value
            _reset_server_tenant_id(tid_token)  # reset tenant_id in tandem
        ms = int((time.monotonic() - start) * 1000)

        task = asyncio.create_task(
            _log_tool_call_async(api_key_id, tool_name, ms)
        )
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)

        # ADR-0034 §D4 / ADR-0021 §3: emit exactly one audit row per unscoped
        # (global/admin) tool call — tenant_id IS NULL means the request bypassed
        # tenant isolation.  Tenant-scoped calls are excluded (tenant_id is set).
        if tenant_id is None and api_key_id is not None:
            audit_task = asyncio.create_task(
                _audit_unscoped_tool_call_async(key_prefix, tool_name)
            )
            _BG_TASKS.add(audit_task)
            audit_task.add_done_callback(_BG_TASKS.discard)

        return result

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, Sequence[ReadResourceContents]],
    ) -> Sequence[ReadResourceContents]:
        """Hook fired for every ``resources/read`` JSON-RPC request.

        Propagates api_key_id + tenant_id into ContextVars so the session-context
        resolver (_get_api_key_id) returns the real tenant key during resource
        reads, preventing the sticky-session bypass where resource reads always
        resolved to the 'default' sentinel.  Token-reset in finally prevents
        cross-request leakage in the asyncio event loop.

        Usage-log insert is intentionally omitted here — resource reads are
        typically bookmark-stable content fetches and are already cached; the
        on_call_tool hook covers tool-call accounting.
        """
        api_key_id: str | None = None
        tenant_id: int | None = None
        try:
            req = get_http_request()
            api_key_id = getattr(req.state, "api_key_id", None)
            tenant_id = getattr(req.state, "tenant_id", None)
            if api_key_id is None:
                # #248: same scope-state loss as on_call_tool — recover from the
                # X-API-Key header so odoo://auto/... resources honour the pin too.
                api_key_id, tenant_id, _ = _recover_identity_from_header(
                    req, api_key_id, tenant_id, None
                )
        except Exception:
            pass  # no active HTTP request (e.g. stdio transport) — fine

        key_token = _set_server_api_key(api_key_id)
        tid_token = _set_server_tenant_id(tenant_id)
        try:
            return await call_next(context)
        finally:
            _reset_server_api_key(key_token)   # reset to pre-call value
            _reset_server_tenant_id(tid_token)  # reset tenant_id in tandem


def _recover_identity_from_header(req, api_key_id, tenant_id, key_prefix):
    """Recover (api_key_id, tenant_id, key_prefix) from the ``X-API-Key`` header.

    Root cause (#248): on the stateful streamable-HTTP transport the per-call
    ``Request`` reaches these FastMCP hooks via the MCP ``request_ctx`` bridge,
    but the ``request.state.api_key_id`` mutation ``AuthMiddleware`` wrote does
    not survive the BaseHTTPMiddleware↔session-manager↔request_ctx boundary — so
    the hooks read ``None`` and the session resolver falls through to the
    ``'default'`` sentinel, silently ignoring ``set_active_version`` /
    ``set_active_profile`` pins (``auto`` then resolves to the latest version).

    The ``X-API-Key`` header DOES survive on the per-call scope (``scope["headers"]``
    is set by the ASGI server and untouched by middleware), and ``AuthMiddleware``
    has already populated the warm ``_KEY_CACHE`` / ``_TENANT_CACHE`` for this exact
    key BEFORE this hook fires (dispatch runs before ``call_next``). Recover the
    numeric PK from that cache. On a cache miss (TTL edge) leave the values
    unchanged — the caller then gets the prior graceful ``'default'`` fallback,
    so there is no regression versus the pre-fix behaviour.

    Called only when ``api_key_id is None`` (state-loss path); a no-op otherwise.
    Never raises — a recovery failure must not break the tool/resource response.
    """
    if api_key_id is not None:
        return api_key_id, tenant_id, key_prefix
    try:
        raw_key = req.headers.get("X-API-Key")
    except Exception:
        return api_key_id, tenant_id, key_prefix
    if not raw_key:
        return api_key_id, tenant_id, key_prefix
    try:
        from src.mcp.middleware import _cache_get, _cache_get_tenant
        hit, kid = _cache_get(raw_key)
        if hit and kid is not None:
            api_key_id = kid
            t_hit, tid = _cache_get_tenant(raw_key)
            if t_hit:
                tenant_id = tid
            if key_prefix is None:
                key_prefix = raw_key[:12]
    except Exception:
        pass  # never raise from middleware
    return api_key_id, tenant_id, key_prefix


def _set_server_api_key(api_key_id: str | None):
    """Set *api_key_id* in server._api_key_id_var (ContextVar) and return the token.

    Returns the ContextVar token so the caller can call _reset_server_api_key(token)
    in a finally block to atomically restore the previous value.  Using the token
    pattern ensures that nested / concurrent calls each restore only their own
    change, preventing cross-request leakage in the asyncio event loop.

    Imported lazily to avoid circular import at module load time.
    """
    try:
        from src.mcp import server as _server  # lazy — avoids circular import
        value = api_key_id if api_key_id is not None else "default"
        return _server._api_key_id_var.set(value)
    except Exception:
        return None  # never raise from middleware — logging failure must not break tool response


def _reset_server_api_key(token) -> None:
    """Reset server._api_key_id_var to its value before _set_server_api_key was called.

    *token* is the value returned by _set_server_api_key.  A None token (e.g.
    when _set_server_api_key caught an exception) is silently ignored.
    """
    if token is None:
        return
    try:
        from src.mcp import server as _server  # lazy — avoids circular import
        _server._api_key_id_var.reset(token)
    except Exception:
        pass  # never raise from middleware


def _set_server_tenant_id(tenant_id: int | None):
    """Set *tenant_id* in server._tenant_id_var (ContextVar) and return the token.

    Mirrors _set_server_api_key for the tenant_id ContextVar introduced in
    ADR-0034 D4.1 (WI-D plumbing).  Returns a token so _reset_server_tenant_id
    can atomically restore the previous value in finally.
    """
    try:
        from src.mcp import server as _server  # lazy — avoids circular import
        return _server._tenant_id_var.set(tenant_id)
    except Exception:
        return None  # never raise from middleware


def _reset_server_tenant_id(token) -> None:
    """Reset server._tenant_id_var to its value before _set_server_tenant_id was called."""
    if token is None:
        return
    try:
        from src.mcp import server as _server  # lazy — avoids circular import
        _server._tenant_id_var.reset(token)
    except Exception:
        pass  # never raise from middleware


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


async def _audit_unscoped_tool_call_async(
    key_prefix: str | None,
    tool_name: str,
) -> None:
    """Emit one admin_audit_log row for an unscoped (global/admin) MCP tool call.

    ADR-0034 §D4: "the only unscoped path is audit-logged".
    ADR-0021 §3: action taxonomy entry «mcp.query.unscoped».

    Fires fire-and-forget from on_call_tool; never raises.  The audit INSERT
    uses a dedicated pool connection (transaction-independent) via write_audit_log.

    Actor format: "api_key:<prefix>" where prefix is the first 12 chars of the
    raw API key, pre-computed in AuthMiddleware.dispatch() and stored in
    request.state.key_prefix — zero additional DB queries on the hot path.
    If key_prefix is unavailable (e.g. stdio transport), actor falls back to
    "api_key:unknown".
    """
    try:
        from src.db.audit import write_audit_log

        actor = f"api_key:{key_prefix}" if key_prefix else "api_key:unknown"
        await asyncio.to_thread(
            write_audit_log,
            actor,
            "mcp.query.unscoped",
            tool_name,
            True,
            {"tool": tool_name},
        )
    except Exception:
        pass  # best-effort — audit failure must never break tool response
