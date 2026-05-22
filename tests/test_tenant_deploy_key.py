# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_tenant_deploy_key.py
"""Tests for ADR-0034 D7 WI-I: tenant self-service deploy-key endpoint.

Covers:
  - Tenant A's first call generates an Ed25519 keypair and returns the public key.
  - Second call is idempotent (same public key returned, no new row inserted).
  - Tenant B gets a different keypair from tenant A (CROSS-TENANT LEAK CHECK).
  - A request authenticated as tenant B's key can never retrieve tenant A's key.
  - A legacy/global API key (tenant_id IS NULL) returns HTTP 403 with a clear error.
  - Unauthenticated request (no X-API-Key) returns HTTP 401.

Auth surface: GET /api/tenant/deploy-key mounted on the MCP ASGI app at :8002.
Tenant identity comes from request.state.tenant_id (set by AuthMiddleware / WI-D);
no path or query parameter for tenant_id.

Markers:
  - pytest.mark.postgres — all tests touch a live PostgreSQL connection.
"""
import os
import unittest.mock as mock
from contextlib import contextmanager

import httpx
import pytest

from src.mcp.middleware import _CACHE_TS, _KEY_CACHE, _TENANT_CACHE

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _checkout_pg_yielding(conn):
    """Return a contextmanager callable that always yields *conn*.

    Used to patch ``src.mcp.server._checkout_pg`` with a context manager that
    yields the test connection for AuthMiddleware's key verification path.
    """
    @contextmanager
    def _cm():
        yield conn
    return _cm


def _build_mcp_app():
    """Return the MCP ASGI app with AuthMiddleware + deploy_key sub-app.

    Mirrors the __main__ block in src/mcp/server.py, adding the deploy_key
    router alongside the existing feedback router.
    """
    from fastapi import FastAPI
    from starlette.middleware import Middleware

    from src.mcp.middleware import AuthMiddleware
    from src.mcp.server import mcp
    from src.web_ui.routes import deploy_key as deploy_key_mod
    from src.web_ui.routes import feedback as feedback_mod

    app = mcp.http_app(
        transport="streamable-http",
        path="/mcp",
        middleware=[Middleware(AuthMiddleware)],
    )

    sub_app = FastAPI()
    sub_app.include_router(feedback_mod.router)
    sub_app.include_router(deploy_key_mod.router)
    app.mount("", sub_app)
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_auth_cache():
    """Wipe in-memory key/tenant caches before/after each test."""
    _KEY_CACHE.clear()
    _CACHE_TS.clear()
    _TENANT_CACHE.clear()
    yield
    _KEY_CACHE.clear()
    _CACHE_TS.clear()
    _TENANT_CACHE.clear()


@pytest.fixture
def pg_deploy_conn(pg_conn):
    """Ensure migrations run and relevant tables are clean before/after each test."""
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM usage_log")
        cur.execute("DELETE FROM api_keys")
        cur.execute("DELETE FROM ssh_key_pairs")
        cur.execute("DELETE FROM tenants")
    if not pg_conn.autocommit:
        pg_conn.commit()
    yield pg_conn
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM usage_log")
        cur.execute("DELETE FROM api_keys")
        cur.execute("DELETE FROM ssh_key_pairs")
        cur.execute("DELETE FROM tenants")
    if not pg_conn.autocommit:
        pg_conn.commit()


