# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_health_endpoint_unit.py
"""Pure-logic unit tests extracted from test_health_endpoint.py (WS-D / DD2 demote).

These tests drive the in-process MCP ASGI app via ``_mcp_http_client`` and only
exercise ``/health`` — which, per ADR-0046, is a pure liveness probe that performs
NO DB I/O (it reads a module-global cache) — or call
``src.mcp.health._get_mcp_tool_count`` directly with ``mcp`` introspection
monkeypatched.  None of them open a real Neo4j or Postgres connection: the ASGI
lifespan starts without a live DB (the parent file's ``*_down_*`` tests prove the
app boots even with a broken driver), so the previous module-level
``[pytest.mark.neo4j, pytest.mark.postgres]`` was file-level contamination on them.

These pure liveness/introspection tests now run in the fast unit tier
(``-m 'not neo4j and not postgres'``).  The parent file keeps the genuine
``/ready`` connectivity test (``test_ready_reports_db_status_keys`` — real DB) and
the two ``*_down_*`` ``/ready`` degradation tests on the integration tier.

DD2 evidence: confirmed ``/health`` (no DB I/O) + ``mcp`` introspection only.
"""
import contextlib

import httpx
import pytest
from asgi_lifespan import LifespanManager

from src.mcp.server import mcp


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
        """/health is pure liveness: liveness keys present, DB-status keys absent.

        Per ADR-0046, ``neo4j``/``postgres`` connectivity moved to ``/ready``;
        ``/health`` must never depend on (or report) the DB pool.
        """
        async with _mcp_http_client() as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("status", "version", "mcp_tools",
                    "embeddings_total", "embeddings_by_chunk_type"):
            assert key in body, f"Missing key: {key}"
        assert body["status"] == "alive"
        assert "neo4j" not in body
        assert "postgres" not in body

    @pytest.mark.asyncio
    async def test_health_always_200_alive(self):
        """/health returns 200 + status='alive' unconditionally (liveness)."""
        async with _mcp_http_client() as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"

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
        # Should return -1 (introspection failed) and HTTP 200 (liveness never 503)
        assert body["mcp_tools"] == -1
        assert resp.status_code == 200  # /health is pure liveness — always 200


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
