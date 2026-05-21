# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Web UI health endpoint (WI-4, pre-launch checklist §10.5).

Business intent: GET /api/health returns 200 with status and version,
accessible WITHOUT authentication. Required for uptime monitoring.
"""

import pytest


@pytest.fixture
def web_app():
    from src.web_ui.app import create_app

    return create_app()


class TestHealthEndpoint:
    """Health endpoint must be accessible without authentication."""

    @pytest.mark.asyncio
    async def test_health_returns_200_unauthenticated(self, web_app):
        """GET /api/health without auth must return 200 with status and version."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/health")

        assert resp.status_code == 200, "Health endpoint must return 200"
        data = resp.json()
        assert "status" in data, "Response must contain 'status' key"
        assert "version" in data, "Response must contain 'version' key"
        assert data["status"] == "ok", "Status must be 'ok'"
        assert isinstance(data["version"], str), "Version must be a string"
