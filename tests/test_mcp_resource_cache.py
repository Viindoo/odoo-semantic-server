# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``src.mcp.resources.ResourceCache`` (WI-F1).

Covers AC-F1-2 + AC-F1-3 — the thread-safe LRU+TTL cache underlying the 7
``odoo://`` resource handlers.  All tests are pure-Python (no DB) and use
the injected ``now_fn`` clock for deterministic TTL assertions.

Scenarios:
  (1)  put + get returns the value + mime_type
  (2)  get miss — absent key → None
  (3)  TTL expiry via injected clock → second read returns None
  (4)  LRU eviction at capacity+1 drops the least-recently-used key
  (5)  get bumps an entry to MRU (cache-hit promotion)
  (6)  put overwrite refreshes timestamp + bumps to MRU
  (7)  get_or_compute miss → calls compute_fn once + caches result
  (8)  get_or_compute hit → compute_fn never invoked
  (9)  clear() drops everything
  (10) __len__ and __contains__ are accurate after each op
  (11) Capacity validation: ctor raises on capacity < 1 and ttl <= 0
  (12) Thread safety: 50 concurrent reads + writes do not corrupt state
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from src.mcp.resources import (
    DEFAULT_CACHE_CAPACITY,
    DEFAULT_CACHE_TTL_SEC,
    MIME_CSS,
    MIME_MARKDOWN,
    ResourceCache,
)

# ---------------------------------------------------------------------------
# Helpers — injectable clock
# ---------------------------------------------------------------------------


