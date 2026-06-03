# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for src/mcp/session.py — pure unit, no DB / Neo4j required.

Covers the in-memory per-(api_key_id, mcp_session_id) pin store introduced by
#251, plus the sentinel normalization and 3-tier resolvers.

Test plan:
  1.  normalize_version_arg  — sentinels collapse to None; real versions pass through.
  2.  normalize_profile_arg  — None/empty/whitespace collapse to None; names pass through.
  3.  resolve_version_v2     — explicit > session pin > _latest_version fallback.
  4.  resolve_profile_v2     — explicit > session pin > None (no authz).
  5.  _ck                    — composite-key distinctness.
  6.  composite isolation    — two mcp_session_ids under one key keep independent pins.
  7.  no cross-field clobber — set version then profile (and vice-versa), both survive.
  8.  LRU / size cap         — oldest-by-set_at entry evicted past the bound.
  9.  24h idle TTL           — stale entry treated as unset.
  10. no-session fallback    — '_nosession' bucket round-trips today's single-key semantics.
  11. A-SCALE-1              — resolve/set perform 0 DB I/O (no _checkout_pg / driver call).
  12. #248 guard             — non-numeric api_key_id → setters False, getter None.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

import src.mcp.session as session_mod
from src.mcp.session import (
    _NO_SESSION_SENTINEL,
    SessionState,
    _cache,
    _cache_lock,
    _ck,
    get_session_state,
    normalize_profile_arg,
    normalize_version_arg,
    resolve_profile_v2,
    resolve_version_v2,
    set_active_profile_db,
    set_active_version_db,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_cache() -> None:
    """Empty the module-level pin store between tests."""
    with _cache_lock:
        _cache.clear()


class _FakeClock:
    """Monotonic clock stub: ``tick(dt)`` advances; call instance for ``now``."""

    def __init__(self, t0: float = 0.0) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# 1. normalize_version_arg
# ---------------------------------------------------------------------------


class TestNormalizeVersionArg:
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
# 2. normalize_profile_arg
# ---------------------------------------------------------------------------


class TestNormalizeProfileArg:
    def test_none_returns_none(self) -> None:
        assert normalize_profile_arg(None) is None

    @pytest.mark.parametrize("empty", ["", "   ", "\t", "\n  "])
    def test_empty_or_whitespace_collapses_to_none(self, empty: str) -> None:
        assert normalize_profile_arg(empty) is None

    @pytest.mark.parametrize("name", ["my-erp-prod", "default-profile", "latest"])
    def test_names_pass_through_unchanged(self, name: str) -> None:
        # Profiles are NOT version sentinels — "latest" is a valid profile name.
        assert normalize_profile_arg(name) == name


# ---------------------------------------------------------------------------
# 3. resolve_version_v2 — resolution order
# ---------------------------------------------------------------------------


class TestResolveVersionV2:
    def setup_method(self) -> None:
        _clear_cache()

    def test_explicit_version_wins_over_session_and_fallback(self) -> None:
        mock_session = MagicMock()
        with patch.object(
            session_mod,
            "get_session_state",
            return_value=SessionState("1", "16.0", None),
        ):
            result = resolve_version_v2("17.0", "1", mock_session)
        assert result == "17.0"
        mock_session.run.assert_not_called()

    def test_sentinel_explicit_falls_to_session_tier(self) -> None:
        mock_session = MagicMock()
        with patch.object(
            session_mod,
            "get_session_state",
            return_value=SessionState("1", "16.0", None),
        ):
            result = resolve_version_v2("latest", "1", mock_session)
        assert result == "16.0"
        mock_session.run.assert_not_called()

    def test_none_explicit_falls_to_session_tier(self) -> None:
        mock_session = MagicMock()
        with patch.object(
            session_mod,
            "get_session_state",
            return_value=SessionState("1", "14.0", None),
        ):
            result = resolve_version_v2(None, "1", mock_session)
        assert result == "14.0"

    def test_no_session_falls_to_latest_fallback(self) -> None:
        mock_session = MagicMock()
        with (
            patch.object(session_mod, "get_session_state", return_value=None),
            patch("src.mcp.server._latest_version", return_value="17.0") as mock_lv,
        ):
            result = resolve_version_v2(None, "1", mock_session)
        assert result == "17.0"
        mock_lv.assert_called_once_with(mock_session)

    def test_session_state_version_none_falls_to_fallback(self) -> None:
        mock_session = MagicMock()
        state = SessionState("1", None, None)
        with (
            patch.object(session_mod, "get_session_state", return_value=state),
            patch("src.mcp.server._latest_version", return_value="17.0") as mock_lv,
        ):
            result = resolve_version_v2(None, "1", mock_session)
        assert result == "17.0"
        mock_lv.assert_called_once_with(mock_session)

    def test_threads_mcp_session_id_into_tier2_lookup(self) -> None:
        """The mcp_session_id must reach the Tier-2 get_session_state lookup."""
        mock_session = MagicMock()
        with patch.object(
            session_mod, "get_session_state", return_value=None
        ) as mock_gss, patch("src.mcp.server._latest_version", return_value="17.0"):
            resolve_version_v2(None, "1", mock_session, "sess-abc")
        mock_gss.assert_called_once_with("1", "sess-abc")


# ---------------------------------------------------------------------------
# 4. resolve_profile_v2 — resolution order (no authz)
# ---------------------------------------------------------------------------


class TestResolveProfileV2:
    def setup_method(self) -> None:
        _clear_cache()

    def test_explicit_profile_wins(self) -> None:
        with patch.object(
            session_mod,
            "get_session_state",
            return_value=SessionState("1", None, "pinned-prof"),
        ):
            assert resolve_profile_v2("explicit-prof", "1", None) == "explicit-prof"

    def test_empty_explicit_falls_to_pin(self) -> None:
        with patch.object(
            session_mod,
            "get_session_state",
            return_value=SessionState("1", None, "pinned-prof"),
        ):
            assert resolve_profile_v2("  ", "1", None) == "pinned-prof"

    def test_no_pin_returns_none(self) -> None:
        with patch.object(session_mod, "get_session_state", return_value=None):
            assert resolve_profile_v2(None, "1", None) is None

    def test_pin_with_none_profile_returns_none(self) -> None:
        with patch.object(
            session_mod,
            "get_session_state",
            return_value=SessionState("1", "17.0", None),
        ):
            assert resolve_profile_v2(None, "1", None) is None

    def test_threads_mcp_session_id(self) -> None:
        with patch.object(
            session_mod, "get_session_state", return_value=None
        ) as mock_gss:
            resolve_profile_v2(None, "1", None, "sess-xyz")
        mock_gss.assert_called_once_with("1", "sess-xyz")


# ---------------------------------------------------------------------------
# 5. _ck — composite-key distinctness
# ---------------------------------------------------------------------------


class TestCompositeKey:
    def test_distinct_sessions_distinct_keys(self) -> None:
        assert _ck("1", "a") != _ck("1", "b")

    def test_distinct_keys_distinct_composite(self) -> None:
        assert _ck("1", "a") != _ck("2", "a")

    def test_no_literal_collision_across_split(self) -> None:
        # Without a separator, ('1','2a') and ('12','a') could collide; the
        # unit-separator prevents that.
        assert _ck("1", "2a") != _ck("12", "a")

    def test_same_pair_same_key(self) -> None:
        assert _ck("7", "s") == _ck("7", "s")


# ---------------------------------------------------------------------------
# 6. Composite isolation — independent pins per (key, session)
# ---------------------------------------------------------------------------


class TestPerSessionIsolation:
    def setup_method(self) -> None:
        _clear_cache()

    def test_two_sessions_one_key_keep_independent_versions(self) -> None:
        assert set_active_version_db("1", "17.0", "sess-A") is True
        assert set_active_version_db("1", "18.0", "sess-B") is True

        sa = get_session_state("1", "sess-A")
        sb = get_session_state("1", "sess-B")
        assert sa is not None and sa.odoo_version == "17.0"
        assert sb is not None and sb.odoo_version == "18.0"

    def test_two_sessions_one_key_keep_independent_profiles(self) -> None:
        assert set_active_profile_db("1", "prof-A", "sess-A") is True
        assert set_active_profile_db("1", "prof-B", "sess-B") is True

        sa = get_session_state("1", "sess-A")
        sb = get_session_state("1", "sess-B")
        assert sa is not None and sa.profile_name == "prof-A"
        assert sb is not None and sb.profile_name == "prof-B"

    def test_resolve_version_v2_isolation(self) -> None:
        set_active_version_db("1", "17.0", "sess-A")
        set_active_version_db("1", "18.0", "sess-B")
        mock_session = MagicMock()
        assert resolve_version_v2("auto", "1", mock_session, "sess-A") == "17.0"
        assert resolve_version_v2("auto", "1", mock_session, "sess-B") == "18.0"


# ---------------------------------------------------------------------------
# 7. No cross-field clobber
# ---------------------------------------------------------------------------


class TestNoCrossFieldClobber:
    def setup_method(self) -> None:
        _clear_cache()

    def test_version_then_profile_both_survive(self) -> None:
        set_active_version_db("1", "17.0", "s")
        set_active_profile_db("1", "prof", "s")
        state = get_session_state("1", "s")
        assert state is not None
        assert state.odoo_version == "17.0"
        assert state.profile_name == "prof"

    def test_profile_then_version_both_survive(self) -> None:
        set_active_profile_db("1", "prof", "s")
        set_active_version_db("1", "17.0", "s")
        state = get_session_state("1", "s")
        assert state is not None
        assert state.odoo_version == "17.0"
        assert state.profile_name == "prof"

    def test_clearing_profile_preserves_version(self) -> None:
        set_active_version_db("1", "17.0", "s")
        set_active_profile_db("1", "prof", "s")
        set_active_profile_db("1", None, "s")
        state = get_session_state("1", "s")
        assert state is not None
        assert state.odoo_version == "17.0"
        assert state.profile_name is None


# ---------------------------------------------------------------------------
# 8. LRU / size cap — oldest-by-set_at evicted past the bound
# ---------------------------------------------------------------------------


class TestSizeCap:
    def setup_method(self) -> None:
        _clear_cache()

    def test_oldest_entry_evicted_when_cap_exceeded(self) -> None:
        clock = _FakeClock()
        with patch.object(session_mod, "_PIN_MAX", 3):
            # 3 entries at increasing timestamps.
            for i, sess in enumerate(["s0", "s1", "s2"]):
                clock.tick(1.0)
                set_active_version_db("1", f"{10 + i}.0", sess, now_fn=clock)
            assert len(_cache) == 3
            # 4th entry overflows → oldest (s0, set_at smallest) evicted.
            clock.tick(1.0)
            set_active_version_db("1", "20.0", "s3", now_fn=clock)

        assert len(_cache) == 3
        assert get_session_state("1", "s0", now_fn=clock) is None
        assert get_session_state("1", "s3", now_fn=clock) is not None

    def test_refreshing_an_entry_protects_it_from_eviction(self) -> None:
        clock = _FakeClock()
        with patch.object(session_mod, "_PIN_MAX", 2):
            clock.tick(1.0)
            set_active_version_db("1", "10.0", "s0", now_fn=clock)
            clock.tick(1.0)
            set_active_version_db("1", "11.0", "s1", now_fn=clock)
            # Refresh s0 so it's now the newest by set_at.
            clock.tick(1.0)
            set_active_version_db("1", "10.1", "s0", now_fn=clock)
            # Overflow: s1 (now oldest) must go, s0 must stay.
            clock.tick(1.0)
            set_active_version_db("1", "12.0", "s2", now_fn=clock)

        assert get_session_state("1", "s1", now_fn=clock) is None
        assert get_session_state("1", "s0", now_fn=clock) is not None


# ---------------------------------------------------------------------------
# 9. 24h idle TTL — stale entry treated as unset
# ---------------------------------------------------------------------------


class TestIdleTTL:
    def setup_method(self) -> None:
        _clear_cache()

    def test_fresh_entry_within_ttl_is_returned(self) -> None:
        clock = _FakeClock()
        set_active_version_db("1", "17.0", "s", now_fn=clock)
        clock.tick(23 * 3600)  # 23h < 24h
        assert get_session_state("1", "s", now_fn=clock) is not None

    def test_stale_entry_past_ttl_treated_as_unset(self) -> None:
        clock = _FakeClock()
        set_active_version_db("1", "17.0", "s", now_fn=clock)
        clock.tick(24 * 3600 + 1)  # just past 24h
        assert get_session_state("1", "s", now_fn=clock) is None

    def test_stale_entry_is_evicted_on_read(self) -> None:
        clock = _FakeClock()
        set_active_version_db("1", "17.0", "s", now_fn=clock)
        clock.tick(24 * 3600 + 1)
        get_session_state("1", "s", now_fn=clock)
        with _cache_lock:
            assert _ck("1", "s") not in _cache


# ---------------------------------------------------------------------------
# 10. No-session fallback — '_nosession' round-trips single-key semantics
# ---------------------------------------------------------------------------


class TestNoSessionFallback:
    def setup_method(self) -> None:
        _clear_cache()

    def test_default_arg_uses_nosession_bucket(self) -> None:
        # Positional callsites compile unchanged AND share one bucket.
        assert set_active_version_db("1", "17.0") is True
        state = get_session_state("1")
        assert state is not None and state.odoo_version == "17.0"
        # The default arg and the explicit sentinel resolve to the SAME bucket
        # (asserted by value: get_session_state returns a snapshot copy, never
        # the live entry, so identity would not hold — and must not, to prevent
        # torn reads under concurrent writes).
        assert get_session_state("1", _NO_SESSION_SENTINEL) == state

    def test_nosession_is_isolated_from_real_sessions(self) -> None:
        set_active_version_db("1", "17.0")  # _nosession
        set_active_version_db("1", "18.0", "real-sess")
        assert get_session_state("1").odoo_version == "17.0"
        assert get_session_state("1", "real-sess").odoo_version == "18.0"

    def test_resolve_version_default_positional_compiles(self) -> None:
        set_active_version_db("1", "16.0")
        mock_session = MagicMock()
        # 3-positional-arg call (legacy callsite) must still work.
        assert resolve_version_v2("auto", "1", mock_session) == "16.0"


# ---------------------------------------------------------------------------
# 11. A-SCALE-1 — resolve / set perform 0 DB I/O
# ---------------------------------------------------------------------------


class TestNoDbIo:
    def setup_method(self) -> None:
        _clear_cache()

    def test_set_and_resolve_never_touch_pg_or_driver(self) -> None:
        """If session.py touched PG, importing _checkout_pg would be attempted.

        We patch it to explode; the set + resolve path must complete without
        ever calling it (in-memory source of truth — A-SCALE-1).
        """
        def boom(*_a, **_k):
            raise AssertionError("DB I/O on the session pin hot path (#251 A-SCALE-1)")

        with patch("src.mcp.server._checkout_pg", side_effect=boom):
            assert set_active_version_db("1", "17.0", "s") is True
            assert set_active_profile_db("1", "prof", "s") is True
            mock_session = MagicMock()
            assert resolve_version_v2("auto", "1", mock_session, "s") == "17.0"
            assert resolve_profile_v2(None, "1", None, "s") == "prof"
            mock_session.run.assert_not_called()


# ---------------------------------------------------------------------------
# 12. #248 loud-fail guard — non-numeric api_key_id
# ---------------------------------------------------------------------------


class TestNonNumericKeyGuard:
    def setup_method(self) -> None:
        _clear_cache()

    @pytest.mark.parametrize("bad_key", ["default", "not-an-int", ""])
    def test_set_version_returns_false_no_store(self, bad_key: str) -> None:
        assert set_active_version_db(bad_key, "17.0", "s") is False
        with _cache_lock:
            assert _cache == {}

    @pytest.mark.parametrize("bad_key", ["default", "not-an-int", ""])
    def test_set_profile_returns_false_no_store(self, bad_key: str) -> None:
        assert set_active_profile_db(bad_key, "prof", "s") is False
        with _cache_lock:
            assert _cache == {}

    def test_get_session_state_returns_none_for_non_numeric(self) -> None:
        assert get_session_state("default", "s") is None


# ---------------------------------------------------------------------------
# 13. Thread safety — concurrent writes for distinct sessions don't corrupt
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def setup_method(self) -> None:
        _clear_cache()

    def test_concurrent_distinct_session_writes_all_land(self) -> None:
        errors: list[Exception] = []
        elock = threading.Lock()

        def writer(i: int) -> None:
            try:
                set_active_version_db("1", f"{i}.0", f"sess-{i}")
            except Exception as exc:  # noqa: BLE001
                with elock:
                    errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        for i in range(50):
            state = get_session_state("1", f"sess-{i}")
            assert state is not None and state.odoo_version == f"{i}.0"
