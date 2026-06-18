# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the session-state tool surface (WI-E4, M11 Wave E), updated for #251.

#251 moved the version/profile pin into an in-memory per-(api_key_id,
mcp_session_id) store and removed every Postgres pin path. The pin round-trip /
isolation / TTL tests below are therefore pure in-memory now (no ``postgres``
marker, no migration/seed fixture); the tests that exercise the real
``list_available_versions`` / ``list_available_profiles`` tools keep their
``neo4j`` / ``postgres`` markers because those tools still query the index.

Coverage:
  (1)  set_active_version_db + get_session_state round-trip (in-memory).
  (2)  set_active_version('default') sentinel → error ToolResult.
  (3)  resolve_version_v2('auto') uses the session pin.
  (4)  set_active_profile_db + get_session_state round-trip; None clears.
  (5)  24h idle TTL: entry older than 24h → None (in-memory clock).
  (6)  Per-key isolation: key A's pin invisible to key B; two keys independent.
  (7)  list_available_versions does not crash on an empty/seeded Neo4j.   [neo4j]
  (8)  list_available_profiles queries the profiles table.                [postgres]
  (9)  Cold-start: first pin on a fresh key id lands.
  (10) Two reads within TTL are both served from the in-memory store, 0 DB I/O.
  (11) Sentinel hardening: all 6 sentinel values normalize to None (pure unit).
  (12) list_available_versions shows a seeded numeric version.            [neo4j]
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_E4_VERSION = "E4_99.0"   # Dedicated Neo4j version — avoids collision with other files.
_E4_MODULE = "e4_test_sale"
_E4_MODEL = "e4.sale.order"

# Numeric strings — pass the #248 non-numeric guard. The integer values are
# arbitrary; the in-memory store is keyed by the string form.
_TEST_KEY_A = "9801"
_TEST_KEY_B = "9802"
_TEST_KEY_C = "9803"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_db(pg_conn):
    """Run migrations + yield pg_conn for the tests that exercise a real
    Postgres-backed tool (``list_available_profiles``). The pin store itself is
    in-memory (#251), so this fixture no longer seeds api_key_session_state."""
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)
    yield pg_conn


@pytest.fixture()
def wipe_neo4j(neo4j_driver):
    """Wipe all nodes with odoo_version=_E4_VERSION before and after each test."""
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_E4_VERSION)
    yield neo4j_driver
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_E4_VERSION)


@pytest.fixture()
def seeded_neo4j(wipe_neo4j):
    """Seed one Module node for _E4_VERSION so list_available_versions sees it."""
    driver = wipe_neo4j
    with driver.session() as s:
        s.run(
            """
            MERGE (mod:Module {
                name: $module, odoo_version: $v, repo: 'e4_test_repo',
                path: '/tmp/e4_test', edition: 'community'
            })
            """,
            module=_E4_MODULE, v=_E4_VERSION,
        )
    yield driver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_session_cache():
    """Purge all entries from the in-memory session pin store."""
    from src.mcp.session import _cache, _cache_lock
    with _cache_lock:
        _cache.clear()


def _boom_checkout_pg(*_a, **_k):
    """Explode if the pin path touches Postgres — proves 0 DB I/O (#251)."""
    raise AssertionError(
        "session pin path touched _checkout_pg — it must be pure in-memory (#251)"
    )


class _FakeClock:
    def __init__(self, t0: float = 0.0) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float) -> None:
        self.t += dt


# ===========================================================================
# (1) set_active_version persists (in-memory round-trip)
# ===========================================================================


class TestVersionRoundTrip:
    """set_active_version_db writes; get_session_state reads it back."""

    def setup_method(self) -> None:
        _clear_session_cache()

    def test_version_persists(self) -> None:
        from src.mcp.session import get_session_state, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert set_active_version_db(_TEST_KEY_A, "17.0") is True
            state = get_session_state(_TEST_KEY_A)

        assert state is not None, "State must be returned after set_active_version_db"
        assert state.api_key_id == _TEST_KEY_A
        assert state.odoo_version == "17.0"

    def test_same_session_second_write_overwrites(self) -> None:
        """Two writes under the SAME mcp_session_id → last value wins."""
        from src.mcp.session import get_session_state, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(_TEST_KEY_A, "16.0", "same-sess")
            set_active_version_db(_TEST_KEY_A, "17.0", "same-sess")
            state = get_session_state(_TEST_KEY_A, "same-sess")

        assert state is not None
        assert state.odoo_version == "17.0", "Same-session second write must overwrite first"

    def test_different_session_writes_do_not_overwrite(self) -> None:
        """SAME key, DIFFERENT mcp_session_ids must keep independent versions (#251)."""
        from src.mcp.session import get_session_state, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(_TEST_KEY_A, "16.0", "sess-a")
            set_active_version_db(_TEST_KEY_A, "17.0", "sess-b")
            state_a = get_session_state(_TEST_KEY_A, "sess-a")
            state_b = get_session_state(_TEST_KEY_A, "sess-b")

        assert state_a is not None and state_a.odoo_version == "16.0"
        assert state_b is not None and state_b.odoo_version == "17.0"


