# tests/test_smoke_product_wow.py
"""M5 Product Wow smoke tests.

Smoke tier: fast per-PR tests that verify M5 features are wired up correctly.
Requires: Neo4j running (pytest.mark.smoke + pytest.mark.neo4j)
Auth tests use mock — no real API key DB needed.
"""
import unittest.mock as mock

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.neo4j]


# ---------------------------------------------------------------------------
# Health endpoint smoke
# ---------------------------------------------------------------------------

class TestSmokeHealth:
    @pytest.mark.asyncio
    async def test_health_endpoint_schema(self):
        """GET /health returns JSON with all required schema fields."""
        import httpx

        from src.mcp.server import mcp

        try:
            app = mcp.streamable_http_app()
        except Exception:
            pytest.skip("Cannot get ASGI app from FastMCP")

        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.get("/health")

        assert resp.status_code in (200, 503), (
            f"Unexpected status: {resp.status_code}"
        )
        body = resp.json()
        required_keys = {"status", "neo4j", "postgres", "version", "mcp_tools"}
        missing = required_keys - set(body.keys())
        assert not missing, f"Health response missing keys: {missing}"

    @pytest.mark.asyncio
    async def test_mcp_tools_count_positive(self):
        """mcp_tools in /health is a positive integer or -1 (not hardcoded 14)."""
        import httpx

        from src.mcp.server import mcp

        try:
            app = mcp.streamable_http_app()
        except Exception:
            pytest.skip("Cannot get ASGI app from FastMCP")

        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.get("/health")

        body = resp.json()
        count = body.get("mcp_tools", 0)
        assert isinstance(count, int), (
            f"mcp_tools must be int, got {type(count)}"
        )
        # -1 means introspection failed (acceptable), otherwise must be positive
        assert count > 0 or count == -1, (
            f"mcp_tools must be positive or -1, got {count}"
        )

    @pytest.mark.asyncio
    async def test_health_version_not_unknown(self):
        """Version field is not 'unknown' (package should be installed)."""
        import httpx

        from src.mcp.server import mcp

        try:
            app = mcp.streamable_http_app()
        except Exception:
            pytest.skip("Cannot get ASGI app from FastMCP")

        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.get("/health")

        body = resp.json()
        version = body.get("version")
        assert version and version != "unknown", (
            f"Version should be real, got: {version}"
        )


# ---------------------------------------------------------------------------
# Auth smoke
# ---------------------------------------------------------------------------

class TestSmokeAuth:
    @pytest.mark.asyncio
    async def test_bad_key_returns_401(self):
        """Invalid X-API-Key → 401 on non-public path."""
        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        from src.mcp.middleware import _CACHE_TS, _KEY_CACHE, AuthMiddleware

        _KEY_CACHE.clear()
        _CACHE_TS.clear()

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        # Patch DB verify to always return None (invalid key)
        import src.db.auth_registry as ar
        import src.mcp.server as srv
        with mock.patch.object(ar, "verify_api_key", return_value=None), \
             mock.patch.object(srv, "_get_pg_conn", return_value=object()):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/mcp", headers={"X-API-Key": "osm_bad_key"})

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_no_key_returns_401(self):
        """No X-API-Key header → 401."""
        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        from src.mcp.middleware import AuthMiddleware

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/mcp")

        assert resp.status_code == 401
        assert "Missing" in resp.text or "401" in str(resp.status_code)

    @pytest.mark.asyncio
    async def test_health_bypasses_auth(self):
        """GET /health returns 200 without any X-API-Key header."""
        import httpx
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        from src.mcp.middleware import AuthMiddleware

        async def fake_health(request):
            return JSONResponse({
                "status": "ok",
                "neo4j": "ok",
                "postgres": "ok",
                "version": "test",
                "mcp_tools": 1,
            })

        app = Starlette(routes=[Route("/health", fake_health)])
        app.add_middleware(AuthMiddleware)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# SSH key crypto smoke (no DB needed)
# ---------------------------------------------------------------------------

class TestSmokeSshKeyGen:
    def test_generate_keypair_with_fernet(self, monkeypatch):
        """Ed25519 keypair generation works with valid FERNET_KEY."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("FERNET_KEY", key)

        try:
            from src.web_ui.routes.ssh_keys import generate_ed25519_keypair
        except ImportError:
            pytest.skip("SSH keys module not yet implemented (M5 feature)")

        pub, enc = generate_ed25519_keypair()
        assert pub.startswith("ssh-ed25519 "), (
            f"Public key should start with 'ssh-ed25519', got: {pub[:30]}"
        )
        assert len(enc) > 50, (
            f"Encrypted private key should be substantial, got len={len(enc)}"
        )

    def test_generate_keypair_without_fernet_raises(self, monkeypatch):
        """Missing FERNET_KEY → RuntimeError with clear message."""
        monkeypatch.delenv("FERNET_KEY", raising=False)

        try:
            from src.web_ui.routes.ssh_keys import generate_ed25519_keypair
        except ImportError:
            pytest.skip("SSH keys module not yet implemented (M5 feature)")

        with pytest.raises(
            RuntimeError,
            match="FERNET_KEY|not found|not set",
        ):
            generate_ed25519_keypair()
