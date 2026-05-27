# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for context propagation to sync MCP tool wrappers.

Bug fixed: set_active_version / set_active_profile crashed with
    "invalid literal for int() with base 10: 'default'"
because concurrent asyncio coroutines (each serving one MCP request) shared a
threading.local() in the same event-loop thread.  One coroutine's finally-reset
would wipe the value set by another coroutine, so the sync tool body saw the
'default' sentinel instead of the real api_key_id.

Fix: replaced threading.local() with contextvars.ContextVar for _api_key_id_var
and _tenant_id_var.  ContextVar gives each coroutine its own isolated copy.

Tests:
  1. threading.local wipe between concurrent coroutines — deterministic
     reproduction (asyncio.Event) of the historical race the fix removes.
  2. ContextVar propagation PASSES under asyncio concurrency (proves the fix).
  3. _get_api_key_id() / _get_tenant_id() return real values when middleware
     sets them via the ContextVar bridge.
  4. Concurrent coroutines: middleware set + tool read — no cross-contamination.
  5. session.py set_active_version_db gracefully skips non-numeric api_key_id.
  6. session.py set_active_profile_db gracefully skips non-numeric api_key_id.
  7. session.py _fetch_from_db returns None for non-numeric api_key_id.
  8. End-to-end: a REAL sync @mcp.tool reads the context through the real
     UsageLogMiddleware via an in-memory fastmcp.Client (the genuine prod path).
