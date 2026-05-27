# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ADR-0034 D4.1 WI-D: tenant_id plumbing.

Covers:
  - AuthStore.verify_api_key_tenant: returns (key_id, tenant_id) or None.
  - AuthStore.create_api_key: persists optional tenant_id.
  - AuthMiddleware.dispatch: sets request.state.tenant_id from verify_api_key_tenant.
  - server._get_tenant_id(): reads _tenant_id_var (ContextVar), defaults to None.
  - Tool-call/resource middleware wires tenant_id through the ContextVar bridge.
  - Legacy keys (tenant_id IS NULL) resolve to None throughout the chain.
  - Tool output unchanged (plumbing only).

Markers:
  - pytest.mark.postgres — any test touching a live DB.
  Unit tests (no DB) run unconditionally.
"""
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from src.mcp.middleware import (
    _CACHE_TS,
    _KEY_CACHE,
    _TENANT_CACHE,
    AuthMiddleware,
    _cache_get,
    _cache_get_tenant,
    _cache_invalidate_by_key_id,
    _cache_set,
    _cache_set_tenant,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_tenant_conn(pg_conn):
    """Ensure migrations are applied and clean auth tables before/after each test."""
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM usage_log")
        cur.execute("DELETE FROM api_keys")
        cur.execute("DELETE FROM tenants")
    if not pg_conn.autocommit:
        pg_conn.commit()
    yield pg_conn
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM usage_log")
        cur.execute("DELETE FROM api_keys")
        cur.execute("DELETE FROM tenants")
    if not pg_conn.autocommit:
        pg_conn.commit()


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


def _create_tenant(pg_conn, name: str) -> int:
    """Helper: insert a tenant row and return its id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
            (name,),
        )
        tid = cur.fetchone()[0]
    if not pg_conn.autocommit:
        pg_conn.commit()
    return tid


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


# ---------------------------------------------------------------------------
# DB-backed tests — require postgres marker
# ---------------------------------------------------------------------------


