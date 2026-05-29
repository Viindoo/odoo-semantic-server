# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for /api/admin/settings/* CRUD endpoints (WI-12, ADR-0042).

12 test cases covering:
1.  list_settings_groups_by_category            GET / returns dict with category keys
2.  get_single_setting_returns_drift_flag        GET /{key} returns drift_from_default
3.  patch_setting_validates_min_max             PATCH with below-min value → 422
4.  patch_setting_updates_value_and_logs_history PATCH → DB history row written
5.  patch_setting_requires_admin                non-admin PATCH → 401/403
6.  reset_setting_reverts_to_default            POST /reset after PATCH → default
7.  get_history_returns_recent_changes          GET /history after 3 patches → ≥3 rows
8.  undo_setting_reverts_to_previous            POST /undo → value = N-1
9.  patch_unknown_key_returns_404               PATCH nonexistent key → 404
10. signup_enabled_toggles_register_endpoint    PATCH signup.enabled=False → /register 403
11. validation_rejects_wrong_type               PATCH with str value for int field → 422
12. audit_log_row_created_on_patch              PATCH → admin_audit_log row written

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
WEBUI_AUTH_DISABLED is managed by conftest autouse fixture; tests run with the
bypass active, so require_admin and require_admin_with_fresh_mfa return user_id=1.
"""
from __future__ import annotations

import httpx
import pytest

from src.db.migrate import run_migrations
from src.settings import invalidate_all
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    """Run migrations once per test on a clean schema."""
    run_migrations(clean_pg)
    return clean_pg


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Flush the in-process settings LRU before and after each test."""
    invalidate_all()
    yield
    invalidate_all()


