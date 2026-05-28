# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_middleware_quota.py
"""Plan-aware quota middleware tests (WI-B2, ADR-0039).

Business intent (6 cases):
  Case 1  free-grandfathered key (1000/month) consume normally — passes.
  Case 2  free key (100/month) hits 100 → 101st call is blocked.
  Case 3  free key (30 rpm) bursts 31 → rpm block returned, NOT monthly.
  Case 4  admin key (quota=0) is unlimited — passes 10000-call simulation.
  Case 5  PlanInfo cache TTL — after cache expiry reassigned plan picked up.
  Case 6  period boundary — new month resets counter.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Uses pg_conn session fixture (no per-test testcontainers spin-up).
"""

import pytest

from src.db.migrate import run_migrations
from src.mcp.middleware import (
    _PLAN_CACHE,
    PlanInfo,
    _cache_lock,
    _check_monthly_quota,
    _check_rate_limit,
    _get_plan_for_key,
    _rate_buckets,
    _usage_buffer,
    _usage_buffer_lock,
)

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Module-level setup / teardown helpers
# ---------------------------------------------------------------------------


def _cleanup_test_keys(conn, key_hashes: list[str]) -> None:
    """Remove test api_keys (and cascade usage_counter rows) by key_hash."""
    if not key_hashes:
        return
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_keys WHERE key_hash = ANY(%s)", (key_hashes,)
        )


def _seed_key(conn, *, name: str, key_hash: str, key_prefix: str, slug: str) -> int:
    """Insert a test api_key for the named plan slug. Returns api_key_id."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = %s", (slug,))
        row = cur.fetchone()
        assert row is not None, f"Plan '{slug}' not found — ensure m13_006 migration ran"
        plan_id = row[0]
        cur.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (key_hash) DO UPDATE SET name = EXCLUDED.name"
            " RETURNING id",
            (name, key_hash, key_prefix, plan_id),
        )
        key_id = cur.fetchone()[0]
    return key_id


def _set_usage_counter(conn, *, api_key_id: int, period: str, count: int) -> None:
    """Upsert usage_counter to a specific count for testing."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO usage_counter (api_key_id, period_yyyymm, call_count, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (api_key_id, period_yyyymm) DO UPDATE
                SET call_count = EXCLUDED.call_count,
                    updated_at = now()
            """,
            (api_key_id, period, count),
        )


def _get_usage_count(conn, *, api_key_id: int, period: str) -> int:
    """Read call_count from usage_counter for a key + period."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT call_count FROM usage_counter"
            " WHERE api_key_id = %s AND period_yyyymm = %s",
            (api_key_id, period),
        )
        row = cur.fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# Fixture: migrated schema + pool accessible from tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def migrated_db(pg_conn):
    """Ensure m13_006 migration has applied and return pg_conn."""
    run_migrations(pg_conn)
    return pg_conn


@pytest.fixture(autouse=True)
def _clear_plan_cache():
    """Clear _PLAN_CACHE and _rate_buckets between tests so each starts fresh."""
    with _cache_lock:
        _PLAN_CACHE.clear()
    _rate_buckets.clear()
    with _usage_buffer_lock:
        _usage_buffer.clear()
    yield
    with _cache_lock:
        _PLAN_CACHE.clear()
    _rate_buckets.clear()
    with _usage_buffer_lock:
        _usage_buffer.clear()


# ---------------------------------------------------------------------------
# Helper: minimal fake pool wrapping a psycopg2 connection for tests.
# Middleware calls pool.getconn() / pool.putconn(conn); this adapter
# satisfies that interface using the shared test pg_conn.
# ---------------------------------------------------------------------------


class _FakePool:
    """Minimal connection pool adapter for test pg_conn."""

    def __init__(self, conn):
        self._conn = conn

    def getconn(self):
        return self._conn

    def putconn(self, conn):
        pass  # shared conn — do not close


# ---------------------------------------------------------------------------
# Case 1: free-grandfathered key (1000/month) consume normally
# ---------------------------------------------------------------------------