class TestVerifyApiKeyTenant:
    """AuthStore.verify_api_key_tenant — DB-backed."""

    def test_global_key_returns_none_tenant_id(self, pg_tenant_conn):
        """Key created without tenant_id returns (key_id, None)."""
        from src.db.pg import auth_store

        raw, _, key_id = auth_store().create_api_key("global-key")
        result = auth_store().verify_api_key_tenant(raw)
        assert result is not None
        got_key_id, got_tenant_id = result
        assert got_key_id == key_id
        assert got_tenant_id is None

    def test_tenant_bound_key_returns_tenant_id(self, pg_tenant_conn):
        """Key created with tenant_id returns (key_id, tenant_id)."""
        from src.db.pg import auth_store

        tid = _create_tenant(pg_tenant_conn, "acme")
        raw, _, key_id = auth_store().create_api_key("acme-key", tenant_id=tid)
        result = auth_store().verify_api_key_tenant(raw)
        assert result is not None
        got_key_id, got_tenant_id = result
        assert got_key_id == key_id
        assert got_tenant_id == tid

    def test_wrong_key_returns_none(self, pg_tenant_conn):
        """Non-existent key returns None."""
        from src.db.pg import auth_store

        result = auth_store().verify_api_key_tenant("osm_notavalidkey")
        assert result is None

    def test_inactive_key_returns_none(self, pg_tenant_conn):
        """Deactivated key returns None."""
        from src.db.pg import auth_store

        raw, _, key_id = auth_store().create_api_key("inactive-tenant-key")
        auth_store().deactivate_api_key(key_id)
        result = auth_store().verify_api_key_tenant(raw)
        assert result is None

    def test_create_api_key_persists_tenant_id(self, pg_tenant_conn):
        """create_api_key with tenant_id stores the FK correctly."""
        from src.db.pg import auth_store

        tid = _create_tenant(pg_tenant_conn, "beta-tenant")
        _, _, key_id = auth_store().create_api_key("beta-key", tenant_id=tid)

        with pg_tenant_conn.cursor() as cur:
            cur.execute("SELECT tenant_id FROM api_keys WHERE id = %s", (key_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == tid

    def test_create_api_key_null_tenant_id_by_default(self, pg_tenant_conn):
        """create_api_key without tenant_id stores NULL."""
        from src.db.pg import auth_store

        _, _, key_id = auth_store().create_api_key("no-tenant-key")

        with pg_tenant_conn.cursor() as cur:
            cur.execute("SELECT tenant_id FROM api_keys WHERE id = %s", (key_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] is None

    def test_verify_api_key_unchanged_return_type(self, pg_tenant_conn):
        """verify_api_key (original) still returns int | None (backward compat)."""
        from src.db.pg import auth_store

        tid = _create_tenant(pg_tenant_conn, "gamma-tenant")
        raw, _, key_id = auth_store().create_api_key("gamma-key", tenant_id=tid)
        result = auth_store().verify_api_key(raw)
        # Must still return an int, not a tuple
        assert result == key_id
        assert isinstance(result, int)

    def test_verify_api_key_tenant_updates_last_used_at(self, pg_tenant_conn):
        """verify_api_key_tenant updates last_used_at (same side effect as original)."""
        from src.db.pg import auth_store

        raw, _, key_id = auth_store().create_api_key("touch-test")

        with pg_tenant_conn.cursor() as cur:
            cur.execute("SELECT last_used_at FROM api_keys WHERE id = %s", (key_id,))
            initial = cur.fetchone()[0]
        assert initial is None

        auth_store().verify_api_key_tenant(raw)

        with pg_tenant_conn.cursor() as cur:
            cur.execute("SELECT last_used_at FROM api_keys WHERE id = %s", (key_id,))
            updated = cur.fetchone()[0]
        assert updated is not None


# ---------------------------------------------------------------------------
# Middleware integration — request.state.tenant_id
# ---------------------------------------------------------------------------


class TestMiddlewareSetsRequestStateTenantId:
    """AuthMiddleware writes request.state.tenant_id from verify_api_key_tenant."""

    @pytest.mark.asyncio
    async def test_tenant_bound_key_sets_tenant_id_on_request_state(self, pg_tenant_conn):
        """Middleware sets request.state.tenant_id = tenant_id for a bound key."""
        from src.db.pg import auth_store

        tid = _create_tenant(pg_tenant_conn, "middleware-tenant")
        raw, _, key_id = auth_store().create_api_key("mw-bound-key", tenant_id=tid)

        captured = {}

        async def capture(request):
            captured["api_key_id"] = request.state.api_key_id
            captured["tenant_id"] = getattr(request.state, "tenant_id", "MISSING")
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", capture)])
        app.add_middleware(AuthMiddleware)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/mcp", headers={"X-API-Key": raw})

        assert resp.status_code == 200
        assert captured["api_key_id"] == key_id
        assert captured["tenant_id"] == tid

    @pytest.mark.asyncio
    async def test_global_key_sets_none_tenant_id_on_request_state(self, pg_tenant_conn):
        """Middleware sets request.state.tenant_id = None for a global key."""
        from src.db.pg import auth_store

        raw, _, key_id = auth_store().create_api_key("mw-global-key")

        captured = {}

        async def capture(request):
            captured["api_key_id"] = request.state.api_key_id
            captured["tenant_id"] = getattr(request.state, "tenant_id", "MISSING")
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", capture)])
        app.add_middleware(AuthMiddleware)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/mcp", headers={"X-API-Key": raw})

        assert resp.status_code == 200
        assert captured["api_key_id"] == key_id
        assert captured["tenant_id"] is None

    @pytest.mark.asyncio
    async def test_cache_hit_still_sets_tenant_id(self, pg_tenant_conn):
        """Cache hit path (second request) correctly propagates tenant_id."""
        from src.db.pg import auth_store

        tid = _create_tenant(pg_tenant_conn, "cache-hit-tenant")
        raw, _, key_id = auth_store().create_api_key("cache-hit-key", tenant_id=tid)

        captured_per_req = []

        async def capture(request):
            captured_per_req.append({
                "api_key_id": request.state.api_key_id,
                "tenant_id": getattr(request.state, "tenant_id", "MISSING"),
            })
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", capture)])
        app.add_middleware(AuthMiddleware)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # First request — DB hit, populates both caches
            await client.get("/mcp", headers={"X-API-Key": raw})
            # Second request — cache hit
            await client.get("/mcp", headers={"X-API-Key": raw})

        assert len(captured_per_req) == 2
        for entry in captured_per_req:
            assert entry["api_key_id"] == key_id
            assert entry["tenant_id"] == tid, (
                f"Cache-hit request must also carry tenant_id={tid}; got {entry['tenant_id']}"
            )


# ---------------------------------------------------------------------------
# ContextVar wiring via UsageLogMiddleware
# ---------------------------------------------------------------------------


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
