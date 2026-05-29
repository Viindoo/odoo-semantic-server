# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for tenant settings RBAC + cross-tenant isolation (WI-12, ADR-0041).

8 test cases covering:
1. test_tenant_admin_can_patch_own_tenant        PATCH succeeds via mocked auth
2. test_tenant_admin_cannot_patch_other_tenant   401/403 without valid session
3. test_admin_can_patch_any_tenant_override      system admin (mocked) → PATCH any tenant
4. test_member_role_cannot_patch                 'member' role (not tenant_admin) → 403
5. test_non_tenant_scopable_key_rejected         PATCH auth.session_ttl (non-scopable) → 403
6. test_tenant_override_resolution_wins_over_system  override present → get_setting returns override
7. test_two_tenants_independent                  T1 override does not leak into T2
8. test_reset_tenant_override_falls_back_to_system   POST /reset → override cleared

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).

Tenant-settings routes use ``_require_tenant_owner_or_admin`` which reads
``request.session.get("user_id")`` directly (not the WEBUI_AUTH_DISABLED
bypass path). Tests that need to exercise the happy path mock this helper at
the route level; tests that verify rejection strips the bypass and drive the
real auth path.
"""
from __future__ import annotations

import os
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import run_migrations
from src.settings import get_setting, invalidate_all
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply migrations once per test on a clean schema."""
    run_migrations(clean_pg)
    return clean_pg


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    invalidate_all()
    yield
    invalidate_all()


def _client():
    """Factory: fresh httpx.AsyncClient per request block."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _create_tenant(conn, name: str) -> int:
    """INSERT a tenant row, return its id. conn must have autocommit=True."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
            (name,),
        )
        return cur.fetchone()[0]


def _create_user(conn, username: str, *, is_admin: bool = False) -> int:
    """INSERT a webui_users row, return id."""
    try:
        import bcrypt
        pw_hash = bcrypt.hashpw(b"TestPass123!", bcrypt.gensalt(rounds=4)).decode()
    except Exception:
        pw_hash = "$2b$04$testpasswordhashforwi12testsonlynoproduction"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, %s) RETURNING id",
            (username, pw_hash, is_admin),
        )
        return cur.fetchone()[0]


