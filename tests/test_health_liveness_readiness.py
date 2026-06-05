# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for WI-D changes: cheap /health liveness + cached /ready readiness.

These tests verify the core correctness contract of the refactored health module:

1. /health never calls the heavy DB count functions on the hot path — it reads
   from the shared cache (or the cache stub returned by _get_ready_data mock).
2. _get_ready_data cache: calling it twice within the TTL window runs the DB
   fetch exactly once (not twice).
3. /ready endpoint returns the expected fields including cache metadata.
4. _check_neo4j / _check_pg return timeout error strings when they exceed the
   timeout budget (rather than hanging or propagating exceptions).

All tests are pure-unit (no DB containers required) — heavy IO is mocked out.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(path: str = "/health") -> MagicMock:
    req = MagicMock()
    req.url.path = path
    return req


# ---------------------------------------------------------------------------
# 1.  /health must NOT invoke the raw fetch functions (no scan on hot path)
# ---------------------------------------------------------------------------


class TestHealthDoesNotScanOnHotPath:
    """Verify /health reads counts from cache, never from a fresh DB scan."""

    @pytest.mark.asyncio
    async def test_health_does_not_call_fetch_embeddings_total(self):
        """_fetch_embeddings_total must NOT be called when /health is hit."""
        from src.mcp import health as health_mod

        fetch_total_calls = 0
        fetch_breakdown_calls = 0

        async def _spy_fetch_total():
            nonlocal fetch_total_calls
            fetch_total_calls += 1
            return 42

        async def _spy_fetch_breakdown():
            nonlocal fetch_breakdown_calls
            fetch_breakdown_calls += 1
            return {"method": 42}

        # Provide pre-warmed cache so _get_ready_data never calls the fetchers.
        fake_cache = {
            "embeddings_total": 100,
            "embeddings_by_chunk_type": {"method": 80, "field": 20},
            "cached_at": time.monotonic(),  # fresh — within TTL
        }

        with (
            patch.object(health_mod, "_fetch_embeddings_total", _spy_fetch_total),
            patch.object(health_mod, "_fetch_embeddings_by_chunk_type", _spy_fetch_breakdown),
            patch.object(health_mod, "_ready_cache", fake_cache),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.health_handler(_make_request("/health"))

        body = resp.body
        assert fetch_total_calls == 0, (
            f"_fetch_embeddings_total was called {fetch_total_calls} times on /health "
            "(must be 0 — counts must come from cache only)"
        )
        assert fetch_breakdown_calls == 0, (
            f"_fetch_embeddings_by_chunk_type was called {fetch_breakdown_calls} times on "
            "/health (must be 0 — counts must come from cache only)"
        )
        import json

        data = json.loads(body)
        # Backward compat: /health still returns these fields
        assert data["embeddings_total"] == 100
        assert data["embeddings_by_chunk_type"] == {"method": 80, "field": 20}

    @pytest.mark.asyncio
    async def test_health_returns_required_liveness_keys(self):
        """All required liveness keys must be present; DB-status keys must NOT.

        Liveness is pool-independent, so ``neo4j`` / ``postgres`` status fields
        belong to ``/ready``, not ``/health``.
        """
        from src.mcp import health as health_mod

        fake_cache = {
            "embeddings_total": 0,
            "embeddings_by_chunk_type": {},
            "cached_at": time.monotonic(),
        }

        with (
            patch.object(health_mod, "_ready_cache", fake_cache),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.health_handler(_make_request("/health"))

        import json

        data = json.loads(resp.body)
        for key in ("status", "version", "mcp_tools",
                    "embeddings_total", "embeddings_by_chunk_type"):
            assert key in data, f"Missing key in /health response: {key}"
        # DB connectivity is NOT a liveness concern — must live on /ready only.
        assert "neo4j" not in data
        assert "postgres" not in data

    @pytest.mark.asyncio
    async def test_health_always_alive_and_200(self):
        """/health returns status='alive' and HTTP 200 unconditionally."""
        from src.mcp import health as health_mod

        fake_cache = {
            "embeddings_total": 591000,
            "embeddings_by_chunk_type": {"method": 300000, "field": 291000},
            "cached_at": time.monotonic(),
        }

        with (
            patch.object(health_mod, "_ready_cache", fake_cache),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.health_handler(_make_request("/health"))

        import json

        data = json.loads(resp.body)
        assert data["status"] == "alive"
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_does_not_touch_db_even_when_down(self):
        """/health must never checkout the DB pool — the #227 anti-wedge invariant.

        Even if Neo4j/PG are down, /health must NOT call _check_neo4j/_check_pg
        and must still answer 200. A DB-coupled liveness probe under pool
        exhaustion was the production failure mode (false 504 → needless restart).
        """
        from src.mcp import health as health_mod

        neo4j_calls = 0
        pg_calls = 0

        async def _spy_neo4j():
            nonlocal neo4j_calls
            neo4j_calls += 1
            return "error:Neo4j down"

        async def _spy_pg():
            nonlocal pg_calls
            pg_calls += 1
            return "error:PG down"

        # Cold cache (no /ready hit yet) — peek must return None, no scan.
        with (
            patch.object(health_mod, "_ready_cache", None),
            patch.object(health_mod, "_check_neo4j", _spy_neo4j),
            patch.object(health_mod, "_check_pg", _spy_pg),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.health_handler(_make_request("/health"))

        import json

        data = json.loads(resp.body)
        assert neo4j_calls == 0, "/health must NOT checkout Neo4j (liveness is pool-independent)"
        assert pg_calls == 0, "/health must NOT checkout PG (liveness is pool-independent)"
        assert resp.status_code == 200
        assert data["status"] == "alive"
        # Cold cache → non-scanning peek yields None / empty, never a fresh scan.
        assert data["embeddings_total"] is None
        assert data["embeddings_by_chunk_type"] == {}


# ---------------------------------------------------------------------------
# 2.  Cache: fetch runs exactly once per TTL window
# ---------------------------------------------------------------------------


class TestReadyCacheDeduplication:
    """Verify the readiness cache prevents redundant DB scans."""

    @pytest.mark.asyncio
    async def test_second_call_within_ttl_does_not_refetch(self):
        """Calling _get_ready_data twice within TTL runs DB fetch exactly once."""
        from src.mcp import health as health_mod

        fetch_count = 0

        async def _counting_fetch_total():
            nonlocal fetch_count
            fetch_count += 1
            return 999

        async def _stub_fetch_breakdown():
            return {"stub": 1}

        # Clear cache to force a cold miss on the first call
        original_cache = health_mod._ready_cache
        health_mod._ready_cache = None
        try:
            with (
                patch.object(health_mod, "_fetch_embeddings_total", _counting_fetch_total),
                patch.object(health_mod, "_fetch_embeddings_by_chunk_type", _stub_fetch_breakdown),
            ):
                result1 = await health_mod._get_ready_data()
                result2 = await health_mod._get_ready_data()
        finally:
            health_mod._ready_cache = original_cache

        assert fetch_count == 1, (
            f"Expected _fetch_embeddings_total to run exactly once (cache hit on 2nd call), "
            f"but it ran {fetch_count} times"
        )
        assert result1["embeddings_total"] == 999
        assert result2["embeddings_total"] == 999

    @pytest.mark.asyncio
    async def test_expired_cache_triggers_refetch(self):
        """After TTL expiry, next call runs a fresh DB fetch."""
        from src.mcp import health as health_mod

        fetch_count = 0

        async def _counting_fetch_total():
            nonlocal fetch_count
            fetch_count += 1
            return 42

        async def _stub_fetch_breakdown():
            return {}

        # Inject an already-expired cache entry
        original_cache = health_mod._ready_cache
        health_mod._ready_cache = {
            "embeddings_total": 0,
            "embeddings_by_chunk_type": {},
            "cached_at": time.monotonic() - (health_mod._READY_CACHE_TTL_S + 1),
        }
        try:
            with (
                patch.object(health_mod, "_fetch_embeddings_total", _counting_fetch_total),
                patch.object(health_mod, "_fetch_embeddings_by_chunk_type", _stub_fetch_breakdown),
            ):
                result = await health_mod._get_ready_data()
        finally:
            health_mod._ready_cache = original_cache

        assert fetch_count == 1, "Expired cache must trigger exactly one fresh DB fetch"
        assert result["embeddings_total"] == 42


# ---------------------------------------------------------------------------
# 3.  /ready endpoint returns expected fields
# ---------------------------------------------------------------------------


class TestReadyEndpoint:
    """Verify /ready response contract."""

    @pytest.mark.asyncio
    async def test_ready_returns_required_keys(self):
        """All required readiness fields must be present in /ready response."""
        from src.mcp import health as health_mod

        fake_cache = {
            "embeddings_total": 591000,
            "embeddings_by_chunk_type": {"method": 300000},
            "cached_at": time.monotonic(),
        }

        with (
            patch.object(health_mod, "_ready_cache", fake_cache),
            patch.object(health_mod, "_check_neo4j", AsyncMock(return_value="ok")),
            patch.object(health_mod, "_check_pg", AsyncMock(return_value="ok")),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.ready_handler(_make_request("/ready"))

        import json

        data = json.loads(resp.body)
        for key in ("status", "neo4j", "postgres", "version", "mcp_tools",
                    "embeddings_total", "embeddings_by_chunk_type",
                    "cache_ttl_s", "cache_age_s"):
            assert key in data, f"Missing key in /ready response: {key}"

    @pytest.mark.asyncio
    async def test_ready_returns_mcp_tools_count(self):
        """mcp_tools must appear in /ready response with the value from _get_mcp_tool_count."""
        from src.mcp import health as health_mod

        fake_cache = {
            "embeddings_total": 0,
            "embeddings_by_chunk_type": {},
            "cached_at": time.monotonic(),
        }

        with (
            patch.object(health_mod, "_ready_cache", fake_cache),
            patch.object(health_mod, "_check_neo4j", AsyncMock(return_value="ok")),
            patch.object(health_mod, "_check_pg", AsyncMock(return_value="ok")),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.ready_handler(_make_request("/ready"))

        import json

        data = json.loads(resp.body)
        assert "mcp_tools" in data, (
            "/ready must include mcp_tools "
            "(consumers migrating from /health must not lose this field)"
        )
        assert data["mcp_tools"] == 25, f"Expected mcp_tools=25 but got {data['mcp_tools']}"

    @pytest.mark.asyncio
    async def test_ready_status_ok_when_both_up(self):
        """When both DBs are up, /ready returns status='ok' and HTTP 200."""
        from src.mcp import health as health_mod

        fake_cache = {
            "embeddings_total": 42,
            "embeddings_by_chunk_type": {"field": 42},
            "cached_at": time.monotonic(),
        }

        with (
            patch.object(health_mod, "_ready_cache", fake_cache),
            patch.object(health_mod, "_check_neo4j", AsyncMock(return_value="ok")),
            patch.object(health_mod, "_check_pg", AsyncMock(return_value="ok")),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.ready_handler(_make_request("/ready"))

        import json

        data = json.loads(resp.body)
        assert data["status"] == "ok"
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_ready_single_db_down_is_degraded_503(self):
        """One DB down: status='degraded' (granular) but HTTP 503 so an
        HTTP-status-only external monitor catches the single-DB outage."""
        from src.mcp import health as health_mod

        fake_cache = {
            "embeddings_total": 42,
            "embeddings_by_chunk_type": {"field": 42},
            "cached_at": time.monotonic(),
        }

        with (
            patch.object(health_mod, "_ready_cache", fake_cache),
            patch.object(health_mod, "_check_neo4j", AsyncMock(return_value="ok")),
            patch.object(
                health_mod, "_check_pg", AsyncMock(return_value="error: pool down")
            ),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.ready_handler(_make_request("/ready"))

        import json

        data = json.loads(resp.body)
        assert data["status"] == "degraded"
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_ready_both_db_down_is_error_503(self):
        """Both DBs down: status='error' and HTTP 503."""
        from src.mcp import health as health_mod

        fake_cache = {
            "embeddings_total": 0,
            "embeddings_by_chunk_type": {},
            "cached_at": time.monotonic(),
        }

        with (
            patch.object(health_mod, "_ready_cache", fake_cache),
            patch.object(
                health_mod, "_check_neo4j", AsyncMock(return_value="error: down")
            ),
            patch.object(
                health_mod, "_check_pg", AsyncMock(return_value="error: down")
            ),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.ready_handler(_make_request("/ready"))

        import json

        data = json.loads(resp.body)
        assert data["status"] == "error"
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_ready_cache_ttl_field_matches_constant(self):
        """cache_ttl_s in /ready response must equal _READY_CACHE_TTL_S constant."""
        from src.mcp import health as health_mod

        fake_cache = {
            "embeddings_total": 0,
            "embeddings_by_chunk_type": {},
            "cached_at": time.monotonic(),
        }

        with (
            patch.object(health_mod, "_ready_cache", fake_cache),
            patch.object(health_mod, "_check_neo4j", AsyncMock(return_value="ok")),
            patch.object(health_mod, "_check_pg", AsyncMock(return_value="ok")),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.ready_handler(_make_request("/ready"))

        import json

        data = json.loads(resp.body)
        assert data["cache_ttl_s"] == health_mod._READY_CACHE_TTL_S

    @pytest.mark.asyncio
    async def test_ready_cache_age_is_non_negative_float(self):
        """cache_age_s in /ready response must be a non-negative number."""
        from src.mcp import health as health_mod

        fake_cache = {
            "embeddings_total": 0,
            "embeddings_by_chunk_type": {},
            "cached_at": time.monotonic() - 5.0,  # 5 seconds old
        }

        with (
            patch.object(health_mod, "_ready_cache", fake_cache),
            patch.object(health_mod, "_check_neo4j", AsyncMock(return_value="ok")),
            patch.object(health_mod, "_check_pg", AsyncMock(return_value="ok")),
            patch.object(health_mod, "_get_mcp_tool_count", AsyncMock(return_value=25)),
        ):
            resp = await health_mod.ready_handler(_make_request("/ready"))

        import json

        data = json.loads(resp.body)
        age = data["cache_age_s"]
        assert isinstance(age, float | int), f"cache_age_s should be numeric, got {type(age)}"
        assert age >= 0, f"cache_age_s must be non-negative, got {age}"


# ---------------------------------------------------------------------------
# 4.  Timeout behaviour for liveness checks
# ---------------------------------------------------------------------------


class TestLivenessCheckTimeouts:
    """Verify _check_neo4j / _check_pg return error strings on timeout."""

    @pytest.mark.asyncio
    async def test_check_neo4j_returns_error_on_timeout(self):
        """_check_neo4j returns error string (not raises) when the check times out."""
        from src.mcp import health as health_mod

        # Simulate a driver whose verify_connectivity hangs longer than timeout
        async def _slow_thread(*_args, **_kwargs):
            await asyncio.sleep(999)

        with (
            patch("asyncio.to_thread", _slow_thread),
            # Shrink timeout to near-zero for the test
            patch.object(health_mod, "_LIVENESS_CHECK_TIMEOUT_S", 0.001),
        ):
            result = await health_mod._check_neo4j()

        assert result.startswith("error:timeout"), (
            f"Expected 'error:timeout...' but got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_check_pg_returns_error_on_connection_failure(self):
        """_check_pg returns error string (not raises) when the connection fails."""
        from contextlib import contextmanager

        from src.mcp import health as health_mod

        class _BrokenConn:
            closed = False

            def cursor(self):
                raise ConnectionError("PG down")

        @contextmanager
        def _mock_checkout():
            yield _BrokenConn()

        with patch("src.mcp.server._checkout_pg", _mock_checkout):
            result = await health_mod._check_pg()

        assert result.startswith("error:"), (
            f"Expected 'error:...' but got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_check_neo4j_returns_ok_on_success(self):
        """_check_neo4j returns 'ok' when connectivity check succeeds."""
        from src.mcp import health as health_mod

        class _WorkingDriver:
            def verify_connectivity(self):
                pass  # no-op = success

        with patch("src.mcp.server._get_driver", return_value=_WorkingDriver()):
            result = await health_mod._check_neo4j()

        assert result == "ok", f"Expected 'ok' but got {result!r}"
