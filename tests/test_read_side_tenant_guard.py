# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-side authorization guard tests (defense-in-depth, ADR-0034 follow-up).

The write-side mint/reactivate/reassign fixes (m13_019 + branch
fix/free-key-tenant-isolation) guarantee a non-admin, user-owned API key is
never ``active=TRUE`` while ``tenant_id IS NULL``. This module pins the
COMPLEMENTARY read-side guard at the MCP auth choke point: even if some future
path leaves such an invalid "unrestricted" key, the MCP middleware must reject
it fail-closed.

INVARIANT (read-side): a request authenticated by a key that is
  - user-owned       (api_keys.user_id IS NOT NULL), AND
  - owner non-admin  (webui_users.is_admin = false), AND
  - unrestricted     (api_keys.tenant_id IS NULL)
must be REJECTED with the same fail-closed response the middleware uses for an
invalid key. Legitimately-unrestricted keys — system/CLI (user_id IS NULL) and
admin-owned (is_admin = true) — continue to work unchanged.

Unit tests (the pure predicate) run unconditionally. DB-backed store +
middleware integration tests require PostgreSQL (throwaway DSN only — the
``clean_pg`` family DROPs tables, so this MUST NOT run against the prod DSN).
"""
from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from src.mcp.middleware import (
    _CACHE_TS,
    _KEY_CACHE,
    _OWNER_CACHE,
    _TENANT_CACHE,
    AuthMiddleware,
    _cache_get_owner,
    _cache_set,
    _cache_set_owner,
    _cache_set_tenant,
    _is_null_tenant_escalation,
)

# ---------------------------------------------------------------------------
# Unit tests — pure predicate, no DB
# ---------------------------------------------------------------------------


class TestIsNullTenantEscalation:
    """The read-side invariant predicate _is_null_tenant_escalation()."""

    def test_user_owned_nonadmin_null_tenant_is_escalation(self):
        # user_id set, non-admin, tenant_id NULL → the forbidden state.
        assert _is_null_tenant_escalation(None, 7, False) is True

    def test_admin_owned_null_tenant_is_allowed(self):
        # admin may legitimately hold an unrestricted key.
        assert _is_null_tenant_escalation(None, 7, True) is False

    def test_system_cli_null_tenant_is_allowed(self):
        # user_id None → system/CLI key → unrestricted by design.
        assert _is_null_tenant_escalation(None, None, False) is False

    def test_user_owned_nonadmin_with_tenant_is_allowed(self):
        # A real tenant scope is already safe.
        assert _is_null_tenant_escalation(5, 7, False) is False

    def test_admin_owned_with_tenant_is_allowed(self):
        assert _is_null_tenant_escalation(5, 7, True) is False


# ---------------------------------------------------------------------------
# DB-backed tests — require postgres marker
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    from src.db.migrate import run_migrations

    run_migrations(clean_pg)
    return clean_pg


@pytest.fixture(autouse=True)
def clear_caches():
    """Wipe all three middleware caches before/after each test for hermetic state."""
    for cache in (_KEY_CACHE, _CACHE_TS, _TENANT_CACHE, _OWNER_CACHE):
        cache.clear()
    yield
    for cache in (_KEY_CACHE, _CACHE_TS, _TENANT_CACHE, _OWNER_CACHE):
        cache.clear()


def _insert_user(conn, username, email, *, is_admin=False):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, email, password_hash, is_admin, is_active) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (username, email, "x", is_admin, True),
        )
        uid = cur.fetchone()[0]
    if not conn.autocommit:
        conn.commit()
    return uid


def _force_invalid_unrestricted(conn, key_id):
    """Force the 'should never exist' state: active, user-owned, tenant_id NULL.

    The write-side fixes prevent this; we craft it directly to prove the
    read-side guard catches it regardless of how it arose.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_keys SET active = TRUE, tenant_id = NULL WHERE id = %s",
            (key_id,),
        )
    if not conn.autocommit:
        conn.commit()


def _app():
    captured = {}

    async def capture(request):
        captured["api_key_id"] = getattr(request.state, "api_key_id", "MISSING")
        captured["tenant_id"] = getattr(request.state, "tenant_id", "MISSING")
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/mcp", capture)])
    app.add_middleware(AuthMiddleware)
    return app, captured


async def _get(app, raw_key):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/mcp", headers={"X-API-Key": raw_key})


# ----- store: verify_api_key_full ------------------------------------------