def _create_tenant(conn, name: str) -> int:
    """Insert a tenant row and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
            (name,),
        )
        tid = cur.fetchone()[0]
    if not conn.autocommit:
        conn.commit()
    return tid


def _ensure_fernet_key():
    """Set FERNET_KEY env var to a valid Fernet key if not already set.

    Allows tests to run without a production FERNET_KEY configured.
    """
    if not os.getenv("FERNET_KEY"):
        from cryptography.fernet import Fernet
        os.environ["FERNET_KEY"] = Fernet.generate_key().decode()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_a_gets_public_key_on_first_call(pg_deploy_conn):
    """Tenant A's first request generates an Ed25519 deploy key and returns it."""
    _ensure_fernet_key()
    from src.db.pg import auth_store

    tid_a = _create_tenant(pg_deploy_conn, "tenant-alpha")
    raw_a, _, _ = auth_store().create_api_key("alpha-key", tenant_id=tid_a)

    app = _build_mcp_app()

    with mock.patch("src.mcp.server._checkout_pg", _checkout_pg_yielding(pg_deploy_conn)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/tenant/deploy-key",
                headers={"X-API-Key": raw_a},
            )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "public_key" in body, f"Missing public_key in response: {body}"
    assert body["public_key"].startswith("ssh-ed25519 "), (
        f"Expected OpenSSH Ed25519 public key, got: {body['public_key'][:60]}"
    )
    assert "instructions" in body, "Missing instructions field"

    # Verify row persisted in DB
    with pg_deploy_conn.cursor() as cur:
        cur.execute(
            "SELECT public_key, key_type, tenant_id FROM ssh_key_pairs "
            "WHERE tenant_id = %s AND key_type = 'deploy_key'",
            (tid_a,),
        )
        row = cur.fetchone()
    assert row is not None, "Deploy key row not persisted in ssh_key_pairs"
    assert row[0] == body["public_key"]
    assert row[1] == "deploy_key"
    assert row[2] == tid_a


@pytest.mark.asyncio
async def test_second_call_is_idempotent(pg_deploy_conn):
    """Second call for the same tenant returns the same public key without creating new rows."""
    _ensure_fernet_key()
    from src.db.pg import auth_store

    tid_a = _create_tenant(pg_deploy_conn, "tenant-beta")
    raw_a, _, _ = auth_store().create_api_key("beta-key", tenant_id=tid_a)

    app = _build_mcp_app()

    with mock.patch("src.mcp.server._checkout_pg", _checkout_pg_yielding(pg_deploy_conn)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp1 = await client.get(
                "/api/tenant/deploy-key",
                headers={"X-API-Key": raw_a},
            )
            _KEY_CACHE.clear()
            _CACHE_TS.clear()
            _TENANT_CACHE.clear()
            resp2 = await client.get(
                "/api/tenant/deploy-key",
                headers={"X-API-Key": raw_a},
            )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["public_key"] == resp2.json()["public_key"], (
        "Second call returned a different public key — should be idempotent"
    )

    # Exactly one deploy_key row for this tenant
    with pg_deploy_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ssh_key_pairs "
            "WHERE tenant_id = %s AND key_type = 'deploy_key'",
            (tid_a,),
        )
        count = cur.fetchone()[0]
    assert count == 1, f"Expected exactly 1 deploy_key row, found {count}"


