# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression for #248 follow-up — ContextVar set in the middleware is INVISIBLE
to the tool body on stateful streamable-HTTP (the gap the prior fix missed).

What the prior fix (test_mcp_session_header_fallback.py) covered
---------------------------------------------------------------
``UsageLogMiddleware.on_call_tool`` recovers the numeric PK from the
``X-API-Key`` header and writes it into ``_api_key_id_var`` BEFORE
``await call_next(...)``. That test drives ``on_call_tool`` and reads
``_get_api_key_id()`` *inside* a stub ``call_next`` — i.e. in the SAME
``contextvars.Context`` as the ``.set()``. There the value propagates, so the
test was green.

The production gap (this file)
------------------------------
On the real stateful streamable-HTTP transport FastMCP runs the tool BODY in a
``contextvars.Context`` that was snapshotted per-connection BEFORE the per-call
``on_call_tool`` runs. The middleware's ``_api_key_id_var.set()`` mutates a
context that is NOT an ancestor of the tool-body context, so the tool body still
reads the ``'default'`` sentinel — even though the middleware (reading in its
OWN context) logged the correct PK. That asymmetry is exactly why the journal
showed ``key_id=33`` in the log line yet ``set_active_version`` returned
"session context unavailable".

The mechanism is reproduced below by ``_run_in_foreign_context`` — a context
captured BEFORE the middleware's ``.set()``, in which ``_api_key_id_var`` is
still ``'default'``. We assert ``_get_api_key_id()`` STILL returns the recovered
numeric PK there, because the fix adds a per-request header-recovery fallback
INSIDE ``_get_api_key_id()`` itself (not only in the middleware).

These tests fail against the pre-fix ``_get_api_key_id()`` (which returned the
raw ``'default'`` sentinel) and pass after it. Pure unit test — NO DB, NO
postgres/neo4j marker. The only mock is the HTTP boundary
(``get_http_request``) and the warm-cache lookup (``_cache_get``).

NOTE: every module is imported LAZILY inside the helpers/fixtures (not at module
top) — other tests in the suite pop+re-import ``src.mcp.server``, so a top-level
binding could point at a stale module while ``patch`` targets the live one.
"""
import contextvars
from unittest.mock import MagicMock, patch

import pytest

RAW_KEY = "osm_test_248ctx_rawkey"
PK = 33  # the exact PK from the production journal (key_id=33)


@pytest.fixture(autouse=True)
def _isolate_ctx():
    """Snapshot/restore the server api_key_id ContextVar so a test that sets it
    to the sentinel cannot leak into a sibling test."""
    from src.mcp import server as _srv

    tok = _srv._api_key_id_var.set(_srv._api_key_id_var.get())
    try:
        yield
    finally:
        _srv._api_key_id_var.reset(tok)


def _force_default_contextvar():
    """Put ``_api_key_id_var`` in the ``'default'`` sentinel state, faithfully
    reproducing the tool-body context where the middleware's ``.set()`` (done in
    a DIFFERENT, non-ancestor context) is invisible."""
    from src.mcp import server as _srv

    _srv._api_key_id_var.set("default")


def _fake_request(headers: dict):
    req = MagicMock()
    req.headers = headers
    return req


def _read_api_key_id_with(headers: dict, *, cache_hit, cache_pk):
    """Read ``_get_api_key_id()`` while the ContextVar is the 'default' sentinel
    (foreign-context condition) and the HTTP/cache boundary is mocked.

    Runs inside a freshly captured ``contextvars.Context`` to mirror the FastMCP
    per-connection context boundary: the middleware's ``.set()`` happened in a
    context that is NOT this one, so the var is still ``'default'`` here.
    """
    from src.mcp import server as _srv

    def _body():
        _force_default_contextvar()
        # Sanity: confirm we really are in the broken state the prior fix missed.
        assert _srv._api_key_id_var.get() == "default"
        with patch.object(_srv, "_recover_api_key_id_from_request",
                          wraps=_srv._recover_api_key_id_from_request):
            with patch("fastmcp.server.dependencies.get_http_request",
                       return_value=_fake_request(headers)), \
                 patch("src.mcp.middleware._cache_get",
                       return_value=(cache_hit, cache_pk)):
                return _srv._get_api_key_id()

    # A brand-new context, mirroring the per-connection snapshot in which the
    # middleware's set() never ran.
    return contextvars.copy_context().run(_body)


def test_tool_body_recovers_pk_when_contextvar_is_default():
    """#248 follow-up CORE: ContextVar still 'default' in the tool body (the
    middleware's set() landed in a non-ancestor context) + warm cache + header
    → _get_api_key_id() recovers the real PK 33, NOT 'default'.

    Pre-fix this returned 'default' → set_active_version no-op → the exact
    "session context unavailable" production error.
    """
    got = _read_api_key_id_with(
        {"X-API-Key": RAW_KEY}, cache_hit=True, cache_pk=PK
    )
    assert got == PK, (
        f"tool body must recover PK {PK} from the X-API-Key header when the "
        f"ContextVar did not propagate; got {got!r} — this is the #248 wedge"
    )
    assert got != "default"


def test_no_header_stays_default_graceful():
    """Negative: no X-API-Key header (stdio/CLI) → no recovery, graceful
    'default'. Preserves the #249 honest-receipt no-op path; must not raise."""
    got = _read_api_key_id_with({}, cache_hit=False, cache_pk=None)
    assert got == "default"