class TestFreeGrandfatheredUnderQuotaPasses:
    """Case 1: free-grandfathered (1000/month, 60 rpm) allows calls within quota."""

    _KEY_HASH = "hash_mq_c1_fg"

    def test_free_grandfathered_under_quota_passes(self, migrated_db):
        key_id = _seed_key(
            migrated_db,
            name="mq_c1_fg",
            key_hash=self._KEY_HASH,
            key_prefix="c1fg",
            slug="free-grandfathered",
        )
        migrated_db.commit()
        pool = _FakePool(migrated_db)

        plan_info = _get_plan_for_key(key_id, pool)
        assert plan_info.slug == "free-grandfathered"
        assert plan_info.quota_calls_per_month == 1000
        assert plan_info.rate_limit_rpm == 60

        # Simulate 5 calls under quota (usage_counter row absent = 0 used)
        for _ in range(5):
            allowed, used, quota = _check_monthly_quota(key_id, plan_info, pool)
            assert allowed is True, f"Expected allowed=True, used={used}, quota={quota}"

        _cleanup_test_keys(migrated_db, [self._KEY_HASH])
        migrated_db.commit()


# ---------------------------------------------------------------------------
# Case 2: free key (100/month) hits ceiling — 101st call blocked
# ---------------------------------------------------------------------------


class TestFreeKeyHitsMonthlyQuota:
    """Case 2: free plan (100/month) — once counter reaches 100, next call blocked."""

    _KEY_HASH = "hash_mq_c2_free"

    def test_free_key_hits_monthly_quota(self, migrated_db):
        import datetime as _dt

        key_id = _seed_key(
            migrated_db,
            name="mq_c2_free",
            key_hash=self._KEY_HASH,
            key_prefix="c2fr",
            slug="free",
        )
        migrated_db.commit()
        pool = _FakePool(migrated_db)

        plan_info = _get_plan_for_key(key_id, pool)
        assert plan_info.slug == "free"
        assert plan_info.quota_calls_per_month == 100

        period = _dt.datetime.now(_dt.UTC).strftime("%Y%m")

        # Set counter to exactly the quota limit
        _set_usage_counter(migrated_db, api_key_id=key_id, period=period, count=100)
        migrated_db.commit()

        # 101st check — must be blocked
        allowed, used, quota = _check_monthly_quota(key_id, plan_info, pool)
        assert allowed is False, (
            f"Expected blocked at quota={quota}, used={used}"
        )
        assert used == 100
        assert quota == 100

        _cleanup_test_keys(migrated_db, [self._KEY_HASH])
        migrated_db.commit()


# ---------------------------------------------------------------------------
# Case 3: free key (30 rpm) burst → rpm block before monthly check matters
# ---------------------------------------------------------------------------


class TestRpmBlockBeforeMonthly:
    """Case 3: free plan (30 rpm) — 31st request in same window hits rpm limit."""

    _KEY_HASH = "hash_mq_c3_rpm"

    def test_rpm_block_before_monthly(self, migrated_db):
        key_id = _seed_key(
            migrated_db,
            name="mq_c3_rpm",
            key_hash=self._KEY_HASH,
            key_prefix="c3rp",
            slug="free",
        )
        migrated_db.commit()
        pool = _FakePool(migrated_db)

        plan_info = _get_plan_for_key(key_id, pool)
        assert plan_info.rate_limit_rpm == 30

        # Fire 30 requests — all should be allowed
        for i in range(30):
            allowed, remaining = _check_rate_limit(key_id, plan_info)
            assert allowed is True, f"Request {i+1} should be allowed, remaining={remaining}"

        # 31st request — must be rate-limited
        allowed_31, remaining_31 = _check_rate_limit(key_id, plan_info)
        assert allowed_31 is False, (
            f"31st request should be rpm-blocked; remaining={remaining_31}"
        )
        assert remaining_31 == 0

        # Monthly quota should still be fine (counter is 0)
        allowed_monthly, used, quota = _check_monthly_quota(key_id, plan_info, pool)
        assert allowed_monthly is True, (
            f"Monthly quota should not be exhausted; used={used}, quota={quota}"
        )

        _cleanup_test_keys(migrated_db, [self._KEY_HASH])
        migrated_db.commit()


# ---------------------------------------------------------------------------
# Case 4: admin key (quota=0) — unlimited, skips monthly check
# ---------------------------------------------------------------------------


class TestAdminQuotaZeroSkipsCheck:
    """Case 4: admin plan (quota_calls_per_month=0) — monthly check always passes."""

    def test_admin_quota_zero_skips_check(self, migrated_db):
        """quota=0 means unlimited — _check_monthly_quota returns (True, 0, 0)."""
        # Simulate an admin-tier PlanInfo with quota=0 (unlimited).
        admin_plan = PlanInfo(
            plan_id=999,
            slug="admin",
            quota_calls_per_month=0,
            rate_limit_rpm=10000,
        )
        pool = _FakePool(migrated_db)

        # Simulate 10000 calls — all must pass
        for i in range(10000):
            allowed, used, quota = _check_monthly_quota(9999, admin_plan, pool)
            assert allowed is True, f"Admin call {i} should pass; used={used}, quota={quota}"
            assert used == 0
            assert quota == 0


