# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_admin_users_bulk_plan.py
"""Tests for PATCH /api/admin/users/{user_id}/plan (M10B P0-ext W-3).

Verifies:
  - Cascade updates ALL keys (active + inactive) owned by user.
  - User with 0 keys returns 200 + keys_updated=0.
  - 422 for non-existent plan_id.
  - 404 for unknown user_id.
  - 403 for non-admin session.
  - Audit log entry with action='user.set_plan_cascade' + details.keys_updated=N.
  - Cache invalidation called once per affected key.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import os
from unittest.mock import patch

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Auth bypass management
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_auth_bypass():
    prev = os.environ.get("WEBUI_AUTH_DISABLED")
    os.environ["WEBUI_AUTH_DISABLED"] = "1"
    yield
    if prev is None:
        os.environ.pop("WEBUI_AUTH_DISABLED", None)
    else:
        os.environ["WEBUI_AUTH_DISABLED"] = prev


# ---------------------------------------------------------------------------
# Schema / seed helpers
# ---------------------------------------------------------------------------


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    yield clean_pg


def _seed_user(pg_conn, *, username: str = "alice", is_admin: bool = False) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active) "
            "VALUES (%s, 'hash', %s, TRUE) RETURNING id",
            (username, is_admin),
        )
        return cur.fetchone()[0]


def _seed_api_key(
    pg_conn,
    *,
    name: str = "test-key",
    user_id: int | None = None,
    active: bool = True,
) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix, active, user_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (name, f"hash_{name}", f"osm_{name[:8]}", active, user_id),
        )
        return cur.fetchone()[0]


def _get_plan_id(pg_conn, slug: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = %s", (slug,))
        return cur.fetchone()[0]


def _find_route(app, path: str, method: str):
    """Return the APIRoute matching an exact path + HTTP method."""
    for r in app.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r
    raise AssertionError(f"route {method} {path} not found")


def _collect_dependency_calls(dependant) -> list:
    """Flatten every callable in a FastAPI Dependant tree (route + sub-deps)."""
    calls = []
    stack = [dependant]
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            calls.append(dep.call)
        stack.extend(dep.dependencies)
    return calls


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestAdminCascadeSetUserPlan:
    @pytest.mark.asyncio
    async def test_cascade_updates_all_user_keys(self, migrated_pg):
        """Cascade updates ALL 3 keys of the user; response.keys_updated=3."""
        _seed_user(migrated_pg, username="admin_ca", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_ca")
        for i in range(3):
            _seed_api_key(migrated_pg, name=f"ckey-{i}", user_id=user_id)
        plan_id = _get_plan_id(migrated_pg, "pro")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/users/{user_id}/plan",
                json={"plan_id": plan_id},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["user_id"] == user_id
        assert data["plan_id"] == plan_id
        assert data["keys_updated"] == 3

        # Verify all keys updated in DB
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT id, plan_id FROM api_keys WHERE user_id = %s ORDER BY id",
                (user_id,),
            )
            rows = cur.fetchall()
        assert all(r[1] == plan_id for r in rows), "All keys must have new plan_id"

    @pytest.mark.asyncio
    async def test_cascade_includes_inactive_keys(self, migrated_pg):
        """Cascade covers active AND inactive keys (D3)."""
        _seed_user(migrated_pg, username="admin_cb", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_cb")
        # 2 active + 1 inactive
        _seed_api_key(migrated_pg, name="ckey-cb-1", user_id=user_id, active=True)
        _seed_api_key(migrated_pg, name="ckey-cb-2", user_id=user_id, active=True)
        inactive_key_id = _seed_api_key(
            migrated_pg, name="ckey-cb-3", user_id=user_id, active=False
        )
        plan_id = _get_plan_id(migrated_pg, "team")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/users/{user_id}/plan",
                json={"plan_id": plan_id},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["keys_updated"] == 3  # includes inactive

        # Verify inactive key also updated
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT plan_id FROM api_keys WHERE id = %s", (inactive_key_id,)
            )
            row = cur.fetchone()
        assert row[0] == plan_id, "Inactive key must also receive the new plan"

    @pytest.mark.asyncio
    async def test_cascade_user_with_zero_keys(self, migrated_pg):
        """User with no keys returns 200 + keys_updated=0."""
        _seed_user(migrated_pg, username="admin_cc", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_cc_nokeys")
        plan_id = _get_plan_id(migrated_pg, "free")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/users/{user_id}/plan",
                json={"plan_id": plan_id},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["keys_updated"] == 0

    @pytest.mark.asyncio
    async def test_cascade_invalid_plan_id_422(self, migrated_pg):
        """Non-existent plan_id -> 422."""
        _seed_user(migrated_pg, username="admin_cd", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_cd")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/users/{user_id}/plan",
                json={"plan_id": 999999},
            )

        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_cascade_unknown_user_404(self, migrated_pg):
        """Non-existent user_id -> 404."""
        _seed_user(migrated_pg, username="admin_ce", is_admin=True)
        plan_id = _get_plan_id(migrated_pg, "free")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                "/api/admin/users/999999/plan",
                json={"plan_id": plan_id},
            )

        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_cascade_non_admin_403(self, migrated_pg):
        """Non-admin session -> 403."""
        import src.web_ui.auth as auth_mod

        _seed_user(migrated_pg, username="admin_cf_main", is_admin=True)
        non_admin_id = _seed_user(migrated_pg, username="non_admin_cf", is_admin=False)
        plan_id = _get_plan_id(migrated_pg, "free")

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: non_admin_id

            app = create_app()
            async with _async_client(app) as client:
                resp = await client.patch(
                    f"/api/admin/users/{non_admin_id}/plan",
                    json={"plan_id": plan_id},
                )
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_cascade_audit_logged(self, migrated_pg):
        """Successful cascade writes admin_audit_log with action='user.set_plan_cascade'."""
        _seed_user(migrated_pg, username="admin_cg", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_cg")
        _seed_api_key(migrated_pg, name="ckey-cg-1", user_id=user_id)
        _seed_api_key(migrated_pg, name="ckey-cg-2", user_id=user_id)
        plan_id = _get_plan_id(migrated_pg, "pro")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/users/{user_id}/plan",
                json={"plan_id": plan_id},
            )

        assert resp.status_code == 200, resp.text

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT action, target, detail FROM admin_audit_log "
                "WHERE action = 'user.set_plan_cascade' AND target = %s",
                (str(user_id),),
            )
            rows = cur.fetchall()

        assert rows, "Expected audit log entry with action='user.set_plan_cascade'"
        action, target, detail = rows[0]
        assert action == "user.set_plan_cascade"
        assert target == str(user_id)
        # detail JSONB: verify keys_updated is present and matches
        if detail is not None:
            assert "keys_updated" in detail
            assert detail["keys_updated"] == 2

    # -----------------------------------------------------------------------
    # Fresh-MFA gate (issue #220): cascade plan-assignment requires fresh MFA
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cascade_set_user_plan_requires_fresh_mfa_403_when_mfa_not_set(
        self, migrated_pg
    ):
        """Cascading a plan to all user keys requires fresh MFA — admin without
        mfa_verified_at in session must receive 403 (business rule: cascade plan
        assignment is entitlement-sensitive, symmetric with grant/revoke/update).
        """
        from unittest.mock import MagicMock, patch

        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod
        from src.web_ui.auth import STEP_UP_ERROR_CODE

        _seed_user(migrated_pg, username="admin_mfa_miss_c", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_mfa_miss_c")

        # Build a fake request with an admin session but NO mfa_verified_at
        scope = {
            "type": "http",
            "method": "PATCH",
            "path": f"/api/admin/users/{user_id}/plan",
            "headers": [],
            "query_string": b"",
            "session": {},  # no mfa_verified_at → freshness check must fail
        }
        fake_request = StarletteRequest(scope)

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        raised: HTTPException | None = None
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: 1  # logged in as admin

            mock_store = MagicMock()
            mock_store.get_user_field.return_value = True  # is_admin=True

            with patch("src.db.pg.auth_store", return_value=mock_store):
                try:
                    await auth_mod.require_admin_with_fresh_mfa(fake_request)
                except HTTPException as exc:
                    raised = exc
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert raised is not None, (
            "require_admin_with_fresh_mfa must raise when mfa_verified_at absent"
        )
        assert raised.status_code == 403, (
            f"Cascade plan without fresh MFA must return 403, got {raised.status_code}"
        )
        detail = raised.detail
        assert isinstance(detail, dict) and detail.get("error") == STEP_UP_ERROR_CODE, (
            f"Expected detail.error={STEP_UP_ERROR_CODE!r}, got {detail!r}"
        )

    @pytest.mark.asyncio
    async def test_cascade_set_user_plan_succeeds_with_fresh_mfa(self, migrated_pg):
        """Cascading a plan to all user keys with fresh MFA in session must succeed.

        Business rule: require_admin_with_fresh_mfa returns user_id when
        mfa_verified_at is present and within the freshness window.
        """
        import time
        from unittest.mock import MagicMock, patch

        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod

        _seed_user(migrated_pg, username="admin_mfa_ok_c", is_admin=True)

        # Build a fake request with an admin session AND a fresh mfa_verified_at
        scope = {
            "type": "http",
            "method": "PATCH",
            "path": "/api/admin/users/1/plan",
            "headers": [],
            "query_string": b"",
            "session": {"mfa_verified_at": str(time.time())},  # fresh timestamp
        }
        fake_request = StarletteRequest(scope)

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        user_id_result: int | None = None
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: 1

            mock_store = MagicMock()
            mock_store.get_user_field.return_value = True  # is_admin=True

            with patch("src.db.pg.auth_store", return_value=mock_store):
                user_id_result = await auth_mod.require_admin_with_fresh_mfa(fake_request)
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert user_id_result == 1, (
            "require_admin_with_fresh_mfa must return user_id=1 with fresh MFA,"
            f" got {user_id_result}"
        )

    def test_cascade_set_user_plan_route_is_wired_to_fresh_mfa(self):
        """ROUTE-wiring guard: PATCH /api/admin/users/{id}/plan must declare
        require_admin_with_fresh_mfa in its dependency tree.

        The helper-level tests above prove the dependency rejects a stale-MFA
        session; this proves the route actually *uses* it. Reverting to plain
        require_admin would make this test fail. Static introspection, so it is
        independent of the middleware test-bypass / session ordering that makes
        full-stack auth-gating tests order-sensitive.
        """
        from src.web_ui.auth import require_admin, require_admin_with_fresh_mfa

        app = create_app()
        route = _find_route(app, "/api/admin/users/{user_id}/plan", "PATCH")
        calls = _collect_dependency_calls(route.dependant)
        assert require_admin_with_fresh_mfa in calls, (
            "PATCH users/{id}/plan must depend on require_admin_with_fresh_mfa"
        )
        assert require_admin not in calls, (
            "Route must NOT use plain require_admin (fresh-MFA is required, #220)"
        )

    @pytest.mark.asyncio
    async def test_cascade_invalidates_cache_per_key(self, migrated_pg):
        """Cache invalidate called once per affected key (N times total)."""
        _seed_user(migrated_pg, username="admin_ch", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_ch")
        key_ids = [
            _seed_api_key(migrated_pg, name=f"ckey-ch-{i}", user_id=user_id)
            for i in range(3)
        ]
        plan_id = _get_plan_id(migrated_pg, "free")

        with patch("src.mcp.middleware._cache_invalidate_by_key_id") as mock_inv:
            app = create_app()
            async with _async_client(app) as client:
                resp = await client.patch(
                    f"/api/admin/users/{user_id}/plan",
                    json={"plan_id": plan_id},
                )

            assert resp.status_code == 200, resp.text
            assert mock_inv.call_count == 3
            # Each key must have been invalidated exactly once
            actual_calls = {c.args[0] for c in mock_inv.call_args_list}
            assert actual_calls == set(key_ids)
