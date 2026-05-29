# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for /api/admin/plans CRUD endpoints (WI-12, ADR-0039).

6 test cases covering:
1. test_list_plans_returns_4_tiers           GET / returns exactly 4 seeded tiers
2. test_get_single_plan_by_slug              GET /{slug} returns correct row
3. test_patch_plan_updates_quota_and_rpm     PATCH /{slug} updates fields in DB
4. test_patch_plan_invalidates_cache         PATCH triggers _PLAN_CACHE clear
5. test_patch_unknown_slug_404               PATCH nonexistent slug → 404
6. test_post_create_plan_501                 POST / → 501 (Phase 2 deferred)

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
WEBUI_AUTH_DISABLED is active (conftest autouse); require_admin and
require_admin_with_fresh_mfa both return user_id=1 without a real session.
"""
from __future__ import annotations

import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply migrations once per test on a clean schema. Plans seeded by m13_006."""
    run_migrations(clean_pg)
    return clean_pg


def _client():
    """Factory: fresh httpx.AsyncClient per request block."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# 1. List plans returns 4 tiers
# ---------------------------------------------------------------------------


class TestListPlans:
    @pytest.mark.asyncio
    async def test_list_plans_returns_all_tiers(self, migrated_pg):
        """GET /api/admin/plans returns every seeded tier ordered by quota ASC.

        Response shape is ``{"plans": [...]}`` (NOT a bare array) — the contract
        consumed by the admin UI shipped in M10B P0-ext (#206): api-keys.astro /
        users.astro read ``data.plans``.

        Seed = m13_006 (free-grandfathered / free / pro / team) +
        m13_009 'unlimited' sentinel (is_public=FALSE, admin-granted; ADR-0041 D5).
        m13_013 then deletes 'free-grandfathered', so post-consolidation the DB
        has exactly 4 plans: free / pro / team / unlimited.
        The list endpoint deliberately returns non-public tiers too so the admin
        UI can render every assignable plan.
        """
        async with _client() as client:
            resp = await client.get("/api/admin/plans")

        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict) and "plans" in body, (
            f"Expected {{'plans': [...]}} wrapper, got {type(body).__name__}"
        )
        plans = body["plans"]
        assert isinstance(plans, list)
        # NOTE (m13_013): m13_013_consolidate_free_plans.sql deletes 'free-grandfathered'
        # (repoints its api_keys to 'unlimited').  Post-consolidation the migrated set is
        # 4 plans: free / pro / team / unlimited.
        assert len(plans) == 4, (
            f"Expected 4 plans (free/pro/team/unlimited after m13_013 removes"
            f" free-grandfathered), got {len(plans)}: {[p['slug'] for p in plans]}"
        )

        # Verify all expected slugs present (incl. the admin-only 'unlimited' sentinel)
        slugs = {p["slug"] for p in plans}
        expected = {"free", "pro", "team", "unlimited"}
        assert slugs == expected, f"Expected slugs {expected}, got {slugs}"

        # Each plan must have the canonical fields
        for plan in plans:
            assert "id" in plan
            assert "slug" in plan
            assert "display_name" in plan
            assert "quota_calls_per_month" in plan
            assert "rate_limit_rpm" in plan

        # Verify ordering: quota ASC
        quotas = [p["quota_calls_per_month"] for p in plans]
        assert quotas == sorted(quotas), f"Plans not ordered by quota ASC: {quotas}"


# ---------------------------------------------------------------------------
# 2. Get single plan by slug
# ---------------------------------------------------------------------------


class TestGetSinglePlan:
    @pytest.mark.asyncio
    async def test_get_single_plan_by_slug(self, migrated_pg):
        """GET /api/admin/plans/free returns the free plan row."""
        async with _client() as client:
            resp = await client.get("/api/admin/plans/free")

        assert resp.status_code == 200
        body = resp.json()
        assert body["slug"] == "free"
        assert body["quota_calls_per_month"] == 100
        assert body["rate_limit_rpm"] == 30

    @pytest.mark.asyncio
    async def test_get_nonexistent_slug_returns_404(self, migrated_pg):
        """GET /api/admin/plans/does-not-exist returns 404."""
        async with _client() as client:
            resp = await client.get("/api/admin/plans/does-not-exist")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. Patch plan updates quota and rpm
# ---------------------------------------------------------------------------


class TestPatchPlan:
    @pytest.mark.asyncio
    async def test_patch_plan_updates_quota_and_rpm(self, migrated_pg):
        """PATCH /api/admin/plans/free updates quota_calls_per_month and rate_limit_rpm."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/plans/free",
                json={
                    "quota_calls_per_month": 150,
                    "rate_limit_rpm": 40,
                    "reason": "test update quota and rpm",
                },
            )

        assert resp.status_code == 200, (
            f"Expected 200 on PATCH, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["slug"] == "free"
        assert body["quota_calls_per_month"] == 150
        assert body["rate_limit_rpm"] == 40

        # Verify DB was updated
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT quota_calls_per_month, rate_limit_rpm FROM plans WHERE slug = 'free'"
            )
            row = cur.fetchone()
        assert row[0] == 150
        assert row[1] == 40

        # Restore original values so other tests in this module see clean state
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE plans SET quota_calls_per_month = 100, rate_limit_rpm = 30 "
                "WHERE slug = 'free'"
            )


# ---------------------------------------------------------------------------
# 4. Patch plan invalidates cache
# ---------------------------------------------------------------------------


class TestPatchPlanInvalidatesCache:
    @pytest.mark.asyncio
    async def test_patch_plan_invalidates_cache(self, migrated_pg):
        """PATCH /api/admin/plans/{slug} calls _invalidate_plan_cache()."""
        with mock.patch(
            "src.web_ui.routes.admin_plans._invalidate_plan_cache"
        ) as mock_invalidate:
            async with _client() as client:
                resp = await client.patch(
                    "/api/admin/plans/pro",
                    json={"rate_limit_rpm": 130, "reason": "cache invalidation test"},
                )
            assert resp.status_code == 200, (
                f"Expected 200 on PATCH, got {resp.status_code}: {resp.text}"
            )
            mock_invalidate.assert_called_once()

        # Restore
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE plans SET rate_limit_rpm = 120 WHERE slug = 'pro'"
            )


# ---------------------------------------------------------------------------
# 5. Patch unknown slug 404
# ---------------------------------------------------------------------------


class TestPatchUnknownSlug:
    @pytest.mark.asyncio
    async def test_patch_unknown_slug_404(self, migrated_pg):
        """PATCH /api/admin/plans/no-such-plan returns 404."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/plans/no-such-plan",
                json={"rate_limit_rpm": 99, "reason": "not found test"},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6. POST create plan returns 501
# ---------------------------------------------------------------------------


class TestCreatePlanNotImplemented:
    @pytest.mark.asyncio
    async def test_post_create_plan_501(self, migrated_pg):
        """POST /api/admin/plans returns 501 — Phase 2 deferred."""
        async with _client() as client:
            resp = await client.post(
                "/api/admin/plans",
                json={
                    "slug": "enterprise",
                    "display_name": "Enterprise",
                    "quota_calls_per_month": 1000000,
                    "rate_limit_rpm": 1000,
                    "reason": "create plan test",
                },
            )
        assert resp.status_code == 501, (
            f"Expected 501 for deferred create-plan, got {resp.status_code}: {resp.text}"
        )
