"""Tests for health endpoint chunk_type breakdown.

Tests the new /health endpoint field that breaks down embeddings by chunk_type.
"""

import contextlib

import httpx
import pytest
from asgi_lifespan import LifespanManager

from src.mcp.server import mcp

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]


@contextlib.asynccontextmanager
async def _mcp_http_client():
    """ASGI client for the MCP HTTP transport (stateless + JSON)."""
    app = mcp.http_app(stateless_http=True, json_response=True)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client:
            yield client


class TestHealthChunkTypeBreakdown:

    @pytest.mark.asyncio
    async def test_health_includes_embeddings_by_chunk_type_key(self):
        """Test /health response includes embeddings_by_chunk_type field."""
        async with _mcp_http_client() as client:
            resp = await client.get("/health")

        assert resp.status_code in (200, 503)
        body = resp.json()

        # New field must be present
        assert "embeddings_by_chunk_type" in body

    @pytest.mark.asyncio
    async def test_embeddings_by_chunk_type_is_dict(self):
        """Test embeddings_by_chunk_type is always a dict (never null)."""
        async with _mcp_http_client() as client:
            resp = await client.get("/health")

        body = resp.json()
        assert isinstance(body["embeddings_by_chunk_type"], dict)

    @pytest.mark.asyncio
    async def test_sum_of_chunk_types_equals_total(self):
        """Test sum of embeddings_by_chunk_type values equals embeddings_total."""
        async with _mcp_http_client() as client:
            resp = await client.get("/health")

        body = resp.json()
        total = body.get("embeddings_total")
        chunk_breakdown = body.get("embeddings_by_chunk_type", {})

        if total is not None and chunk_breakdown:
            # Sum should equal total
            assert sum(chunk_breakdown.values()) == total

    @pytest.mark.asyncio
    async def test_chunk_type_values_are_integers(self):
        """Test each chunk_type count is an integer."""
        async with _mcp_http_client() as client:
            resp = await client.get("/health")

        body = resp.json()
        chunk_breakdown = body.get("embeddings_by_chunk_type", {})

        for chunk_type, count in chunk_breakdown.items():
            assert isinstance(chunk_type, str), f"chunk_type {chunk_type} should be string"
            assert isinstance(count, int), f"count for {chunk_type} should be int"
            assert count >= 0, f"count for {chunk_type} should be non-negative"

    @pytest.mark.asyncio
    async def test_backward_compatibility_existing_fields_present(self):
        """Test /health still includes all original fields (backward compat)."""
        async with _mcp_http_client() as client:
            resp = await client.get("/health")

        body = resp.json()

        # All original fields must be present
        for key in ("status", "neo4j", "postgres", "version", "mcp_tools", "embeddings_total"):
            assert key in body, f"Missing original key: {key}"


@pytest.mark.asyncio
async def test_get_embeddings_by_chunk_type_returns_dict_on_success():
    """Test _get_embeddings_by_chunk_type returns dict when DB available."""
    from src.mcp.health import _get_embeddings_by_chunk_type

    # This test runs with actual DB fixture (neo4j/postgres marks)
    result = await _get_embeddings_by_chunk_type()

    # Result should be dict or None (if DB unavailable in test)
    assert result is None or isinstance(result, dict)

    if result is not None:
        # If available, all values should be ints
        for chunk_type, count in result.items():
            assert isinstance(chunk_type, str)
            assert isinstance(count, int)
            assert count >= 0