@pytest.mark.asyncio
async def test_tenant_b_gets_different_key_from_tenant_a(pg_deploy_conn):
    """CROSS-TENANT LEAK CHECK: Tenant B's public key must differ from Tenant A's.

    This ensures a different keypair is generated per tenant — not a shared key.
    """
    _ensure_fernet_key()
    from src.db.pg import auth_store

    tid_a = _create_tenant(pg_deploy_conn, "tenant-gamma-a")
    tid_b = _create_tenant(pg_deploy_conn, "tenant-gamma-b")
    raw_a, _, _ = auth_store().create_api_key("gamma-a-key", tenant_id=tid_a)
    raw_b, _, _ = auth_store().create_api_key("gamma-b-key", tenant_id=tid_b)

    app = _build_mcp_app()

    with mock.patch("src.mcp.server._checkout_pg", _checkout_pg_yielding(pg_deploy_conn)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp_a = await client.get(
                "/api/tenant/deploy-key",
                headers={"X-API-Key": raw_a},
            )
            _KEY_CACHE.clear()
            _CACHE_TS.clear()
            _TENANT_CACHE.clear()
            resp_b = await client.get(
                "/api/tenant/deploy-key",
                headers={"X-API-Key": raw_b},
            )

    assert resp_a.status_code == 200, f"Tenant A failed: {resp_a.text}"
    assert resp_b.status_code == 200, f"Tenant B failed: {resp_b.text}"

    key_a = resp_a.json()["public_key"]
    key_b = resp_b.json()["public_key"]

    assert key_a != key_b, (
        "CROSS-TENANT LEAK: Tenant A and Tenant B received the SAME deploy-key public key. "
        "Each tenant must have a distinct keypair."
    )


@pytest.mark.asyncio
async def test_tenant_b_key_cannot_retrieve_tenant_a_deploy_key(pg_deploy_conn):
    """CROSS-TENANT LEAK CHECK: Authenticating as tenant B must NOT return tenant A's key.

    This is the critical security test. Tenant B's API key resolves to tenant_id=B;
    the endpoint uses ONLY request.state.tenant_id — so B always receives B's
    key (or a freshly generated B key), never A's.
    """
    _ensure_fernet_key()
    from src.db.pg import auth_store

    tid_a = _create_tenant(pg_deploy_conn, "tenant-delta-a")
    tid_b = _create_tenant(pg_deploy_conn, "tenant-delta-b")
    raw_a, _, _ = auth_store().create_api_key("delta-a-key", tenant_id=tid_a)
    raw_b, _, _ = auth_store().create_api_key("delta-b-key", tenant_id=tid_b)

    app = _build_mcp_app()

    with mock.patch("src.mcp.server._checkout_pg", _checkout_pg_yielding(pg_deploy_conn)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # First: create tenant A's deploy key
            resp_a = await client.get(
                "/api/tenant/deploy-key",
                headers={"X-API-Key": raw_a},
            )
            _KEY_CACHE.clear()
            _CACHE_TS.clear()
            _TENANT_CACHE.clear()
            # Now: authenticate as tenant B — must get B's key, NOT A's
            resp_b = await client.get(
                "/api/tenant/deploy-key",
                headers={"X-API-Key": raw_b},
            )

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200

    key_a = resp_a.json()["public_key"]
    key_b = resp_b.json()["public_key"]

    assert key_b != key_a, (
        "CROSS-TENANT LEAK: Authenticating as Tenant B returned Tenant A's deploy-key. "
        "The endpoint MUST use request.state.tenant_id (from authenticated key), "
        "not any user-controlled parameter."
    )

    # Verify DB isolation: B's row is distinct from A's
    with pg_deploy_conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, public_key FROM ssh_key_pairs WHERE key_type = 'deploy_key' "
            "ORDER BY tenant_id",
        )
        rows = cur.fetchall()

    tenant_ids = [r[0] for r in rows]
    public_keys = [r[1] for r in rows]
    assert tid_a in tenant_ids, "Tenant A deploy-key row missing from DB"
    assert tid_b in tenant_ids, "Tenant B deploy-key row missing from DB"
    assert len(set(public_keys)) == 2, (
        "Both tenants share the same public key in the DB — this is a leak."
    )


@pytest.mark.asyncio
async def test_legacy_global_key_returns_403(pg_deploy_conn):
    """A legacy/global key (tenant_id IS NULL) receives HTTP 403 with a clear error message."""
    _ensure_fernet_key()
    from src.db.pg import auth_store

    # Global key — no tenant_id
    raw_global, _, _ = auth_store().create_api_key("global-admin-key")

    app = _build_mcp_app()

    with mock.patch("src.mcp.server._checkout_pg", _checkout_pg_yielding(pg_deploy_conn)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/tenant/deploy-key",
                headers={"X-API-Key": raw_global},
            )

    assert resp.status_code == 403, (
        f"Expected 403 for global key, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "no_tenant_bound", (
        f"Expected error='no_tenant_bound', got: {body}"
    )
    assert "detail" in body, "Response must include a 'detail' explanation"


@pytest.mark.asyncio
async def test_unauthenticated_request_returns_401():
    """Request without X-API-Key header returns 401 (AuthMiddleware enforcement)."""
    from fastapi import FastAPI
    from starlette.middleware import Middleware

    from src.mcp.middleware import AuthMiddleware
    from src.mcp.server import mcp
    from src.web_ui.routes import deploy_key as deploy_key_mod

    app = mcp.http_app(
        transport="streamable-http",
        path="/mcp",
        middleware=[Middleware(AuthMiddleware)],
    )
    sub_app = FastAPI()
    sub_app.include_router(deploy_key_mod.router)
    app.mount("", sub_app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/tenant/deploy-key")

    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"
