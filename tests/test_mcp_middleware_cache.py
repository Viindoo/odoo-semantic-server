# SPDX-License-Identifier: AGPL-3.0-or-later
"""Concurrency and thread-safety tests for MCP middleware cache."""
import concurrent.futures
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAuthMiddlewareCache:
    """Test thread-safety of key cache in middleware."""

    def test_cache_get_set_thread_safe(self):
        """Multiple threads writing to cache simultaneously must not raise."""
        from src.mcp import middleware as middleware_mod

        # Reset cache state
        middleware_mod._KEY_CACHE.clear()
        middleware_mod._CACHE_TS.clear()

        # Mock hash_key to return predictable hashes
        call_count = [0]
        original_hash_key = middleware_mod._hash_key

        def mock_hash_key(raw_key: str) -> str:
            call_count[0] += 1
            return original_hash_key(raw_key)

        middleware_mod._hash_key = mock_hash_key

        try:
            results = {"exceptions": [], "success_count": 0}

            def set_and_get(key_id: int, raw_key: str):
                """Set and immediately get from cache."""
                try:
                    middleware_mod._cache_set(raw_key, key_id)
                    hit, retrieved_id = middleware_mod._cache_get(raw_key)
                    if hit and retrieved_id == key_id:
                        results["success_count"] += 1
                except Exception as e:
                    results["exceptions"].append(str(e))

            # Run 50 threads, each setting/getting 10 keys
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = []
                for i in range(50):
                    future = executor.submit(set_and_get, i, f"key_{i}")
                    futures.append(future)
                concurrent.futures.wait(futures)

            assert len(results["exceptions"]) == 0, f"Exceptions: {results['exceptions']}"
            assert results["success_count"] == 50
            assert call_count[0] == 100  # 2 hash calls per thread (1 in set + 1 in get)
        finally:
            middleware_mod._hash_key = original_hash_key

    def test_cache_invalidate_thread_safe(self):
        """Multiple threads invalidating cache simultaneously must not raise."""
        from src.mcp import middleware as middleware_mod

        # Reset cache state
        middleware_mod._KEY_CACHE.clear()
        middleware_mod._CACHE_TS.clear()

        # Pre-populate cache with 100 keys
        for i in range(100):
            middleware_mod._cache_set(f"key_{i}", i)

        results = {"exceptions": [], "success_count": 0}

        def invalidate_and_verify(raw_key: str):
            """Invalidate a key and verify it's gone."""
            try:
                middleware_mod._cache_invalidate(raw_key)
                hit, _ = middleware_mod._cache_get(raw_key)
                if not hit:
                    results["success_count"] += 1
            except Exception as e:
                results["exceptions"].append(str(e))

        # Run 50 threads, each invalidating 2 keys
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            for i in range(50):
                future = executor.submit(invalidate_and_verify, f"key_{i % 100}")
                futures.append(future)
            concurrent.futures.wait(futures)

        assert len(results["exceptions"]) == 0, f"Exceptions: {results['exceptions']}"
        assert results["success_count"] == 50

    def test_cache_invalidate_by_key_id_thread_safe(self):
        """Multiple threads invalidating by key_id simultaneously must not raise."""
        from src.mcp import middleware as middleware_mod

        # Reset cache state
        middleware_mod._KEY_CACHE.clear()
        middleware_mod._CACHE_TS.clear()

        # Pre-populate cache: 100 keys mapping to 10 key_ids
        for i in range(100):
            key_id = i % 10  # 10 unique key_ids
            middleware_mod._cache_set(f"key_{i}", key_id)

        results = {"exceptions": [], "success_count": 0}

        def invalidate_by_id(key_id: int):
            """Invalidate all keys with given key_id."""
            try:
                middleware_mod._cache_invalidate_by_key_id(key_id)
                # Verify no keys with this id remain
                found_any = any(
                    v == key_id for v in middleware_mod._KEY_CACHE.values()
                )
                if not found_any:
                    results["success_count"] += 1
            except Exception as e:
                results["exceptions"].append(str(e))

        # Run 10 threads, each invalidating one key_id (0-9)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = []
            for key_id in range(10):
                future = executor.submit(invalidate_by_id, key_id)
                futures.append(future)
            concurrent.futures.wait(futures)

        assert len(results["exceptions"]) == 0, f"Exceptions: {results['exceptions']}"
        assert results["success_count"] == 10

    def test_cache_expiry_under_concurrent_access(self):
        """Cache TTL enforcement must be thread-safe under concurrent access."""
        from src.mcp import middleware as middleware_mod

        # Reset cache state
        middleware_mod._KEY_CACHE.clear()
        middleware_mod._CACHE_TS.clear()

        # Set a key with immediate expiry (TTL = 0)
        original_ttl = middleware_mod._CACHE_TTL
        middleware_mod._CACHE_TTL = 0.01  # 10ms

        try:
            middleware_mod._cache_set("expiring_key", 123)

            results = {"exceptions": [], "expired_count": 0}

            def check_expiry():
                """Check if key expired."""
                try:
                    time.sleep(0.02)  # Wait for expiry
                    hit, _ = middleware_mod._cache_get("expiring_key")
                    if not hit:
                        results["expired_count"] += 1
                except Exception as e:
                    results["exceptions"].append(str(e))

            # Run 20 threads checking expiry concurrently
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(check_expiry) for _ in range(20)]
                concurrent.futures.wait(futures)

            assert len(results["exceptions"]) == 0, f"Exceptions: {results['exceptions']}"
            assert results["expired_count"] == 20

        finally:
            middleware_mod._CACHE_TTL = original_ttl

    def test_cache_state_consistency_after_concurrent_ops(self):
        """After concurrent ops, cache state must be consistent."""
        from src.mcp import middleware as middleware_mod

        # Reset cache state
        middleware_mod._KEY_CACHE.clear()
        middleware_mod._CACHE_TS.clear()

        results = {"exceptions": []}

        def mixed_ops(thread_id: int):
            """Mix of set, get, invalidate operations."""
            try:
                for i in range(10):
                    raw_key = f"thread_{thread_id}_key_{i}"
                    key_id = thread_id * 10 + i

                    # Set
                    middleware_mod._cache_set(raw_key, key_id)

                    # Get
                    hit, retrieved = middleware_mod._cache_get(raw_key)
                    assert hit and retrieved == key_id

                    # Invalidate
                    middleware_mod._cache_invalidate(raw_key)

                    # Verify gone
                    hit, _ = middleware_mod._cache_get(raw_key)
                    assert not hit
            except Exception as e:
                results["exceptions"].append(f"Thread {thread_id}: {str(e)}")

        # Run 20 threads with mixed operations
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(mixed_ops, i) for i in range(20)]
            concurrent.futures.wait(futures)

        assert len(results["exceptions"]) == 0, f"Exceptions: {results['exceptions']}"
        # After all ops, cache should be empty
        assert len(middleware_mod._KEY_CACHE) == 0
        assert len(middleware_mod._CACHE_TS) == 0


