# tests/test_mcp_middleware.py
"""Regression tests for MCP middleware P0 fix: PG pool must be initialized at startup.

Marker: pytest.mark.postgres — tests that need a live PostgreSQL connection.

Verifies that an authenticated request returns HTTP 401 (not 500 / RuntimeError)
when the pool was NOT pre-initialized before the ASGI app started.  This proves
the lifespan startup hook in server.py.__main__ initialises the pool before
AuthMiddleware.dispatch runs.
"""
import asyncio

import httpx
import pytest
from asgi_lifespan import LifespanManager
from starlette.middleware import Middleware

pytestmark = pytest.mark.postgres


@pytest.fixture()
def _reset_pg_pool_for_middleware_test():
    """Wipe the module-level PG pool + store singletons before/after the test.

    We save and restore the session-scoped pool (created by the conftest pg_conn
    fixture) so subsequent postgres-marked tests remain unaffected.
    """
    import src.db.pg as pg_mod

    saved_pool = pg_mod._pool
    saved_auth = pg_mod._auth_store
    saved_repo = pg_mod._repo_store
    saved_job = pg_mod._job_store

    # Force pool to None — simulates a cold server start where init_pool() was
    # never called manually.
    pg_mod._pool = None
    pg_mod._auth_store = None
    pg_mod._repo_store = None
    pg_mod._job_store = None

    yield

    # Tear down any pool the test may have created via the lifespan hook.
    if pg_mod._pool is not None and pg_mod._pool is not saved_pool:
        try:
            pg_mod._pool.close()
        except Exception:
            pass

    # Restore session-scoped state.
    pg_mod._pool = saved_pool
    pg_mod._auth_store = saved_auth
    pg_mod._repo_store = saved_repo
    pg_mod._job_store = saved_job


@pytest.mark.asyncio
async def test_auth_middleware_returns_401_not_500_when_pool_not_pre_initialized(
    pg_conn,  # ensures PG is reachable — skip if not
    monkeypatch,
    _reset_pg_pool_for_middleware_test,
):
    """Authenticated request with a bad key must return 401, not 500.

    Regression for: APIKeyAuthMiddleware.dispatch calls auth_store() → get_pool()
    before any tool handler runs.  Prior to the P0 fix, get_pool() raised
    RuntimeError (pool not initialized) and every MCP request returned 500.

    After the fix, the lifespan startup hook initializes the pool before the app
    accepts traffic, so AuthMiddleware can safely call auth_store() and return
    401 for an invalid key.
    """
    import os

    # Point _ensure_pg() at the test DB so the lifespan hook can connect.
    test_dsn = os.getenv(
        "PG_TEST_DSN",
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
    )
    monkeypatch.setenv("PG_DSN", test_dsn)

    # Import lazily to pick up env changes; also ensure _pool is None (fixture above).
    from contextlib import asynccontextmanager

    from src.mcp.middleware import AuthMiddleware
    from src.mcp.server import _ensure_pg, mcp

    # Build the app the same way __main__ does, including the lifespan startup hook.

    app = mcp.http_app(
        transport="streamable-http",
        path="/mcp",
        middleware=[Middleware(AuthMiddleware)],
    )

    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan_with_pg(a):
        await asyncio.to_thread(_ensure_pg)
        async with existing_lifespan(a):
            yield

    app.router.lifespan_context = _lifespan_with_pg

    # Boot the app — lifespan hook runs here, initialising the PG pool.
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # Send request with a garbage API key — must be rejected by middleware.
            response = await client.get(
                "/mcp",
                headers={"X-API-Key": "definitely-invalid-key-for-regression-test"},
            )

    # 401 proves AuthMiddleware ran successfully (pool was initialised).
    # 500 would indicate RuntimeError from get_pool() — the pre-fix failure mode.
    assert response.status_code == 401, (
        f"Expected 401 (invalid API key) but got {response.status_code}. "
        f"Body: {response.text[:200]!r}. "
        "A 500 here means the PG pool was not initialized by the lifespan hook."
    )