def test_cold_cache_stays_default_graceful():
    """Negative: header present but cache miss (TTL edge) → graceful 'default'.
    No wrong PK invented; identical to pre-fix behaviour on a cold cache."""
    got = _read_api_key_id_with(
        {"X-API-Key": RAW_KEY}, cache_hit=False, cache_pk=None
    )
    assert got == "default"


def test_two_requests_resolve_independently_no_cross_leak():
    """SECURITY: two simulated requests with DIFFERENT keys each resolve to
    their OWN PK from their OWN header — never each other's. The fallback derives
    the id solely from the current request's X-API-Key, so no cross-request /
    cross-tenant leak is possible."""
    from src.mcp import server as _srv

    key_a, pk_a = "osm_key_AAAA", 11
    key_b, pk_b = "osm_key_BBBB", 22

    # _cache_get is keyed on the raw key; emulate the real per-key lookup.
    def _cache_by_key(raw):
        return ({key_a: (True, pk_a), key_b: (True, pk_b)}.get(raw, (False, None)))

    def _read(header_key):
        def _body():
            _force_default_contextvar()
            with patch("fastmcp.server.dependencies.get_http_request",
                       return_value=_fake_request({"X-API-Key": header_key})), \
                 patch("src.mcp.middleware._cache_get", side_effect=_cache_by_key):
                return _srv._get_api_key_id()
        return contextvars.copy_context().run(_body)

    assert _read(key_a) == pk_a
    assert _read(key_b) == pk_b
    # Re-read A again after B — still A's PK, proving no sticky/global bleed.
    assert _read(key_a) == pk_a


def test_explicit_contextvar_wins_over_header_fallback():
    """No regression: when the ContextVar DID propagate (non-'default' value),
    that value is returned and the header fallback is NOT consulted — even if the
    cache holds a different PK. The fallback only fires on the 'default'
    sentinel, preserving the ADR-0029 ContextVar mechanism as primary."""
    from src.mcp import server as _srv

    propagated_pk = 99

    def _body():
        _srv._api_key_id_var.set(propagated_pk)
        # If the fallback wrongly fired, it would clobber 99 with 12345.
        with patch("fastmcp.server.dependencies.get_http_request",
                   return_value=_fake_request({"X-API-Key": RAW_KEY})), \
             patch("src.mcp.middleware._cache_get", return_value=(True, 12345)):
            return _srv._get_api_key_id()

    assert contextvars.copy_context().run(_body) == propagated_pk