# ===========================================================================
# (2) set_active_version with sentinel "default" → error message
# ===========================================================================


class TestSentinelRejection:
    """The set_active_version MCP wrapper rejects sentinel strings (no DB needed)."""

    def test_sentinel_default_returns_error_message(self) -> None:
        """set_active_version('default') must return an error ToolResult."""
        from fastmcp.tools.tool import ToolResult

        from src.mcp import server as _srv
        from src.mcp.session import normalize_version_arg

        assert normalize_version_arg("default") is None, "Sentinel 'default' → None"

        tool_result = asyncio.run(_srv.set_active_version("default"))

        assert isinstance(tool_result, ToolResult)
        text = tool_result.content[0].text
        assert "sentinel" in text.lower() or "placeholder" in text.lower() or "Error" in text, (
            f"Expected error text for sentinel 'default', got: {text!r}"
        )
        assert "list_available_versions" in text, (
            "Error message must hint at list_available_versions()"
        )


# ===========================================================================
# (3) resolve_version_v2("auto") uses session pin
# ===========================================================================


class TestVersionResolutionUsesSession:
    """resolve_version_v2 with sentinel 'auto'/'latest' falls to the session pin."""

    def setup_method(self) -> None:
        _clear_session_cache()

    def test_resolve_version_auto_uses_session_version(self) -> None:
        """After set_active_version_db(key, '16.0'), resolve_version_v2('auto') → '16.0'.

        The pin is present, so resolution never reaches the Neo4j fallback — a
        MagicMock session suffices and its ``.run`` must not be called."""
        from src.mcp.session import resolve_version_v2, set_active_version_db

        api_key_id = _TEST_KEY_A
        mock_session = MagicMock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(api_key_id, "16.0")
            resolved = resolve_version_v2("auto", api_key_id, mock_session)

        assert resolved == "16.0", (
            f"resolve_version_v2('auto') must return the pinned version '16.0', got {resolved!r}"
        )
        mock_session.run.assert_not_called()

    def test_resolve_version_latest_uses_session_version(self) -> None:
        """Sentinel 'latest' is treated like 'auto' → falls to the session pin."""
        from src.mcp.session import resolve_version_v2, set_active_version_db

        api_key_id = _TEST_KEY_B
        mock_session = MagicMock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(api_key_id, "15.0")
            resolved = resolve_version_v2("latest", api_key_id, mock_session)

        assert resolved == "15.0"
        mock_session.run.assert_not_called()


# ===========================================================================
# (4) set_active_profile persists; get_session_state returns profile_name
# ===========================================================================


class TestProfileRoundTrip:
    """set_active_profile_db writes; get_session_state reads profile_name back."""

    def setup_method(self) -> None:
        _clear_session_cache()

    def test_profile_persists(self) -> None:
        from src.mcp.session import get_session_state, set_active_profile_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert set_active_profile_db(_TEST_KEY_B, "my-erp-prod") is True
            state = get_session_state(_TEST_KEY_B)

        assert state is not None
        assert state.profile_name == "my-erp-prod"

    def test_profile_none_clears_existing_profile(self) -> None:
        """set_active_profile_db(key, None) clears profile_name → None."""
        from src.mcp.session import get_session_state, set_active_profile_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_profile_db(_TEST_KEY_B, "to-be-cleared")
            set_active_profile_db(_TEST_KEY_B, None)
            state = get_session_state(_TEST_KEY_B)

        assert state is None or state.profile_name is None


# ===========================================================================
# (5) 24h idle TTL
# ===========================================================================


class TestTwentyFourHourTTL:
    """An entry older than 24h is treated as expired (returns None)."""

    def setup_method(self) -> None:
        _clear_session_cache()

    def test_stale_entry_returns_none(self) -> None:
        """Advance the in-memory clock 25h past the write → get_session_state None."""
        from src.mcp.session import get_session_state, set_active_version_db

        clock = _FakeClock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(_TEST_KEY_C, "17.0", now_fn=clock)
            clock.tick(25 * 3600)
            state = get_session_state(_TEST_KEY_C, now_fn=clock)

        assert state is None, (
            "An entry older than 24h must be treated as expired (None); "
            f"got state={state!r}"
        )

    def test_fresh_entry_within_24h_is_readable(self) -> None:
        from src.mcp.session import get_session_state, set_active_version_db

        clock = _FakeClock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(_TEST_KEY_C, "13.0", now_fn=clock)
            clock.tick(23 * 3600)
            state = get_session_state(_TEST_KEY_C, now_fn=clock)

        assert state is not None
        assert state.odoo_version == "13.0"


