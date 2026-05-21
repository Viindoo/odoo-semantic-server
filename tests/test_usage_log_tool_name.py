# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for UsageLogMiddleware — tool_name extraction at FastMCP layer.

OBS-2 / F5: usage_log.tool_name was always 'unknown' because AuthMiddleware
read an X-Tool-Name HTTP header that no MCP client ever sends. Fix: move tool
name extraction into a FastMCP-layer Middleware where context.message.name is
available after JSON-RPC parsing.

Tests in this file:
  - UsageLogMiddleware.on_call_tool populates tool_name from context.message.name
    (not from HTTP headers).
  - Calls to multiple tool names each produce a row with the correct tool_name.
  - api_key_id is read from request.state (set by AuthMiddleware) when an HTTP
    request is available.
  - The middleware is gracefully no-op when no HTTP request is available
    (e.g. stdio transport).
  - No DB insert fires from the ASGI _log_usage_async path (double-insert guard).
"""
import asyncio
from unittest.mock import MagicMock, patch

import mcp.types as mt
import pytest

from src.mcp.tool_log_middleware import UsageLogMiddleware, _log_tool_call_async

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(tool_name: str) -> MagicMock:
    """Build a minimal MiddlewareContext for on_call_tool invocation."""
    ctx = MagicMock()
    ctx.message = mt.CallToolRequestParams(name=tool_name, arguments={})
    ctx.fastmcp_context = None
    return ctx


async def _simple_call_next(_context):
    """Stub call_next that returns a sentinel ToolResult."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Tests for _log_tool_call_async (pure async helper)
# ---------------------------------------------------------------------------

class TestLogToolCallAsync:

    @pytest.mark.asyncio
    async def test_inserts_correct_tool_name(self, monkeypatch):
        """log_usage is called with the exact tool_name passed in."""
        mock_store = MagicMock()
        mock_store.log_usage = MagicMock()
        monkeypatch.setattr("src.db.pg.auth_store", lambda: mock_store)

        await _log_tool_call_async(api_key_id=1, tool_name="resolve_model", ms=42)

        mock_store.log_usage.assert_called_once_with(1, "resolve_model", 42)

    @pytest.mark.asyncio
    async def test_inserts_find_examples_tool_name(self, monkeypatch):
        """log_usage called correctly for find_examples tool."""
        mock_store = MagicMock()
        mock_store.log_usage = MagicMock()
        monkeypatch.setattr("src.db.pg.auth_store", lambda: mock_store)

        await _log_tool_call_async(api_key_id=5, tool_name="find_examples", ms=100)

        mock_store.log_usage.assert_called_once_with(5, "find_examples", 100)

    @pytest.mark.asyncio
    async def test_inserts_impact_analysis_tool_name(self, monkeypatch):
        """log_usage called correctly for impact_analysis tool."""
        mock_store = MagicMock()
        mock_store.log_usage = MagicMock()
        monkeypatch.setattr("src.db.pg.auth_store", lambda: mock_store)

        await _log_tool_call_async(api_key_id=3, tool_name="impact_analysis", ms=200)

        mock_store.log_usage.assert_called_once_with(3, "impact_analysis", 200)

    @pytest.mark.asyncio
    async def test_none_api_key_id_does_not_raise(self, monkeypatch):
        """api_key_id=None (unauthenticated) is accepted without raising."""
        mock_store = MagicMock()
        mock_store.log_usage = MagicMock()
        monkeypatch.setattr("src.db.pg.auth_store", lambda: mock_store)

        await _log_tool_call_async(api_key_id=None, tool_name="resolve_model", ms=10)

        mock_store.log_usage.assert_called_once_with(None, "resolve_model", 10)

    @pytest.mark.asyncio
    async def test_db_exception_is_swallowed(self, monkeypatch):
        """If log_usage raises, the exception is silently swallowed."""
        mock_store = MagicMock()
        mock_store.log_usage = MagicMock(side_effect=RuntimeError("DB dead"))
        monkeypatch.setattr("src.db.pg.auth_store", lambda: mock_store)

        # Must not raise
        await _log_tool_call_async(api_key_id=1, tool_name="resolve_model", ms=5)


# ---------------------------------------------------------------------------
# Tests for UsageLogMiddleware.on_call_tool
# ---------------------------------------------------------------------------

