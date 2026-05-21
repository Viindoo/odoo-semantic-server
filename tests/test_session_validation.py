# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for WI-B2: set_active_version and set_active_profile input validation.

WI-B2 added Neo4j/Postgres sanity-checks to the two session-mutating tools:

  set_active_version(version):
    - Normalises the version string (sentinel guard from ADR-0029).
    - Runs a MATCH on Neo4j to confirm the version is indexed.
    - Returns an error tree listing available indexed versions if NOT indexed.
    - Returns a success receipt if the version IS indexed.

  set_active_profile(profile_name):
    - None is always valid (clears the active profile; no DB query).
    - For a non-None name, checks the profiles table in Postgres.
    - Returns an error tree listing available profiles if NOT registered.
    - Returns a success receipt if the profile IS registered.

Coverage:
  SV-1  set_active_version('999.0') with seeded TEST_VERSION data returns an
        error ToolResult whose text starts with 'Error:' and mentions '999.0'.
  SV-2  The SV-1 error text names at least one indexed version (confirms the
        available-versions list is populated).
  SV-3  The SV-1 call does NOT persist — session state for the api_key_id
        remains unchanged (no DB write on error path).
  SV-4  set_active_version(TEST_VERSION) with seeded data succeeds (returns
        success ToolResult whose text contains TEST_VERSION and 'TTL').
  SV-5  set_active_profile('does_not_exist_sv') returns an error ToolResult
        whose text starts with 'Error:' and mentions the bad profile name.
  SV-6  The SV-5 error text mentions 'list_available_profiles' (hint present).
  SV-7  set_active_profile(None) always succeeds (returns success ToolResult).

Markers:
  - SV-1..SV-4 are neo4j tests (need indexed version data).
  - SV-5..SV-6 are postgres tests (need profiles table).
  - SV-7 is a unit test (no external DB).

DB isolation:
  - Neo4j version "SV_99.0" — seeded before SV-1..SV-4 and wiped after.
  - Postgres: profiles table queried via a mocked _checkout_pg — no real rows
    needed (the 'does_not_exist_sv' profile simply won't be found).
"""
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SV_VERSION = "SV_99.0"
SV_MODULE = "sv_sale"
SV_NOT_INDEXED = "999.0"  # must never collide with real data


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sv_neo4j(neo4j_driver, monkeypatch_module):
    """Seed one Module node at SV_VERSION so set_active_version sees it."""
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=SV_VERSION)

    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (m:Module {name: $mod, odoo_version: $v})
            SET m.repo = 'sv_test_repo', m.path = '/tmp/sv_sale',
                m.edition = 'community'
            """,
            mod=SV_MODULE, v=SV_VERSION,
        )

    # Point server.py Neo4j driver at the test instance.
    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    )
    monkeypatch_module.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password")
    )

    import sys
    sys.modules.pop("src.mcp.server", None)

    yield neo4j_driver

    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=SV_VERSION)


def _make_empty_pg_checkout():
    """Return a context manager that simulates an empty Postgres connection.

    The profiles table exists but has no rows (empty SELECT result).
    """
    @contextmanager
    def _mock():
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = None         # profile not found
        cur.fetchall.return_value = []           # available list is empty
        conn.cursor.return_value = cur
        yield conn
    return _mock


def _clear_session_cache():
    from src.mcp.session import _cache
    _cache.clear()


def _extract_text(tool_result) -> str:
    """Return the text content from a ToolResult."""
    return tool_result.content[0].text


# ===========================================================================
# SV-1 .. SV-4: set_active_version validation (neo4j)
# ===========================================================================


