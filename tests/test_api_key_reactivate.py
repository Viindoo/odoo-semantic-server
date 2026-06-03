# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_api_key_reactivate.py
"""Tests for POST /api/api-keys/{key_id}/reactivate (M10B P0-ext W-4).

Verifies:
  - Admin can reactivate any inactive key unconditionally.
  - Owner (non-admin) can reactivate their own inactive key.
  - Non-owner non-admin -> 403.
  - Unauthenticated -> 401.
  - Unknown key_id -> 404.
  - Idempotent: reactivating an already-active key returns 200.
  - Audit log entry written with action='api_key.reactivate'.
  - Cache invalidation called with correct key_id.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Auth bypass is used for data-correctness tests; auth-gating tests patch directly.
"""
import os

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Auth bypass management (mirrors pattern from test_admin_api_key_plan.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_auth_bypass():
    """Enable test auth bypass for data-correctness tests.

    Mirrors the pattern in test_admin_api_key_plan.py.
    """
    prev = os.environ.get("WEBUI_AUTH_DISABLED")
    os.environ["WEBUI_AUTH_DISABLED"] = "1"
    yield
    if prev is None:
        os.environ.pop("WEBUI_AUTH_DISABLED", None)
    else:
        os.environ["WEBUI_AUTH_DISABLED"] = prev


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def migrated_pg(clean_pg):
    """Run all migrations on a clean DB, yield the connection."""
    run_migrations(clean_pg)
    yield clean_pg


def _seed_user(pg_conn, *, username: str = "alice", is_admin: bool = False) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active) "
            "VALUES (%s, 'hash', %s, TRUE) RETURNING id",
            (username, is_admin),
        )
        uid = cur.fetchone()[0]
    pg_conn.commit()
    return uid