class TestCacheSplitFailClosed:
    """FIX-1: _KEY_CACHE hit + _TENANT_CACHE miss must trigger DB verify (fail-closed).

    Security contract: if key_id is cached but tenant_id is NOT cached (e.g.
    post-deploy window where old code wrote _KEY_CACHE before _TENANT_CACHE was
    introduced), the middleware MUST go to DB to resolve the correct tenant_id.
    It must NOT silently assign tenant_id=None (unscoped/admin), which would be
    a cross-tenant escalation vulnerability.
    """

    @pytest.mark.asyncio
    async def test_key_cache_hit_tenant_cache_miss_triggers_verify(self):
        """KEY_CACHE hit but TENANT_CACHE miss → _do_verify is called → correct tenant_id set."""
        from src.mcp import middleware as middleware_mod

        raw_key = "test-split-key-abc123"
        expected_key_id = 42
        expected_tenant_id = 7

        # Pre-populate _KEY_CACHE only (simulate old code path that did not
        # call _cache_set_tenant)
        middleware_mod._cache_set(raw_key, expected_key_id)
        # Do NOT call _cache_set_tenant — leaving _TENANT_CACHE empty for this key

        try:
            # Verify _KEY_CACHE hit but _TENANT_CACHE miss
            hit, cached_key_id = middleware_mod._cache_get(raw_key)
            tenant_hit, _ = middleware_mod._cache_get_tenant(raw_key)
            assert hit, "Pre-condition: _KEY_CACHE must be populated"
            # Note: _CACHE_TS being set makes _cache_get_tenant return (True, None)
            # because the TTL entry exists. The real split scenario is when
            # verify_api_key_tenant returns tenant_id != None for what would
            # have been the stale None. Test the dispatch logic directly.

            # Build a minimal mock request
            mock_request = MagicMock()
            mock_request.url.path = "/mcp"
            mock_request.headers.get = MagicMock(return_value=raw_key)
            mock_request.state = MagicMock()

            verify_called = []

            # Patch _cache_get_tenant to return miss (False) even though key exists
            # This simulates the exact split scenario
            original_cache_get_tenant = middleware_mod._cache_get_tenant

            def _mock_cache_get_tenant(k):
                if k == raw_key:
                    return False, None  # force tenant cache miss
                return original_cache_get_tenant(k)

            # Patch _do_verify (via asyncio.to_thread) to capture call + return tenant_id.
            # _do_verify now yields a 4-tuple (key_id, tenant_id, user_id, owner_is_admin);
            # user_id=None marks this stub key as system/CLI so the read-side guard allows it.
            async def _mock_to_thread(fn, *a, **kw):
                verify_called.append(True)
                return (expected_key_id, expected_tenant_id, None, False)

            mock_call_next = AsyncMock(return_value=MagicMock(status_code=200))

            with patch.object(
                middleware_mod, "_cache_get_tenant", side_effect=_mock_cache_get_tenant
            ):
                with patch("asyncio.to_thread", side_effect=_mock_to_thread):
                    with patch.object(
                        middleware_mod, "_check_rate_limit", return_value=(True, 99)
                    ):
                        middleware = middleware_mod.AuthMiddleware(app=MagicMock())
                        await middleware.dispatch(mock_request, mock_call_next)

            # DB verify must have been called (not skipped due to key cache hit)
            assert verify_called, (
                "verify was NOT called despite tenant cache miss — "
                "tenant_id=None would have been silently used (FAIL-OPEN)"
            )

            # The correct tenant_id from DB must be stored on request.state
            assert mock_request.state.tenant_id == expected_tenant_id, (
                f"Expected tenant_id={expected_tenant_id} from DB, "
                f"got {mock_request.state.tenant_id} — fail-open bug still present"
            )

        finally:
            middleware_mod._cache_invalidate(raw_key)

    @pytest.mark.asyncio
    async def test_both_caches_hit_does_not_trigger_verify(self):
        """When KEY_CACHE + TENANT_CACHE + OWNER_CACHE all hit, no DB verify
        (happy path unchanged — the owner-meta cache must be warmed too)."""
        from src.mcp import middleware as middleware_mod

        raw_key = "test-both-hit-key-xyz987"
        expected_key_id = 55
        expected_tenant_id = 3

        middleware_mod._cache_set(raw_key, expected_key_id)
        middleware_mod._cache_set_tenant(raw_key, expected_tenant_id)
        # Read-side guard: owner-meta cache must also be warm for a cache-served
        # response. A real tenant_id here means the key is already scoped, so the
        # guard never fires regardless of owner metadata.
        middleware_mod._cache_set_owner(raw_key, 9, False)

        try:
            verify_called = []

            async def _mock_to_thread(fn, *a, **kw):
                verify_called.append(True)
                return (expected_key_id, expected_tenant_id, 9, False)

            mock_request = MagicMock()
            mock_request.url.path = "/mcp"
            mock_request.headers.get = MagicMock(return_value=raw_key)
            mock_request.state = MagicMock()

            mock_call_next = AsyncMock(return_value=MagicMock(status_code=200))

            with patch("asyncio.to_thread", side_effect=_mock_to_thread):
                with patch.object(middleware_mod, "_check_rate_limit", return_value=(True, 99)):
                    middleware = middleware_mod.AuthMiddleware(app=MagicMock())
                    await middleware.dispatch(mock_request, mock_call_next)

            # DB verify must NOT be called when both caches hit
            assert not verify_called, (
                "verify was called even though both caches hit — "
                "unnecessary DB round-trip on hot path"
            )

            # tenant_id from cache is used
            assert mock_request.state.tenant_id == expected_tenant_id

        finally:
            middleware_mod._cache_invalidate(raw_key)
