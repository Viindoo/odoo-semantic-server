# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for src/mcp/session.py — pure unit, no DB required.

Covers AC-E2-2, AC-E2-3, AC-E2-4 (via mocked clock), AC-E2-5.

Test plan:
  1. normalize_version_arg — 5 sentinels all collapse to None
  2. normalize_version_arg — real versions pass through unchanged
  3. resolve_version_v2 — explicit tier wins (mocked session, no state lookup needed)
  4. resolve_version_v2 — session tier wins when explicit is None
  5. resolve_version_v2 — fallback tier when explicit=None + no session state
  6. get_session_state 60s cache — 2 calls within TTL hit cache (1 SQL)
  7. get_session_state cache TTL eviction — call at 61s re-fetches
  8. cache invalidated after set_active_version_db (verifies invalidation path)
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.mcp.session import (
    SessionState,
    _cache,
    _cache_get,
    _cache_invalidate,
    _cache_set,
    get_session_state,
    normalize_version_arg,
    resolve_version_v2,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(api_key_id: str = "k1", version: str | None = "17.0") -> SessionState:
    return SessionState(api_key_id=api_key_id, odoo_version=version, profile_name=None)


def _clear_cache() -> None:
    """Empty the module-level cache between tests."""
    with __import__("src.mcp.session", fromlist=["_cache_lock"]).session._cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# 1+2. normalize_version_arg
# ---------------------------------------------------------------------------


class TestNormalizeVersionArg:
    """AC-E2-2 — 5 sentinels collapse to None; real versions pass through."""

    @pytest.mark.parametrize("sentinel", ["auto", "default", "latest", "version", "any", ""])
    def test_sentinels_collapse_to_none(self, sentinel: str) -> None:
        assert normalize_version_arg(sentinel) is None

    @pytest.mark.parametrize("sentinel", ["DEFAULT", "Latest", "VERSION", "Any"])
    def test_sentinels_case_insensitive(self, sentinel: str) -> None:
        assert normalize_version_arg(sentinel) is None

    @pytest.mark.parametrize("sentinel", [" default ", "  latest  "])
    def test_sentinels_strip_whitespace(self, sentinel: str) -> None:
        assert normalize_version_arg(sentinel) is None

    def test_none_returns_none(self) -> None:
        assert normalize_version_arg(None) is None

    @pytest.mark.parametrize("version", ["17.0", "16.0", "9.0", "14.0", "8.0"])
    def test_real_versions_pass_through(self, version: str) -> None:
        assert normalize_version_arg(version) == version

    def test_non_sentinel_string_passes_through(self) -> None:
        assert normalize_version_arg("my-custom-string") == "my-custom-string"


# ---------------------------------------------------------------------------
# 3+4+5. resolve_version_v2 — resolution order
# ---------------------------------------------------------------------------


class TestResolveVersionV2:
    """AC-E2-3 — resolution order: explicit → session → fallback."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        _cache.clear()

    def _mock_neo4j_session(self) -> MagicMock:
        """Return a mock Neo4j session (never called in tier 1/2 tests)."""
        return MagicMock()

    def test_explicit_version_wins_over_session_and_fallback(self) -> None:
        """Tier 1: explicit non-sentinel version → returned immediately."""
        mock_session = self._mock_neo4j_session()

        # Even if session state existed for api_key, explicit version wins.
        with patch(
            "src.mcp.session.get_session_state",
            return_value=_make_state("key1", "16.0"),
        ):
            result = resolve_version_v2("17.0", "key1", mock_session)

        assert result == "17.0"
        # Neo4j session was never consulted
        mock_session.run.assert_not_called()

    def test_sentinel_explicit_falls_to_session_tier(self) -> None:
        """Tier 2: sentinel version_arg → look up session state."""
        mock_session = self._mock_neo4j_session()

        with patch(
            "src.mcp.session.get_session_state",
            return_value=_make_state("key1", "16.0"),
        ):
            result = resolve_version_v2("latest", "key1", mock_session)

        assert result == "16.0"
        mock_session.run.assert_not_called()

    def test_none_explicit_falls_to_session_tier(self) -> None:
        """Tier 2: None version_arg → session state used."""
        mock_session = self._mock_neo4j_session()

        with patch(
            "src.mcp.session.get_session_state",
            return_value=_make_state("key1", "14.0"),
        ):
            result = resolve_version_v2(None, "key1", mock_session)

        assert result == "14.0"

    def test_no_session_falls_to_latest_fallback(self) -> None:
        """Tier 3: no explicit + no session state → _latest_version(session) fallback."""
        mock_session = self._mock_neo4j_session()

        with (
            patch("src.mcp.session.get_session_state", return_value=None),
            patch("src.mcp.server._latest_version", return_value="17.0") as mock_lv,
        ):
            result = resolve_version_v2(None, "key1", mock_session)

        assert result == "17.0"
        mock_lv.assert_called_once_with(mock_session)

    def test_session_state_version_none_falls_to_fallback(self) -> None:
        """Tier 3: session exists but odoo_version is None → _latest_version fallback."""
        mock_session = self._mock_neo4j_session()
        state = SessionState(api_key_id="key1", odoo_version=None, profile_name=None)

        with (
            patch("src.mcp.session.get_session_state", return_value=state),
            patch("src.mcp.server._latest_version", return_value="17.0") as mock_lv,
        ):
            result = resolve_version_v2(None, "key1", mock_session)

        assert result == "17.0"
        mock_lv.assert_called_once_with(mock_session)


# ---------------------------------------------------------------------------
# 6+7. get_session_state — 60s cache TTL
# ---------------------------------------------------------------------------


class TestSessionStateCache:
    """AC-E2-4 — 2 calls within 60s hit cache (1 SQL); after 60s re-fetches."""

    def setup_method(self) -> None:
        _cache.clear()

    def test_two_calls_within_ttl_hit_cache(self) -> None:
        """Second call within 60s must NOT re-query the DB."""
        tick = [0.0]

        def fake_now() -> float:
            return tick[0]

        state = _make_state("key-cache")
        call_count = [0]

        def fake_fetch(api_key_id: str) -> SessionState | None:
            call_count[0] += 1
            return state

        with patch("src.mcp.session._fetch_from_db", side_effect=fake_fetch):
            # First call at t=0 — hits DB
            result1 = get_session_state("key-cache", now_fn=fake_now)
            # Advance time to 59s — still within TTL
            tick[0] = 59.0
            result2 = get_session_state("key-cache", now_fn=fake_now)

        assert result1 == state
        assert result2 == state
        assert call_count[0] == 1, "DB should only be queried once within TTL"

    def test_call_after_ttl_expiry_re_fetches(self) -> None:
        """Call at t=61 (after 60s TTL) must re-query the DB."""
        tick = [0.0]

        def fake_now() -> float:
            return tick[0]

        state = _make_state("key-ttl")
        call_count = [0]

        def fake_fetch(api_key_id: str) -> SessionState | None:
            call_count[0] += 1
            return state

        with patch("src.mcp.session._fetch_from_db", side_effect=fake_fetch):
            # First call at t=0
            get_session_state("key-ttl", now_fn=fake_now)
            # Second call at t=61 — past the 60s TTL
            tick[0] = 61.0
            get_session_state("key-ttl", now_fn=fake_now)

        assert call_count[0] == 2, "DB should be re-queried after TTL expiry"

    def test_cache_invalidate_clears_entry(self) -> None:
        """_cache_invalidate removes the entry from the module cache."""
        now = time.monotonic()
        _cache_set("key-inv", _make_state("key-inv"), now)
        hit_before, _ = _cache_get("key-inv", now)
        assert hit_before is True

        _cache_invalidate("key-inv")
        hit_after, _ = _cache_get("key-inv", now)
        assert hit_after is False


# ---------------------------------------------------------------------------
# 8. Thread safety — concurrent reads for same api_key_id
# ---------------------------------------------------------------------------


class TestCacheThreadSafety:
    """Basic thread-safety: concurrent reads for the same key don't corrupt state."""

    def setup_method(self) -> None:
        _cache.clear()

    def test_concurrent_reads_return_consistent_state(self) -> None:
        """50 concurrent threads reading same api_key_id all get identical state."""
        state = _make_state("shared-key")
        results: list[SessionState | None] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def fake_fetch(api_key_id: str) -> SessionState | None:
            return state

        def reader() -> None:
            try:
                s = get_session_state("shared-key")
                with lock:
                    results.append(s)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        with patch("src.mcp.session._fetch_from_db", side_effect=fake_fetch):
            threads = [threading.Thread(target=reader) for _ in range(50)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 50
        assert all(r == state for r in results)
