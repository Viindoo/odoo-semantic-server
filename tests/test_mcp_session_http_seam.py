# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issue #251 — the HTTP seam: two live MCP sessions on ONE API key resolve their
OWN pinned version.

This drives the REAL ``UsageLogMiddleware.on_call_tool`` hook with two distinct
``mcp-session-id`` headers (the transport's per-session id) and asserts that,
inside the tool body, ``_resolve_version('auto', ...)`` returns each session's
own pinned version — proving the header → ``_mcp_session_id_var`` → pin-store
plumbing keys per (api_key_id, mcp_session_id).

The only mock is the HTTP boundary (``get_http_request``): 8 test files run the
streamable transport with ``stateless_http=True`` (where the id is None), so we
inject the header directly via the ``_fake_request`` seam rather than opening a
real stateful socket. The in-memory pin store + the ContextVar reset in the
hook's ``finally`` are real.

Lazy-import discipline (commit 6cb7698): every module is imported inside the
helpers so a top-level binding cannot point at a stale module after another
test's ``sys.modules.pop`` + re-import of ``src.mcp.server``.
"""
from unittest.mock import MagicMock, patch

import pytest

PK = 70251  # numeric api_key PK on request.state (no #248 recovery needed)


def _clear_cache() -> None:
    from src.mcp.session import _cache, _cache_lock
    with _cache_lock:
        _cache.clear()


@pytest.fixture(autouse=True)
def _isolate_ctx_and_store():
    """Snapshot/restore the server ContextVars and clear the pin store so neither
    leaks across tests (token discipline + store hygiene)."""
    from src.mcp import server as _srv

    ak = _srv._api_key_id_var.set(_srv._api_key_id_var.get())
    sid = _srv._mcp_session_id_var.set(_srv._mcp_session_id_var.get())
    _clear_cache()
    try:
        yield
    finally:
        _srv._api_key_id_var.reset(ak)
        _srv._mcp_session_id_var.reset(sid)
        _clear_cache()


class _State:
    """A bare request.state with api_key_id present (no #248 state-loss here)."""

    def __init__(self, api_key_id) -> None:
        self.api_key_id = api_key_id


def _fake_request(mcp_session_id: str | None):
    from mcp.server.streamable_http import MCP_SESSION_ID_HEADER

    req = MagicMock()
    req.state = _State(PK)
    headers = {}
    if mcp_session_id is not None:
        headers[MCP_SESSION_ID_HEADER] = mcp_session_id
    req.headers = headers
    return req


async def _resolve_auto_via_on_call_tool(fake_req) -> str:
    """Run the real on_call_tool hook; inside call_next resolve 'auto' through the
    real _resolve_version funnel and return the version the tool body would see."""
    from src.mcp import server as _srv
    from src.mcp import tool_log_middleware as tlm

    captured: dict = {}
    mw = tlm.UsageLogMiddleware()
    ctx = MagicMock()
    ctx.message.name = "model_inspect"

    async def call_next(_ctx):
        # A MagicMock Neo4j session — a present pin means the funnel never reaches
        # the Neo4j fallback, so .run must not be called.
        captured["version"] = _srv._resolve_version("auto", captured["neo4j"])
        return MagicMock()

    captured["neo4j"] = MagicMock()
    with patch.object(tlm, "get_http_request", return_value=fake_req), \
         patch.object(tlm, "_log_tool_call_async"), \
         patch.object(tlm, "_audit_unscoped_tool_call_async"):
        await mw.on_call_tool(ctx, call_next)
    captured["neo4j"].run.assert_not_called()
    return captured["version"]


@pytest.mark.asyncio
async def test_two_session_headers_resolve_own_pinned_version():
    """Two distinct mcp-session-id headers under one API key each resolve their
    OWN pinned version through the real middleware → funnel path (#251)."""
    from src.mcp.session import set_active_version_db

    # Pin two different versions for the SAME key, keyed by two session ids.
    assert set_active_version_db(str(PK), "16.0", "sess-A") is True
    assert set_active_version_db(str(PK), "19.0", "sess-B") is True

    ver_a = await _resolve_auto_via_on_call_tool(_fake_request("sess-A"))
    ver_b = await _resolve_auto_via_on_call_tool(_fake_request("sess-B"))

    assert ver_a == "16.0", f"session A header must resolve its own pin 16.0, got {ver_a!r}"
    assert ver_b == "19.0", f"session B header must resolve its own pin 19.0, got {ver_b!r}"


@pytest.mark.asyncio
async def test_session_id_contextvar_reset_after_call():
    """The hook resets _mcp_session_id_var in finally so a header does not leak
    into the next call (no cross-session contamination)."""
    from src.mcp import server as _srv
    from src.mcp.session import set_active_version_db

    set_active_version_db(str(PK), "16.0", "sess-A")
    await _resolve_auto_via_on_call_tool(_fake_request("sess-A"))

    # After the call the server ContextVar is back at the no-session sentinel.
    assert _srv._get_mcp_session_id() == _srv._session._NO_SESSION_SENTINEL


@pytest.mark.asyncio
async def test_header_less_call_uses_nosession_bucket():
    """A call with NO mcp-session-id header resolves the '_nosession' pin (the
    stdio / single-session bucket) — unchanged pre-#251 behaviour."""
    from src.mcp import server as _srv
    from src.mcp.session import _NO_SESSION_SENTINEL, set_active_version_db

    # Pin under the no-session bucket only.
    set_active_version_db(str(PK), "15.0", _NO_SESSION_SENTINEL)

    ver = await _resolve_auto_via_on_call_tool(_fake_request(None))
    assert ver == "15.0", f"header-less call must use the _nosession bucket, got {ver!r}"
    assert _srv._session._NO_SESSION_SENTINEL == _NO_SESSION_SENTINEL


def test_get_mcp_session_id_no_request_returns_sentinel():
    """Outside any HTTP request, _get_mcp_session_id() returns the no-session
    sentinel (stdio / single-session fallback)."""
    from src.mcp import server as _srv

    # No ContextVar set (autouse fixture leaves it at default) and no HTTP
    # request bound → both resolution tiers yield the sentinel.
    assert _srv._get_mcp_session_id() == _srv._session._NO_SESSION_SENTINEL
