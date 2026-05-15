"""Tests for src/auth.py and src/mcp/middleware.py — no DB required."""
import time

import pytest

from src.auth import generate_api_key, hash_key
from src.mcp.middleware import (
    _CACHE_TS,
    _CACHE_TTL,
    _KEY_CACHE,
    _PUBLIC_PATHS,
    AuthMiddleware,
    _cache_get,
    _cache_invalidate,
    _cache_set,
)


class TestGenerateApiKey:
    def test_returns_tuple_of_strings(self):
        raw, hashed = generate_api_key()
        assert isinstance(raw, str)
        assert isinstance(hashed, str)

    def test_raw_has_osm_prefix(self):
        raw, _ = generate_api_key()
        assert raw.startswith("osm_")

    def test_hash_matches_hash_key(self):
        raw, hashed = generate_api_key()
        assert hashed == hash_key(raw)

    def test_two_calls_produce_different_keys(self):
        raw1, _ = generate_api_key()
        raw2, _ = generate_api_key()
        assert raw1 != raw2


class TestHashKey:
    def test_deterministic(self):
        assert hash_key("test_key") == hash_key("test_key")

    def test_different_inputs_different_hashes(self):
        assert hash_key("key1") != hash_key("key2")

    def test_hmac_sha256_length(self):
        h = hash_key("any_key")
        assert len(h) == 64  # HMAC-SHA256 hex = 64 chars


class TestCacheOperations:
    def setup_method(self):
        # Clear caches before each test
        _KEY_CACHE.clear()
        _CACHE_TS.clear()

    def test_cache_miss_on_empty(self):
        hit, val = _cache_get("nonexistent")
        assert hit is False
        assert val is None

    def test_cache_set_and_get(self):
        _cache_set("key1", 42)
        hit, val = _cache_get("key1")
        assert hit is True
        assert val == 42

    def test_cache_none_value(self):
        _cache_set("invalid_key", None)
        hit, val = _cache_get("invalid_key")
        assert hit is True
        assert val is None

    def test_cache_invalidate(self):
        _cache_set("key2", 99)
        _cache_invalidate("key2")
        hit, _ = _cache_get("key2")
        assert hit is False

    def test_cache_expired(self):
        _cache_set("key3", 7)
        # Manually expire: use hash as cache key (I2: keys stored hashed)
        _CACHE_TS[hash_key("key3")] = time.monotonic() - _CACHE_TTL - 1
        hit, _ = _cache_get("key3")
        assert hit is False


class TestPublicPaths:
    def test_health_is_public(self):
        assert "/health" in _PUBLIC_PATHS


class TestAuthMiddlewareUnit:
    """Unit tests using httpx + mock — no real DB."""

    @pytest.mark.asyncio
    async def test_missing_key_returns_401(self):
        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/mcp")
        assert resp.status_code == 401
        assert "Missing X-API-Key" in resp.text

    @pytest.mark.asyncio
    async def test_health_path_bypasses_auth(self):
        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def health(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/health", health)])
        app.add_middleware(AuthMiddleware)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_key_returns_401(self, monkeypatch):
        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        # Clear cache so we go to DB path
        _KEY_CACHE.clear()
        _CACHE_TS.clear()

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        # Patch auth_store().verify_api_key to return None (invalid key)
        from unittest.mock import MagicMock

        import src.db.pg as pg_mod

        mock_auth = MagicMock()
        mock_auth.verify_api_key.return_value = None
        monkeypatch.setattr(pg_mod, "auth_store", lambda: mock_auth)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/mcp", headers={"X-API-Key": "osm_invalid"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_key_returns_200(self, monkeypatch):
        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        _KEY_CACHE.clear()
        _CACHE_TS.clear()

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        from unittest.mock import MagicMock

        import src.db.pg as pg_mod

        mock_auth = MagicMock()
        mock_auth.verify_api_key.return_value = 1
        monkeypatch.setattr(pg_mod, "auth_store", lambda: mock_auth)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/mcp", headers={"X-API-Key": "osm_validkey"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cached_key_skips_db(self, monkeypatch):
        """Second request with same key must use cache — db function must NOT be called."""
        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        _KEY_CACHE.clear()
        _CACHE_TS.clear()

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        from unittest.mock import MagicMock

        import src.db.pg as pg_mod

        call_count = {"n": 0}

        def counting_verify(key):
            call_count["n"] += 1
            return 1

        mock_auth = MagicMock()
        mock_auth.verify_api_key.side_effect = counting_verify
        monkeypatch.setattr(pg_mod, "auth_store", lambda: mock_auth)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/mcp", headers={"X-API-Key": "osm_cached"})
            await client.get("/mcp", headers={"X-API-Key": "osm_cached"})

        # DB should have been called exactly once; second request uses cache
        assert call_count["n"] == 1