"""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. threading.local FAILS under asyncio concurrency (regression reference)
# ---------------------------------------------------------------------------


class TestThreadingLocalRace:
    """Proves that threading.local() is NOT safe for asyncio concurrency.

    This is NOT a test of our code — it documents why the old approach was
    wrong.  The old _api_key_id_local = threading.local() suffered this race.
    """

    @pytest.mark.asyncio
    async def test_threading_local_wipe_between_concurrent_coroutines(self):
        """threading.local is shared across coroutines in one event-loop thread.

        Deterministic reproduction of the production race (forced via
        asyncio.Event, no reliance on scheduler timing): coroutine A sets the
        shared thread-local and yields; coroutine B sets then *resets* (del) the
        SAME shared thread-local in its finally; when A resumes it reads the
        'default' sentinel because B's reset wiped A's value.  That sentinel is
        exactly what fed ``int('default')`` in the session-write path and crashed
        ``set_active_version`` on the live (concurrent) server.  ContextVar — the
        fix — makes this impossible because each coroutine gets its own copy
        (see TestContextVarPropagation).  This test documents the historical bug.
        """
        _local = threading.local()

        def _set(val):
            if val is not None:
                _local.value = val
            else:
                try:
                    del _local.value
                except AttributeError:
                    pass

        def _get():
            return getattr(_local, "value", "default")

        a_has_set = asyncio.Event()
        b_has_reset = asyncio.Event()

        async def coro_a() -> str:
            _set("A")                 # A: middleware sets the shared local
            a_has_set.set()           # release B to run now
            await b_has_reset.wait()  # B sets+resets the SAME local meanwhile
            return _get()             # A: tool body reads — value wiped by B

        async def coro_b() -> str:
            await a_has_set.wait()    # run only after A has set its value
            _set("B")                 # B overwrites the shared local
            _set(None)                # B: finally-reset wipes the shared local
            b_has_reset.set()         # release A to resume and read
            return _get()

        a_result, _b_result = await asyncio.gather(coro_a(), coro_b())

        # Deterministic contamination: A set "A" but reads the sentinel because
        # the thread-local is shared and B's reset wiped it — the exact mechanism
        # of the prod crash (sentinel → int('default') → ValueError).
        assert a_result == "default", (
            f"threading.local cross-coroutine wipe expected: A set 'A' but should "
            f"read the wiped sentinel; got {a_result!r}"
        )


# ---------------------------------------------------------------------------
# 2. ContextVar PASSES under asyncio concurrency (proves the fix)
# ---------------------------------------------------------------------------


class TestContextVarPropagation:
    """Proves that ContextVar is safe for asyncio concurrency."""

    @pytest.mark.asyncio
    async def test_contextvar_isolated_per_coroutine(self):
        """Each coroutine sees its own ContextVar value — no cross-contamination."""
        _var: ContextVar[str] = ContextVar("_test_var", default="default")

        async def simulate_request(key_id: int) -> str:
            token = _var.set(str(key_id))
            try:
                await asyncio.sleep(0)  # yield — other coroutines interleave
                result = _var.get()
                return result
            finally:
                _var.reset(token)

        results = await asyncio.gather(
            simulate_request(1),
            simulate_request(2),
            simulate_request(3),
        )

        assert results == ["1", "2", "3"], (
            f"ContextVar must isolate values per coroutine; got {results!r}"
        )

    @pytest.mark.asyncio
    async def test_contextvar_default_after_reset(self):
        """After reset(), the ContextVar returns the default value."""
        _var: ContextVar[str] = ContextVar("_test_reset", default="default")

        token = _var.set("real_value")
        assert _var.get() == "real_value"
        _var.reset(token)
        assert _var.get() == "default"


# ---------------------------------------------------------------------------
# 3. _get_api_key_id() / _get_tenant_id() with middleware ContextVar bridge
# ---------------------------------------------------------------------------


class TestGetApiKeyIdContextVar:
    """_get_api_key_id() and _get_tenant_id() read from ContextVars correctly."""

    def test_get_api_key_id_returns_default_when_unset(self):
        """Default sentinel 'default' when no middleware has set the ContextVar."""
        from src.mcp.server import _api_key_id_var, _get_api_key_id

        # Ensure we start from the default
        token = _api_key_id_var.set("default")
        try:
            assert _get_api_key_id() == "default"
        finally:
            _api_key_id_var.reset(token)

    def test_get_api_key_id_returns_set_value(self):
        """_get_api_key_id() returns the value set by the middleware."""
        from src.mcp.server import _api_key_id_var, _get_api_key_id

        token = _api_key_id_var.set(42)
        try:
            assert _get_api_key_id() == 42
        finally:
            _api_key_id_var.reset(token)

    def test_get_tenant_id_returns_none_when_unset(self):
        """Default None for tenant_id when no middleware has set the ContextVar."""
        from src.mcp.server import _get_tenant_id, _tenant_id_var

        token = _tenant_id_var.set(None)
        try:
            assert _get_tenant_id() is None
        finally:
            _tenant_id_var.reset(token)

    def test_get_tenant_id_returns_set_value(self):
        """_get_tenant_id() returns the value set by the middleware."""
        from src.mcp.server import _get_tenant_id, _tenant_id_var

        token = _tenant_id_var.set(99)
        try:
            assert _get_tenant_id() == 99
        finally:
            _tenant_id_var.reset(token)

    @pytest.mark.asyncio
    async def test_concurrent_requests_isolated(self):
        """Concurrent coroutines each see their own api_key_id — no leakage."""
        from src.mcp.server import _api_key_id_var, _get_api_key_id, _get_tenant_id, _tenant_id_var

        async def simulate_request(key_id: int, tenant_id: int | None) -> tuple:
            key_token = _api_key_id_var.set(str(key_id))
            tid_token = _tenant_id_var.set(tenant_id)
            try:
                await asyncio.sleep(0)  # yield
                # Both reads must see the values THIS coroutine set
                return _get_api_key_id(), _get_tenant_id()
            finally:
                _api_key_id_var.reset(key_token)
                _tenant_id_var.reset(tid_token)

        results = await asyncio.gather(
            simulate_request(1, 10),
            simulate_request(2, 20),
            simulate_request(3, None),
        )

        assert results[0] == ("1", 10), f"Request 1: {results[0]}"
        assert results[1] == ("2", 20), f"Request 2: {results[1]}"
        assert results[2] == ("3", None), f"Request 3: {results[2]}"


# ---------------------------------------------------------------------------
# 4. Middleware bridge: _set_server_api_key / _reset_server_api_key
# ---------------------------------------------------------------------------


class TestMiddlewareBridge:
    """_set_server_api_key and _set_server_tenant_id write to ContextVars correctly."""

    def test_set_and_reset_api_key(self):
        """_set_server_api_key returns a token; _reset_server_api_key restores default."""
        from src.mcp.server import _get_api_key_id
        from src.mcp.tool_log_middleware import _reset_server_api_key, _set_server_api_key

        token = _set_server_api_key(42)
        assert _get_api_key_id() == 42
        _reset_server_api_key(token)
        assert _get_api_key_id() == "default"

    def test_set_none_becomes_default_sentinel(self):
        """_set_server_api_key(None) writes the 'default' sentinel."""
        from src.mcp.server import _get_api_key_id
        from src.mcp.tool_log_middleware import _reset_server_api_key, _set_server_api_key

        token = _set_server_api_key(None)
        assert _get_api_key_id() == "default"
        _reset_server_api_key(token)

    def test_set_and_reset_tenant_id(self):
        """_set_server_tenant_id returns a token; _reset_server_tenant_id restores None."""
        from src.mcp.server import _get_tenant_id
        from src.mcp.tool_log_middleware import _reset_server_tenant_id, _set_server_tenant_id

        token = _set_server_tenant_id(77)
        assert _get_tenant_id() == 77
        _reset_server_tenant_id(token)
        assert _get_tenant_id() is None

    @pytest.mark.asyncio
    async def test_concurrent_middleware_no_cross_contamination(self):
        """Concurrent coroutines each using the middleware bridge see isolated values.

        This is the end-to-end proof that the fix resolves the production crash:
        before the fix, one coroutine's _reset_server_api_key would wipe another
        coroutine's value → sync tool body sees 'default' → int('default') → crash.
        After the fix, each coroutine's ContextVar is isolated.
        """
        from src.mcp.server import _get_api_key_id, _get_tenant_id
        from src.mcp.tool_log_middleware import (
            _reset_server_api_key,
            _reset_server_tenant_id,
            _set_server_api_key,
            _set_server_tenant_id,
        )

        async def simulate_tool_call(key_id: int, tenant_id: int | None) -> tuple:
            key_token = _set_server_api_key(key_id)
            tid_token = _set_server_tenant_id(tenant_id)
            try:
                await asyncio.sleep(0)  # yield — other coroutines interleave
                # Sync tool body reads:
                seen_key = _get_api_key_id()
                seen_tid = _get_tenant_id()
                return seen_key, seen_tid
            finally:
                _reset_server_api_key(key_token)
                _reset_server_tenant_id(tid_token)

        results = await asyncio.gather(
            simulate_tool_call(1, 10),
            simulate_tool_call(2, 20),
            simulate_tool_call(3, None),
        )

        assert results[0] == (1, 10),   f"Request 1 saw wrong values: {results[0]}"
        assert results[1] == (2, 20),   f"Request 2 saw wrong values: {results[1]}"
        assert results[2] == (3, None), f"Request 3 saw wrong values: {results[2]}"

        # After all requests, the ContextVars restore to their defaults:
        assert _get_api_key_id() == "default"
        assert _get_tenant_id() is None


# ---------------------------------------------------------------------------
# 5. session.py belt-and-suspenders guards
# ---------------------------------------------------------------------------


class TestSessionNonNumericGuard:
    """session.py functions gracefully skip when api_key_id is non-numeric."""

    def test_set_active_version_db_skips_for_default_sentinel(self):
        """set_active_version_db('default', ...) logs + returns without DB write."""
        from src.mcp.session import set_active_version_db

        # _checkout_pg is imported lazily from src.mcp.server inside the function.
        # Patch it at the source location.
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("src.mcp.server._checkout_pg", return_value=mock_conn):
            set_active_version_db("default", "17.0")
            # The guard should have returned early — _checkout_pg context manager
            # should NOT have been entered.
            mock_conn.__enter__.assert_not_called()

    def test_set_active_version_db_skips_for_arbitrary_string(self):
        """set_active_version_db('not-an-int', ...) also skips gracefully."""
        from src.mcp.session import set_active_version_db

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("src.mcp.server._checkout_pg", return_value=mock_conn):
            set_active_version_db("not-an-int", "17.0")
            mock_conn.__enter__.assert_not_called()

    def test_set_active_version_db_proceeds_for_numeric_string(self):
        """set_active_version_db('42', ...) attempts the DB write (enters context)."""
        from src.mcp.session import set_active_version_db

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cur)
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)

        with patch("src.mcp.server._checkout_pg", return_value=mock_conn):
            set_active_version_db("42", "17.0")
            # Guard passed → _checkout_pg was entered
            mock_conn.__enter__.assert_called_once()

    def test_set_active_profile_db_skips_for_default_sentinel(self):
        """set_active_profile_db('default', ...) skips gracefully."""
        from src.mcp.session import set_active_profile_db

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("src.mcp.server._checkout_pg", return_value=mock_conn):
            set_active_profile_db("default", "my-profile")
            mock_conn.__enter__.assert_not_called()

    def test_fetch_from_db_returns_none_for_default_sentinel(self):
        """_fetch_from_db('default') returns None without hitting the DB."""
        from src.mcp.session import _fetch_from_db

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("src.mcp.server._checkout_pg", return_value=mock_conn):
            result = _fetch_from_db("default")
            assert result is None
            # The guard short-circuits before importing _checkout_pg
            mock_conn.__enter__.assert_not_called()

    def test_fetch_from_db_returns_none_for_arbitrary_string(self):
        """_fetch_from_db('not-an-int') returns None without DB hit."""
        from src.mcp.session import _fetch_from_db

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("src.mcp.server._checkout_pg", return_value=mock_conn):
            result = _fetch_from_db("not-an-int")
            assert result is None
            mock_conn.__enter__.assert_not_called()


# ---------------------------------------------------------------------------
# 8. End-to-end: a REAL sync @mcp.tool reads the context through the REAL
#    UsageLogMiddleware via an in-memory fastmcp.Client.
# ---------------------------------------------------------------------------


class TestEndToEndSyncToolThroughMiddleware:
    """Drive a real synchronous ``@mcp.tool`` through the real ``UsageLogMiddleware``
    via an in-memory ``fastmcp.Client`` and assert the tool body sees the
    authenticated ``api_key_id`` / ``tenant_id`` — not the ``'default'`` sentinel.

    Unlike the ContextVar-mechanics tests above, this exercises the genuine
    FastMCP middleware -> sync-tool-body boundary that ``set_active_version`` /
    ``set_active_profile`` use in production.  It would catch a regression if a
    future change (or FastMCP upgrade) ever broke context propagation across
    that boundary again.
    """

    @pytest.mark.asyncio
    async def test_sync_tool_sees_real_context_via_middleware(self, monkeypatch):
        from fastmcp import Client, FastMCP

        import src.mcp.tool_log_middleware as tlm
        from src.mcp.server import _get_api_key_id, _get_tenant_id
        from src.mcp.tool_log_middleware import UsageLogMiddleware

        # Fake the authenticated HTTP request that AuthMiddleware populates on
        # request.state before the FastMCP handler runs.
        fake_state = type(
            "State", (), {"api_key_id": 4321, "tenant_id": 7, "key_prefix": "k_probe"}
        )()
        fake_req = type("Req", (), {"state": fake_state})()
        monkeypatch.setattr(tlm, "get_http_request", lambda: fake_req)

        # Silence the fire-and-forget usage-log insert (no DB pool in unit tests).
        async def _noop(*_a, **_k):
            return None

        monkeypatch.setattr(tlm, "_log_tool_call_async", _noop)

        mcp = FastMCP("test-e2e-ctx")
        mcp.add_middleware(UsageLogMiddleware())

        @mcp.tool()
        def probe_ctx() -> str:
            # A REAL synchronous tool body — the same execution path used by
            # set_active_version / set_active_profile.
            return f"{_get_api_key_id()}|{_get_tenant_id()}"

        async with Client(mcp) as client:
            result = await client.call_tool("probe_ctx", {})

        # Robustly extract the returned string across fastmcp result shapes.
        text = getattr(result, "data", None)
        if not isinstance(text, str):
            text = result.content[0].text
        assert text == "4321|7", (
            "sync tool body must see the authenticated context via ContextVar; "
            f"got {text!r} (a '|default' / '|None' value means the bridge failed)"
        )