def _seed_api_key(
    pg_conn,
    *,
    name: str = "test-key",
    user_id: int | None = None,
    active: bool = True,
) -> int:
    """Insert an api_key row with the given active state."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix, active, user_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (name, f"hash_{name}", f"osm_{name[:8]}", active, user_id),
        )
        kid = cur.fetchone()[0]
    pg_conn.commit()
    return kid


def _deactivate_key(pg_conn, key_id: int) -> None:
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE api_keys SET active = FALSE WHERE id = %s", (key_id,))
    pg_conn.commit()


def _get_key_active(pg_conn, key_id: int) -> bool:
    with pg_conn.cursor() as cur:
        cur.execute("SELECT active FROM api_keys WHERE id = %s", (key_id,))
        row = cur.fetchone()
    return bool(row[0]) if row else False


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestApiKeyReactivate:
    @pytest.mark.asyncio
    async def test_admin_reactivates_any_inactive_key(self, migrated_pg):
        """Admin session: deactivate key owned by another user, then reactivate.

        Expected: 200, active=True.
        """
        _seed_user(migrated_pg, username="admin_r1", is_admin=True)
        other_uid = _seed_user(migrated_pg, username="user_r1")
        key_id = _seed_api_key(migrated_pg, name="key-r1-inactive", user_id=other_uid, active=True)
        _deactivate_key(migrated_pg, key_id)
        assert not _get_key_active(migrated_pg, key_id)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(f"/api/api-keys/{key_id}/reactivate")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["key_id"] == key_id
        assert data["active"] is True

        # Verify DB
        assert _get_key_active(migrated_pg, key_id)

    @pytest.mark.asyncio
    async def test_owner_reactivates_own_inactive_key(self, migrated_pg):
        """Non-admin owner: deactivate own key then reactivate -> 200 active=True."""
        import src.web_ui.auth as auth_mod

        owner_id = _seed_user(migrated_pg, username="owner_r2", is_admin=False)
        key_id = _seed_api_key(migrated_pg, name="key-r2-owner", user_id=owner_id, active=True)
        _deactivate_key(migrated_pg, key_id)
        assert not _get_key_active(migrated_pg, key_id)

        orig_cuid = auth_mod.current_user_id
        try:
            # Non-admin owner session. Stub current_user_id ONLY — not
            # is_test_bypass_active: the bypass keeps the auth middleware out of
            # the way so the request reaches the route, while the fully-stubbed
            # current_user_id makes the route's ownership guard run against
            # owner_id. Patching is_test_bypass_active here is unnecessary
            # (current_user_id is already replaced) and was harmful — it mutated
            # only the middleware's copied binding, making this test
            # order-dependent (issue #220 follow-up).
            auth_mod.current_user_id = lambda req: owner_id

            app = create_app()
            async with _async_client(app) as client:
                resp = await client.post(f"/api/api-keys/{key_id}/reactivate")
        finally:
            auth_mod.current_user_id = orig_cuid

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["active"] is True

        assert _get_key_active(migrated_pg, key_id)

    @pytest.mark.asyncio
    async def test_non_owner_non_admin_403(self, migrated_pg):
        """Non-admin, non-owner: attempt to reactivate another user's key -> 403."""
        import src.web_ui.auth as auth_mod

        owner_id = _seed_user(migrated_pg, username="owner_r3", is_admin=False)
        attacker_id = _seed_user(migrated_pg, username="attacker_r3", is_admin=False)
        key_id = _seed_api_key(migrated_pg, name="key-r3-owner", user_id=owner_id, active=False)

        orig_cuid = auth_mod.current_user_id
        try:
            # Stub current_user_id ONLY (see test_owner_reactivates_own_inactive_key):
            # the non-owner attacker's id drives the route ownership guard while the
            # auth-middleware bypass stays on. Not patching is_test_bypass_active
            # keeps this test order-independent (issue #220 follow-up).
            auth_mod.current_user_id = lambda req: attacker_id

            app = create_app()
            async with _async_client(app) as client:
                resp = await client.post(f"/api/api-keys/{key_id}/reactivate")
        finally:
            auth_mod.current_user_id = orig_cuid

        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, migrated_pg):
        """No session -> 401."""
        import src.web_ui.auth as auth_mod

        key_id = _seed_api_key(migrated_pg, name="key-r4-unauth", active=False)

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: None

            app = create_app()
            async with _async_client(app) as client:
                resp = await client.post(f"/api/api-keys/{key_id}/reactivate")
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert resp.status_code == 401, resp.text

    @pytest.mark.asyncio
    async def test_unknown_key_404(self, migrated_pg):
        """Admin reactivating non-existent key_id -> 404."""
        _seed_user(migrated_pg, username="admin_r5", is_admin=True)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post("/api/api-keys/999999/reactivate")

        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_idempotent_reactivate_already_active(self, migrated_pg):
        """Admin reactivates a key that is already active.

        Expected: 200 (idempotent, mirrors deactivate behaviour).
        """
        _seed_user(migrated_pg, username="admin_r6", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_r6")
        key_id = _seed_api_key(migrated_pg, name="key-r6-active", user_id=user_id, active=True)
        # Key is already active — reactivate should still return 200
        assert _get_key_active(migrated_pg, key_id)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(f"/api/api-keys/{key_id}/reactivate")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["active"] is True

    @pytest.mark.asyncio
    async def test_reactivate_audit_logged(self, migrated_pg):
        """Successful reactivation writes admin_audit_log with action='api_key.reactivate'."""
        _seed_user(migrated_pg, username="admin_r7", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_r7")
        key_id = _seed_api_key(migrated_pg, name="key-r7-audit", user_id=user_id, active=False)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(f"/api/api-keys/{key_id}/reactivate")

        assert resp.status_code == 200, resp.text

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT action, target FROM admin_audit_log "
                "WHERE action = 'api_key.reactivate' AND target = %s",
                (str(key_id),),
            )
            rows = cur.fetchall()

        assert rows, "Expected audit log entry with action='api_key.reactivate'"
        action, target = rows[0]
        assert action == "api_key.reactivate"
        assert target == str(key_id)

    @pytest.mark.asyncio
    async def test_reactivate_evicts_stale_auth_cache_for_key(self, migrated_pg):
        """Reactivation evicts the key's cached auth entries (so the next auth
        re-verifies against the now-active DB row).

        Asserts the observable effect: a primed in-memory auth-cache entry for
        the key (and its plan-cache entry) is GONE after reactivation, so the
        middleware's next lookup is a cache miss and re-reads the fresh DB state.
        This is stronger than asserting a helper was called with key_id — it
        verifies the cache actually no longer serves the stale (deactivated)
        snapshot, the bug a missing/incorrect invalidation would introduce.
        """
        import src.mcp.middleware as mw

        _seed_user(migrated_pg, username="admin_r8", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_r8")
        key_id = _seed_api_key(migrated_pg, name="key-r8-cache", user_id=user_id, active=False)

        # Prime the per-request auth caches as if this key had been used recently.
        raw_key = f"raw-secret-for-{key_id}"
        mw._cache_set(raw_key, key_id)
        mw._PLAN_CACHE[key_id] = (
            mw.PlanInfo(plan_id=1, slug="free", quota_calls_per_month=200, rate_limit_rpm=30),
            mw.time.monotonic(),
        )
        assert mw._cache_get(raw_key)[0] is True, "precondition: key cached"

        try:
            app = create_app()
            async with _async_client(app) as client:
                resp = await client.post(f"/api/api-keys/{key_id}/reactivate")
            assert resp.status_code == 200, resp.text

            # The stale auth-cache entry must be evicted → next lookup misses
            # and the middleware re-verifies against the reactivated DB row.
            hit, _ = mw._cache_get(raw_key)
            assert hit is False, (
                "reactivation must evict the key's cached auth entry so the next "
                "request re-verifies against the fresh DB state"
            )
            assert key_id not in mw._PLAN_CACHE, (
                "reactivation must drop the stale plan-cache entry for the key"
            )
        finally:
            mw._cache_invalidate(raw_key)
            mw._PLAN_CACHE.pop(key_id, None)