class _FakeClock:
    """Manually-advanced monotonic clock for TTL determinism."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, secs: float) -> None:
        self._t += secs


@pytest.fixture()
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture()
def cache(clock: _FakeClock) -> Iterator[ResourceCache]:
    """Fresh 3-slot 10s-TTL cache so eviction + expiry are easy to drive."""
    c = ResourceCache(capacity=3, ttl=10.0, now_fn=clock)
    yield c
    c.clear()


# ===========================================================================
# (1) put + get returns the value + mime_type
# ===========================================================================


def test_put_then_get_returns_value_and_mime(cache: ResourceCache) -> None:
    cache.put("k1", "hello world", MIME_MARKDOWN)
    got = cache.get("k1")
    assert got is not None
    assert got == ("hello world", MIME_MARKDOWN)


def test_put_uses_markdown_mime_by_default(cache: ResourceCache) -> None:
    cache.put("k1", "body")
    got = cache.get("k1")
    assert got is not None
    assert got[1] == MIME_MARKDOWN


# ===========================================================================
# (2) get miss — absent key → None
# ===========================================================================


def test_get_returns_none_when_key_absent(cache: ResourceCache) -> None:
    assert cache.get("never-inserted") is None


# ===========================================================================
# (3) TTL expiry via injected clock
# ===========================================================================


def test_ttl_expiry_drops_entry(
    cache: ResourceCache, clock: _FakeClock,
) -> None:
    cache.put("k1", "body")
    assert cache.get("k1") is not None
    clock.advance(10.5)  # past 10s TTL
    assert cache.get("k1") is None, "Entry must be evicted after TTL"
    # And the eviction must have actually shrunk the cache.
    assert len(cache) == 0


def test_ttl_does_not_expire_within_window(
    cache: ResourceCache, clock: _FakeClock,
) -> None:
    cache.put("k1", "body")
    clock.advance(9.99)
    assert cache.get("k1") == ("body", MIME_MARKDOWN)


# ===========================================================================
# (4) LRU eviction at capacity+1
# ===========================================================================


def test_lru_eviction_drops_oldest(cache: ResourceCache) -> None:
    cache.put("k1", "v1")
    cache.put("k2", "v2")
    cache.put("k3", "v3")
    # Adding a 4th must evict k1 (least-recently used).
    cache.put("k4", "v4")
    assert len(cache) == 3
    assert cache.get("k1") is None
    assert cache.get("k2") is not None
    assert cache.get("k3") is not None
    assert cache.get("k4") is not None


def test_capacity_1000_default_is_documented() -> None:
    assert DEFAULT_CACHE_CAPACITY == 1000
    assert DEFAULT_CACHE_TTL_SEC == 300.0


# ===========================================================================
# (5) get bumps an entry to MRU
# ===========================================================================


def test_get_promotes_to_mru(cache: ResourceCache) -> None:
    cache.put("k1", "v1")
    cache.put("k2", "v2")
    cache.put("k3", "v3")
    # Touch k1 — it should now be MRU.
    assert cache.get("k1") is not None
    # Add a 4th — k2 (now LRU) must be evicted, NOT k1.
    cache.put("k4", "v4")
    assert cache.get("k2") is None, "k2 should have been evicted as LRU"
    assert cache.get("k1") is not None, "k1 was promoted by .get(); must survive"


# ===========================================================================
# (6) put overwrite refreshes timestamp + bumps to MRU
# ===========================================================================


def test_put_overwrite_refreshes_timestamp(
    cache: ResourceCache, clock: _FakeClock,
) -> None:
    cache.put("k1", "v1")
    clock.advance(8.0)  # still within TTL
    cache.put("k1", "v1-new")
    clock.advance(8.0)  # 16s since original put; 8s since overwrite
    # Entry should still be live because overwrite reset fetched_at.
    got = cache.get("k1")
    assert got is not None
    assert got[0] == "v1-new"


def test_put_overwrite_bumps_to_mru(cache: ResourceCache) -> None:
    cache.put("k1", "v1")
    cache.put("k2", "v2")
    cache.put("k3", "v3")
    # Overwrite k1 — should promote to MRU.
    cache.put("k1", "v1-new")
    # Add k4 → k2 (now LRU) evicted.
    cache.put("k4", "v4")
    assert cache.get("k2") is None
    assert cache.get("k1") == ("v1-new", MIME_MARKDOWN)


# ===========================================================================
# (7+8) get_or_compute
# ===========================================================================


def test_get_or_compute_miss_invokes_compute_fn_once(
    cache: ResourceCache,
) -> None:
    call_count = 0

    def _compute() -> tuple[str, str]:
        nonlocal call_count
        call_count += 1
        return "computed-body", MIME_CSS

    val1, mime1 = cache.get_or_compute("k1", _compute)
    val2, mime2 = cache.get_or_compute("k1", _compute)

    assert val1 == val2 == "computed-body"
    assert mime1 == mime2 == MIME_CSS
    assert call_count == 1, "compute_fn must run exactly once across 2 reads"


def test_get_or_compute_hit_skips_compute_fn(cache: ResourceCache) -> None:
    cache.put("k1", "pre-cached", MIME_MARKDOWN)

    def _compute() -> tuple[str, str]:
        raise AssertionError("compute_fn must NOT be called on cache hit")

    val, mime = cache.get_or_compute("k1", _compute)
    assert val == "pre-cached"
    assert mime == MIME_MARKDOWN


def test_get_or_compute_recomputes_after_ttl(
    cache: ResourceCache, clock: _FakeClock,
) -> None:
    calls: list[int] = []

    def _compute() -> tuple[str, str]:
        calls.append(1)
        return f"v{len(calls)}", MIME_MARKDOWN

    first, _ = cache.get_or_compute("k1", _compute)
    clock.advance(11.0)  # past TTL
    second, _ = cache.get_or_compute("k1", _compute)
    assert first == "v1"
    assert second == "v2"
    assert sum(calls) == 2


# ===========================================================================
# (9) clear() drops everything
# ===========================================================================


def test_clear_drops_all(cache: ResourceCache) -> None:
    cache.put("k1", "v1")
    cache.put("k2", "v2")
    assert len(cache) == 2
    cache.clear()
    assert len(cache) == 0
    assert cache.get("k1") is None
    assert cache.get("k2") is None


# ===========================================================================
# (10) __len__ and __contains__
# ===========================================================================


def test_len_and_contains_track_state(cache: ResourceCache) -> None:
    assert len(cache) == 0
    assert "k1" not in cache
    cache.put("k1", "v1")
    assert len(cache) == 1
    assert "k1" in cache
    cache.put("k2", "v2")
    assert len(cache) == 2
    cache.clear()
    assert len(cache) == 0
    assert "k1" not in cache


# ===========================================================================
# (11) Constructor validation
# ===========================================================================


def test_ctor_rejects_zero_capacity() -> None:
    with pytest.raises(ValueError, match="capacity"):
        ResourceCache(capacity=0)


def test_ctor_rejects_negative_ttl() -> None:
    with pytest.raises(ValueError, match="ttl"):
        ResourceCache(ttl=-1.0)


def test_ctor_rejects_zero_ttl() -> None:
    with pytest.raises(ValueError, match="ttl"):
        ResourceCache(ttl=0.0)


# ===========================================================================
# (12) Thread safety — 50 concurrent reads + writes
# ===========================================================================


def test_concurrent_reads_writes_do_not_corrupt() -> None:
    """50 threads hammer put/get/get_or_compute — no exceptions, no bad reads."""
    cache = ResourceCache(capacity=100, ttl=60.0)
    errors: list[Exception] = []
    barrier = threading.Barrier(50)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            for _ in range(40):
                key = f"k{i % 10}"
                cache.put(key, f"v{i}", MIME_MARKDOWN)
                got = cache.get(key)
                if got is not None:
                    # The value must be one of the values written for this key.
                    assert got[1] == MIME_MARKDOWN
                cache.get_or_compute(
                    f"compute-k{i}", lambda i=i: (f"c{i}", MIME_CSS),
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        if t.is_alive():
            raise AssertionError(f"Thread {t.name} hung")

    assert errors == [], f"Concurrent ops raised: {errors!r}"
    # Cache should be populated with 10 keyN entries + up to 50 compute-kN.
    assert len(cache) <= 100


def test_eviction_under_concurrent_writes_stays_within_capacity() -> None:
    """Writing 200 keys to a 50-slot cache from many threads keeps len ≤ 50."""
    cache = ResourceCache(capacity=50, ttl=60.0)
    barrier = threading.Barrier(20)

    def worker(start: int) -> None:
        barrier.wait()
        for i in range(start, start + 50):
            cache.put(f"key{i}", f"v{i}")

    threads = [threading.Thread(target=worker, args=(i * 50,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert len(cache) == 50, f"Cache must stay at capacity 50, got {len(cache)}"
