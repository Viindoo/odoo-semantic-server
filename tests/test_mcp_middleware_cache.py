"""Concurrency and thread-safety tests for MCP middleware cache."""
import concurrent.futures
import time

import pytest

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]


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