# ---------------------------------------------------------------------------
# Case 5: PlanInfo cache TTL — after expiry, new plan picked up
# ---------------------------------------------------------------------------


class TestPlanCacheTtlRefresh:
    """Case 5: _PLAN_CACHE expires after TTL; reassigned plan is fetched fresh."""

    _KEY_HASH_ORIG = "hash_mq_c5_orig"
    _KEY_HASH_NEW = "hash_mq_c5_new"

    def test_plan_cache_ttl_refresh(self, migrated_db, monkeypatch):
        import src.mcp.middleware as mw

        # Seed key initially on free plan
        key_id = _seed_key(
            migrated_db,
            name="mq_c5_ttl",
            key_hash=self._KEY_HASH_ORIG,
            key_prefix="c5tt",
            slug="free",
        )
        migrated_db.commit()
        pool = _FakePool(migrated_db)

        # First lookup — should cache free plan
        plan_info_1 = _get_plan_for_key(key_id, pool)
        assert plan_info_1.slug == "free"

        # The cache entry exists
        with _cache_lock:
            assert key_id in _PLAN_CACHE

        # Reassign key to pro plan in DB
        with migrated_db.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = 'pro'")
            pro_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE api_keys SET plan_id = %s WHERE key_hash = %s",
                (pro_id, self._KEY_HASH_ORIG),
            )
        migrated_db.commit()

        # Second lookup before TTL — should still return cached free
        plan_info_cached = _get_plan_for_key(key_id, pool)
        assert plan_info_cached.slug == "free", (
            "Should still return cached free plan before TTL expires"
        )

        # Monkeypatch TTL to 0 to force cache miss on next call
        monkeypatch.setattr(mw, "_CACHE_TTL", 0.0)
        with _cache_lock:
            # Also expire the entry by backdating its timestamp
            if key_id in mw._PLAN_CACHE:
                plan, _ts = mw._PLAN_CACHE[key_id]
                mw._PLAN_CACHE[key_id] = (plan, 0.0)  # epoch timestamp = always expired

        # Third lookup after TTL expired — should fetch pro from DB
        plan_info_3 = _get_plan_for_key(key_id, pool)
        assert plan_info_3.slug == "pro", (
            f"After TTL expiry should fetch pro from DB, got {plan_info_3.slug!r}"
        )

        _cleanup_test_keys(migrated_db, [self._KEY_HASH_ORIG])
        migrated_db.commit()


# ---------------------------------------------------------------------------
# Case 6: period boundary — new month resets counter
# ---------------------------------------------------------------------------


class TestPeriodBoundaryResets:
    """Case 6: usage_counter is per-period; a new month always starts at 0."""

    _KEY_HASH = "hash_mq_c6_period"

    def test_period_boundary_resets(self, migrated_db):
        """Counter for previous month must not affect check for current month."""
        import datetime as _dt

        key_id = _seed_key(
            migrated_db,
            name="mq_c6_period",
            key_hash=self._KEY_HASH,
            key_prefix="c6pd",
            slug="free",
        )
        migrated_db.commit()
        pool = _FakePool(migrated_db)

        plan_info = _get_plan_for_key(key_id, pool)
        assert plan_info.quota_calls_per_month == 100

        # Simulate previous month exhausted (e.g. Jan 2025)
        prev_period = "202501"
        _set_usage_counter(migrated_db, api_key_id=key_id, period=prev_period, count=100)
        migrated_db.commit()

        # Current period check — no row for current month → used=0 → allowed
        allowed, used, quota = _check_monthly_quota(key_id, plan_info, pool)
        assert allowed is True, (
            f"Current month has no usage — must be allowed; used={used}, quota={quota}"
        )
        assert used == 0, f"No current-month row → used must be 0, got {used}"

        # Now set current month to exactly quota
        current_period = _dt.datetime.now(_dt.UTC).strftime("%Y%m")
        _set_usage_counter(
            migrated_db, api_key_id=key_id, period=current_period, count=100
        )
        migrated_db.commit()

        # Should now be blocked
        allowed_at_limit, used_at_limit, quota_at_limit = _check_monthly_quota(
            key_id, plan_info, pool
        )
        assert allowed_at_limit is False, (
            f"At quota limit must be blocked; used={used_at_limit}, quota={quota_at_limit}"
        )

        _cleanup_test_keys(migrated_db, [self._KEY_HASH])
        migrated_db.commit()
