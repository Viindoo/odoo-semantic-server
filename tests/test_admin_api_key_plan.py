# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_admin_api_key_plan.py
"""Tests for PATCH /api/admin/api-keys/{key_id}/plan (M10B P0-ext W-3).

Verifies:
  - Admin can set plan_id (with and without per-key overrides).
  - Overrides accept NULL to reset to plan default.
  - 422 for non-existent plan_id.
  - 422 for negative override value (pydantic ge=0 rejects before DB).
  - 404 for unknown key_id.
  - 403 for non-admin session.
  - 401 for unauthenticated request.
  - Audit log entry with action='api_key.set_plan' and correct details.
  - Cache invalidation called with key_id after each successful update.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Auth bypass is used for admin-data tests; auth-gating tests patch directly.
"""
import os
from unittest.mock import patch

import httpx
import pytest

from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Auth bypass management (mirrors pattern from test_admin_users.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_auth_bypass():
    """Enable test auth bypass for this module's tests.

    conftest._bypass_webui_auth_for_legacy_tests sets WEBUI_AUTH_DISABLED=1 for
    files NOT in real_auth_flow_files. Since this file is NOT in that set the
    bypass is already enabled by conftest. This fixture is a belt-and-suspenders
    guard to ensure bypass is on for all data-correctness tests.
    """
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


@pytest.fixture(scope="module")
def migrated_pg(migrated_pg_module):
    """Module-scoped: migrate ONCE for this file (was per-test via clean_pg).

    Safe because each test seeds DISTINCT usernames (admin_a/user_a … admin_j …)
    and DISTINCT api-key names (key_hash = f"hash_{name}") — no UNIQUE collision
    under shared accumulation — and assertions are either filtered by the row's
    own key_id, relative, or mock-driven (never an absolute count / fixed id).
    DO NOT add a test here that reuses a seed identifier or asserts an absolute
    row count.
    """
    return migrated_pg_module


def _seed_user(pg_conn, *, username: str = "alice", is_admin: bool = False) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active) "
            "VALUES (%s, 'hash', %s, TRUE) RETURNING id",
            (username, is_admin),
        )
        return cur.fetchone()[0]


