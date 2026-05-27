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
  1. threading.local propagation FAILS under asyncio concurrency (proves the
     old code was broken — this test would FAIL on the pre-fix implementation).
  2. ContextVar propagation PASSES under asyncio concurrency (proves the fix).
  3. _get_api_key_id() / _get_tenant_id() return real values when middleware
     sets them via the ContextVar bridge.
  4. Concurrent coroutines: middleware set + tool read — no cross-contamination.
  5. session.py set_active_version_db gracefully skips non-numeric api_key_id.
  6. session.py set_active_profile_db gracefully skips non-numeric api_key_id.
  7. session.py _fetch_from_db returns None for non-numeric api_key_id.
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
    async def test_threading_local_leaks_between_concurrent_coroutines(self):
        """threading.local is shared across coroutines in the same event-loop thread.

        When coroutine A's finally deletes the thread-local before coroutine B's
        tool body reads it, B sees the 'default' sentinel — the root cause of the
        production crash.
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

        async def simulate_request(key_id: int) -> str:
            _set(key_id)      # middleware sets local
            await asyncio.sleep(0)  # yield — other coroutines interleave
            result = _get()   # tool body reads local
            _set(None)        # middleware finally resets local
            return str(result)

        results = await asyncio.gather(
            simulate_request(1),
            simulate_request(2),
            simulate_request(3),
        )

        # With threading.local(), cross-contamination DOES occur:
        # at least one result will NOT match its expected key_id.
        expected = ["1", "2", "3"]
        # We assert the race is observable (proves the bug was real):
        assert results != expected, (
            "Expected threading.local() to exhibit cross-contamination under "
            "concurrent asyncio coroutines.  If all results match, the test "
            "setup may not be triggering the race."
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