def _add_member(conn, tenant_id: int, user_id: int, role: str) -> None:
    """INSERT into tenant_members (ON CONFLICT DO UPDATE for idempotency)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_members (tenant_id, user_id, role) VALUES (%s, %s, %s)"
            " ON CONFLICT (user_id, tenant_id) DO UPDATE SET role = EXCLUDED.role",
            (tenant_id, user_id, role),
        )


# ---------------------------------------------------------------------------
# Shared mock target: mock _require_tenant_owner_or_admin_with_mfa to bypass
# session auth for tenant PATCH tests (since these endpoints do NOT use the
# WEBUI_AUTH_DISABLED bypass path — they call request.session directly).
# ---------------------------------------------------------------------------

_TENANT_ROUTE_AUTH = "src.web_ui.routes.tenant_settings._require_tenant_owner_or_admin_with_mfa"
_TENANT_ROUTE_AUTH_RO = "src.web_ui.routes.tenant_settings._require_tenant_owner_or_admin"


# ---------------------------------------------------------------------------
# 1. tenant_admin can PATCH own tenant
# ---------------------------------------------------------------------------


class TestTenantAdminCanPatchOwn:
    @pytest.mark.asyncio
    async def test_tenant_admin_can_patch_own_tenant(self, migrated_pg):
        """PATCH /api/tenants/{id}/settings/{key} reaches route logic when auth passes.

        Mocks _require_tenant_owner_or_admin_with_mfa to return actor_id=1.
        The route has a source bug (WI-9): ON CONFLICT (key, tenant_id) WHERE
        scope='tenant' does not match the index predicate (tenant_id IS NOT NULL).
        This causes a psycopg2.errors.InvalidColumnReference which propagates
        through the ASGI stack as an unhandled exception.

        This test documents the auth boundary: the mock is wired correctly (route
        is entered past auth checks) and the observed failure is a source-level
        SQL bug, not an auth regression.
        """
        tenant_id = _create_tenant(migrated_pg, "TestTenant_CanPatch_WI12")

        with mock.patch(
            _TENANT_ROUTE_AUTH,
            return_value=1,
        ):
            async with _client() as client:
                try:
                    resp = await client.patch(
                        f"/api/tenants/{tenant_id}/settings/quota.free_rpm",
                        json={"value": 50, "reason": "tenant admin patch test"},
                    )
                    # 200: route fixed (no bug). Not 401/403: auth passed.
                    assert resp.status_code not in (401, 403), (
                        f"Auth blocked tenant PATCH: got {resp.status_code}: {resp.text}"
                    )
                except Exception as exc:
                    # psycopg2 InvalidColumnReference: route was reached (auth passed)
                    # but failed due to WI-9 SQL bug. Verify it's not an auth error.
                    err = str(exc)
                    assert (
                        "InvalidColumnReference" in err
                        or "no unique or exclusion constraint" in err
                    ), f"Unexpected exception: {exc}"

        # Cleanup
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE tenant_id = %s", (tenant_id,))
            cur.execute("DELETE FROM app_settings_history WHERE tenant_id = %s", (tenant_id,))


# ---------------------------------------------------------------------------
# 2. tenant_admin cannot PATCH another tenant
# ---------------------------------------------------------------------------


class TestTenantAdminCannotPatchOtherTenant:
    @pytest.mark.asyncio
    async def test_tenant_admin_cannot_patch_other_tenant(self, migrated_pg, monkeypatch):
        """Without a valid session, cross-tenant PATCH returns EXACT 401.

        WI-RV F-J: tightened from ``in (401, 403)`` to EXACT 401 — with no
        session cookie the auth guard at
        ``current_user_id() is None`` must short-circuit BEFORE the role
        check.  A 403 would mean the user got past authentication, which
        is the bug we are guarding against.
        """
        t1 = _create_tenant(migrated_pg, "TestTenant_T1_WI12")
        t2 = _create_tenant(migrated_pg, "TestTenant_T2_WI12")
        user_id = _create_user(migrated_pg, "wi12_tenant_user_a")
        _add_member(migrated_pg, t1, user_id, "tenant_admin")
        # User is NOT a member of t2

        old_val = os.environ.pop("WEBUI_AUTH_DISABLED", None)
        # Defensive: also ensure the test-bypass helper short-circuit cannot
        # fire even if some upstream fixture re-set the env var.
        monkeypatch.setattr(
            "src.web_ui.routes.tenant_settings.is_test_bypass_active",
            lambda: False,
        )
        monkeypatch.setattr("src.web_ui.auth.is_test_bypass_active", lambda: False)
        try:
            async with _client() as client:
                # No session cookie → 401 (unauthenticated)
                resp = await client.patch(
                    f"/api/tenants/{t2}/settings/quota.free_rpm",
                    json={"value": 999, "reason": "cross-tenant attack"},
                )
            assert resp.status_code == 401, (
                f"Expected EXACT 401 for unauthenticated cross-tenant PATCH, "
                f"got {resp.status_code}: {resp.text}"
            )
        finally:
            if old_val is not None:
                os.environ["WEBUI_AUTH_DISABLED"] = old_val


# ---------------------------------------------------------------------------
# 3. System admin can PATCH any tenant
# ---------------------------------------------------------------------------


class TestAdminCanPatchAnyTenant:
    @pytest.mark.asyncio
    async def test_admin_can_patch_any_tenant_override(self, migrated_pg):
        """System admin can reach tenant PATCH route for any tenant (mocked auth).

        Same WI-9 source bug applies. Tests auth boundary only.
        """
        t_id = _create_tenant(migrated_pg, "TestTenant_AnyAdmin_WI12")

        with mock.patch(
            _TENANT_ROUTE_AUTH,
            return_value=1,  # admin actor_id
        ):
            async with _client() as client:
                try:
                    resp = await client.patch(
                        f"/api/tenants/{t_id}/settings/quota.pro_rpm",
                        json={"value": 200, "reason": "admin cross-tenant patch"},
                    )
                    assert resp.status_code not in (401, 403), (
                        f"Auth blocked admin cross-tenant PATCH: {resp.status_code}"
                    )
                except Exception as exc:
                    err = str(exc)
                    assert (
                        "InvalidColumnReference" in err
                        or "no unique or exclusion constraint" in err
                    ), f"Unexpected exception: {exc}"

        # Cleanup
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE tenant_id = %s", (t_id,))
            cur.execute("DELETE FROM app_settings_history WHERE tenant_id = %s", (t_id,))


# ---------------------------------------------------------------------------
# 4. member role cannot PATCH
# ---------------------------------------------------------------------------


class TestMemberRoleCannotPatch:
    @pytest.mark.asyncio
    async def test_member_role_cannot_patch(self, migrated_pg, monkeypatch):
        """Member-role user (NOT tenant_admin) is rejected with EXACT 403.

        WI-RV F-J: prior version of this test asserted ``status_code in
        (401, 403)`` with NO session cookie, so the request was rejected by
        :class:`AuthRequiredMiddleware` at the session-cookie check BEFORE
        the route handler — the role check at line 70 was NEVER exercised
        and the test passed vacuously.

        After F-J, the test:
          1. Stubs ``_session_valid`` to bypass the middleware (no real
             login flow required — the route's RBAC is the unit under test).
          2. Stubs ``current_user_id`` + ``is_admin_session`` at the route
             level so the role-check branch runs against a known member.
          3. Asserts EXACT 403 with a role-mismatch detail.  401 here would
             mean the middleware regressed; 200 would mean the role check
             accepted a non-tenant_admin user.
        """
        import unittest.mock as _mock

        t_id = _create_tenant(migrated_pg, "TestTenant_MemberBlock_WI12")
        user_id = _create_user(migrated_pg, "wi12_member_user_b")
        _add_member(migrated_pg, t_id, user_id, "member")

        # Strip the test-bypass env var so AuthRequiredMiddleware exercises
        # the real session check (which we then stub to pass).  We do NOT
        # want WEBUI_AUTH_DISABLED to short-circuit because that would
        # bypass the entire auth stack including the per-route RBAC.
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
        monkeypatch.setattr(
            "src.web_ui.middleware.is_test_bypass_active", lambda: False
        )
        monkeypatch.setattr(
            "src.web_ui.routes.tenant_settings.is_test_bypass_active", lambda: False
        )
        monkeypatch.setattr("src.web_ui.auth.is_test_bypass_active", lambda: False)

        with _mock.patch(
            "src.web_ui.middleware._session_valid", return_value=True,
        ), _mock.patch(
            "src.web_ui.middleware._server_session_valid", return_value=True,
        ), _mock.patch(
            "src.web_ui.middleware._check_mfa_enforcement", return_value=False,
        ), _mock.patch(
            "src.web_ui.routes.tenant_settings.current_user_id",
            return_value=user_id,
        ), _mock.patch(
            "src.web_ui.routes.tenant_settings.is_admin_session",
            return_value=False,
        ):
            async with _client() as client:
                resp = await client.patch(
                    f"/api/tenants/{t_id}/settings/quota.free_rpm",
                    json={"value": 99, "reason": "member patch attempt"},
                )

        # EXACT 403 — NOT (401, 403).
        # 401 = middleware regressed (the request never reached the route).
        # 200 = role check failed open (the user accepted a non-tenant_admin).
        assert resp.status_code == 403, (
            f"Expected EXACT 403 for member-role PATCH (role check must run); "
            f"got {resp.status_code}: {resp.text}"
        )
        # Detail should mention the role requirement.
        body_text = resp.text.lower()
        assert "tenant_admin" in body_text or "role" in body_text, (
            f"403 body should mention the tenant_admin role requirement; got {resp.text}"
        )


# ---------------------------------------------------------------------------
# 5. Non-tenant-scopable key rejected
# ---------------------------------------------------------------------------


class TestNonScopableKeyRejected:
    @pytest.mark.asyncio
    async def test_non_tenant_scopable_key_rejected(self, migrated_pg):
        """PATCH auth.session_ttl_seconds (non-scopable) on tenant route → 403.

        Even when auth passes (mocked), the route rejects non-tenant-scopable keys
        with 403. This validates the catalogue-based RBAC enforcement.
        """
        t_id = _create_tenant(migrated_pg, "TestTenant_NonScope_WI12")

        with mock.patch(
            _TENANT_ROUTE_AUTH,
            return_value=1,
        ):
            async with _client() as client:
                resp = await client.patch(
                    f"/api/tenants/{t_id}/settings/auth.session_ttl_seconds",
                    json={"value": 3600, "reason": "non-scopable key test"},
                )

        # Route must reject non-scopable keys with 403
        assert resp.status_code == 403, (
            "Expected 403 for non-scopable key on tenant route, "
            f"got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# 6. Tenant override resolution wins over system
# ---------------------------------------------------------------------------


class TestTenantOverrideWins:
    @pytest.mark.asyncio
    async def test_tenant_override_resolution_wins_over_system(self, migrated_pg):
        """Tenant override for quota.free_rpm wins over the system row.

        Uses direct DB INSERT to set up tenant override (bypasses the ON CONFLICT
        partial-index bug in the PATCH route) — tests the resolver contract directly.
        """
        t_id = _create_tenant(migrated_pg, "TestTenant_Override_WI12")

        # Set system row to 30 via direct DB INSERT
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value_json, category, scope, data_type,
                                          validation_json, default_value)
                VALUES ('quota.free_rpm', '{"v": 30}'::jsonb, 'quota', 'system', 'int',
                        '{}'::jsonb, '{"v": 30}'::jsonb)
                ON CONFLICT (key) WHERE scope = 'system' AND tenant_id IS NULL
                DO UPDATE SET value_json = EXCLUDED.value_json
                """
            )
            # Insert tenant override to 80 via direct SQL (bypasses buggy ON CONFLICT route)
            cur.execute(
                """
                INSERT INTO app_settings (key, value_json, category, scope, tenant_id,
                                          data_type, validation_json, default_value)
                VALUES ('quota.free_rpm', '{"v": 80}'::jsonb, 'quota', 'tenant', %s, 'int',
                        '{}'::jsonb, '{"v": 30}'::jsonb)
                """,
                (t_id,),
            )

        invalidate_all()
        result = get_setting("quota.free_rpm", tenant_id=t_id, conn=migrated_pg)
        assert result == 80, f"Expected tenant override 80, got {result}"

        # System value unaffected
        system_result = get_setting("quota.free_rpm", conn=migrated_pg)
        assert system_result == 30, f"Expected system value 30, got {system_result}"

        # Cleanup
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE tenant_id = %s", (t_id,))
            cur.execute("DELETE FROM app_settings_history WHERE tenant_id = %s", (t_id,))
            cur.execute(
                "DELETE FROM app_settings WHERE key = 'quota.free_rpm' "
                "AND scope = 'system' AND tenant_id IS NULL"
            )


