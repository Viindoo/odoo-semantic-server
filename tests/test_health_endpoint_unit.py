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

pytestmark = pytest.mark.http


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
    async def test_mcp_tools_returns_minus_one_on_introspection_failure(self, monkeypatch):
        """When list_tools() raises, /health still serves -1 (never crashes/503).

        fastmcp v3 introspects the tool surface via the single public async
        ``list_tools()`` (the v2 ``get_tools()`` + ``_tool_manager`` fallback both
        gone). The liveness contract is unchanged: a broken introspection must
        degrade to -1 with HTTP 200, never raise out of /health (#324).
        """
        from src.mcp.server import mcp

        async def broken_list_tools():
            raise RuntimeError("simulated list_tools failure")

        monkeypatch.setattr(mcp, "list_tools", broken_list_tools, raising=False)

        async with _mcp_http_client() as client:
            resp = await client.get("/health")
        body = resp.json()
        # Introspection failure degrades to -1, liveness stays 200.
        assert body["mcp_tools"] == -1, (
            f"Expected -1 on list_tools failure, got {body['mcp_tools']}"
        )
        assert resp.status_code == 200  # /health is pure liveness — always 200


@pytest.mark.asyncio
async def test_tool_count_reads_list_tools(monkeypatch):
    """_get_mcp_tool_count returns the real list_tools() length (the v3 surface)."""
    from src.mcp import health as health_mod
    from src.mcp.server import mcp

    # The public v3 accessor is the single source the helper reads.
    real_count = len(await mcp.list_tools())
    assert real_count > 0, "sanity: the server must own at least one tool"

    count = await health_mod._get_mcp_tool_count()
    assert count == real_count


@pytest.mark.asyncio
async def test_tool_count_returns_minus_one_when_list_tools_raises(monkeypatch):
    """When list_tools() raises, _get_mcp_tool_count degrades to -1 (no raise)."""
    from src.mcp import health as health_mod
    from src.mcp.server import mcp

    async def boom():
        raise RuntimeError("FastMCP API unstable")

    monkeypatch.setattr(mcp, "list_tools", boom, raising=False)

    count = await health_mod._get_mcp_tool_count()
    assert count == -1  # graceful sentinel, helper never propagates the error