class TestUsageLogMiddleware:

    @pytest.mark.asyncio
    async def test_tool_name_extracted_from_context_message(self, monkeypatch):
        """on_call_tool reads tool name from context.message.name, not HTTP headers."""
        logged: list[dict] = []

        async def mock_log(api_key_id, tool_name, ms):
            logged.append({"api_key_id": api_key_id, "tool_name": tool_name, "ms": ms})

        import src.mcp.tool_log_middleware as tlm
        monkeypatch.setattr(tlm, "_log_tool_call_async", mock_log)

        # Patch get_http_request to raise (simulates no HTTP context / stdio transport)
        with patch(
            "src.mcp.tool_log_middleware.get_http_request",
            side_effect=RuntimeError("no HTTP"),
        ):
            mw = UsageLogMiddleware()
            ctx = _make_context("resolve_model")
            await mw.on_call_tool(ctx, _simple_call_next)

        # Wait for background tasks to complete
        await asyncio.sleep(0.05)

        assert len(logged) == 1
        assert logged[0]["tool_name"] == "resolve_model"
        assert logged[0]["api_key_id"] is None  # fallback when no HTTP request

    @pytest.mark.asyncio
    async def test_api_key_id_read_from_request_state(self, monkeypatch):
        """on_call_tool reads api_key_id from request.state set by AuthMiddleware."""
        logged: list[dict] = []

        async def mock_log(api_key_id, tool_name, ms):
            logged.append({"api_key_id": api_key_id, "tool_name": tool_name})

        import src.mcp.tool_log_middleware as tlm
        monkeypatch.setattr(tlm, "_log_tool_call_async", mock_log)

        # Build a mock HTTP request with api_key_id=42 on state (set by AuthMiddleware)
        mock_request = MagicMock()
        mock_request.state.api_key_id = 42

        with patch(
            "src.mcp.tool_log_middleware.get_http_request",
            return_value=mock_request,
        ):
            mw = UsageLogMiddleware()
            ctx = _make_context("find_examples")
            await mw.on_call_tool(ctx, _simple_call_next)

        await asyncio.sleep(0.05)

        assert len(logged) == 1
        assert logged[0]["tool_name"] == "find_examples"
        assert logged[0]["api_key_id"] == 42

    @pytest.mark.asyncio
    async def test_multiple_tools_each_logged_correctly(self, monkeypatch):
        """Three consecutive tool calls each produce a correctly-named log entry."""
        logged: list[dict] = []

        async def mock_log(api_key_id, tool_name, ms):
            logged.append({"api_key_id": api_key_id, "tool_name": tool_name})

        import src.mcp.tool_log_middleware as tlm
        monkeypatch.setattr(tlm, "_log_tool_call_async", mock_log)

        mock_request = MagicMock()
        mock_request.state.api_key_id = 7

        with patch(
            "src.mcp.tool_log_middleware.get_http_request",
            return_value=mock_request,
        ):
            mw = UsageLogMiddleware()
            for tool in ("resolve_model", "find_examples", "impact_analysis"):
                ctx = _make_context(tool)
                await mw.on_call_tool(ctx, _simple_call_next)

        await asyncio.sleep(0.05)

        assert len(logged) == 3
        names = [r["tool_name"] for r in logged]
        assert names == ["resolve_model", "find_examples", "impact_analysis"]
        assert all(r["api_key_id"] == 7 for r in logged)

    @pytest.mark.asyncio
    async def test_missing_api_key_id_on_state_falls_back_to_none(self, monkeypatch):
        """If request.state has no api_key_id attr (edge case), fall back to None."""
        logged: list[dict] = []

        async def mock_log(api_key_id, tool_name, ms):
            logged.append({"api_key_id": api_key_id, "tool_name": tool_name})

        import src.mcp.tool_log_middleware as tlm
        monkeypatch.setattr(tlm, "_log_tool_call_async", mock_log)

        # Request state without api_key_id attribute — getattr(..., None) returns None
        mock_request = MagicMock()
        del mock_request.state.api_key_id  # ensure attribute is absent

        with patch(
            "src.mcp.tool_log_middleware.get_http_request",
            return_value=mock_request,
        ):
            mw = UsageLogMiddleware()
            ctx = _make_context("check_module_exists")
            await mw.on_call_tool(ctx, _simple_call_next)

        await asyncio.sleep(0.05)

        assert len(logged) == 1
        assert logged[0]["tool_name"] == "check_module_exists"
        assert logged[0]["api_key_id"] is None

    @pytest.mark.asyncio
    async def test_on_call_tool_returns_call_next_result(self, monkeypatch):
        """on_call_tool must return the result from call_next, not swallow it."""
        import src.mcp.tool_log_middleware as tlm

        async def noop_log(*_args, **_kwargs):
            pass

        monkeypatch.setattr(tlm, "_log_tool_call_async", noop_log)

        sentinel = MagicMock(name="tool_result_sentinel")

        async def call_next_with_sentinel(_ctx):
            return sentinel

        with patch(
            "src.mcp.tool_log_middleware.get_http_request",
            side_effect=RuntimeError("no HTTP"),
        ):
            mw = UsageLogMiddleware()
            ctx = _make_context("resolve_model")
            result = await mw.on_call_tool(ctx, call_next_with_sentinel)

        assert result is sentinel


# ---------------------------------------------------------------------------
# Guard: ASGI _log_usage_async must NOT call auth_store().log_usage anymore
# ---------------------------------------------------------------------------

class TestAsgiLayerNoLongerWritesToDb:
    """Ensure the ASGI middleware stopped calling log_usage (double-insert guard).

    After the F5 fix, _log_usage_async in middleware.py should only emit a
    Python logger line — it must not call auth_store().log_usage().
    """

    @pytest.mark.asyncio
    async def test_log_usage_not_called_from_asgi_middleware(self, monkeypatch):
        from starlette.datastructures import Headers
        from starlette.requests import Request

        from src.mcp.middleware import _log_usage_async

        mock_store = MagicMock()
        mock_store.log_usage = MagicMock()
        monkeypatch.setattr("src.db.pg.auth_store", lambda: mock_store)

        # Build a minimal mock Request — only url.path and headers needed
        mock_request = MagicMock(spec=Request)
        mock_request.url.path = "/mcp"
        mock_request.headers = Headers({})

        await _log_usage_async(key_id=1, request=mock_request, ms=50)

        # The ASGI layer must NOT call log_usage after the F5 fix
        mock_store.log_usage.assert_not_called()
