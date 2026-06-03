# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issue #251 — per-(api_key_id, mcp_session_id) pin clobber regressions.

THE bug: the sticky MCP pin used to be keyed by ``api_key_id`` alone, so two
concurrent Claude Code sessions on ONE API key — each pinned to a different Odoo
version — clobbered each other (last write won; ``auto`` resolved to the one
shared pin). #251 re-keys the in-memory pin store by ``(api_key_id,
mcp_session_id)`` so each live session keeps its own version AND profile.

Every test here is named after the business rule it protects and is falsifiable:
run against the pre-Wave-1 (per-key) store, the clobber regressions go RED
(session B's write would overwrite session A's pin). They are pure in-memory —
no marker — and a booby-trapped ``_checkout_pg`` proves 0 DB I/O on the hot path.
"""

from unittest.mock import MagicMock, patch

import pytest

KEY = "501"  # numeric string — passes the #248 non-numeric guard
KEY2 = "502"


def _clear_cache() -> None:
    from src.mcp.session import _cache, _cache_lock
    with _cache_lock:
        _cache.clear()


def _boom_checkout_pg(*_a, **_k):
    raise AssertionError(
        "session pin path touched _checkout_pg — it must be pure in-memory (#251)"
    )


@pytest.fixture(autouse=True)
def _fresh_store():
    _clear_cache()
    yield
    _clear_cache()


# ---------------------------------------------------------------------------
# A0 — the version clobber regression (RED on the pre-#251 per-key store)
# ---------------------------------------------------------------------------


class TestTwoSessionsOneKeyKeepOwnVersion:
    """Two sessions under one API key must each resolve their OWN pinned version."""

    def test_distinct_sessions_keep_distinct_versions(self) -> None:
        from src.mcp.session import get_session_state, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert set_active_version_db(KEY, "16.0", "aaa") is True
            assert set_active_version_db(KEY, "19.0", "bbb") is True

            sa = get_session_state(KEY, "aaa")
            sb = get_session_state(KEY, "bbb")

        assert sa is not None and sa.odoo_version == "16.0", (
            "session 'aaa' must keep 16.0 — a per-key store would have clobbered it to 19.0 (#251)"
        )
        assert sb is not None and sb.odoo_version == "19.0"

    def test_auto_resolves_per_session(self) -> None:
        """resolve_version_v2('auto') honours the calling session's own pin."""
        from src.mcp.session import resolve_version_v2, set_active_version_db

        mock_session = MagicMock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(KEY, "16.0", "aaa")
            set_active_version_db(KEY, "19.0", "bbb")
            assert resolve_version_v2("auto", KEY, mock_session, "aaa") == "16.0"
            assert resolve_version_v2("auto", KEY, mock_session, "bbb") == "19.0"
        mock_session.run.assert_not_called()


# ---------------------------------------------------------------------------
# Profile clobber regression — same shape on profile_name
# ---------------------------------------------------------------------------


class TestTwoSessionsOneKeyKeepOwnProfile:
    """Two sessions under one API key must each keep their OWN pinned profile."""

    def test_distinct_sessions_keep_distinct_profiles(self) -> None:
        from src.mcp.session import get_session_state, set_active_profile_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert set_active_profile_db(KEY, "acme", "aaa") is True
            assert set_active_profile_db(KEY, "globex", "bbb") is True

            sa = get_session_state(KEY, "aaa")
            sb = get_session_state(KEY, "bbb")

        assert sa is not None and sa.profile_name == "acme", (
            "session 'aaa' must keep 'acme' — a per-key store would have clobbered it (#251)"
        )
        assert sb is not None and sb.profile_name == "globex"

    def test_resolve_profile_per_session(self) -> None:
        from src.mcp.session import resolve_profile_v2, set_active_profile_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_profile_db(KEY, "acme", "aaa")
            set_active_profile_db(KEY, "globex", "bbb")
            assert resolve_profile_v2(None, KEY, None, "aaa") == "acme"
            assert resolve_profile_v2(None, KEY, None, "bbb") == "globex"


# ---------------------------------------------------------------------------
# version + profile coexist under one (key, session) — no cross-field clobber
# ---------------------------------------------------------------------------


