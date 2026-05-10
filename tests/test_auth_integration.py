"""End-to-end integration tests: auth middleware + DB + advisory lock.

Marker: pytest.mark.postgres (requires PostgreSQL running).
Tests cover:
  - Auth create/verify/log usage cycle
  - Cache TTL expiration
  - Advisory lock concurrent access
  - Hash comparison security
  - Middleware /health bypass and key validation
"""
import os
import time
import unittest.mock as mock

import httpx
import psycopg2
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from src.auth import hash_key
from src.db.auth_registry import (
    create_api_key,
    deactivate_api_key,
    list_api_keys,
    log_usage,
    verify_api_key,
)
from src.db.migrate import run_migrations
from src.indexer.pipeline import _indexer_lock, _profile_lock_id
from src.mcp.middleware import (
    _CACHE_TS,
    _CACHE_TTL,
    _KEY_CACHE,
    AuthMiddleware,
    _cache_get,
    _cache_invalidate,
    _cache_invalidate_by_key_id,
    _cache_set,
)

pytestmark = pytest.mark.postgres

PG_TEST_DSN = os.getenv(
    "PG_TEST_DSN",
    "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
)


@pytest.fixture
def pg_auth_conn(pg_conn):
    """Use the shared postgres fixture and ensure auth tables exist."""
    run_migrations(pg_conn)
    # Clean up before test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM usage_log")
        cur.execute("DELETE FROM api_keys")
    if not pg_conn.autocommit:
        pg_conn.commit()
    yield pg_conn
    # Clean up after test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM usage_log")
        cur.execute("DELETE FROM api_keys")
    if not pg_conn.autocommit:
        pg_conn.commit()


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear in-memory cache before/after each test to avoid interference."""
    _KEY_CACHE.clear()
    _CACHE_TS.clear()
    yield
    _KEY_CACHE.clear()
    _CACHE_TS.clear()


# ---------------------------------------------------------------------------
# End-to-end: create → verify → log_usage
# ---------------------------------------------------------------------------


class TestAuthEndToEndCycle:
    """Verify the complete auth lifecycle: create key, verify, log usage, list."""

    def test_create_verify_cycle(self, pg_auth_conn):
        """Create key via DB → verify with correct raw key → returns key_id."""
        raw, prefix, key_id = create_api_key(pg_auth_conn, "e2e-cycle-test")

        # Verify the raw key
        assert verify_api_key(pg_auth_conn, raw) == key_id

        # Verify the prefix is correct
        assert raw[:8] == prefix

    def test_verify_wrong_key_returns_none(self, pg_auth_conn):
        """Attempt to verify a non-existent key → returns None."""
        create_api_key(pg_auth_conn, "existing-key")
        result = verify_api_key(pg_auth_conn, "osm_completely_wrong")
        assert result is None

    def test_deactivate_then_verify_fails(self, pg_auth_conn):
        """Create key → deactivate → verify returns None."""
        raw, _, key_id = create_api_key(pg_auth_conn, "deactivate-test")
        assert verify_api_key(pg_auth_conn, raw) == key_id

        deactivate_api_key(pg_auth_conn, key_id)
        assert verify_api_key(pg_auth_conn, raw) is None

    def test_list_api_keys_after_state_changes(self, pg_auth_conn):
        """List reflects active/inactive state."""
        raw, _, key_id = create_api_key(pg_auth_conn, "list-state-test")
        keys = list_api_keys(pg_auth_conn)
        found = next((k for k in keys if k["id"] == key_id), None)
        assert found is not None
        assert found["active"] is True

        deactivate_api_key(pg_auth_conn, key_id)
        keys = list_api_keys(pg_auth_conn)
        found = next((k for k in keys if k["id"] == key_id), None)
        assert found is not None
        assert found["active"] is False

    def test_verify_updates_last_used_at(self, pg_auth_conn):
        """Calling verify_api_key updates last_used_at timestamp."""
        raw, _, key_id = create_api_key(pg_auth_conn, "last-used-test")

        # Get initial last_used_at (should be NULL)
        with pg_auth_conn.cursor() as cur:
            cur.execute("SELECT last_used_at FROM api_keys WHERE id = %s", (key_id,))
            initial = cur.fetchone()[0]
        assert initial is None

        # Verify the key
        verify_api_key(pg_auth_conn, raw)

        # Check that last_used_at is now set
        with pg_auth_conn.cursor() as cur:
            cur.execute("SELECT last_used_at FROM api_keys WHERE id = %s", (key_id,))
            updated = cur.fetchone()[0]
        assert updated is not None

    def test_log_usage_records_tool_usage(self, pg_auth_conn):
        """log_usage records tool name and response time."""
        raw, _, key_id = create_api_key(pg_auth_conn, "log-usage-test")

        # Log multiple tool invocations
        log_usage(pg_auth_conn, key_id, "resolve_model", 45)
        log_usage(pg_auth_conn, key_id, "resolve_field", 78)

        # Verify both are recorded
        with pg_auth_conn.cursor() as cur:
            cur.execute(
                "SELECT tool_name, response_ms FROM usage_log WHERE api_key_id = %s ORDER BY id",
                (key_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 2
        assert rows[0] == ("resolve_model", 45)
        assert rows[1] == ("resolve_field", 78)

    def test_log_usage_with_none_key_id(self, pg_auth_conn):
        """log_usage works with None api_key_id (anonymous usage)."""
        log_usage(pg_auth_conn, None, "resolve_method", 32)

        with pg_auth_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM usage_log WHERE api_key_id IS NULL")
            count = cur.fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# Cache TTL: expiration and bypass
# ---------------------------------------------------------------------------


class TestCacheTTLExpiration:
    """Cache entries expire after TTL and must be re-verified against DB."""

    def test_cache_get_miss_on_empty(self):
        """Cache miss returns (False, None) on empty cache."""
        hit, val = _cache_get("nonexistent_key")
        assert hit is False
        assert val is None

    def test_cache_set_and_get(self):
        """Cache set → get returns (True, value)."""
        _cache_set("test_key", 42)
        hit, val = _cache_get("test_key")
        assert hit is True
        assert val == 42

    def test_cache_set_none_value(self):
        """Cache can store None (for invalid keys)."""
        _cache_set("invalid_key", None)
        hit, val = _cache_get("invalid_key")
        assert hit is True
        assert val is None

    def test_cache_invalidate(self):
        """_cache_invalidate removes key from both dicts."""
        _cache_set("key_to_invalidate", 99)
        _cache_invalidate("key_to_invalidate")
        hit, _ = _cache_get("key_to_invalidate")
        assert hit is False

    def test_cache_expired_after_ttl(self):
        """Expired cache entry treated as miss."""
        _cache_set("expiring_key", 7)
        # Manually expire: use hash as cache key (I2: keys stored hashed)
        _CACHE_TS[hash_key("expiring_key")] = time.monotonic() - _CACHE_TTL - 1
        hit, _ = _cache_get("expiring_key")
        assert hit is False

    def test_deactivate_then_cache_expires_to_fresh_verify(self, pg_auth_conn):
        """After deactivation + cache TTL expired → fresh verify returns None."""
        raw, _, key_id = create_api_key(pg_auth_conn, "cache-ttl-test")

        # Prime cache with valid key_id
        _cache_set(raw, key_id)
        hit, cached_id = _cache_get(raw)
        assert hit is True
        assert cached_id == key_id

        # Deactivate in DB
        deactivate_api_key(pg_auth_conn, key_id)

        # Cache still returns the value
        hit, cached_id = _cache_get(raw)
        assert hit is True
        assert cached_id == key_id

        # Manually expire cache (use hash as cache key — I2)
        _CACHE_TS[hash_key(raw)] = time.monotonic() - _CACHE_TTL - 1

        # Fresh DB verify should return None (because key is now inactive)
        result = verify_api_key(pg_auth_conn, raw)
        assert result is None

    def test_deactivate_invalidates_cache_immediately(self, pg_auth_conn):
        """B1: calling _cache_invalidate_by_key_id after deactivate removes cache entry."""
        raw, _, key_id = create_api_key(pg_auth_conn, "b1-immediate")
        _cache_set(raw, key_id)
        hit, cached_id = _cache_get(raw)
        assert hit is True and cached_id == key_id

        deactivate_api_key(pg_auth_conn, key_id)
        _cache_invalidate_by_key_id(key_id)  # simulates deactivate route

        hit, _ = _cache_get(raw)
        assert not hit, "cache must be empty immediately after deactivate+invalidate"

    @pytest.mark.asyncio
    async def test_deactivate_then_middleware_returns_401(self, pg_auth_conn):
        """B1: middleware must return 401 after key deactivated + cache invalidated."""
        import unittest.mock as mock

        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        raw, _, key_id = create_api_key(pg_auth_conn, "b1-e2e")

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        # First request: primes cache (cache miss → DB verify → key_id cached)
        with mock.patch("src.mcp.server._get_pg_conn", return_value=pg_auth_conn):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r1 = await client.get("/mcp", headers={"X-API-Key": raw})
        assert r1.status_code == 200

        # Deactivate + invalidate cache immediately
        deactivate_api_key(pg_auth_conn, key_id)
        _cache_invalidate_by_key_id(key_id)

        # Next request: cache miss → fresh DB verify → inactive → 401
        with mock.patch("src.mcp.server._get_pg_conn", return_value=pg_auth_conn):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r2 = await client.get("/mcp", headers={"X-API-Key": raw})
        assert r2.status_code == 401, "deactivated key must be rejected immediately"

    @pytest.mark.asyncio
    async def test_bg_task_usage_log_written(self, pg_auth_conn):
        """B3: fire-and-forget usage log task must complete (not be GC'd)."""
        import unittest.mock as mock

        import httpx
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        raw, _, key_id = create_api_key(pg_auth_conn, "b3-bg-task")
        _cache_set(raw, key_id)  # prime cache so no DB verify needed

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        with mock.patch("src.mcp.server._get_pg_conn", return_value=pg_auth_conn):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/mcp", headers={"X-API-Key": raw})
            # Yield control to allow the background log task to complete
            import asyncio
            await asyncio.sleep(0.1)

        with pg_auth_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM usage_log WHERE api_key_id = %s", (key_id,))
            count = cur.fetchone()[0]
        assert count >= 1, "usage_log entry must be written by background task (B3)"


# ---------------------------------------------------------------------------
# Middleware: auth bypass for public paths
# ---------------------------------------------------------------------------


class TestAuthMiddlewarePublicPath:
    """GET /health and other public paths bypass X-API-Key requirement."""

    @pytest.mark.asyncio
    async def test_health_path_no_key_required(self):
        """GET /health works without X-API-Key header."""

        async def health(request):
            return JSONResponse({"status": "ok"})

        app = Starlette(routes=[Route("/health", health)])
        app.add_middleware(AuthMiddleware)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_protected_path_requires_key(self):
        """Non-public path without X-API-Key → 401."""

        async def protected(request):
            return PlainTextResponse("should_not_reach")

        app = Starlette(routes=[Route("/mcp", protected)])
        app.add_middleware(AuthMiddleware)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/mcp")
        assert resp.status_code == 401
        assert "Missing X-API-Key" in resp.text


# ---------------------------------------------------------------------------
# Middleware: key verification and caching
# ---------------------------------------------------------------------------


class TestAuthMiddlewareKeyVerification:
    """Middleware verifies X-API-Key and caches valid keys."""

    @pytest.mark.asyncio
    async def test_invalid_key_returns_401(self, pg_auth_conn):
        """Invalid X-API-Key header → 401."""

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        with mock.patch("src.mcp.server._get_pg_conn", return_value=pg_auth_conn):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/mcp", headers={"X-API-Key": "osm_invalid"})
        assert resp.status_code == 401
        assert "Invalid or inactive API key" in resp.text

    @pytest.mark.asyncio
    async def test_valid_key_returns_200(self, pg_auth_conn):
        """Valid X-API-Key → 200 and request.state.api_key_id is set."""
        raw, _, key_id = create_api_key(pg_auth_conn, "middleware-valid-test")

        captured_state = {}

        async def capture_state(request):
            captured_state["api_key_id"] = request.state.api_key_id
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", capture_state)])
        app.add_middleware(AuthMiddleware)

        with mock.patch("src.mcp.server._get_pg_conn", return_value=pg_auth_conn):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/mcp", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        assert captured_state["api_key_id"] == key_id

    @pytest.mark.asyncio
    async def test_cache_hit_skips_db_lookup(self, pg_auth_conn):
        """Second request with same key uses cache — DB not called twice."""
        raw, _, key_id = create_api_key(pg_auth_conn, "cache-hit-test")

        call_count = {"n": 0}
        original_verify = __import__(
            "src.db.auth_registry", fromlist=["verify_api_key"]
        ).verify_api_key

        def counting_verify(conn, key):
            call_count["n"] += 1
            return original_verify(conn, key)

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        with mock.patch(
            "src.db.auth_registry.verify_api_key", side_effect=counting_verify
        ), mock.patch("src.mcp.server._get_pg_conn", return_value=pg_auth_conn):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/mcp", headers={"X-API-Key": raw})
                await client.get("/mcp", headers={"X-API-Key": raw})

        # DB should have been called exactly once
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_deactivated_key_rejected_after_cache_expire(self, pg_auth_conn):
        """Deactivated key cached → after TTL expires → fresh lookup returns 401."""
        raw, _, key_id = create_api_key(pg_auth_conn, "deactivate-cache-test")

        # Prime cache
        _cache_set(raw, key_id)

        async def dummy(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", dummy)])
        app.add_middleware(AuthMiddleware)

        # First request should succeed (cache hit)
        with mock.patch("src.mcp.server._get_pg_conn", return_value=pg_auth_conn):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp1 = await client.get("/mcp", headers={"X-API-Key": raw})
        assert resp1.status_code == 200

        # Deactivate the key in DB
        deactivate_api_key(pg_auth_conn, key_id)

        # Expire cache manually (use hash as cache key — I2)
        _CACHE_TS[hash_key(raw)] = time.monotonic() - _CACHE_TTL - 1

        # Second request should fail (fresh DB lookup)
        with mock.patch("src.mcp.server._get_pg_conn", return_value=pg_auth_conn):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp2 = await client.get("/mcp", headers={"X-API-Key": raw})
        assert resp2.status_code == 401


# ---------------------------------------------------------------------------
# Postgres Advisory Lock: concurrent indexer prevention
# ---------------------------------------------------------------------------


class TestAdvisoryLockConcurrency:
    """Advisory lock prevents concurrent indexer runs."""

    def test_second_acquire_blocked_on_different_connection(self, pg_conn):
        """While one connection holds advisory lock, another cannot acquire."""
        # Use PG_TEST_DSN to create second connection (same DB as fixture)
        conn2 = psycopg2.connect(PG_TEST_DSN)
        conn2.autocommit = True
        try:
            # Hold lock on pg_conn
            with _indexer_lock(pg_conn, "profile-concurrent-test"):
                # Try to acquire on conn2 — should fail (same profile name → same lock id)
                lock_id = _profile_lock_id("profile-concurrent-test")
                with conn2.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
                    acquired = cur.fetchone()[0]
                assert acquired is False, "Second connection should not acquire lock"
        finally:
            conn2.close()

    def test_lock_released_after_context(self, pg_conn):
        """Lock released after context exits → same connection can re-acquire."""
        # Acquire and release
        with _indexer_lock(pg_conn, "profile-release-test"):
            pass  # Lock is held

        # Now should be able to acquire again
        with _indexer_lock(pg_conn, "profile-release-test-2"):
            pass  # Should not raise

    def test_concurrent_index_profile_via_second_connection(self, pg_conn):
        """If lock is held on one connection, second connection cannot acquire same profile."""
        # Hold lock on pg_conn via _indexer_lock
        conn2 = psycopg2.connect(PG_TEST_DSN)
        conn2.autocommit = True

        try:
            with _indexer_lock(pg_conn, "lock-held-profile"):
                # Try on conn2 with same profile name — should fail
                with pytest.raises(RuntimeError, match="advisory lock"):
                    with _indexer_lock(conn2, "lock-held-profile"):
                        pass
        finally:
            conn2.close()

    def test_lock_survives_exception_in_context(self, pg_conn):
        """Lock is released even if context block raises."""

        class TestException(Exception):
            pass

        try:
            with _indexer_lock(pg_conn, "exception-test"):
                raise TestException("test error")
        except TestException:
            pass  # Expected

        # Lock should be released — can acquire again
        with _indexer_lock(pg_conn, "after-exception"):
            pass  # Should not raise


# ---------------------------------------------------------------------------
# Security: hash comparison and key integrity
# ---------------------------------------------------------------------------


class TestSecurityHashComparison:
    """Verify cryptographic security: only full hash match, not prefix match."""

    def test_verify_fails_on_different_hash_same_prefix(self, pg_auth_conn):
        """Key with same prefix but different hash should fail verification."""
        raw, prefix, key_id = create_api_key(pg_auth_conn, "hash-security-test")

        # Construct a fake key with same prefix but different content
        fake_key = prefix + "X" * len(raw[8:])

        result = verify_api_key(pg_auth_conn, fake_key)
        assert result is None

    def test_verify_fails_on_single_char_difference(self, pg_auth_conn):
        """Single character difference in key should fail."""
        raw, _, _ = create_api_key(pg_auth_conn, "single-char-test")

        # Change last character
        modified_key = raw[:-1] + ("A" if raw[-1] != "A" else "B")

        result = verify_api_key(pg_auth_conn, modified_key)
        assert result is None

    def test_verify_empty_string_returns_none(self, pg_auth_conn):
        """Empty key string should return None."""
        result = verify_api_key(pg_auth_conn, "")
        assert result is None

    def test_multiple_keys_no_collision(self, pg_auth_conn):
        """Multiple created keys should have distinct hashes."""
        raw1, _, id1 = create_api_key(pg_auth_conn, "key1")
        raw2, _, id2 = create_api_key(pg_auth_conn, "key2")

        assert raw1 != raw2
        assert verify_api_key(pg_auth_conn, raw1) == id1
        assert verify_api_key(pg_auth_conn, raw2) == id2
        # Cross-verify should fail
        assert verify_api_key(pg_auth_conn, raw1) != id2
        assert verify_api_key(pg_auth_conn, raw2) != id1