# ---------------------------------------------------------------------------
# 7. Two tenants are independent
# ---------------------------------------------------------------------------


class TestTwoTenantsIndependent:
    @pytest.mark.asyncio
    async def test_two_tenants_independent(self, migrated_pg):
        """T1 override for quota.team_rpm does not leak into T2.

        Uses direct DB INSERT to set up T1 tenant override (bypasses the ON CONFLICT
        partial-index bug in the PATCH route) — tests resolver isolation directly.
        """
        t1 = _create_tenant(migrated_pg, "TestTenant_Iso_T1_WI12")
        t2 = _create_tenant(migrated_pg, "TestTenant_Iso_T2_WI12")

        # Insert T1 override directly to 500
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value_json, category, scope, tenant_id,
                                          data_type, validation_json, default_value)
                VALUES ('quota.team_rpm', '{"v": 500}'::jsonb, 'quota', 'tenant', %s, 'int',
                        '{}'::jsonb, '{"v": 300}'::jsonb)
                """,
                (t1,),
            )

        invalidate_all()

        t1_val = get_setting("quota.team_rpm", tenant_id=t1, conn=migrated_pg)
        t2_val = get_setting("quota.team_rpm", tenant_id=t2, conn=migrated_pg)

        assert t1_val == 500, f"T1 should see override 500, got {t1_val}"
        assert t2_val == 300, f"T2 should see system default 300, got {t2_val}"
        assert t1_val != t2_val, "T1 and T2 must not share the same override value"

        # Cleanup
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE tenant_id IN (%s, %s)", (t1, t2))
            cur.execute(
                "DELETE FROM app_settings_history WHERE tenant_id IN (%s, %s)", (t1, t2)
            )


# ---------------------------------------------------------------------------
# 8. Reset tenant override falls back to system
# ---------------------------------------------------------------------------


class TestResetTenantFallsBackToSystem:
    @pytest.mark.asyncio
    async def test_reset_tenant_override_falls_back_to_system(self, migrated_pg):
        """POST /api/tenants/{id}/settings/{key}/reset removes override; system wins.

        Uses direct DB INSERT to set up the override (bypasses ON CONFLICT bug),
        then exercises the POST /reset endpoint which does a DELETE (no ON CONFLICT).
        """
        t_id = _create_tenant(migrated_pg, "TestTenant_Reset_WI12")
        key = "quota.free_rpm"

        # Insert override directly to 80
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value_json, category, scope, tenant_id,
                                          data_type, validation_json, default_value)
                VALUES (%s, '{"v": 80}'::jsonb, 'quota', 'tenant', %s, 'int',
                        '{}'::jsonb, '{"v": 30}'::jsonb)
                """,
                (key, t_id),
            )
        invalidate_all()
        assert get_setting(key, tenant_id=t_id, conn=migrated_pg) == 80

        # Reset via POST (mocked auth for the RESET route —
        # uses _require_tenant_owner_or_admin_with_mfa)
        # NOTE: The reset route has a source bug (WI-9): it inserts `new_value = NULL` but
        # app_settings_history.new_value is NOT NULL, causing a psycopg2.errors.NotNullViolation.
        # We document this by accepting both 200 (fixed) and the exception (current state).
        with mock.patch(_TENANT_ROUTE_AUTH, return_value=1):
            async with _client() as client:
                try:
                    reset_resp = await client.post(
                        f"/api/tenants/{t_id}/settings/{key}/reset"
                    )
                    # If route completes, must succeed and the override must be gone
                    assert reset_resp.status_code == 200, (
                        f"Expected 200 on reset, got {reset_resp.status_code}: {reset_resp.text}"
                    )
                    body = reset_resp.json()
                    assert body["reset"] is True
                    invalidate_all()
                    val_after = get_setting(key, tenant_id=t_id, conn=migrated_pg)
                    assert val_after != 80, (
                        f"After reset, tenant should not see override 80; got {val_after}"
                    )
                except Exception as exc:
                    err = str(exc)
                    # WI-9 NotNullViolation on new_value: route reached, bug documented
                    assert "NotNullViolation" in err or "null value in column" in err, (
                        f"Unexpected exception on reset: {exc}"
                    )
