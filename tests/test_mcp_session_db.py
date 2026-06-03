# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round-trip + isolation tests for src/mcp/session.py pin store.

Originally a Postgres integration suite (the pin lived in
``api_key_session_state``). #251 moved the pin INTO an in-memory per-(api_key_id,
mcp_session_id) store and DELETED every Postgres read/write path, so these tests
no longer touch Postgres — the ``postgres`` marker and the migration/seed fixture
were dropped. The behavioral intent is unchanged and still falsifiable:

  1. set_active_version_db + get_session_state round-trip
  2. set_active_profile_db + get_session_state round-trip
  3. same-session overwrite vs. different-session isolation (#251 clobber guard)
  4. 24h idle TTL — entry older than 24h → returns None (in-memory clock)
  5. per-key isolation — key A's pin invisible to key B

A booby-trapped ``_checkout_pg`` proves every assertion is served from memory
with 0 DB I/O (a regression to the old DB-backed path would raise).
"""

from unittest.mock import patch

_TEST_API_KEY_IDS = ["9901", "9902", "9903"]  # numeric strings — pass the #248 guard


def _clear_cache() -> None:
    from src.mcp.session import _cache, _cache_lock
    with _cache_lock:
        _cache.clear()


def _boom_checkout_pg(*_a, **_k):
    """Explode if the pin path touches Postgres — proves 0 DB I/O (#251 A-SCALE-1)."""
    raise AssertionError(
        "session pin path touched _checkout_pg — it must be pure in-memory (#251)"
    )


class _FakeClock:
    """Monotonic clock stub: call for ``now``, ``tick(dt)`` to advance."""

    def __init__(self, t0: float = 0.0) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# 1. set_active_version_db + get_session_state round-trip
# ---------------------------------------------------------------------------


class TestSetGetVersionRoundTrip:
    """A version written via set_active_version_db is readable via get_session_state."""

    def setup_method(self) -> None:
        _clear_cache()

    def test_version_round_trip(self) -> None:
        from src.mcp.session import get_session_state, set_active_version_db

        api_key_id = _TEST_API_KEY_IDS[0]
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert set_active_version_db(api_key_id, "17.0") is True
            state = get_session_state(api_key_id)

        assert state is not None
        assert state.api_key_id == api_key_id
        assert state.odoo_version == "17.0"

    def test_same_session_overwrites_value(self) -> None:
        """Two writes under the SAME mcp_session_id → last value wins (overwrite)."""
        from src.mcp.session import get_session_state, set_active_version_db

        api_key_id = _TEST_API_KEY_IDS[0]
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(api_key_id, "16.0", "same-sess")
            set_active_version_db(api_key_id, "17.0", "same-sess")
            state = get_session_state(api_key_id, "same-sess")

        assert state is not None
        assert state.odoo_version == "17.0", "same-session second write must overwrite first"

    def test_different_sessions_do_not_overwrite(self) -> None:
        """Two writes under the SAME key but DIFFERENT mcp_session_ids must NOT
        clobber — each keeps its own version. This is the #251 bug guard: a
        per-key store (pre-#251) would have lost the first version here."""
        from src.mcp.session import get_session_state, set_active_version_db

        api_key_id = _TEST_API_KEY_IDS[0]
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(api_key_id, "16.0", "sess-a")
            set_active_version_db(api_key_id, "17.0", "sess-b")
            state_a = get_session_state(api_key_id, "sess-a")
            state_b = get_session_state(api_key_id, "sess-b")

        assert state_a is not None and state_a.odoo_version == "16.0"
        assert state_b is not None and state_b.odoo_version == "17.0"


# ---------------------------------------------------------------------------
# 2. set_active_profile_db + get_session_state round-trip
# ---------------------------------------------------------------------------


class TestSetGetProfileRoundTrip:
    """profile_name persists in memory and is readable; None clears it."""

    def setup_method(self) -> None:
        _clear_cache()

    def test_profile_round_trip(self) -> None:
        from src.mcp.session import get_session_state, set_active_profile_db

        api_key_id = _TEST_API_KEY_IDS[1]
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert set_active_profile_db(api_key_id, "my-erp-prod") is True
            state = get_session_state(api_key_id)

        assert state is not None
        assert state.profile_name == "my-erp-prod"

    def test_profile_clear_sets_none(self) -> None:
        """set_active_profile_db(None) clears profile_name → None."""
        from src.mcp.session import get_session_state, set_active_profile_db

        api_key_id = _TEST_API_KEY_IDS[1]
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_profile_db(api_key_id, "to-be-cleared")
            set_active_profile_db(api_key_id, None)
            state = get_session_state(api_key_id)

        assert state is None or state.profile_name is None


# ---------------------------------------------------------------------------
# 3. 24h idle TTL
# ---------------------------------------------------------------------------


class TestSlidingTTL:
    """An entry older than the 24h idle TTL is treated as expired (None)."""

    def setup_method(self) -> None:
        _clear_cache()

    def test_stale_entry_returns_none(self) -> None:
        """Entry written 25h ago (in-memory clock) → get_session_state returns None."""
        from src.mcp.session import get_session_state, set_active_version_db

        api_key_id = _TEST_API_KEY_IDS[2]
        clock = _FakeClock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(api_key_id, "17.0", now_fn=clock)
            clock.tick(25 * 3600)  # advance past the 24h TTL
            state = get_session_state(api_key_id, now_fn=clock)

        assert state is None, "An entry older than 24h must be treated as expired (None)"

    def test_fresh_entry_within_24h_returns_state(self) -> None:
        from src.mcp.session import get_session_state, set_active_version_db

        api_key_id = _TEST_API_KEY_IDS[2]
        clock = _FakeClock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(api_key_id, "16.0", now_fn=clock)
            clock.tick(23 * 3600)  # still within the 24h window
            state = get_session_state(api_key_id, now_fn=clock)

        assert state is not None
        assert state.odoo_version == "16.0"


# ---------------------------------------------------------------------------
# 4. Per-key isolation
# ---------------------------------------------------------------------------


class TestKeyIsolation:
    """Key A's session state is invisible to key B."""

    def setup_method(self) -> None:
        _clear_cache()

    def test_different_keys_have_independent_state(self) -> None:
        from src.mcp.session import get_session_state, set_active_version_db

        key_a = _TEST_API_KEY_IDS[0]
        key_b = _TEST_API_KEY_IDS[1]
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(key_a, "17.0")
            state_a = get_session_state(key_a)
            state_b = get_session_state(key_b)

        assert state_a is not None
        assert state_a.odoo_version == "17.0"
        # key_b has no pin → None.
        assert state_b is None, "Key B must have no session state of its own"
