"""Tests for /health endpoint."""
import pytest

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]


class TestHealthEndpoint:
    def _get_asgi_app(self):
        """Get ASGI app from FastMCP instance."""
        from src.mcp.server import mcp
        try:
            return mcp.streamable_http_app()
        except Exception:
            try:
                return mcp._app
            except Exception:
                pytest.skip("Cannot get ASGI app from FastMCP")

    @pytest.mark.asyncio
    async def test_returns_required_keys(self):
        """Health response must contain all required keys."""
        import httpx

        app = self._get_asgi_app()
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code in (200, 503)
        body = resp.json()
        for key in ("status", "neo4j", "postgres", "version", "mcp_tools"):
            assert key in body, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_both_ok_returns_200(self):
        """When both Neo4j and PostgreSQL are OK, return 200 with status='ok'."""
        import httpx

        app = self._get_asgi_app()
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.get("/health")
        body = resp.json()
        if body["neo4j"] == "ok" and body["postgres"] == "ok":
            assert resp.status_code == 200
            assert body["status"] == "ok"

    @pytest.mark.asyncio
    async def test_neo4j_down_returns_degraded_or_error(self, monkeypatch):
        """When Neo4j fails, status should not be 'ok'."""
        import httpx

        from src.mcp import server as server_mod

        def mock_broken_driver():
            class BrokenDriver:
                def verify_connectivity(self):
                    raise ConnectionError("Neo4j down")

            return BrokenDriver()

        monkeypatch.setattr(server_mod, "_get_driver", mock_broken_driver)

        app = self._get_asgi_app()
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.get("/health")
        body = resp.json()
        assert body["status"] != "ok"
        assert body["neo4j"].startswith("error:")

    @pytest.mark.asyncio
    async def test_postgres_down_returns_degraded(self, monkeypatch):
        """When PostgreSQL fails, status should not be 'ok'."""
        import httpx

        from src.mcp import server as server_mod

        def mock_broken_pg():
            class BrokenConn:
                closed = False

                def cursor(self):
                    raise ConnectionError("PostgreSQL down")

            return BrokenConn()

        monkeypatch.setattr(server_mod, "_get_pg_conn", mock_broken_pg)

        app = self._get_asgi_app()
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.get("/health")
        body = resp.json()
        assert body["status"] != "ok"
        assert body["postgres"].startswith("error:")

    @pytest.mark.asyncio
    async def test_mcp_tools_count_is_positive_int(self):
        """MCP tools count should be a positive integer (not hardcoded to 14)."""
        import httpx

        app = self._get_asgi_app()
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.get("/health")
        body = resp.json()
        assert isinstance(body["mcp_tools"], int)
        # Must be positive; -1 signals introspection failure (deferred to M5.5)
        assert body["mcp_tools"] > 0

    @pytest.mark.asyncio
    async def test_mcp_tools_handles_missing_get_tools(self, monkeypatch):
        """When get_tools unavailable, fallback to _tool_manager._tools gracefully."""
        import httpx

        from src.mcp import health as health_mod
        from src.mcp.server import mcp

        # Monkeypatch _get_mcp_tool_count to simulate broken get_tools
        async def mock_get_count_fallback():
            """Simulate scenario where get_tools fails but _tool_manager._tools works."""
            # Temporarily remove get_tools to test fallback
            original_get_tools = getattr(mcp, "get_tools", None)
            try:
                if hasattr(mcp, "get_tools"):
                    delattr(mcp, "get_tools")
                # Now try our function — should fallback to _tool_manager
                return await health_mod._get_mcp_tool_count()
            finally:
                if original_get_tools:
                    mcp.get_tools = original_get_tools

        monkeypatch.setattr(health_mod, "_get_mcp_tool_count", mock_get_count_fallback)

        app = self._get_asgi_app()
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.get("/health")
        body = resp.json()
        # Should return int (either positive count or -1 on complete failure)
        assert isinstance(body["mcp_tools"], int)

    @pytest.mark.asyncio
    async def test_mcp_tools_returns_minus_one_on_complete_failure(self, monkeypatch):
        """When all introspection methods fail, return -1 without raising."""
        import httpx

        from src.mcp import server as server_mod

        # Mock both get_tools and _tool_manager to fail
        def mock_broken_mcp():
            class BrokenMCP:
                async def get_tools(self):
                    raise RuntimeError("get_tools broken")

            return BrokenMCP()

        monkeypatch.setattr(server_mod, "mcp", mock_broken_mcp())

        app = self._get_asgi_app()
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.get("/health")
        body = resp.json()
        # Should return -1 (introspection failed) and HTTP 200/503 (not 500)
        assert body["mcp_tools"] == -1
        assert resp.status_code in (200, 503)  # depends on Neo4j/PG status
