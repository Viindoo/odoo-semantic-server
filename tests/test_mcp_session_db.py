# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB integration tests for src/mcp/session.py — requires live PostgreSQL.

Marker: pytest.mark.postgres

Covers AC-E2-6:
  1. set_active_version_db + get_session_state round-trip
  2. set_active_profile_db + get_session_state round-trip
  3. 24h sliding TTL — row updated_at mocked to >24h → returns None
  4. Tenant isolation — key A's state invisible to key B

These tests use the pg_conn session fixture from conftest.py (autocommit=True).
The api_key_session_state table is created by running migrations via run_migrations().
After each test the test rows are cleaned up by deleting the inserted api_key_ids.
"""

import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_API_KEY_IDS = [9901, 9902, 9903]  # Fake integer IDs that won't conflict


@pytest.fixture()
def session_db(pg_conn):
    """Run migrations, init pool, seed api_keys parents, yield pg_conn.

    api_key_session_state.api_key_id is a FK to api_keys(id) ON DELETE CASCADE
    (migration 0005, ADR-0029).  To exercise UPSERT against that table we need
    the parent api_keys rows to exist with the test IDs, otherwise every INSERT
    raises ForeignKeyViolation.

    Why not insert into api_key_session_state directly with hand-rolled SQL?
    Because the production code under test (`set_active_version_db`) does an
    UPSERT into api_key_session_state — exercising the real path requires the
    real FK parent.
    """
    import os

    from src.db.migrate import run_migrations
    from src.db.pg import get_pool, init_pool

    test_dsn = os.getenv(
        "PG_TEST_DSN",
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
    )

    # Ensure pool is initialised (may already be from conftest pg_conn fixture)
    try:
        get_pool()
    except RuntimeError:
        init_pool(test_dsn, min_conn=1, max_conn=3)

    # Ensure schema exists (creates api_keys + api_key_session_state with FK).
    run_migrations(pg_conn)

    # Seed api_keys parent rows for the synthetic test IDs.  api_keys.id is
    # SERIAL so we use OVERRIDING SYSTEM VALUE to plant our chosen PKs.  Use
    # ON CONFLICT to make the seed re-entrant across the function-scoped
    # fixture lifecycle.
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (id, name, key_hash, key_prefix) "
            "OVERRIDING SYSTEM VALUE VALUES "
            "(%s, 'session-db-test-1', 'hash-9901', 'osm_t1'), "
            "(%s, 'session-db-test-2', 'hash-9902', 'osm_t2'), "
            "(%s, 'session-db-test-3', 'hash-9903', 'osm_t3') "
            "ON CONFLICT (id) DO NOTHING",
            tuple(_TEST_API_KEY_IDS),
        )

    # Pre-clean: this fixture does NOT depend on `clean_pg`, so the
    # api_key_session_state table is not wiped automatically between tests.
    # Delete only OUR test rows to keep per-test state isolated.
    with pg_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_key_session_state WHERE api_key_id = ANY(%s)",
            (_TEST_API_KEY_IDS,),
        )

    yield pg_conn

    # Post-clean: symmetric with pre-clean.
    with pg_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_key_session_state WHERE api_key_id = ANY(%s)",
            (_TEST_API_KEY_IDS,),
        )


# ---------------------------------------------------------------------------
# Helper to inject PG pool env into session module (patch _checkout_pg)
# ---------------------------------------------------------------------------

def _make_checkout_pg(conn):
    """Return a context manager that yields *conn* directly, bypassing the pool."""
    from contextlib import contextmanager

    @contextmanager
    def _mock_checkout_pg():
        yield conn

    return _mock_checkout_pg


# ---------------------------------------------------------------------------
# Test 1: set_active_version_db + get_session_state round-trip
# ---------------------------------------------------------------------------


class TestSetGetVersionRoundTrip:
    """AC-E2-6 test 1 — version persists and is readable via get_session_state."""

    def test_version_round_trip(self, session_db) -> None:
        from unittest.mock import patch

        from src.mcp.session import _cache, get_session_state, set_active_version_db

        api_key_id = str(_TEST_API_KEY_IDS[0])
        _cache.clear()

        checkout = _make_checkout_pg(session_db)

        with patch("src.mcp.server._checkout_pg", checkout):
            set_active_version_db(api_key_id, "17.0")
            # Cache was invalidated by set_active_version_db → next call hits DB
            _cache.clear()  # Force DB re-read
            state = get_session_state(api_key_id)

        assert state is not None
        assert state.api_key_id == api_key_id
        assert state.odoo_version == "17.0"

    def test_version_upsert_updates_value(self, session_db) -> None:
        """Second set_active_version_db overwrites the first."""
        from unittest.mock import patch

        from src.mcp.session import _cache, get_session_state, set_active_version_db

        api_key_id = str(_TEST_API_KEY_IDS[0])
        _cache.clear()

        checkout = _make_checkout_pg(session_db)

        with patch("src.mcp.server._checkout_pg", checkout):
            set_active_version_db(api_key_id, "16.0")
            _cache.clear()
            set_active_version_db(api_key_id, "17.0")
            _cache.clear()
            state = get_session_state(api_key_id)

        assert state is not None
        assert state.odoo_version == "17.0"


# ---------------------------------------------------------------------------
# Test 2: set_active_profile_db + get_session_state round-trip
# ---------------------------------------------------------------------------


class TestSetGetProfileRoundTrip:
    """AC-E2-6 test 2 — profile_name persists and is readable."""

    def test_profile_round_trip(self, session_db) -> None:
        from unittest.mock import patch

        from src.mcp.session import _cache, get_session_state, set_active_profile_db

        api_key_id = str(_TEST_API_KEY_IDS[1])
        _cache.clear()

        checkout = _make_checkout_pg(session_db)

        with patch("src.mcp.server._checkout_pg", checkout):
            set_active_profile_db(api_key_id, "my-erp-prod")
            _cache.clear()
            state = get_session_state(api_key_id)

        assert state is not None
        assert state.profile_name == "my-erp-prod"

    def test_profile_clear_sets_none(self, session_db) -> None:
        """set_active_profile_db(None) stores NULL → get returns profile_name=None."""
        from unittest.mock import patch

        from src.mcp.session import _cache, get_session_state, set_active_profile_db

        api_key_id = str(_TEST_API_KEY_IDS[1])
        _cache.clear()

        checkout = _make_checkout_pg(session_db)

        with patch("src.mcp.server._checkout_pg", checkout):
            set_active_profile_db(api_key_id, "to-be-cleared")
            _cache.clear()
            set_active_profile_db(api_key_id, None)
            _cache.clear()
            state = get_session_state(api_key_id)

        # Row still exists but profile_name should be None / falsy
        assert state is None or state.profile_name is None


# ---------------------------------------------------------------------------
# Test 3: 24h sliding TTL
# ---------------------------------------------------------------------------


class TestSlidingTTL:
    """AC-E2-6 test 3 — rows older than 24h are treated as expired (None)."""

    def test_stale_row_returns_none(self, session_db) -> None:
        """Manually back-date updated_at to >24h ago; get_session_state returns None."""
        from unittest.mock import patch

        from src.mcp.session import _cache, get_session_state

        api_key_id = str(_TEST_API_KEY_IDS[2])
        _cache.clear()

        # Insert a row with an updated_at that is 25 hours in the past
        with session_db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_key_session_state (api_key_id, odoo_version, updated_at)
                VALUES (%s, %s, NOW() - INTERVAL '25 hours')
                ON CONFLICT (api_key_id) DO UPDATE
                    SET odoo_version = EXCLUDED.odoo_version,
                        updated_at   = EXCLUDED.updated_at
                """,
                (int(api_key_id), "17.0"),
            )

        checkout = _make_checkout_pg(session_db)

        with patch("src.mcp.server._checkout_pg", checkout):
            state = get_session_state(api_key_id)

        # Row exists but is stale — must be treated as None
        assert state is None, (
            "A row with updated_at > 24h must be treated as expired (None)"
        )

    def test_fresh_row_within_24h_returns_state(self, session_db) -> None:
        """Row updated_at = NOW() must be readable."""
        from unittest.mock import patch

        from src.mcp.session import _cache, get_session_state, set_active_version_db

        api_key_id = str(_TEST_API_KEY_IDS[2])
        _cache.clear()

        checkout = _make_checkout_pg(session_db)

        with patch("src.mcp.server._checkout_pg", checkout):
            set_active_version_db(api_key_id, "16.0")
            _cache.clear()
            state = get_session_state(api_key_id)

        assert state is not None
        assert state.odoo_version == "16.0"


# ---------------------------------------------------------------------------
# Test 4: Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """AC-E2-6 test 4 — key A's session state is invisible to key B."""

    def test_different_keys_have_independent_state(self, session_db) -> None:
        from unittest.mock import patch

        from src.mcp.session import _cache, get_session_state, set_active_version_db

        key_a = str(_TEST_API_KEY_IDS[0])
        key_b = str(_TEST_API_KEY_IDS[1])
        _cache.clear()

        checkout = _make_checkout_pg(session_db)

        with patch("src.mcp.server._checkout_pg", checkout):
            set_active_version_db(key_a, "17.0")
            # key_b has no row — should return None
            _cache.clear()
            state_a = get_session_state(key_a)
            state_b = get_session_state(key_b)

        assert state_a is not None
        assert state_a.odoo_version == "17.0"
        # key_b either has no row or has a different version
        assert state_b is None or state_b.odoo_version != "17.0"