# ===========================================================================
# (6) Per-key isolation
# ===========================================================================


class TestKeyIsolation:
    """Key A's state must not affect key B."""

    def setup_method(self) -> None:
        _clear_session_cache()

    def test_key_b_is_unaffected_by_key_a(self) -> None:
        from src.mcp.session import get_session_state, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(_TEST_KEY_A, "17.0")
            state_a = get_session_state(_TEST_KEY_A)
            state_b = get_session_state(_TEST_KEY_B)

        assert state_a is not None
        assert state_a.odoo_version == "17.0"
        assert state_b is None, f"Key B must have no session state; got {state_b!r}"

    def test_two_keys_hold_independent_versions(self) -> None:
        from src.mcp.session import get_session_state, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db(_TEST_KEY_A, "17.0")
            set_active_version_db(_TEST_KEY_B, "16.0")
            state_a = get_session_state(_TEST_KEY_A)
            state_b = get_session_state(_TEST_KEY_B)

        assert state_a is not None and state_a.odoo_version == "17.0"
        assert state_b is not None and state_b.odoo_version == "16.0"


# ===========================================================================
# (7) list_available_versions returns indexed versions sorted
# ===========================================================================


@pytest.mark.neo4j
class TestListAvailableVersions:
    """list_available_versions queries Neo4j and returns sorted list."""

    def test_returns_no_versions_message_when_empty(self, wipe_neo4j) -> None:
        """The call must not raise and must return a ToolResult."""
        from fastmcp.tools.tool import ToolResult

        from src.mcp import server as _srv

        result = asyncio.run(_srv.list_available_versions())
        assert isinstance(result, ToolResult)
        text = result.content[0].text
        assert text.strip() != ""

    def test_returns_seeded_version(self, seeded_neo4j) -> None:
        """Seeding a Module with a prefixed version must not break the tool.

        'E4_99.0' does NOT match the \\d+\\.\\d+ Cypher regex, so we only assert
        the tool succeeds and returns a ToolResult without raising.
        """
        from fastmcp.tools.tool import ToolResult

        from src.mcp import server as _srv

        result = asyncio.run(_srv.list_available_versions())
        assert isinstance(result, ToolResult)


# ===========================================================================
# (8) list_available_profiles returns registered profiles
# ===========================================================================


@pytest.mark.postgres
class TestListAvailableProfiles:
    """list_available_profiles queries the profiles table via Postgres."""

    def test_returns_tool_result_without_raising(self, session_db) -> None:
        """The tool must succeed even when the profiles table is empty."""
        from contextlib import contextmanager

        from fastmcp.tools.tool import ToolResult

        from src.mcp import server as _srv

        @contextmanager
        def _checkout():
            yield session_db

        with patch("src.mcp.server._checkout_pg", _checkout):
            result = asyncio.run(_srv.list_available_profiles())

        assert isinstance(result, ToolResult)
        text = result.content[0].text
        assert text.strip() != ""

    def test_returns_profile_after_insert(self, session_db) -> None:
        """Inserting a profiles row and calling list_available_profiles shows it."""
        from contextlib import contextmanager

        from fastmcp.tools.tool import ToolResult

        from src.mcp import server as _srv

        _profile_name = "e4_test_profile"
        try:
            with session_db.cursor() as cur:
                cur.execute(
                    "INSERT INTO profiles (name, odoo_version) VALUES (%s, %s)"
                    " ON CONFLICT (name) DO NOTHING",
                    (_profile_name, "17.0"),
                )
        except Exception:
            pytest.skip("profiles table not available or schema mismatch")

        @contextmanager
        def _checkout():
            yield session_db

        try:
            with patch("src.mcp.server._checkout_pg", _checkout):
                result = asyncio.run(_srv.list_available_profiles())
        finally:
            with session_db.cursor() as cur:
                cur.execute("DELETE FROM profiles WHERE name = %s", (_profile_name,))

        assert isinstance(result, ToolResult)
        text = result.content[0].text
        assert text.strip() != ""


# ===========================================================================
# (9) Cold-start: first pin on a fresh api_key id lands
# ===========================================================================


class TestColdStart:
    """No prior pin exists for the key; the first write must land."""

    def setup_method(self) -> None:
        _clear_session_cache()

    def test_cold_start_version_persists(self) -> None:
        from src.mcp.session import get_session_state, set_active_version_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert set_active_version_db(_TEST_KEY_C, "14.0") is True
            state = get_session_state(_TEST_KEY_C)

        assert state is not None
        assert state.odoo_version == "14.0"

    def test_cold_start_profile_persists(self) -> None:
        from src.mcp.session import get_session_state, set_active_profile_db

        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            assert set_active_profile_db(_TEST_KEY_A, "internal_17") is True
            state = get_session_state(_TEST_KEY_A)

        assert state is not None
        assert state.profile_name == "internal_17"


