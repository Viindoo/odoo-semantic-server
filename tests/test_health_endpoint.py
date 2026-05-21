# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for /health endpoint."""
import contextlib

import httpx
import pytest
from asgi_lifespan import LifespanManager

from src.mcp.server import mcp

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]


@contextlib.asynccontextmanager
async def _mcp_http_client():
    """ASGI client for the MCP HTTP transport (stateless + JSON; see test_smoke_e2e_mcp_http)."""
    app = mcp.http_app(stateless_http=True, json_response=True)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client:
            yield client


class TestHealthEndpoint:

    @pytest.mark.asyncio
    async def test_returns_required_keys(self):
        """Health response must contain all required keys."""

        async with _mcp_http_client() as client:
            resp = await client.get("/health")
        assert resp.status_code in (200, 503)
        body = resp.json()
        for key in ("status", "neo4j", "postgres", "version", "mcp_tools"):
            assert key in body, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_both_ok_returns_200(self):
        """When both Neo4j and PostgreSQL are OK, return 200 with status='ok'."""

        async with _mcp_http_client() as client:
            resp = await client.get("/health")
        body = resp.json()
        if body["neo4j"] == "ok" and body["postgres"] == "ok":
            assert resp.status_code == 200
            assert body["status"] == "ok"

    @pytest.mark.asyncio
    async def test_neo4j_down_returns_degraded_or_error(self, monkeypatch):
        """When Neo4j fails, status should not be 'ok'."""

        from src.mcp import server as server_mod

        def mock_broken_driver():
            class BrokenDriver:
                def verify_connectivity(self):
                    raise ConnectionError("Neo4j down")

            return BrokenDriver()

        monkeypatch.setattr(server_mod, "_get_driver", mock_broken_driver)

        async with _mcp_http_client() as client:
            resp = await client.get("/health")
        body = resp.json()
        assert body["status"] != "ok"
        assert body["neo4j"].startswith("error:")

    @pytest.mark.asyncio
    async def test_postgres_down_returns_degraded(self, monkeypatch):
        """When PostgreSQL fails, status should not be 'ok'."""
        from contextlib import contextmanager

        from src.mcp import server as server_mod

        class BrokenConn:
            closed = False

            def cursor(self):
                raise ConnectionError("PostgreSQL down")

        @contextmanager
        def mock_broken_checkout():
            yield BrokenConn()

        monkeypatch.setattr(server_mod, "_checkout_pg", mock_broken_checkout)

        async with _mcp_http_client() as client:
            resp = await client.get("/health")
        body = resp.json()
        assert body["status"] != "ok"
        assert body["postgres"].startswith("error:")

    @pytest.mark.asyncio
    async def test_mcp_tools_count_is_positive_int(self):
        """MCP tools count should be a positive integer (not hardcoded to 14)."""

        async with _mcp_http_client() as client:
            resp = await client.get("/health")
        body = resp.json()
        assert isinstance(body["mcp_tools"], int)
        # Must be positive; -1 signals introspection failure (deferred to M5.5)
        assert body["mcp_tools"] > 0

    @pytest.mark.asyncio
    async def test_mcp_tools_handles_missing_get_tools(self, monkeypatch):
        """When get_tools raises, fallback to _tool_manager._tools gracefully."""
        from src.mcp.server import mcp

        # Override get_tools to raise — exercises the except branch in
        # _get_mcp_tool_count which then falls back to mcp._tool_manager._tools.
        # (delattr on a class-bound method fails with AttributeError, so we
        # shadow it with a raising callable instead.)
        async def broken_get_tools():
            raise RuntimeError("simulated get_tools failure")

        monkeypatch.setattr(mcp, "get_tools", broken_get_tools, raising=False)

        async with _mcp_http_client() as client:
            resp = await client.get("/health")
        body = resp.json()
        # Fallback should produce a positive count from _tool_manager._tools.
        assert isinstance(body["mcp_tools"], int)
        assert body["mcp_tools"] > 0, (
            f"Expected fallback to return positive count, got {body['mcp_tools']}"
        )

    @pytest.mark.asyncio
    async def test_mcp_tools_returns_minus_one_on_complete_failure(self, monkeypatch):
        """When all introspection methods fail, return -1 without raising."""

        from src.mcp import server as server_mod

        # Mock both get_tools and _tool_manager to fail
        def mock_broken_mcp():
            class BrokenMCP:
                async def get_tools(self):
                    raise RuntimeError("get_tools broken")

            return BrokenMCP()

        monkeypatch.setattr(server_mod, "mcp", mock_broken_mcp())

        async with _mcp_http_client() as client:
            resp = await client.get("/health")
        body = resp.json()
        # Should return -1 (introspection failed) and HTTP 200/503 (not 500)
        assert body["mcp_tools"] == -1
        assert resp.status_code in (200, 503)  # depends on Neo4j/PG status


@pytest.mark.asyncio
async def test_get_tools_failure_falls_through_to_private_api(monkeypatch):
    """When mcp.get_tools() raises, _tool_manager._tools fallback is used."""
    from src.mcp import health as health_mod
    from src.mcp.server import mcp

    async def boom():
        raise RuntimeError("FastMCP API unstable")

    # Patch get_tools to raise; keep private _tool_manager intact
    monkeypatch.setattr(mcp, "get_tools", boom, raising=False)

    # Sanity: private API must exist for this test to be meaningful
    assert hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools")
    private_count = len(mcp._tool_manager._tools)

    count = await health_mod._get_mcp_tool_count()
    assert count == private_count  # fallback executed, returned private count