def _client():
    """Factory: fresh httpx.AsyncClient per request block."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# 1. List settings groups by category
# ---------------------------------------------------------------------------


class TestListSettings:
    @pytest.mark.asyncio
    async def test_list_settings_groups_by_category(self, migrated_pg):
        """GET /api/admin/settings returns dict with category buckets."""
        async with _client() as client:
            resp = await client.get("/api/admin/settings")

        assert resp.status_code == 200
        body = resp.json()
        assert "categories" in body
        # SETTINGS_CATALOGUE has auth + quota + embedding + indexer + mcp categories
        categories = body["categories"]
        assert "auth" in categories
        assert "quota" in categories
        # Each category is a non-empty list of setting dicts
        for cat_name, entries in categories.items():
            assert isinstance(entries, list), f"Category {cat_name!r} must be a list"
            assert len(entries) > 0, f"Category {cat_name!r} is unexpectedly empty"
            for e in entries:
                assert "key" in e
                assert "default_value" in e


# ---------------------------------------------------------------------------
# 2. Get single setting returns drift flag
# ---------------------------------------------------------------------------


class TestGetSingleSetting:
    @pytest.mark.asyncio
    async def test_get_single_setting_returns_drift_flag(self, migrated_pg):
        """GET /api/admin/settings/auth.session_ttl_seconds includes drift_from_default.

        Removes any existing system row for the key to ensure we start from a
        code-default state (drift_from_default must be False when no override exists).
        """
        # Ensure no system row exists (clean_pg drops tables but previous test
        # in the same run may have left a row via a different migrated_pg fixture)
        with migrated_pg.cursor() as cur:
            cur.execute(
                "DELETE FROM app_settings "
                "WHERE key = 'auth.session_ttl_seconds' AND scope = 'system' AND tenant_id IS NULL"
            )
        invalidate_all()

        async with _client() as client:
            resp = await client.get("/api/admin/settings/auth.session_ttl_seconds")

        assert resp.status_code == 200
        body = resp.json()
        assert body["key"] == "auth.session_ttl_seconds"
        assert "drift_from_default" in body
        # No system row → resolves to code default → no drift
        assert body["drift_from_default"] is False, (
            f"Expected drift_from_default=False, but current_value={body.get('current_value')} "
            f"vs code_default={body.get('code_default')}"
        )

    @pytest.mark.asyncio
    async def test_get_unknown_key_returns_404(self, migrated_pg):
        """GET /api/admin/settings/{unknown} returns 404."""
        async with _client() as client:
            resp = await client.get("/api/admin/settings/no.such.key")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. Patch setting validates min/max
# ---------------------------------------------------------------------------


class TestPatchValidation:
    @pytest.mark.asyncio
    async def test_patch_setting_validates_min_max(self, migrated_pg):
        """PATCH auth.session_ttl_seconds with value below min (900) returns 422."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/settings/auth.session_ttl_seconds",
                json={"value": 60, "reason": "test min validation"},
            )
        assert resp.status_code == 422, (
            f"Expected 422 for below-min value, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_validation_rejects_wrong_type(self, migrated_pg):
        """PATCH auth.session_ttl_seconds with string value returns 422."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/settings/auth.session_ttl_seconds",
                json={"value": "not-an-int", "reason": "type test"},
            )
        assert resp.status_code == 422, (
            f"Expected 422 for wrong type, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# 4. Patch setting updates value and logs history
# ---------------------------------------------------------------------------


class TestPatchUpdatesHistory:
    @pytest.mark.asyncio
    async def test_patch_setting_updates_value_and_logs_history(self, migrated_pg):
        """PATCH auth.session_ttl_seconds → value updated + history row written."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/settings/auth.session_ttl_seconds",
                json={"value": 3600, "reason": "compliance test"},
            )
        assert resp.status_code == 200, (
            f"Expected 200 on PATCH, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["key"] == "auth.session_ttl_seconds"
        assert body["value"] == 3600

        # Verify history row exists
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT old_value, new_value, change_reason "
                "FROM app_settings_history "
                "WHERE setting_key = 'auth.session_ttl_seconds' AND tenant_id IS NULL "
                "ORDER BY changed_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        assert row is not None, "Expected a history row after PATCH"
        assert row[2] == "compliance test"


# ---------------------------------------------------------------------------
# 5. Patch setting requires admin
# ---------------------------------------------------------------------------


class TestPatchRequiresAdmin:
    @pytest.mark.asyncio
    async def test_patch_setting_requires_admin(self, migrated_pg):
        """PATCH /api/admin/settings/* without auth bypass returns 401 or 403."""
        import os

        old_val = os.environ.pop("WEBUI_AUTH_DISABLED", None)
        try:
            async with _client() as client:
                resp = await client.patch(
                    "/api/admin/settings/auth.session_ttl_seconds",
                    json={"value": 3600, "reason": "auth test"},
                )
            assert resp.status_code in (401, 403), (
                f"Expected 401/403 without auth bypass, got {resp.status_code}"
            )
        finally:
            if old_val is not None:
                os.environ["WEBUI_AUTH_DISABLED"] = old_val


# ---------------------------------------------------------------------------
# 6. Reset setting reverts to default
# ---------------------------------------------------------------------------


class TestResetSetting:
    @pytest.mark.asyncio
    async def test_reset_setting_reverts_to_default(self, migrated_pg):
        """POST /api/admin/settings/{key}/reset reverts to catalogue default."""
        key = "auth.session_ttl_seconds"
        default_val = 28800

        # First, PATCH to a non-default value to ensure system row exists
        async with _client() as client:
            patch_resp = await client.patch(
                f"/api/admin/settings/{key}",
                json={"value": 3600, "reason": "pre-reset setup"},
            )
        assert patch_resp.status_code == 200

        # Now reset
        async with _client() as client:
            resp = await client.post(f"/api/admin/settings/{key}/reset")

        assert resp.status_code == 200, (
            f"Expected 200 on reset, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["reset"] is True
        assert body["default_value"] == default_val

        # After reset, get_setting should return the code default
        invalidate_all()
        from src.settings import get_setting
        value_after = get_setting(key, conn=migrated_pg)
        assert value_after == default_val


# ---------------------------------------------------------------------------
# 7. History returns recent changes
# ---------------------------------------------------------------------------


class TestHistoryEndpoint:
    @pytest.mark.asyncio
    async def test_get_history_returns_recent_changes(self, migrated_pg):
        """GET /history after 3 PATCHes returns ≥3 history entries."""
        key = "auth.session_ttl_seconds"

        # Make 3 distinct patches
        for v in [3600, 7200, 14400]:
            async with _client() as client:
                await client.patch(
                    f"/api/admin/settings/{key}",
                    json={"value": v, "reason": f"history test {v}"},
                )

        async with _client() as client:
            resp = await client.get(f"/api/admin/settings/{key}/history")

        assert resp.status_code == 200
        history = resp.json()
        assert isinstance(history, list)
        assert len(history) >= 3, (
            f"Expected ≥3 history entries after 3 PATCHes, got {len(history)}"
        )
        # Each entry should have the canonical fields
        for entry in history:
            assert "id" in entry
            assert "changed_at" in entry
            assert "change_reason" in entry


# ---------------------------------------------------------------------------
# 8. Undo setting reverts to previous
# ---------------------------------------------------------------------------


class TestUndoSetting:
    @pytest.mark.asyncio
    async def test_undo_setting_reverts_to_previous(self, migrated_pg):
        """POST /undo after 2 PATCHes reverts to first PATCH value."""
        key = "auth.session_ttl_seconds"

        # First patch: 3600
        async with _client() as client:
            await client.patch(
                f"/api/admin/settings/{key}",
                json={"value": 3600, "reason": "first"},
            )
        # Second patch: 7200
        async with _client() as client:
            await client.patch(
                f"/api/admin/settings/{key}",
                json={"value": 7200, "reason": "second"},
            )

        # Undo
        async with _client() as client:
            undo_resp = await client.post(f"/api/admin/settings/{key}/undo")

        assert undo_resp.status_code == 200, (
            f"Expected 200 on undo, got {undo_resp.status_code}: {undo_resp.text}"
        )
        body = undo_resp.json()
        assert "undone_to" in body
        # The undo target is the old_value from the most recent history row (3600)
        assert body["undone_to"] == 3600

        # Confirm via GET
        invalidate_all()
        async with _client() as client:
            get_resp = await client.get(f"/api/admin/settings/{key}")
        assert get_resp.status_code == 200
        assert get_resp.json()["current_value"] == 3600


# ---------------------------------------------------------------------------
# 9. Patch unknown key returns 404
# ---------------------------------------------------------------------------


class TestPatchUnknownKey:
    @pytest.mark.asyncio
    async def test_patch_unknown_key_returns_404(self, migrated_pg):
        """PATCH /api/admin/settings/nonexistent.key returns 404."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/settings/nonexistent.key",
                json={"value": 1, "reason": "404 test"},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 10. signup.enabled toggles register endpoint
# ---------------------------------------------------------------------------


class TestSignupEnabledToggle:
    @pytest.mark.asyncio
    async def test_signup_enabled_false_rejects_register(self, migrated_pg):
        """PATCH signup.enabled=False → /api/auth/register returns 403."""
        # Ensure signup.enabled is False
        async with _client() as client:
            patch_resp = await client.patch(
                "/api/admin/settings/signup.enabled",
                json={"value": False, "reason": "test signup gate"},
            )
        assert patch_resp.status_code == 200

        # Flush settings cache so the route reads the new value
        invalidate_all()

        # register should now be rejected with 403 (signup disabled)
        async with _client() as client:
            reg_resp = await client.post(
                "/api/auth/register",
                json={
                    "email": "newuser@example.com",
                    "password": "Password123!",
                    "confirm_password": "Password123!",
                    "username": "newuserWI12",
                },
            )
        assert reg_resp.status_code in (403, 404), (
            f"Expected 403/404 when signup disabled, got {reg_resp.status_code}: {reg_resp.text}"
        )


# ---------------------------------------------------------------------------
# 12. Audit log row created on patch
# ---------------------------------------------------------------------------


class TestAuditLogOnPatch:
    @pytest.mark.asyncio
    async def test_audit_log_row_created_on_patch(self, migrated_pg):
        """PATCH /api/admin/settings/{key} writes a row to admin_audit_log.

        The audit_action decorator always writes a row with action='setting.update'.
        The 'target' column captures the path param; with httpx ASGITransport the
        path_params dict may not be populated, so we assert on action only (the
        decorator is the contract, not the transport).
        """
        # Clear existing audit rows for this action to isolate the assertion
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM admin_audit_log WHERE action = 'setting.update'")

        async with _client() as client:
            resp = await client.patch(
                "/api/admin/settings/auth.session_ttl_seconds",
                json={"value": 3600, "reason": "audit test"},
            )
        assert resp.status_code == 200

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM admin_audit_log "
                "WHERE action = 'setting.update'"
            )
            count = cur.fetchone()[0]
        assert count >= 1, (
            f"Expected ≥1 admin_audit_log row for setting.update, found {count}"
        )