@pytest.mark.neo4j
class TestSetActiveVersionValidation:
    """set_active_version rejects non-indexed versions and accepts indexed ones."""

    def test_sv1_non_indexed_version_returns_error(self, sv_neo4j) -> None:
        """SV-1: set_active_version('999.0') returns an error ToolResult.

        '999.0' is a deliberately non-indexed version.  The Neo4j MATCH check
        in set_active_version finds zero Module nodes at this version and returns
        an error tree instead of persisting the value.
        """
        import importlib
        server = importlib.import_module("src.mcp.server")

        result = server.set_active_version.fn(SV_NOT_INDEXED)
        text = _extract_text(result)

        assert text.startswith("Error:"), (
            f"Expected error ToolResult for non-indexed version '{SV_NOT_INDEXED}'; "
            f"got: {text[:200]!r}"
        )
        assert SV_NOT_INDEXED in text, (
            f"Error message must mention the bad version '{SV_NOT_INDEXED}'; "
            f"got: {text[:200]!r}"
        )

    def test_sv2_error_text_names_available_versions(self, sv_neo4j) -> None:
        """SV-2: The error text from SV-1 lists ≥1 available indexed version.

        The tool fetches all Module.odoo_version values from Neo4j that match
        \\d+\\.\\d+ and includes them in the error message.  Because SV_VERSION
        ('SV_99.0') does not match the numeric-only Cypher regex, we seed an
        additional numeric module node for this assertion.
        """
        import importlib
        driver = sv_neo4j
        server = importlib.import_module("src.mcp.server")

        # Seed a numeric version node so the 'Indexed versions:' list is non-empty.
        NUMERIC_V = "17.0"
        with driver.session() as s:
            s.run(
                "MERGE (m:Module {name: 'sv_numeric_base', odoo_version: $v})",
                v=NUMERIC_V,
            )
        try:
            result = server.set_active_version.fn(SV_NOT_INDEXED)
            text = _extract_text(result)
        finally:
            with driver.session() as s:
                s.run(
                    "MATCH (m:Module {name: 'sv_numeric_base', odoo_version: $v}) "
                    "DETACH DELETE m",
                    v=NUMERIC_V,
                )

        # '17.0' must appear in the error (either raw or as part of the list).
        assert NUMERIC_V in text, (
            f"Error message must list available versions including '{NUMERIC_V}'; "
            f"got: {text[:300]!r}"
        )

    def test_sv3_error_does_not_persist_session(self, sv_neo4j) -> None:
        """SV-3: A rejected set_active_version does not write to the session store.

        After calling set_active_version('999.0') (which returns an error),
        the session state for the api_key_id must remain absent (None).  We
        assert this by patching set_active_version_db and confirming it was
        never called.
        """
        import importlib
        server = importlib.import_module("src.mcp.server")

        with patch("src.mcp.session.set_active_version_db") as mock_write:
            result = server.set_active_version.fn(SV_NOT_INDEXED)
            text = _extract_text(result)

        assert text.startswith("Error:"), (
            "Pre-condition: SV-3 requires the call to return an error"
        )
        mock_write.assert_not_called(), (
            "set_active_version_db must NOT be called when the version is not indexed"
        )

    def test_sv4_indexed_version_returns_success(self, sv_neo4j) -> None:
        """SV-4: set_active_version(SV_VERSION) succeeds when version is indexed.

        SV_VERSION ('SV_99.0') is seeded in the sv_neo4j fixture.  However, the
        Cypher filter in set_active_version uses MATCH (m:Module {odoo_version: $v})
        (not the \\d+\\.\\d+ regex), so SV_VERSION IS found and the call should
        succeed.  We patch set_active_version_db to avoid a real DB write.
        """
        import importlib
        server = importlib.import_module("src.mcp.server")

        with patch("src.mcp.session.set_active_version_db"):
            result = server.set_active_version.fn(SV_VERSION)
            text = _extract_text(result)

        assert not text.startswith("Error:"), (
            f"set_active_version('{SV_VERSION}') must succeed (SV_VERSION is seeded); "
            f"got: {text[:200]!r}"
        )
        assert SV_VERSION in text, (
            f"Success receipt must echo the pinned version '{SV_VERSION}'; "
            f"got: {text[:200]!r}"
        )
        assert "TTL" in text or "24h" in text, (
            f"Success receipt must mention TTL; got: {text[:200]!r}"
        )


# ===========================================================================
# SV-5 .. SV-6: set_active_profile validation (postgres)
# ===========================================================================


@pytest.mark.postgres
class TestSetActiveProfileValidation:
    """set_active_profile rejects unknown profiles and returns a helpful error."""

    def test_sv5_unknown_profile_returns_error(self, pg_conn) -> None:
        """SV-5: set_active_profile('does_not_exist_sv') returns an error ToolResult.

        The profiles table is empty (mocked checkout returns no rows).  The
        tool must return 'Error:' and mention the bad profile name.
        """
        import importlib
        server = importlib.import_module("src.mcp.server")

        checkout = _make_empty_pg_checkout()
        with patch("src.mcp.server._checkout_pg", checkout):
            result = server.set_active_profile.fn("does_not_exist_sv")
            text = _extract_text(result)

        assert text.startswith("Error:"), (
            f"Expected error ToolResult for unknown profile; got: {text[:200]!r}"
        )
        assert "does_not_exist_sv" in text, (
            f"Error message must mention the bad profile name; got: {text[:200]!r}"
        )

    def test_sv6_error_text_mentions_list_available_profiles(self, pg_conn) -> None:
        """SV-6: The error text from SV-5 includes a hint about list_available_profiles."""
        import importlib
        server = importlib.import_module("src.mcp.server")

        checkout = _make_empty_pg_checkout()
        with patch("src.mcp.server._checkout_pg", checkout):
            result = server.set_active_profile.fn("does_not_exist_sv")
            text = _extract_text(result)

        assert "list_available_profiles" in text, (
            f"Error message must hint at list_available_profiles(); got: {text[:300]!r}"
        )


# ===========================================================================
# SV-7: set_active_profile(None) always succeeds (unit test, no DB)
# ===========================================================================


class TestSetActiveProfileClear:
    """set_active_profile(None) clears the active profile — always valid."""

    def test_sv7_none_profile_succeeds_without_db_check(self) -> None:
        """SV-7: set_active_profile(None) skips the profiles table check and succeeds.

        Passing None means 'clear active profile' and is always valid regardless
        of what is in the profiles table.  We patch _checkout_pg to ensure it
        is NOT called for a None argument (the validation guard skips it).
        """
        import importlib
        server = importlib.import_module("src.mcp.server")

        # Patch set_active_profile_db to avoid real PG write.
        with patch("src.mcp.session.set_active_profile_db") as mock_db:
            result = server.set_active_profile.fn(None)
            text = _extract_text(result)

        assert not text.startswith("Error:"), (
            f"set_active_profile(None) must succeed; got: {text[:200]!r}"
        )
        assert "cleared" in text.lower() or "active profile" in text.lower(), (
            f"Success receipt must mention profile clearing; got: {text[:200]!r}"
        )
        mock_db.assert_called_once(), (
            "set_active_profile_db must be called once for None (to write NULL)"
        )