def _seed_api_key(pg_conn, *, name: str = "test-key", user_id: int | None = None) -> int:
    """Insert an api_key row using the default plan (free) set by migration."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix, active, user_id) "
            "VALUES (%s, %s, %s, TRUE, %s) RETURNING id",
            (name, f"hash_{name}", f"osm_{name[:8]}", user_id),
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


class TestAdminSetApiKeyPlan:
    @pytest.mark.asyncio
    async def test_admin_set_plan_only(self, migrated_pg):
        """Admin PATCH with plan_id only (no overrides) -> 200; DB plan_id updated."""
        _seed_user(migrated_pg, username="admin_a", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_a")
        key_id = _seed_api_key(migrated_pg, name="key-plan-only", user_id=user_id)
        target_plan_id = _get_plan_id(migrated_pg, "pro")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/plan",
                json={"plan_id": target_plan_id},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["key_id"] == key_id
        assert data["plan"]["id"] == target_plan_id
        assert data["rate_limit_override"] is None
        assert data["quota_override"] is None

        # Verify DB
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT plan_id, rate_limit_override, quota_override FROM api_keys WHERE id = %s",
                (key_id,),
            )
            row = cur.fetchone()
        assert row[0] == target_plan_id
        assert row[1] is None
        assert row[2] is None

    @pytest.mark.asyncio
    async def test_admin_set_plan_with_overrides(self, migrated_pg):
        """Admin PATCH with plan_id + overrides -> 200; DB columns set correctly."""
        _seed_user(migrated_pg, username="admin_b", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_b")
        key_id = _seed_api_key(migrated_pg, name="key-with-overrides", user_id=user_id)
        target_plan_id = _get_plan_id(migrated_pg, "free")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/plan",
                json={
                    "plan_id": target_plan_id,
                    "rate_limit_override": 5000,
                    "quota_override": 10000,
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["rate_limit_override"] == 5000
        assert data["quota_override"] == 10000

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT plan_id, rate_limit_override, quota_override FROM api_keys WHERE id = %s",
                (key_id,),
            )
            row = cur.fetchone()
        assert row[0] == target_plan_id
        assert row[1] == 5000
        assert row[2] == 10000

    @pytest.mark.asyncio
    async def test_admin_set_overrides_to_null(self, migrated_pg):
        """Admin PATCH with explicit null overrides -> 200; DB columns set to NULL."""
        _seed_user(migrated_pg, username="admin_c", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_c")
        key_id = _seed_api_key(migrated_pg, name="key-null-overrides", user_id=user_id)

        # First set non-null overrides
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET rate_limit_override = 999, quota_override = 888 WHERE id = %s",
                (key_id,),
            )

        target_plan_id = _get_plan_id(migrated_pg, "team")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/plan",
                json={
                    "plan_id": target_plan_id,
                    "rate_limit_override": None,
                    "quota_override": None,
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["rate_limit_override"] is None
        assert data["quota_override"] is None

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT rate_limit_override, quota_override FROM api_keys WHERE id = %s",
                (key_id,),
            )
            row = cur.fetchone()
        assert row[0] is None
        assert row[1] is None

    @pytest.mark.asyncio
    async def test_admin_set_plan_invalid_plan_id_422(self, migrated_pg):
        """Admin PATCH with non-existent plan_id -> 422."""
        _seed_user(migrated_pg, username="admin_d", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_d")
        key_id = _seed_api_key(migrated_pg, name="key-invalid-plan", user_id=user_id)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/plan",
                json={"plan_id": 999999},
            )

        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_admin_set_plan_negative_override_422(self, migrated_pg):
        """Admin PATCH with rate_limit_override=-1 -> 422 from pydantic ge=0."""
        _seed_user(migrated_pg, username="admin_e", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_e")
        key_id = _seed_api_key(migrated_pg, name="key-neg-override", user_id=user_id)
        plan_id = _get_plan_id(migrated_pg, "free")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/plan",
                json={"plan_id": plan_id, "rate_limit_override": -1},
            )

        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_admin_set_plan_unknown_key_404(self, migrated_pg):
        """Admin PATCH with non-existent key_id -> 404."""
        _seed_user(migrated_pg, username="admin_f", is_admin=True)
        plan_id = _get_plan_id(migrated_pg, "free")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                "/api/admin/api-keys/999999/plan",
                json={"plan_id": plan_id},
            )

        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_non_admin_set_plan_403(self, migrated_pg):
        """Plan-assignment dependency raises 403 for an authenticated non-admin.

        Tests the route dependency (require_admin_with_fresh_mfa) directly rather
        than through the HTTP stack — the latter flips the global auth bypass off
        and relies on the auth middleware still bypassing, an order-dependent
        asymmetry (issue #220 follow-up). The 403 is raised by the inner
        require_admin gate before the MFA check, so a non-admin is rejected
        regardless of MFA state. Route wiring is covered separately by
        test_set_api_key_plan_route_is_wired_to_fresh_mfa.
        """
        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod

        non_admin_id = _seed_user(migrated_pg, username="non_admin_g", is_admin=False)
        scope = {"type": "http", "method": "PATCH", "path": "/", "headers": [], "query_string": b""}
        fake_request = StarletteRequest(scope)

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        raised: HTTPException | None = None
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: non_admin_id
            try:
                await auth_mod.require_admin_with_fresh_mfa(fake_request)
            except HTTPException as exc:
                raised = exc
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert raised is not None, "require_admin_with_fresh_mfa must reject a non-admin"
        assert raised.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_set_plan_unauthenticated_401(self, migrated_pg):
        """No session -> require_admin raises 401."""
        import src.web_ui.auth as auth_mod

        key_id = _seed_api_key(migrated_pg, name="key-unauth-h")
        plan_id = _get_plan_id(migrated_pg, "free")

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: None

            app = create_app()
            async with _async_client(app) as client:
                resp = await client.patch(
                    f"/api/admin/api-keys/{key_id}/plan",
                    json={"plan_id": plan_id},
                )
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert resp.status_code == 401, resp.text

    @pytest.mark.asyncio
    async def test_admin_set_plan_audit_logged(self, migrated_pg):
        """Successful PATCH writes admin_audit_log with action='api_key.set_plan'."""
        _seed_user(migrated_pg, username="admin_i", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_i")
        key_id = _seed_api_key(migrated_pg, name="key-audit-i", user_id=user_id)
        plan_id = _get_plan_id(migrated_pg, "pro")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/plan",
                json={"plan_id": plan_id},
            )

        assert resp.status_code == 200, resp.text

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT action, target, detail FROM admin_audit_log "
                "WHERE action = 'api_key.set_plan' AND target = %s",
                (str(key_id),),
            )
            rows = cur.fetchall()

        assert rows, "Expected audit log entry with action='api_key.set_plan'"
        action, target, detail = rows[0]
        assert action == "api_key.set_plan"
        assert target == str(key_id)
        # detail is JSONB; verify key fields are present
        if detail is not None:
            assert "new_plan_id" in detail or "key_id" in detail

    @pytest.mark.asyncio
    async def test_admin_set_plan_invalidates_cache(self, migrated_pg):
        """Successful PATCH calls _cache_invalidate_by_key_id(key_id).

        The route imports _cache_invalidate_by_key_id locally inside the handler
        (`from src.mcp.middleware import _cache_invalidate_by_key_id`), so we
        patch the source location (src.mcp.middleware) — the import resolves to
        the same object at call time.
        """
        _seed_user(migrated_pg, username="admin_j", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_j")
        key_id = _seed_api_key(migrated_pg, name="key-cache-j", user_id=user_id)
        plan_id = _get_plan_id(migrated_pg, "free")

        with patch("src.mcp.middleware._cache_invalidate_by_key_id") as mock_inv:
            app = create_app()
            async with _async_client(app) as client:
                resp = await client.patch(
                    f"/api/admin/api-keys/{key_id}/plan",
                    json={"plan_id": plan_id},
                )

            assert resp.status_code == 200, resp.text
            mock_inv.assert_called_once_with(key_id)

    # -----------------------------------------------------------------------
    # BLOCK-1 regression: partial-update (model_fields_set) semantics
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_admin_set_plan_only_preserves_existing_overrides(self, migrated_pg):
        """PATCH {plan_id} only — overrides absent from body must be preserved in DB.

        Before the BLOCK-1 fix, Pydantic defaulted absent fields to None and the
        helper unconditionally wrote all three columns, silently NULLing overrides
        the admin had set.  model_fields_set now gates which columns are updated.
        """
        _seed_user(migrated_pg, username="admin_k", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_k")
        key_id = _seed_api_key(migrated_pg, name="key-preserve-k", user_id=user_id)

        # Seed pre-existing overrides directly in DB
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET rate_limit_override = 5000, quota_override = 10000"
                " WHERE id = %s",
                (key_id,),
            )

        new_plan_id = _get_plan_id(migrated_pg, "pro")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/plan",
                # Body contains plan_id only — no override fields
                json={"plan_id": new_plan_id},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Response body must reflect the preserved (unchanged) DB values
        assert data["rate_limit_override"] == 5000, (
            f"rate_limit_override should be preserved as 5000, got {data['rate_limit_override']}"
        )
        assert data["quota_override"] == 10000, (
            f"quota_override should be preserved as 10000, got {data['quota_override']}"
        )

        # DB verification
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT plan_id, rate_limit_override, quota_override FROM api_keys WHERE id = %s",
                (key_id,),
            )
            row = cur.fetchone()
        assert row[0] == new_plan_id, f"plan_id should be {new_plan_id}, got {row[0]}"
        assert row[1] == 5000, f"rate_limit_override should be preserved 5000, got {row[1]}"
        assert row[2] == 10000, f"quota_override should be preserved 10000, got {row[2]}"

    @pytest.mark.asyncio
    async def test_admin_set_plan_explicit_null_clears_overrides(self, migrated_pg):
        """PATCH with explicit null override values -> DB columns set to NULL.

        Sending rate_limit_override: null and quota_override: null in the JSON
        body puts both fields in model_fields_set, so the helper writes NULL and
        clears the previously-set overrides.
        """
        _seed_user(migrated_pg, username="admin_l", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_l")
        key_id = _seed_api_key(migrated_pg, name="key-clear-null-l", user_id=user_id)

        # Seed non-null overrides
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET rate_limit_override = 7777, quota_override = 8888"
                " WHERE id = %s",
                (key_id,),
            )

        plan_id = _get_plan_id(migrated_pg, "team")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/plan",
                json={
                    "plan_id": plan_id,
                    "rate_limit_override": None,
                    "quota_override": None,
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["rate_limit_override"] is None, (
            f"rate_limit_override should be None (cleared), got {data['rate_limit_override']}"
        )
        assert data["quota_override"] is None, (
            f"quota_override should be None (cleared), got {data['quota_override']}"
        )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT rate_limit_override, quota_override FROM api_keys WHERE id = %s",
                (key_id,),
            )
            row = cur.fetchone()
        assert row[0] is None, f"DB rate_limit_override should be NULL, got {row[0]}"
        assert row[1] is None, f"DB quota_override should be NULL, got {row[1]}"

    # -----------------------------------------------------------------------
    # Fresh-MFA gate (issue #220): plan-assignment requires fresh MFA
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_set_api_key_plan_requires_fresh_mfa_403_when_mfa_not_set(
        self, migrated_pg
    ):
        """Changing an API key's plan requires fresh MFA — admin without
        mfa_verified_at in session must receive 403 (business rule: plan
        assignment is an entitlement-sensitive op, symmetric with grant/revoke).
        """
        from unittest.mock import MagicMock, patch

        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod
        from src.web_ui.auth import STEP_UP_ERROR_CODE

        _seed_user(migrated_pg, username="admin_mfa_miss", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_mfa_miss")
        key_id = _seed_api_key(migrated_pg, name="key-mfa-miss", user_id=user_id)

        # Build a fake request with an admin session but NO mfa_verified_at
        scope = {
            "type": "http",
            "method": "PATCH",
            "path": f"/api/admin/api-keys/{key_id}/plan",
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
            f"Plan assignment without fresh MFA must return 403, got {raised.status_code}"
        )
        detail = raised.detail
        assert isinstance(detail, dict) and detail.get("error") == STEP_UP_ERROR_CODE, (
            f"Expected detail.error={STEP_UP_ERROR_CODE!r}, got {detail!r}"
        )

    @pytest.mark.asyncio
    async def test_set_api_key_plan_succeeds_with_fresh_mfa(self, migrated_pg):
        """Changing an API key's plan with fresh MFA in session must succeed (200).

        Business rule: require_admin_with_fresh_mfa returns user_id when
        mfa_verified_at is present and within the freshness window.
        """
        import time
        from unittest.mock import MagicMock, patch

        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod

        _seed_user(migrated_pg, username="admin_mfa_ok", is_admin=True)

        # Build a fake request with an admin session AND a fresh mfa_verified_at
        scope = {
            "type": "http",
            "method": "PATCH",
            "path": "/api/admin/api-keys/1/plan",
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

    def test_set_api_key_plan_route_is_wired_to_fresh_mfa(self):
        """ROUTE-wiring guard: PATCH /api/admin/api-keys/{id}/plan must declare
        require_admin_with_fresh_mfa in its dependency tree.

        The helper-level tests above prove require_admin_with_fresh_mfa rejects a
        stale-MFA session; this proves the route actually *uses* it. Reverting
        the route to plain require_admin would make this test fail — closing the
        gap where a Depends downgrade would otherwise pass unnoticed.
        (Static introspection, so it is independent of the middleware test-bypass
        and session state that make full-stack auth-gating tests order-sensitive.)
        """
        from src.web_ui.auth import require_admin, require_admin_with_fresh_mfa

        app = create_app()
        route = _find_route(app, "/api/admin/api-keys/{key_id}/plan", "PATCH")
        calls = _collect_dependency_calls(route.dependant)
        assert require_admin_with_fresh_mfa in calls, (
            "PATCH api-keys/{id}/plan must depend on require_admin_with_fresh_mfa"
        )
        assert require_admin not in calls, (
            "Route must NOT use plain require_admin (fresh-MFA is required, #220)"
        )

    @pytest.mark.asyncio
    async def test_admin_set_only_rate_override_preserves_quota_override(self, migrated_pg):
        """PATCH with plan_id + rate_limit_override only -> quota_override preserved.

        Only rate_limit_override is in model_fields_set, so quota_override column
        must remain unchanged in the DB while rate_limit_override is updated.
        """
        _seed_user(migrated_pg, username="admin_m", is_admin=True)
        user_id = _seed_user(migrated_pg, username="user_m")
        key_id = _seed_api_key(migrated_pg, name="key-partial-m", user_id=user_id)

        # Seed both overrides
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET rate_limit_override = 5000, quota_override = 10000"
                " WHERE id = %s",
                (key_id,),
            )

        plan_id = _get_plan_id(migrated_pg, "free")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/admin/api-keys/{key_id}/plan",
                # quota_override intentionally absent from body
                json={"plan_id": plan_id, "rate_limit_override": 9999},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["rate_limit_override"] == 9999, (
            f"rate_limit_override should be updated to 9999, got {data['rate_limit_override']}"
        )
        assert data["quota_override"] == 10000, (
            f"quota_override should be preserved as 10000, got {data['quota_override']}"
        )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT rate_limit_override, quota_override FROM api_keys WHERE id = %s",
                (key_id,),
            )
            row = cur.fetchone()
        assert row[0] == 9999, f"DB rate_limit_override should be 9999, got {row[0]}"
        assert row[1] == 10000, f"DB quota_override should be preserved 10000, got {row[1]}"
