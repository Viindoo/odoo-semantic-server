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


# NOTE (WS-D / DD2 demote): six pure liveness/introspection tests that only hit
# /health (no DB I/O per ADR-0046) or call _get_mcp_tool_count directly moved to
# tests/test_health_endpoint_unit.py. The /ready connectivity test and the two
# /ready degradation tests below stay on the neo4j+postgres integration tier.


class TestHealthEndpoint:

    @pytest.mark.asyncio
    async def test_ready_reports_db_status_keys(self):
        """/ready carries the DB-connectivity contract that left /health."""
        async with _mcp_http_client() as client:
            resp = await client.get("/ready")
        assert resp.status_code in (200, 503)
        body = resp.json()
        for key in ("status", "neo4j", "postgres", "version",
                    "embeddings_total", "embeddings_by_chunk_type"):
            assert key in body, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_neo4j_down_health_stays_alive_ready_degrades(self, monkeypatch):
        """Neo4j down: /health still 200 alive (anti-#227); /ready not 'ok'."""

        from src.mcp import server as server_mod

        def mock_broken_driver():
            class BrokenDriver:
                def verify_connectivity(self):
                    raise ConnectionError("Neo4j down")

            return BrokenDriver()

        monkeypatch.setattr(server_mod, "_get_driver", mock_broken_driver)

        async with _mcp_http_client() as client:
            health = await client.get("/health")
            ready = await client.get("/ready")
        # Liveness must be unaffected by a DB outage.
        assert health.status_code == 200
        assert health.json()["status"] == "alive"
        # Readiness reflects the outage.
        rbody = ready.json()
        assert rbody["status"] != "ok"
        assert rbody["neo4j"].startswith("error:")

    @pytest.mark.asyncio
    async def test_postgres_down_health_stays_alive_ready_degrades(self, monkeypatch):
        """PostgreSQL down: /health still 200 alive; /ready not 'ok'."""
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
        # /ready may serve cached counts; clear the cache so this probe actually
        # exercises the (now broken) PG path rather than a warm cache entry.
        from src.mcp import health as health_mod
        monkeypatch.setattr(health_mod, "_ready_cache", None)

        async with _mcp_http_client() as client:
            health = await client.get("/health")
            ready = await client.get("/ready")
        assert health.status_code == 200
        assert health.json()["status"] == "alive"
        rbody = ready.json()
        assert rbody["status"] != "ok"
        assert rbody["postgres"].startswith("error:")
