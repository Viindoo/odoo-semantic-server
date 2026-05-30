# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for GET /api/plans (public pricing-page endpoint).

Business intent (3 cases):
  T1  Response shape is {"plans": [...]} with required fields including prices.
  T2  Only public, non-archived plans are returned.
  T3  Prices field contains per-currency map (e.g. {"USD": 1900}).

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
from __future__ import annotations

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
    """Apply migrations once per test on a clean schema."""
    run_migrations(clean_pg)
    return clean_pg


def _client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# T1: Response shape includes prices field
# ---------------------------------------------------------------------------


class TestPublicPlansShape:
    @pytest.mark.asyncio
    async def test_returns_plans_wrapper(self, migrated_pg):
        """GET /api/plans returns {"plans": [...]} wrapper."""
        async with _client() as client:
            resp = await client.get("/api/plans")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict) and "plans" in body, (
            f"Expected {{plans: [...]}} wrapper, got {body!r}"
        )

    @pytest.mark.asyncio
    async def test_each_plan_has_prices_field(self, migrated_pg):
        """GET /api/plans includes prices field in each plan (C3/plans.py)."""
        async with _client() as client:
            resp = await client.get("/api/plans")
        assert resp.status_code == 200
        plans = resp.json()["plans"]
        assert len(plans) > 0, "Expected at least one public plan"
        for plan in plans:
            assert "prices" in plan, (
                f"Plan {plan.get('slug', '?')} missing 'prices' field"
            )

    @pytest.mark.asyncio
    async def test_each_plan_has_required_fields(self, migrated_pg):
        """GET /api/plans includes all required pricing fields."""
        required = {
            "id", "slug", "display_name", "quota_calls_per_month",
            "rate_limit_rpm", "seat_limit", "price_cents", "currency",
            "billing_interval", "prices",
        }
        async with _client() as client:
            resp = await client.get("/api/plans")
        for plan in resp.json()["plans"]:
            missing = required - set(plan.keys())
            assert not missing, (
                f"Plan {plan.get('slug', '?')} missing fields: {missing}"
            )


# ---------------------------------------------------------------------------
# T2: Only public, non-archived plans returned
# ---------------------------------------------------------------------------


class TestPublicPlansFiltering:
    @pytest.mark.asyncio
    async def test_private_plans_excluded(self, migrated_pg):
        """GET /api/plans does not return non-public plans (e.g. 'unlimited')."""
        async with _client() as client:
            resp = await client.get("/api/plans")
        slugs = {p["slug"] for p in resp.json()["plans"]}
        assert "unlimited" not in slugs, (
            "The 'unlimited' sentinel (is_public=FALSE) must not appear in public plans"
        )

    @pytest.mark.asyncio
    async def test_archived_plans_excluded(self, migrated_pg):
        """GET /api/plans excludes archived plans."""
        # Archive 'pro' temporarily.
        with migrated_pg.cursor() as cur:
            cur.execute("UPDATE plans SET is_archived = TRUE WHERE slug = 'pro'")
        migrated_pg.commit()

        async with _client() as client:
            resp = await client.get("/api/plans")
        slugs = {p["slug"] for p in resp.json()["plans"]}

        # Restore
        with migrated_pg.cursor() as cur:
            cur.execute("UPDATE plans SET is_archived = FALSE WHERE slug = 'pro'")
        migrated_pg.commit()

        assert "pro" not in slugs, "Archived plan 'pro' must not appear in public list"


# ---------------------------------------------------------------------------
# T3: Prices field contains seeded per-currency data
# ---------------------------------------------------------------------------


class TestPublicPlansPricesContent:
    @pytest.mark.asyncio
    async def test_pro_plan_prices_has_usd(self, migrated_pg):
        """GET /api/plans: pro plan has a USD entry in prices (seeded by m13_015)."""
        async with _client() as client:
            resp = await client.get("/api/plans")
        pro_plans = [p for p in resp.json()["plans"] if p["slug"] == "pro"]
        assert pro_plans, "Expected 'pro' plan in public plans"
        pro = pro_plans[0]
        prices = pro.get("prices") or {}
        assert "USD" in prices, (
            f"Expected 'pro' plan to have USD in prices, got: {prices}"
        )
        # seeded value from m13_015
        assert prices["USD"] == 1900, (
            f"Expected pro USD price = 1900 cents, got {prices['USD']}"
        )
