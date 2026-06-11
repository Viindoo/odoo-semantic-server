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


# ===========================================================================
# #274 — multi-version / multi-actor concurrency guarantee
#
# Business rule: an EXPLICIT odoo_version / profile_name is Tier-1 and ALWAYS
# wins over any session pin, which makes the resolver race-free by construction
# for (R1) multi-version-in-one-flow and (R2) concurrent sub-actors sharing one
# mcp-session-id. The pin is single-actor convenience only; a deliberate 'auto'
# with NO resolvable pin AND an empty index must fail loud, never silent-default.
# ===========================================================================


class TestExplicitVersionAlwaysOverridesPin:
    """R1 + R2 (version): an explicit, concrete version is never overridden by a
    pin — proven against a session that is pinned to a *different* version."""

    def test_explicit_version_beats_a_conflicting_pin(self) -> None:
        """R1: session 's' is pinned to 19.0, but an explicit '17.0'/'18.0'/'16.0'
        on the same session resolves to the explicit value — the pin never wins."""
        from src.mcp.session import resolve_version_v2, set_active_version_db

        mock_session = MagicMock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(KEY, "19.0", "s")  # pin one version
            for explicit in ("17.0", "18.0", "16.0"):
                assert resolve_version_v2(explicit, KEY, mock_session, "s") == explicit, (
                    f"explicit {explicit!r} must win over the 19.0 pin (Tier-1 always "
                    "wins) — this is the property that makes multi-version flows "
                    "race-free (#274 R1)"
                )
        # Explicit never needs the Neo4j fallback.
        mock_session.run.assert_not_called()

    def test_concurrent_actors_sharing_one_session_id_each_get_their_explicit_version(
        self,
    ) -> None:
        """R2: 3 sub-actors share ONE mcp_session_id under one key and pin in
        sequence 19 -> 17 -> 18 ('auto' last-write-wins would yield 18 for all).
        Each actor that passes its OWN explicit version still resolves correctly,
        proving explicit is immune to the shared-slot clobber."""
        from src.mcp.session import (
            get_session_state,
            resolve_version_v2,
            set_active_version_db,
        )

        shared_sid = "shared-parent-sid"
        mock_session = MagicMock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            # Three actors pin sequentially on the SAME (key, session) slot.
            set_active_version_db(KEY, "19.0", shared_sid)
            set_active_version_db(KEY, "17.0", shared_sid)
            set_active_version_db(KEY, "18.0", shared_sid)

            # The shared pin is genuinely last-write-wins (18.0) — this is what an
            # 'auto' caller would (ambiguously) get. That is the hazard explicit avoids.
            pinned = get_session_state(KEY, shared_sid)
            assert pinned is not None and pinned.odoo_version == "18.0"
            assert resolve_version_v2("auto", KEY, mock_session, shared_sid) == "18.0"

            # But each actor passing its OWN explicit version is immune to the clobber.
            assert resolve_version_v2("19.0", KEY, mock_session, shared_sid) == "19.0"
            assert resolve_version_v2("17.0", KEY, mock_session, shared_sid) == "17.0"
            assert resolve_version_v2("18.0", KEY, mock_session, shared_sid) == "18.0"
        mock_session.run.assert_not_called()


class TestExplicitProfileAlwaysOverridesPin:
    """R1 + R2 (profile): an explicit profile_name is never overridden by a pin."""

    def test_explicit_profile_beats_a_conflicting_pin(self) -> None:
        from src.mcp.session import resolve_profile_v2, set_active_profile_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_profile_db(KEY, "acme", "s")  # pin one profile
            for explicit in ("globex", "initech", "umbrella"):
                assert resolve_profile_v2(explicit, KEY, None, "s") == explicit, (
                    f"explicit profile {explicit!r} must win over the 'acme' pin "
                    "(Tier-1 always wins) — #274 R1/R2 profile parity"
                )

    def test_concurrent_actors_sharing_one_session_id_each_get_their_explicit_profile(
        self,
    ) -> None:
        from src.mcp.session import (
            get_session_state,
            resolve_profile_v2,
            set_active_profile_db,
        )

        shared_sid = "shared-parent-sid"
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_profile_db(KEY, "acme", shared_sid)
            set_active_profile_db(KEY, "globex", shared_sid)
            set_active_profile_db(KEY, "initech", shared_sid)

            # Shared slot is last-write-wins (initech) — what a profile-omitting
            # ('auto') caller would ambiguously inherit.
            pinned = get_session_state(KEY, shared_sid)
            assert pinned is not None and pinned.profile_name == "initech"
            assert resolve_profile_v2(None, KEY, None, shared_sid) == "initech"

            # Each actor passing its OWN explicit profile is immune to the clobber.
            assert resolve_profile_v2("acme", KEY, None, shared_sid) == "acme"
            assert resolve_profile_v2("globex", KEY, None, shared_sid) == "globex"
            assert resolve_profile_v2("initech", KEY, None, shared_sid) == "initech"


