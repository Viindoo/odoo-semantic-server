# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_ee_modules_helper.py
"""Tests for src/data/ee_modules.py DB-backed helper.

Covers:
1. get_ee_modules_from_db_after_migration — returns 16 active entries from DB.
2. cache_hit_within_ttl — second call within TTL does not hit DB.
3. invalidate_clears_cache — invalidate_ee_modules_cache() forces fresh query.
4. fallback_when_db_unreachable — exception in get_pool → static fallback.
5. force_refresh_bypasses_cache — force_refresh=True ignores cached value.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
from unittest.mock import patch

import pytest

from src.data.ee_modules import (
    _FALLBACK_EE_MODULES,
    get_ee_modules,
    invalidate_ee_modules_cache,
)

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixture — ensure DB is migrated and cache is clean before each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_cache():
    """Always start each test with a clean in-process cache."""
    invalidate_ee_modules_cache()
    yield
    invalidate_ee_modules_cache()


@pytest.fixture
def migrated_pg(clean_pg):
    """Run migrations so ee_modules table exists."""
    from src.db.migrate import run_migrations

    # Drop table so run_migrations re-creates with backfill
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ee_modules CASCADE")
    run_migrations(clean_pg)
    yield clean_pg
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ee_modules CASCADE")


# ---------------------------------------------------------------------------
# 1. DB read after migration
# ---------------------------------------------------------------------------


class TestGetEeModulesFromDbAfterMigration:
    def test_returns_16_entries(self, migrated_pg):
        """get_ee_modules() must return 16 active entries when DB is populated."""
        rows = get_ee_modules(conn=migrated_pg)
        assert len(rows) == 16, (
            f"Expected 16 entries from DB, got {len(rows)}"
        )

    def test_rows_have_expected_keys(self, migrated_pg):
        """Every returned row must have the required dict keys."""
        rows = get_ee_modules(conn=migrated_pg)
        required_keys = {"name", "since_version", "vt_equivalent", "description", "deprecated"}
        for row in rows:
            assert required_keys.issubset(row.keys()), (
                f"Row missing keys: {required_keys - row.keys()}"
            )

    def test_all_active(self, migrated_pg):
        """All returned rows must have deprecated=False (filtered by query)."""
        rows = get_ee_modules(conn=migrated_pg)
        assert all(not r["deprecated"] for r in rows), (
            "get_ee_modules() returned deprecated rows unexpectedly"
        )


# ---------------------------------------------------------------------------
# 2. Cache hit within TTL
# ---------------------------------------------------------------------------


class TestCacheHitWithinTtl:
    def test_second_call_does_not_query_db(self, migrated_pg):
        """Within TTL, a second call must not execute a new DB query."""
        # Prime the cache
        first = get_ee_modules(conn=migrated_pg)
        assert len(first) == 16

        # Patch _fetch_from_db to detect if it's called
        with patch(
            "src.data.ee_modules._fetch_from_db", wraps=lambda conn: None
        ) as mock_fetch:
            second = get_ee_modules(conn=migrated_pg)
            mock_fetch.assert_not_called()

        assert first == second


# ---------------------------------------------------------------------------
# 3. Invalidate clears cache
# ---------------------------------------------------------------------------


class TestInvalidateClears:
    def test_invalidate_clears_cache(self, migrated_pg):
        """After invalidate_ee_modules_cache(), next call must re-query the DB."""
        # Prime
        get_ee_modules(conn=migrated_pg)
        invalidate_ee_modules_cache()

        # After invalidation, _fetch_from_db must be called again
        with patch(
            "src.data.ee_modules._fetch_from_db",
            return_value=[{"name": "x", "since_version": None, "vt_equivalent": None,
                           "description": None, "deprecated": False}],
        ) as mock_fetch:
            rows = get_ee_modules(conn=migrated_pg)
            mock_fetch.assert_called_once()

        assert rows == [{"name": "x", "since_version": None, "vt_equivalent": None,
                         "description": None, "deprecated": False}]


# ---------------------------------------------------------------------------
# 4. Fallback when DB unreachable
# ---------------------------------------------------------------------------


class TestFallbackWhenDbUnreachable:
    def test_fallback_when_db_unreachable(self):
        """When get_pool() raises, get_ee_modules() must return _FALLBACK_EE_MODULES."""
        with patch(
            "src.data.ee_modules._fetch_from_db",
            return_value=None,
        ):
            rows = get_ee_modules()

        assert rows == _FALLBACK_EE_MODULES, (
            "Expected static fallback when DB unreachable"
        )


# ---------------------------------------------------------------------------
# 5. force_refresh bypasses cache
# ---------------------------------------------------------------------------


class TestForceRefreshBypassesCache:
    def test_force_refresh_bypasses_cache(self, migrated_pg):
        """force_refresh=True must bypass the in-process cache and re-query."""
        # Prime the cache with a mocked single-entry result
        with patch(
            "src.data.ee_modules._fetch_from_db",
            return_value=[{"name": "cached_entry", "since_version": None,
                           "vt_equivalent": None, "description": None, "deprecated": False}],
        ):
            cached = get_ee_modules(conn=migrated_pg)
            assert len(cached) == 1

        # force_refresh must bypass the above cached value and query real DB
        fresh = get_ee_modules(conn=migrated_pg, force_refresh=True)
        assert len(fresh) == 16, (
            "force_refresh should bypass cache and return real DB rows "
            f"(expected 16, got {len(fresh)})"
        )
