# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_rate_limiting.py
"""Unit tests for per-API-key rate limiter in src.mcp.middleware."""
import time

from src.mcp.middleware import PlanInfo, _check_rate_limit, _rate_buckets


def _plan(rpm: int) -> PlanInfo:
    """Build a minimal PlanInfo for rate-limit unit tests."""
    return PlanInfo(plan_id=0, slug="test", quota_calls_per_month=0, rate_limit_rpm=rpm)


def test_rate_limit_allows_up_to_limit():
    _rate_buckets.clear()
    for i in range(5):
        allowed, remaining = _check_rate_limit(api_key_id=999, plan_info=_plan(5))
        assert allowed, f"Request {i + 1} should be allowed (remaining={remaining})"
    # 6th request should be blocked
    allowed, remaining = _check_rate_limit(api_key_id=999, plan_info=_plan(5))
    assert not allowed, "6th request should be blocked"
    assert remaining == 0


def test_rate_limit_remaining_decrements():
    """With limit=N, `remaining` must decrease by exactly 1 each request.

    Business contract: the caller relies on `remaining` to surface accurate
    rate-limit headroom (X-RateLimit-Remaining). After the i-th allowed request
    (1-indexed) the reported remaining is N-i. This asserts strict monotonic
    decrement, not just "< limit".
    """
    _rate_buckets.clear()
    limit = 10
    for i in range(1, limit + 1):
        allowed, remaining = _check_rate_limit(api_key_id=100, plan_info=_plan(limit))
        assert allowed, f"request {i} within limit must be allowed"
        assert remaining == limit - i, (
            f"after request {i} remaining must be {limit - i}, got {remaining}"
        )
    # The (limit+1)-th request is over the window and must be blocked.
    allowed, remaining = _check_rate_limit(api_key_id=100, plan_info=_plan(limit))
    assert not allowed, "request beyond the limit must be blocked"
    assert remaining == 0


def test_rate_limit_different_keys_isolated():
    _rate_buckets.clear()
    # Fill up key 1
    for _ in range(5):
        _check_rate_limit(api_key_id=1, plan_info=_plan(5))
    # Key 1 should now be blocked
    allowed1, _ = _check_rate_limit(api_key_id=1, plan_info=_plan(5))
    assert not allowed1, "Key 1 should be rate-limited"
    # Key 2 should be independent and allowed
    allowed2, _ = _check_rate_limit(api_key_id=2, plan_info=_plan(5))
    assert allowed2, "Key 2 should not be rate-limited"


def test_rate_limit_window_expiry():
    """Entries older than 60s should be pruned.

    Pre-fills bucket with timestamps 61s in the past to trigger pruning logic.
    """
    _rate_buckets.clear()
    old_ts = time.monotonic() - 61
    from collections import deque
    _rate_buckets[77] = deque([old_ts] * 5)

    # Next request should be allowed (old entries pruned)
    allowed, _ = _check_rate_limit(api_key_id=77, plan_info=_plan(5))
    assert allowed, "Old entries should be pruned, request should be allowed"


def test_rate_limit_returns_correct_remaining():
    """First request under limit=10 must report exactly remaining==9.

    On an empty bucket the limiter reserves the slot for the current request,
    so the headroom advertised to the caller is limit-1.
    """
    _rate_buckets.clear()
    allowed, remaining = _check_rate_limit(api_key_id=200, plan_info=_plan(10))
    assert allowed
    assert remaining == 9