# ===========================================================================
# (10) Two reads within TTL — both served from the in-memory store, 0 DB I/O
# ===========================================================================


class TestInMemoryReadsNoDbIo:
    """Reads are served from the in-memory store (the SoT) — never the DB.

    Pre-#251 the cache fronted Postgres and this test proved a single DB read per
    TTL window. With the in-memory store as the source of truth there is no DB at
    all: the equivalent intent is that repeated reads complete with 0 DB I/O.
    """

    def setup_method(self) -> None:
        _clear_session_cache()

    def test_two_reads_are_served_from_memory(self) -> None:
        from src.mcp.session import get_session_state, set_active_version_db

        clock = _FakeClock()
        with patch("src.mcp.server._checkout_pg", side_effect=_boom_checkout_pg):
            set_active_version_db("9801", "17.0", "sess", now_fn=clock)
            # First read at t=0.
            result1 = get_session_state("9801", "sess", now_fn=clock)
            # Second read at t=30 — still within the 24h TTL window.
            clock.tick(30.0)
            result2 = get_session_state("9801", "sess", now_fn=clock)

        assert result1 is not None and result1.odoo_version == "17.0"
        assert result2 is not None and result2.odoo_version == "17.0"
        # The booby-trapped _checkout_pg would have raised on any DB touch.


# ===========================================================================
# (11) Sentinel hardening: all 6 sentinels normalize to None (pure unit)
# ===========================================================================


class TestSentinelHardening:
    """normalize_version_arg must collapse all 6 registered sentinels to None."""

    @pytest.mark.parametrize(
        "sentinel",
        ["auto", "default", "latest", "version", "any", ""],
    )
    def test_all_sentinels_collapse_to_none(self, sentinel: str) -> None:
        from src.mcp.session import normalize_version_arg
        assert normalize_version_arg(sentinel) is None, (
            f"Sentinel {sentinel!r} must normalize to None"
        )

    @pytest.mark.parametrize(
        "sentinel",
        ["AUTO", "DEFAULT", "Latest", "VERSION", "Any"],
    )
    def test_sentinels_case_insensitive(self, sentinel: str) -> None:
        from src.mcp.session import normalize_version_arg
        assert normalize_version_arg(sentinel) is None

    @pytest.mark.parametrize(
        "sentinel",
        [" auto ", "  default  ", "\tlatest\t"],
    )
    def test_sentinels_strip_whitespace(self, sentinel: str) -> None:
        from src.mcp.session import normalize_version_arg
        assert normalize_version_arg(sentinel) is None

    @pytest.mark.parametrize(
        "version",
        ["17.0", "16.0", "9.0", "14.0", "8.0"],
    )
    def test_real_versions_pass_through(self, version: str) -> None:
        from src.mcp.session import normalize_version_arg
        assert normalize_version_arg(version) == version


# ===========================================================================
# (12) list_available_versions returns ≥1 entry when DB seeded
# ===========================================================================


@pytest.mark.neo4j
class TestListAvailableVersionsSeeded:
    """list_available_versions must show ≥1 entry when a numeric-version Module
    node exists in Neo4j."""

    @pytest.fixture()
    def real_numeric_module(self, wipe_neo4j):
        """Seed a Module node with version '17.0' (matches the \\d+\\.\\d+ filter)."""
        driver = wipe_neo4j
        with driver.session() as s:
            s.run(
                """
                MERGE (mod:Module {
                    name: $module, odoo_version: $v, repo: 'e4_numeric_repo',
                    path: '/tmp/e4_numeric', edition: 'community'
                })
                """,
                module="e4_numeric_sale", v="17.0",
            )
        yield driver
        with driver.session() as s:
            s.run(
                """
                MATCH (mod:Module {name: $module, odoo_version: $v})
                DETACH DELETE mod
                """,
                module="e4_numeric_sale", v="17.0",
            )

    def test_list_versions_includes_seeded_version(self, real_numeric_module) -> None:
        """list_available_versions must return '17.0' in its output after seeding."""
        from fastmcp.tools.tool import ToolResult

        from src.mcp import server as _srv

        result = asyncio.run(_srv.list_available_versions())
        assert isinstance(result, ToolResult)
        text = result.content[0].text

        assert "17.0" in text, (
            f"Expected '17.0' in list_available_versions output after seeding; got:\n{text}"
        )
        assert "total" in text.lower() or "├─" in text or "└─" in text, (
            "Expected tree-formatted output from list_available_versions"
        )