class TestVersionProfileCoexist:
    """Setting version then profile (and vice-versa) must NOT clobber the other."""

    def test_version_then_profile(self) -> None:
        from src.mcp.session import get_session_state, set_active_profile_db, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(KEY, "17.0", "s")
            set_active_profile_db(KEY, "acme", "s")
            state = get_session_state(KEY, "s")

        assert state is not None
        assert state.odoo_version == "17.0"
        assert state.profile_name == "acme"

    def test_profile_then_version(self) -> None:
        from src.mcp.session import get_session_state, set_active_profile_db, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_profile_db(KEY, "acme", "s")
            set_active_version_db(KEY, "17.0", "s")
            state = get_session_state(KEY, "s")

        assert state is not None
        assert state.odoo_version == "17.0"
        assert state.profile_name == "acme"

    def test_coexisting_pins_isolated_across_two_sessions(self) -> None:
        """version+profile pinned in session A do not bleed into session B."""
        from src.mcp.session import get_session_state, set_active_profile_db, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(KEY, "17.0", "a")
            set_active_profile_db(KEY, "acme", "a")
            set_active_version_db(KEY, "18.0", "b")
            set_active_profile_db(KEY, "globex", "b")

            sa = get_session_state(KEY, "a")
            sb = get_session_state(KEY, "b")

        assert (sa.odoo_version, sa.profile_name) == ("17.0", "acme")
        assert (sb.odoo_version, sb.profile_name) == ("18.0", "globex")


# ---------------------------------------------------------------------------
# A-SCALE-1 at the RESOLVER level — version/profile resolve do 0 DB I/O
# ---------------------------------------------------------------------------


class TestResolverNoDbIo:
    """resolve_version_v2 / resolve_profile_v2 / get_session_state never touch
    Postgres or the Neo4j driver when a pin exists (hot-path scale guarantee)."""

    def test_resolve_paths_perform_no_db_io(self) -> None:
        from src.mcp.session import (
            get_session_state,
            resolve_profile_v2,
            resolve_version_v2,
            set_active_profile_db,
            set_active_version_db,
        )

        mock_session = MagicMock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(KEY, "17.0", "s")
            set_active_profile_db(KEY, "acme", "s")

            assert resolve_version_v2("auto", KEY, mock_session, "s") == "17.0"
            assert resolve_profile_v2(None, KEY, None, "s") == "acme"
            assert get_session_state(KEY, "s") is not None

        # Neo4j driver must not be queried (pin present → no fallback).
        mock_session.run.assert_not_called()


# ---------------------------------------------------------------------------
# #248 loud-fail preserved — non-numeric api_key_id never silently succeeds
# ---------------------------------------------------------------------------


class TestNonNumericKeyLoudFail:
    """A lost / non-numeric api_key_id must fail loud at the setter (returns
    False, stores nothing) and yield no pin at the resolver."""

    @pytest.mark.parametrize("bad_key", ["default", "not-an-int", ""])
    def test_set_version_returns_false_no_store(self, bad_key: str) -> None:
        from src.mcp.session import _cache, _cache_lock, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert set_active_version_db(bad_key, "17.0", "s") is False
        with _cache_lock:
            assert _cache == {}

    @pytest.mark.parametrize("bad_key", ["default", "not-an-int", ""])
    def test_set_profile_returns_false_no_store(self, bad_key: str) -> None:
        from src.mcp.session import _cache, _cache_lock, set_active_profile_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert set_active_profile_db(bad_key, "acme", "s") is False
        with _cache_lock:
            assert _cache == {}

    def test_resolver_falls_to_latest_for_non_numeric_key(self) -> None:
        """With a non-numeric key there is no pin, so 'auto' must fall through to
        the Neo4j latest-version fallback rather than silently returning a stale
        pin (which a silent-success bug would have produced)."""
        from src.mcp.session import resolve_version_v2

        mock_session = MagicMock()
        with patch("src.mcp.server._latest_version", return_value="19.0") as lv:
            assert resolve_version_v2("auto", "default", mock_session, "s") == "19.0"
        lv.assert_called_once_with(mock_session)
