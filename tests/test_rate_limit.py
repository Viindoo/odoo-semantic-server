# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_rate_limit.py
"""Unit tests for src/web_ui/rate_limit.py — per-IP sliding-window rate limiter.

No Postgres or Docker required.

Business intent (6 cases):
  T1  Requests within limit are allowed.
  T2  Exceeding the limit returns False.
  T3  Window resets after elapsed time (old timestamps pruned).
  T4  Multiple IPs are isolated (one IP exhausted does not affect another).
  T5  X-Forwarded-For header is preferred over raw client host.
  T6  Falls back to client.host when no forwarding headers present.
"""

import asyncio

# ---------------------------------------------------------------------------
# Helpers — reset module-level state between tests
# ---------------------------------------------------------------------------

def _reset_buckets() -> None:
    """Clear rate_limit module state so tests are independent."""
    from src.web_ui import rate_limit as rl
    rl._per_ip_buckets.clear()
    rl._last_prune = 0.0
    rl._lock = None  # force re-create so each test gets a fresh Lock


# ---------------------------------------------------------------------------
# T1: Requests within limit are allowed
# ---------------------------------------------------------------------------

class TestWithinLimit:
    """T1: check_ip_rate_limit returns True while under the limit."""

    def test_five_requests_all_allowed(self):
        _reset_buckets()
        from src.web_ui.rate_limit import check_ip_rate_limit

        results = [
            asyncio.get_event_loop().run_until_complete(
                check_ip_rate_limit("1.2.3.4", limit=5, window_seconds=60)
            )
            for _ in range(5)
        ]
        assert all(results), "All 5 requests within limit must be allowed"

    def test_single_request_allowed(self):
        _reset_buckets()
        from src.web_ui.rate_limit import check_ip_rate_limit

        result = asyncio.get_event_loop().run_until_complete(
            check_ip_rate_limit("5.5.5.5", limit=3, window_seconds=60)
        )
        assert result is True


# ---------------------------------------------------------------------------
# T2: Exceeding the limit returns False
# ---------------------------------------------------------------------------

class TestExceedLimit:
    """T2: check_ip_rate_limit returns False when limit is exhausted."""

    def test_sixth_request_denied(self):
        _reset_buckets()
        from src.web_ui.rate_limit import check_ip_rate_limit

        loop = asyncio.get_event_loop()
        for _ in range(5):
            loop.run_until_complete(
                check_ip_rate_limit("10.0.0.1", limit=5, window_seconds=60)
            )
        # 6th request must be denied.
        result = loop.run_until_complete(
            check_ip_rate_limit("10.0.0.1", limit=5, window_seconds=60)
        )
        assert result is False, "6th request must be denied when limit=5"

    def test_bucket_not_updated_on_deny(self):
        """After denial, bucket size stays at limit (not incremented further)."""
        _reset_buckets()
        from src.web_ui import rate_limit as rl
        from src.web_ui.rate_limit import check_ip_rate_limit

        loop = asyncio.get_event_loop()
        ip = "10.0.0.2"
        for _ in range(3):
            loop.run_until_complete(check_ip_rate_limit(ip, limit=3, window_seconds=60))
        size_before = len(rl._per_ip_buckets.get(ip, []))
        # Deny attempt.
        loop.run_until_complete(check_ip_rate_limit(ip, limit=3, window_seconds=60))
        size_after = len(rl._per_ip_buckets.get(ip, []))
        assert size_before == size_after == 3, (
            "Bucket must not grow past limit on denied request"
        )


# ---------------------------------------------------------------------------
# T3: Window resets after elapsed time
# ---------------------------------------------------------------------------