@pytestmark_db
class TestVerifyApiKeyFull:
    """AuthStore.verify_api_key_full returns (key_id, tenant_id, user_id, is_admin)."""

    def test_user_owned_nonadmin_null_tenant(self, migrated_pg):
        from src.db.pg import auth_store

        store = auth_store()
        uid = _insert_user(migrated_pg, "u1", "u1@gmail.com")
        raw, _, key_id = store.create_api_key("k1", user_id=uid, tenant_id=None)
        # mint may have scoped it; force the invalid unrestricted state.
        _force_invalid_unrestricted(migrated_pg, key_id)

        result = store.verify_api_key_full(raw)
        assert result == (key_id, None, uid, False)

    def test_admin_owned_null_tenant(self, migrated_pg):
        from src.db.pg import auth_store

        store = auth_store()
        uid = _insert_user(migrated_pg, "boss", "boss@x.com", is_admin=True)
        raw, _, key_id = store.create_api_key("k2", user_id=uid, tenant_id=None)

        result = store.verify_api_key_full(raw)
        assert result == (key_id, None, uid, True)

    def test_system_cli_key_user_id_none(self, migrated_pg):
        from src.db.pg import auth_store

        store = auth_store()
        raw, _, key_id = store.create_api_key("cli", user_id=None, tenant_id=None)

        kid, tid, user_id, is_admin = store.verify_api_key_full(raw)
        assert kid == key_id
        assert tid is None
        assert user_id is None
        assert is_admin is False  # no owner row → coerced to False

    def test_scoped_key_returns_tenant(self, migrated_pg):
        from src.db.pg import auth_store

        store = auth_store()
        uid = _insert_user(migrated_pg, "u2", "u2@gmail.com")
        tid = store.get_public_tenant_id()
        raw, _, key_id = store.create_api_key("k3", user_id=uid, tenant_id=tid)

        result = store.verify_api_key_full(raw)
        assert result == (key_id, tid, uid, False)

    def test_invalid_key_returns_none(self, migrated_pg):
        from src.db.pg import auth_store

        assert auth_store().verify_api_key_full("osm_nope") is None

    def test_inactive_key_returns_none(self, migrated_pg):
        from src.db.pg import auth_store

        store = auth_store()
        raw, _, key_id = store.create_api_key("dead", user_id=None, tenant_id=None)
        store.deactivate_api_key(key_id)
        assert store.verify_api_key_full(raw) is None


# ----- middleware integration ----------------------------------------------


@pytestmark_db
class TestMiddlewareReadSideGuard:
    """AuthMiddleware enforces the read-side invariant fail-closed."""

    @pytest.mark.asyncio
    async def test_user_owned_nonadmin_null_tenant_rejected(self, migrated_pg):
        """The headline: a user-owned non-admin NULL-tenant key is denied."""
        from src.db.pg import auth_store

        store = auth_store()
        uid = _insert_user(migrated_pg, "ext", "exposed@gmail.com")
        raw, _, key_id = store.create_api_key("ext", user_id=uid, tenant_id=None)
        _force_invalid_unrestricted(migrated_pg, key_id)

        app, captured = _app()
        resp = await _get(app, raw)

        # Same fail-closed response as an invalid key.
        assert resp.status_code == 401
        # Request never reached the route handler.
        assert captured == {}

    @pytest.mark.asyncio
    async def test_admin_owned_null_tenant_allowed(self, migrated_pg):
        from src.db.pg import auth_store

        store = auth_store()
        uid = _insert_user(migrated_pg, "boss2", "boss2@x.com", is_admin=True)
        raw, _, key_id = store.create_api_key("adm", user_id=uid, tenant_id=None)

        app, captured = _app()
        resp = await _get(app, raw)

        assert resp.status_code == 200
        assert captured["api_key_id"] == key_id
        assert captured["tenant_id"] is None

    @pytest.mark.asyncio
    async def test_system_cli_null_tenant_allowed(self, migrated_pg):
        from src.db.pg import auth_store

        store = auth_store()
        raw, _, key_id = store.create_api_key("cli2", user_id=None, tenant_id=None)

        app, captured = _app()
        resp = await _get(app, raw)

        assert resp.status_code == 200
        assert captured["api_key_id"] == key_id
        assert captured["tenant_id"] is None

    @pytest.mark.asyncio
    async def test_normal_scoped_key_allowed_and_scope_resolves(self, migrated_pg):
        from src.db.pg import auth_store

        store = auth_store()
        uid = _insert_user(migrated_pg, "u3", "u3@gmail.com")
        tid = store.get_public_tenant_id()
        raw, _, key_id = store.create_api_key("scoped", user_id=uid, tenant_id=tid)

        app, captured = _app()
        resp = await _get(app, raw)

        assert resp.status_code == 200
        assert captured["api_key_id"] == key_id
        assert captured["tenant_id"] == tid

    @pytest.mark.asyncio
    async def test_guard_holds_on_cache_hit(self, migrated_pg):
        """A second request (cache-served) must still be rejected — the owner
        cache carries user_id + is_admin so the guard fires without a 2nd query."""
        from src.db.pg import auth_store

        store = auth_store()
        uid = _insert_user(migrated_pg, "ext2", "exposed2@gmail.com")
        raw, _, key_id = store.create_api_key("ext2", user_id=uid, tenant_id=None)
        _force_invalid_unrestricted(migrated_pg, key_id)

        app, _captured = _app()
        first = await _get(app, raw)
        second = await _get(app, raw)

        assert first.status_code == 401
        assert second.status_code == 401

    def test_owner_cache_carries_fields_one_roundtrip(self, migrated_pg):
        """verify_api_key_full populates _OWNER_CACHE via _cache_set_owner so a
        warm cache window resolves the guard without a second DB query."""
        from src.db.pg import auth_store

        store = auth_store()
        uid = _insert_user(migrated_pg, "u4", "u4@gmail.com")
        raw, _, key_id = store.create_api_key("k4", user_id=uid, tenant_id=None)

        kid, tid, user_id, is_admin = store.verify_api_key_full(raw)
        # Simulate what the middleware does after the single round-trip.
        _cache_set(raw, kid)
        _cache_set_tenant(raw, tid)
        _cache_set_owner(raw, user_id, is_admin)

        owner_hit, cached_uid, cached_admin = _cache_get_owner(raw)
        assert owner_hit is True
        assert cached_uid == uid
        assert cached_admin is False
