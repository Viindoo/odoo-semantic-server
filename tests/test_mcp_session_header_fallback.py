# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression for #248 — version/profile pin ignored over HTTP (api_key_id='default').

Root cause (pinned): on the stateful streamable-HTTP transport the per-call
``Request`` reaches ``UsageLogMiddleware.on_call_tool`` / ``on_read_resource`` via
the MCP ``request_ctx`` bridge, but the ``request.state.api_key_id`` mutation
``AuthMiddleware`` wrote does NOT survive the
BaseHTTPMiddleware↔session-manager↔request_ctx boundary. The hook therefore reads
``None`` and writes the ``'default'`` sentinel into ``_api_key_id_var``, so
``set_active_version_db`` / ``get_session_state`` silently no-op and ``auto``
resolves to the latest version instead of the pinned one.

The fix recovers the numeric PK from the ``X-API-Key`` header (which DOES survive
on the per-call scope) via ``AuthMiddleware``'s warm cache.

This test drives the REAL hook → real ``_recover_identity_from_header`` → real
``_api_key_id_var`` ContextVar → real ``_get_api_key_id()`` inside ``call_next``.
The ONLY mock is the HTTP boundary (``get_http_request``), used to faithfully
reproduce the pinned state-loss (``request.state`` has no ``api_key_id``). It is
NOT a tautology: deleting the recovery call turns the first test RED ('default').

No DB needed — the background usage-log / audit tasks are patched to no-ops so the
test is fast and runs on any tier.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.mcp import middleware as _mw
from src.mcp import server as _srv
from src.mcp.tool_log_middleware import UsageLogMiddleware

RAW_KEY = "osm_test_248_rawkey"
PK = 424248
TENANT = 77


@pytest.fixture(autouse=True)
def _isolate_ctx_and_cache():
    """Snapshot/restore the server ContextVars and clear the auth cache for RAW_KEY
    so neither leaks across tests (token discipline + cache hygiene)."""
    ak = _srv._api_key_id_var.set(_srv._api_key_id_var.get())
    tid = _srv._tenant_id_var.set(_srv._tenant_id_var.get())
    _mw._cache_invalidate(RAW_KEY)
    try:
        yield
    finally:
        _srv._api_key_id_var.reset(ak)
        _srv._tenant_id_var.reset(tid)
        _mw._cache_invalidate(RAW_KEY)


class _State:
    """A bare request.state — attributes set explicitly per test."""


def _fake_request(headers: dict, *, state_api_key_id=None, state_tenant_id=None):
    req = MagicMock()
    st = _State()
    if state_api_key_id is not None:
        st.api_key_id = state_api_key_id
    if state_tenant_id is not None:
        st.tenant_id = state_tenant_id
    req.state = st
    req.headers = headers
    return req


async def _capture_identity_via_on_call_tool(fake_req) -> dict:
    """Run the real on_call_tool hook with a stub call_next that records what
    _get_api_key_id()/_get_tenant_id() return INSIDE the tool body."""
    captured: dict = {}
    mw = UsageLogMiddleware()
    ctx = MagicMock()
    ctx.message.name = "model_inspect"

    async def call_next(_ctx):
        captured["api_key_id"] = _srv._get_api_key_id()
        captured["tenant_id"] = _srv._get_tenant_id()
        return MagicMock()

    with patch("src.mcp.tool_log_middleware.get_http_request", return_value=fake_req), \
         patch("src.mcp.tool_log_middleware._log_tool_call_async"), \
         patch("src.mcp.tool_log_middleware._audit_unscoped_tool_call_async"):
        await mw.on_call_tool(ctx, call_next)
    return captured


async def _capture_identity_via_on_read_resource(fake_req) -> dict:
    captured: dict = {}
    mw = UsageLogMiddleware()
    ctx = MagicMock()

    async def call_next(_ctx):
        captured["api_key_id"] = _srv._get_api_key_id()
        captured["tenant_id"] = _srv._get_tenant_id()
        return []

    with patch("src.mcp.tool_log_middleware.get_http_request", return_value=fake_req):
        await mw.on_read_resource(ctx, call_next)
    return captured


@pytest.mark.asyncio
async def test_call_tool_recovers_pk_from_header_when_state_lost():
    """#248 core: state-loss + warm cache → tool body sees the real PK, not 'default'."""
    _mw._cache_set(RAW_KEY, PK)
    _mw._cache_set_tenant(RAW_KEY, TENANT)
    _mw._cache_set_owner(RAW_KEY, None, False)

    captured = await _capture_identity_via_on_call_tool(
        _fake_request({"X-API-Key": RAW_KEY})  # state has NO api_key_id (loss)
    )

    assert captured["api_key_id"] == PK, (
        f"tool body must see the recovered PK {PK}, got {captured['api_key_id']!r} "
        "— set_active_version would no-op and 'auto' would resolve to latest (#248)"
    )
    assert captured["api_key_id"] != "default"
    assert captured["tenant_id"] == TENANT


@pytest.mark.asyncio
async def test_read_resource_recovers_pk_from_header_when_state_lost():
    """odoo://auto/... resources honour the pin too (same seam, on_read_resource)."""
    _mw._cache_set(RAW_KEY, PK)
    _mw._cache_set_tenant(RAW_KEY, TENANT)
    _mw._cache_set_owner(RAW_KEY, None, False)

    captured = await _capture_identity_via_on_read_resource(
        _fake_request({"X-API-Key": RAW_KEY})
    )

    assert captured["api_key_id"] == PK
    assert captured["tenant_id"] == TENANT


@pytest.mark.asyncio
async def test_state_present_wins_over_header_fallback():
    """No regression: when request.state carries api_key_id, it is used as-is and
    the header fallback is not consulted (fallback only fires on state-loss)."""
    # Seed the cache with a DIFFERENT PK; if the fallback wrongly fired it would
    # clobber the state value, so this also guards the 'only on None' guard.
    _mw._cache_set(RAW_KEY, 999999)
    _mw._cache_set_tenant(RAW_KEY, 1)
    _mw._cache_set_owner(RAW_KEY, None, False)

    captured = await _capture_identity_via_on_call_tool(
        _fake_request({"X-API-Key": RAW_KEY}, state_api_key_id=PK, state_tenant_id=TENANT)
    )

    assert captured["api_key_id"] == PK
    assert captured["tenant_id"] == TENANT


@pytest.mark.asyncio
async def test_cold_cache_degrades_gracefully_to_default():
    """Negative control: state-loss AND a cold cache (TTL edge) → graceful 'default'
    fallback, exactly the pre-fix behaviour — no crash, no wrong PK invented."""
    # Cache intentionally NOT seeded for RAW_KEY (invalidated by the fixture).
    captured = await _capture_identity_via_on_call_tool(
        _fake_request({"X-API-Key": RAW_KEY})
    )
    assert captured["api_key_id"] == "default"


@pytest.mark.asyncio
async def test_no_header_no_recovery_stdio_path():
    """stdio / no-auth transport: no X-API-Key header → no recovery, 'default'
    (the legitimate no-op path; must not raise)."""
    _mw._cache_set(RAW_KEY, PK)  # warm, but no header to look it up by
    captured = await _capture_identity_via_on_call_tool(_fake_request({}))
    assert captured["api_key_id"] == "default"