class TestWindowReset:
    """T3: Old timestamps are pruned when outside the window."""

    def test_old_timestamps_pruned(self, monkeypatch):
        """Monkeypatching time.monotonic so the window effectively expired."""
        _reset_buckets()
        from src.web_ui import rate_limit as rl

        # Simulate 5 requests at t=0
        base_time = 1000.0
        call_count = 0

        def fake_monotonic():
            nonlocal call_count
            # First 5 calls (during the 5 fill-up) return base_time.
            # Subsequent calls return base_time + 61 (window expired).
            if call_count < 5:
                call_count += 1
                return base_time
            call_count += 1
            return base_time + 61.0  # past the 60s window

        monkeypatch.setattr(rl.time, "monotonic", fake_monotonic)

        loop = asyncio.get_event_loop()
        ip = "192.168.1.1"
        for _ in range(5):
            loop.run_until_complete(rl.check_ip_rate_limit(ip, limit=5, window_seconds=60))

        # 6th call — now 61s later — should pass because old timestamps are pruned.
        result = loop.run_until_complete(
            rl.check_ip_rate_limit(ip, limit=5, window_seconds=60)
        )
        assert result is True, "After window expiry, new request must be allowed"

    def test_within_window_no_reset(self, monkeypatch):
        """Requests still within the window must remain throttled."""
        _reset_buckets()
        from src.web_ui import rate_limit as rl

        base_time = 2000.0
        monkeypatch.setattr(rl.time, "monotonic", lambda: base_time)

        loop = asyncio.get_event_loop()
        ip = "192.168.1.2"
        for _ in range(2):
            loop.run_until_complete(rl.check_ip_rate_limit(ip, limit=2, window_seconds=60))

        # Still within window — must be denied.
        result = loop.run_until_complete(
            rl.check_ip_rate_limit(ip, limit=2, window_seconds=60)
        )
        assert result is False, "Must be denied when still within window"


# ---------------------------------------------------------------------------
# T4: Multiple IPs are isolated
# ---------------------------------------------------------------------------

class TestIpIsolation:
    """T4: Different IPs have independent buckets."""

    def test_two_ips_independent(self):
        _reset_buckets()
        from src.web_ui.rate_limit import check_ip_rate_limit

        loop = asyncio.get_event_loop()
        ip_a = "100.0.0.1"
        ip_b = "100.0.0.2"

        # Exhaust ip_a (limit=3).
        for _ in range(3):
            loop.run_until_complete(check_ip_rate_limit(ip_a, limit=3, window_seconds=60))
        # ip_a is now exhausted.
        assert loop.run_until_complete(
            check_ip_rate_limit(ip_a, limit=3, window_seconds=60)
        ) is False, "ip_a must be exhausted"

        # ip_b still has its own fresh bucket.
        assert loop.run_until_complete(
            check_ip_rate_limit(ip_b, limit=3, window_seconds=60)
        ) is True, "ip_b must be independent of ip_a"


# ---------------------------------------------------------------------------
# T5 + T6: get_client_ip header resolution
# ---------------------------------------------------------------------------

class TestGetClientIp:
    """T5 + T6: get_client_ip resolves the right IP from request headers."""

    def _make_request(self, xff=None, real_ip=None, client_host=None):
        """Build a minimal mock request object."""
        headers = {}
        if xff:
            headers["x-forwarded-for"] = xff
        if real_ip:
            headers["x-real-ip"] = real_ip

        class FakeClient:
            def __init__(self, host):
                self.host = host

        class FakeRequest:
            def __init__(self):
                self.headers = headers
                self.client = FakeClient(client_host) if client_host else None

        return FakeRequest()

    def test_xff_preferred(self):
        from src.web_ui.rate_limit import get_client_ip

        req = self._make_request(
            xff="203.0.113.1, 10.0.0.1",
            real_ip="10.0.0.1",
            client_host="127.0.0.1",
        )
        result = asyncio.get_event_loop().run_until_complete(get_client_ip(req))
        assert result == "203.0.113.1", f"Must pick first XFF entry, got {result!r}"

    def test_real_ip_fallback(self):
        from src.web_ui.rate_limit import get_client_ip

        req = self._make_request(real_ip="10.10.10.10", client_host="127.0.0.1")
        result = asyncio.get_event_loop().run_until_complete(get_client_ip(req))
        assert result == "10.10.10.10", (
            f"Must use X-Real-IP when XFF absent, got {result!r}"
        )

    def test_client_host_fallback(self):
        from src.web_ui.rate_limit import get_client_ip

        req = self._make_request(client_host="172.16.0.5")
        result = asyncio.get_event_loop().run_until_complete(get_client_ip(req))
        assert result == "172.16.0.5", (
            f"Must use client.host as last resort, got {result!r}"
        )

    def test_unknown_when_no_ip(self):
        from src.web_ui.rate_limit import get_client_ip

        class FakeRequest:
            headers: dict = {}
            client = None

        result = asyncio.get_event_loop().run_until_complete(get_client_ip(FakeRequest()))
        assert result == "unknown"