class TestDistinctSessionsResolveExplicitIndependently:
    """(c) Two DIFFERENT mcp_session_ids under one key never clobber, and an
    explicit version on one does not leak into the other's pin."""

    def test_two_sessions_explicit_and_pin_do_not_cross(self) -> None:
        from src.mcp.session import resolve_version_v2, set_active_version_db

        mock_session = MagicMock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(KEY, "16.0", "sess-a")
            set_active_version_db(KEY, "19.0", "sess-b")

            # 'auto' honours each session's own pin (no cross-session clobber).
            assert resolve_version_v2("auto", KEY, mock_session, "sess-a") == "16.0"
            assert resolve_version_v2("auto", KEY, mock_session, "sess-b") == "19.0"

            # An explicit on sess-a wins locally and never mutates sess-b's pin.
            assert resolve_version_v2("17.0", KEY, mock_session, "sess-a") == "17.0"
            assert resolve_version_v2("auto", KEY, mock_session, "sess-b") == "19.0"
        mock_session.run.assert_not_called()


class TestSentinelNoPinEmptyIndexFailsLoud:
    """(d / R-A2) A sentinel ('auto'/'latest'/'') with NO resolvable pin AND an
    empty index must FAIL LOUD with an actionable message that names the explicit
    odoo_version= contract — never a silent default.

    RED-before / GREEN-after R-A2: before the #274 hardening the empty-index
    message did not name the per-call contract; the assertion below pins the
    business guarantee (refuse-to-invent-a-default + tell the caller exactly what
    to do) rather than a brittle exact string."""

    @pytest.mark.parametrize("sentinel", ["auto", "latest", "", "any"])
    def test_version_sentinel_no_pin_empty_index_raises_actionable(
        self, sentinel: str
    ) -> None:
        from src.mcp.session import resolve_version_v2

        mock_session = MagicMock()
        # No pin for this (key, session); _latest_version returns None (empty index).
        with patch("src.mcp.server._latest_version", return_value=None):
            with pytest.raises(ValueError) as exc:
                resolve_version_v2(sentinel, KEY, mock_session, "s")

        msg = str(exc.value).lower()
        assert "odoo_version" in msg, (
            "fail-loud message must name the explicit odoo_version= contract so the "
            "caller knows how to recover — not a silent default (#274 R-A2)"
        )
        assert "explicit" in msg, (
            "message must steer the caller to pass an EXPLICIT version (the Tier-1 "
            "value that always wins) — #274 R-A2"
        )

    def test_pinned_auto_still_resolves_when_index_present(self) -> None:
        """Guardrail: the fail-loud path must NOT break the single-actor 'auto'
        convenience. With a pin present, 'auto' resolves to the pin and never even
        consults the (empty-or-not) index."""
        from src.mcp.session import resolve_version_v2, set_active_version_db

        mock_session = MagicMock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(KEY, "17.0", "s")
            with patch("src.mcp.server._latest_version", return_value=None) as lv:
                assert resolve_version_v2("auto", KEY, mock_session, "s") == "17.0"
            lv.assert_not_called()  # pin short-circuits before the fallback

    def test_profile_sentinel_no_pin_resolves_to_none_not_a_default(self) -> None:
        """Profile parity for (d): with no explicit profile and no pin, the
        resolver returns None ('no opinion — caller default applies'), never a
        silently-invented profile name. None is the correct fail-safe here because
        the ADR-0034 tenant choke (server.py) decides the actual scope; inventing a
        profile here could *widen* a tenant's view, which must never happen."""
        from src.mcp.session import resolve_profile_v2

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert resolve_profile_v2(None, KEY, None, "s") is None
            assert resolve_profile_v2("", KEY, None, "s") is None
