# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_tenant_id_plumbing_unit.py
"""Pure-logic unit tests extracted from test_tenant_id_plumbing.py (WS-D / DD2 demote).

These tests exercise the in-memory tenant/key cache primitives
(``_cache_set`` / ``_cache_get_tenant`` / ``_cache_invalidate_by_key_id`` over the
module-global ``_KEY_CACHE`` / ``_TENANT_CACHE`` / ``_CACHE_TS`` dicts) and the
``_tenant_id_var`` ContextVar accessors.  They open NO Postgres connection, run
NO migrations, and execute NO SQL — and crucially they do NOT exercise any
RLS / GUC / SET ROLE / cross-tenant enforcement path (the genuine tenant-isolation
invariants live in test_billing_rls.py / test_embeddings_rls.py /
test_cross_tenant_isolation.py, which stay on the postgres tier — DD2 FALSE-DEMOTE).

G08 note: this is the "pure plumbing logic không chạm DB" case explicitly allowed
to demote — NOT a weakening of any tenant-isolation invariant.  The DB-backed
verify/middleware tests (which need real api_keys/tenants rows) remain in the
parent file under ``pytestmark = pytest.mark.postgres``.

DD2 evidence: confirmed in-memory cache dict ops + ContextVar set/reset only.
"""
import pytest

from src.mcp.middleware import (
    _CACHE_TS,
    _KEY_CACHE,
    _TENANT_CACHE,
    _cache_get,
    _cache_get_tenant,
    _cache_invalidate_by_key_id,
    _cache_set,
    _cache_set_tenant,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """Wipe both caches before/after each test."""
    _KEY_CACHE.clear()
    _CACHE_TS.clear()
    _TENANT_CACHE.clear()
    yield
    _KEY_CACHE.clear()
    _CACHE_TS.clear()
    _TENANT_CACHE.clear()


# ---------------------------------------------------------------------------
# Unit tests — no DB
# ---------------------------------------------------------------------------

class TestTenantCacheFunctions:
    """_cache_set_tenant / _cache_get_tenant in middleware."""

    def test_set_and_get_tenant_id(self):
        """Store a non-None tenant_id and retrieve it."""
        _cache_set("my_raw_key", 42)
        _cache_set_tenant("my_raw_key", 7)
        hit, tid = _cache_get_tenant("my_raw_key")
        assert hit is True
        assert tid == 7

    def test_set_and_get_none_tenant_id(self):
        """Global key (tenant_id=None) is stored and retrieved correctly."""
        _cache_set("global_key", 99)
        _cache_set_tenant("global_key", None)
        hit, tid = _cache_get_tenant("global_key")
        assert hit is True
        assert tid is None

    def test_tenant_cache_miss_on_empty(self):
        """Miss returns (False, None) when nothing stored."""
        hit, tid = _cache_get_tenant("nonexistent")
        assert hit is False
        assert tid is None

    def test_tenant_cache_shares_ttl_with_key_cache(self):
        """tenant cache returns (True, None) when key cache is warm but tenant was not set."""
        # Simulate old code path: _cache_set without _cache_set_tenant
        _cache_set("old_path_key", 5)
        # No _cache_set_tenant call
        hit, tid = _cache_get_tenant("old_path_key")
        # Hit because key cache has a valid timestamp — tid falls back to None
        assert hit is True
        assert tid is None

    def test_cache_invalidate_by_key_id_clears_tenant_cache(self):
        """_cache_invalidate_by_key_id removes from _TENANT_CACHE as well."""
        _cache_set("key_to_invalidate", 3)
        _cache_set_tenant("key_to_invalidate", 10)
        _cache_invalidate_by_key_id(3)
        hit, _ = _cache_get("key_to_invalidate")
        tenant_hit, _ = _cache_get_tenant("key_to_invalidate")
        assert hit is False
        assert tenant_hit is False


class TestGetTenantIdAccessor:
    """server._get_tenant_id() — ContextVar accessor."""

    def test_returns_none_when_not_set(self):
        from src.mcp.server import _get_tenant_id
        # Should return None (default) with no active request context.
        assert _get_tenant_id() is None

    def test_returns_value_when_set(self):
        from src.mcp import server as _server
        from src.mcp.server import _get_tenant_id

        token = _server._tenant_id_var.set(42)
        try:
            assert _get_tenant_id() == 42
        finally:
            _server._tenant_id_var.reset(token)

    def test_returns_none_after_clear(self):
        from src.mcp import server as _server
        from src.mcp.server import _get_tenant_id

        token = _server._tenant_id_var.set(99)
        _server._tenant_id_var.reset(token)
        assert _get_tenant_id() is None


class TestSetServerTenantIdHelper:
    """_set_server_tenant_id / _reset_server_tenant_id correctly set + restore
    the _tenant_id_var ContextVar via the token returned by .set()."""

    def test_set_populates_context_var(self):
        from src.mcp.server import _get_tenant_id
        from src.mcp.tool_log_middleware import (
            _reset_server_tenant_id,
            _set_server_tenant_id,
        )

        token = _set_server_tenant_id(77)
        try:
            assert _get_tenant_id() == 77
        finally:
            _reset_server_tenant_id(token)

    def test_reset_restores_to_none(self):
        from src.mcp.server import _get_tenant_id
        from src.mcp.tool_log_middleware import (
            _reset_server_tenant_id,
            _set_server_tenant_id,
        )

        token = _set_server_tenant_id(55)
        _reset_server_tenant_id(token)
        assert _get_tenant_id() is None

    def test_set_none_then_reset_is_safe(self):
        """Setting None (the default) then resetting must not raise."""
        from src.mcp.server import _get_tenant_id
        from src.mcp.tool_log_middleware import (
            _reset_server_tenant_id,
            _set_server_tenant_id,
        )

        token = _set_server_tenant_id(None)  # no-op value, must not raise
        _reset_server_tenant_id(token)
        assert _get_tenant_id() is None
