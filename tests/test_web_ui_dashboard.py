# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_dashboard.py
"""Tests for /api/dashboard Web UI routes — requires PostgreSQL (M8 Phase 8 fix).

Regression test for: GET /api/dashboard/stats returns 500 when profiles have
datetime fields (created_at). Root cause: JSONResponse used raw json.dumps which
cannot serialize datetime. Fixed by wrapping response with _json_safe().
"""
import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _async_client(app):
    """Return an AsyncClient backed by the ASGI app via ASGITransport."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestDashboardStats:
    @pytest.mark.asyncio
    async def test_stats_empty_db_returns_200(self, migrated_pg):
        """GET /api/dashboard/stats → 200 with zero api/ssh keys.

        Migration 0004 seeds 5 root profiles, so profiles list is non-empty.
        The test verifies the endpoint does not error and key counts are zero.
        """
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/dashboard/stats")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["profiles"], list)
        assert data["api_key_count"] == 0
        assert data["ssh_key_count"] == 0
        assert data["error"] is None

    @pytest.mark.asyncio
    async def test_stats_with_profile_returns_200_not_500(self, migrated_pg):
        """GET /api/dashboard/stats with a profile that has datetime fields → 200.

        Regression: profiles/repos rows have created_at (datetime) and
        last_indexed_at (datetime | None). Without _json_safe wrapping the
        JSONResponse raises TypeError → 500. This test confirms the fix holds.
        """
        from src.db.pg import repo_store

        repo_store().add_profile(name="test_dash", odoo_version="17.0", description="test")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/dashboard/stats")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["profiles"], list)
        # Migration 0004 seeds 5 root profiles; this test adds one more.
        named = [p for p in data["profiles"] if p["name"] == "test_dash"]
        assert len(named) == 1
        profile = named[0]
        assert profile["name"] == "test_dash"
        # created_at must be a JSON string (ISO-8601), not a datetime object
        if "created_at" in profile:
            assert isinstance(profile["created_at"], str), (
                "created_at must be serialized as ISO string, not raw datetime"
            )
        assert data["error"] is None

    @pytest.mark.asyncio
    async def test_stats_shape(self, migrated_pg):
        """GET /api/dashboard/stats → response has all expected keys."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/dashboard/stats")

        assert resp.status_code == 200
        data = resp.json()
        for key in ("profiles", "api_key_count", "ssh_key_count", "embeddings_total", "error"):
            assert key in data, f"Missing key: {key}"
