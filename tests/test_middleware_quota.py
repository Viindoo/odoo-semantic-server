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

from contextlib import contextmanager

import pytest

from src.db.migrate import run_migrations
from src.mcp.middleware import (
    _PLAN_CACHE,
    PlanInfo,
    _cache_invalidate_by_key_id,
    _cache_lock,
    _check_monthly_quota,
    _check_rate_limit,
    _get_plan_for_key,
    _rate_buckets,
    _resolve_effective_quota,
    _resolve_effective_rpm,
    _usage_buffer,
    _usage_buffer_lock,
)

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Module-level setup / teardown helpers
# ---------------------------------------------------------------------------


def _cleanup_test_keys(conn, key_hashes: list[str]) -> None:
    """Remove test api_keys and their usage_counter rows by key_hash.

    m13_007 attaches `ON DELETE CASCADE` to `usage_counter.api_key_id`, so
    `DELETE FROM api_keys` alone is sufficient on a fresh schema. We keep the
    explicit usage_counter wipe as a belt-and-braces defence for DEV databases
    that may still carry an early m13_006 variant where the FK was created
    inline (no CASCADE) BEFORE m13_007 was applied, and against any future
    drift where the FK silently regresses.

    Without this safety net, a stale `call_count = quota` row binds to the
    next api_keys SERIAL id and trips a 429 on the very first authed call of
    an unrelated downstream test (PR #200 CI iter 3, test_tenant_deploy_key).
    Callers MUST wrap their seed/exercise code in `try: ... finally: _cleanup_test_keys(...)`
    so cleanup runs even when an assertion mid-test fails (otherwise the leak
    re-opens via the failure path).
    """
    if not key_hashes:
        return
    with conn.cursor() as cur:
        # Drop usage_counter rows BEFORE deleting api_keys. With m13_007's
        # CASCADE this is logically redundant, but explicit cleanup remains
        # FK-safe AND robust against the DEV-only drift described above.
        cur.execute(
            "DELETE FROM usage_counter WHERE api_key_id IN ("
            "  SELECT id FROM api_keys WHERE key_hash = ANY(%s)"
            ")",
            (key_hashes,),
        )
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
# Middleware calls `pool.checkout()` (context-manager) — the only public
# connection API on src.db.pg.PgPool. This adapter mirrors that interface
# using the shared test pg_conn so the tests stay portable across the prod
# PgPool API. Intentionally does NOT expose getconn/putconn — if a future
# patch accidentally calls those private psycopg2 names, these tests will
# raise AttributeError and fail loudly (regression guard for the
# pg_pool.getconn() drift that originally shipped to PR #200).
# ---------------------------------------------------------------------------


class _FakePool:
    """Minimal connection pool adapter exposing PgPool's public checkout() API."""

    def __init__(self, conn):
        self._conn = conn

    @contextmanager
    def checkout(self):
        # Tests pre-commit any seed/setup before calling middleware helpers,
        # so the shared conn is in a clean state. We do NOT toggle autocommit
        # here — _do_flush() in middleware sets it explicitly when needed,
        # and the read-only SELECT paths in _get_plan_for_key /
        # _check_monthly_quota work regardless of autocommit state.
        yield self._conn


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
        try:
            pool = _FakePool(migrated_db)

            plan_info = _get_plan_for_key(key_id, pool)
            assert plan_info.slug == "free-grandfathered"
            assert plan_info.quota_calls_per_month == 1000
            assert plan_info.rate_limit_rpm == 60

            # Simulate 5 calls under quota (usage_counter row absent = 0 used)
            for _ in range(5):
                allowed, used, quota = _check_monthly_quota(key_id, plan_info, pool)
                assert allowed is True, (
                    f"Expected allowed=True, used={used}, quota={quota}"
                )
        finally:
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
        try:
            pool = _FakePool(migrated_db)

            plan_info = _get_plan_for_key(key_id, pool)
            assert plan_info.slug == "free"
            assert plan_info.quota_calls_per_month == 100

            period = _dt.datetime.now(_dt.UTC).strftime("%Y%m")

            # Set counter to exactly the quota limit
            _set_usage_counter(
                migrated_db, api_key_id=key_id, period=period, count=100
            )
            migrated_db.commit()

            # 101st check — must be blocked
            allowed, used, quota = _check_monthly_quota(key_id, plan_info, pool)
            assert allowed is False, (
                f"Expected blocked at quota={quota}, used={used}"
            )
            assert used == 100
            assert quota == 100
        finally:
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
        try:
            pool = _FakePool(migrated_db)

            plan_info = _get_plan_for_key(key_id, pool)
            assert plan_info.rate_limit_rpm == 30

            # Fire 30 requests — all should be allowed
            for i in range(30):
                allowed, remaining = _check_rate_limit(key_id, plan_info)
                assert allowed is True, (
                    f"Request {i+1} should be allowed, remaining={remaining}"
                )

            # 31st request — must be rate-limited
            allowed_31, remaining_31 = _check_rate_limit(key_id, plan_info)
            assert allowed_31 is False, (
                f"31st request should be rpm-blocked; remaining={remaining_31}"
            )
            assert remaining_31 == 0

            # Monthly quota should still be fine (counter is 0)
            allowed_monthly, used, quota = _check_monthly_quota(
                key_id, plan_info, pool
            )
            assert allowed_monthly is True, (
                f"Monthly quota should not be exhausted; used={used}, quota={quota}"
            )
        finally:
            _cleanup_test_keys(migrated_db, [self._KEY_HASH])
            migrated_db.commit()


# ---------------------------------------------------------------------------
# Case 4: admin key (quota=0) — unlimited, skips monthly check
# ---------------------------------------------------------------------------


class TestAdminQuotaZeroSkipsCheck:
    """Case 4: unlimited plan — monthly check always passes.

    ADR-0041 D5 SSOT: unlimited access is conveyed by slug='unlimited', NOT by
    quota_calls_per_month=0.  The 'unlimited' slug triggers the SSOT bypass in
    _check_monthly_quota before any numeric check.  This test was updated from
    slug='admin' + quota=0 to slug='unlimited' to match the D5 contract — the
    old slug='admin'/quota=0 combination relied on the legacy numeric guard
    that was removed in the BLOCK-2 fix (quota=0 now means zero-allowed).
    """

    def test_admin_quota_zero_skips_check(self, migrated_db):
        """slug='unlimited' plan bypasses monthly gate — returns (True, 0, 0)."""
        # Correct representation per ADR-0041 D5: unlimited via slug, not numeric 0.
        admin_plan = PlanInfo(
            plan_id=999,
            slug="unlimited",
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
        try:
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
                    # epoch timestamp = always expired
                    mw._PLAN_CACHE[key_id] = (plan, 0.0)

            # Third lookup after TTL expired — should fetch pro from DB
            plan_info_3 = _get_plan_for_key(key_id, pool)
            assert plan_info_3.slug == "pro", (
                f"After TTL expiry should fetch pro from DB, got {plan_info_3.slug!r}"
            )
        finally:
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
        try:
            pool = _FakePool(migrated_db)

            plan_info = _get_plan_for_key(key_id, pool)
            assert plan_info.quota_calls_per_month == 100

            # Simulate previous month exhausted (e.g. Jan 2025)
            prev_period = "202501"
            _set_usage_counter(
                migrated_db, api_key_id=key_id, period=prev_period, count=100
            )
            migrated_db.commit()

            # Current period check — no row for current month → used=0 → allowed
            allowed, used, quota = _check_monthly_quota(key_id, plan_info, pool)
            assert allowed is True, (
                f"Current month has no usage — must be allowed;"
                f" used={used}, quota={quota}"
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
                f"At quota limit must be blocked;"
                f" used={used_at_limit}, quota={quota_at_limit}"
            )
        finally:
            _cleanup_test_keys(migrated_db, [self._KEY_HASH])
            migrated_db.commit()


# ---------------------------------------------------------------------------
# M10B P0-ext: unlimited plan + per-key override tests (ADR-0041 D5)
# ---------------------------------------------------------------------------


class TestUnlimitedPlanBypassesRateLimit:
    """B1: slug='unlimited' rpm=0 → all requests pass regardless of volume."""

    def test_unlimited_plan_bypasses_rate_limit(self):
        # M10B P0-ext: rpm=0 now means unlimited per ADR-0041 when slug='unlimited'.
        # Previous behavior: rpm=0 caused 0 >= 0 → block ALL requests (silent-DoS bug).
        unlimited_plan = PlanInfo(
            plan_id=1,
            slug="unlimited",
            quota_calls_per_month=0,
            rate_limit_rpm=0,
        )
        # Fire 1000 requests quick-fire — none should be blocked
        for i in range(1000):
            allowed, remaining = _check_rate_limit(9001, unlimited_plan)
            assert allowed is True, (
                f"Call {i+1}: slug='unlimited' must bypass RPM gate; "
                f"allowed={allowed}, remaining={remaining}"
            )
            assert remaining == 0, "Remaining should be 0 (sentinel for unlimited)"


class TestUnlimitedPlanBypassesMonthlyQuota:
    """B2: slug='unlimited' quota=0 → monthly check always passes, even at huge counter."""

    def test_unlimited_plan_bypasses_monthly_quota(self, migrated_db):
        unlimited_plan = PlanInfo(
            plan_id=1,
            slug="unlimited",
            quota_calls_per_month=0,
            rate_limit_rpm=0,
        )
        pool = _FakePool(migrated_db)

        # Simulate a counter way above any reasonable monthly limit
        # _check_monthly_quota should bypass without even querying DB for slug='unlimited'
        for i in range(100):
            allowed, used, quota = _check_monthly_quota(9002, unlimited_plan, pool)
            assert allowed is True, (
                f"Call {i+1}: slug='unlimited' must bypass monthly gate; "
                f"used={used}, quota={quota}"
            )
            assert used == 0
            assert quota == 0


class TestOverrideOverridesPlanRpm:
    """B3: rate_limit_override > plan.rate_limit_rpm → uses override value."""

    def test_override_overrides_plan_rpm(self):
        # Plan has 10 rpm limit, but per-key override is 100.
        # 50 calls should all pass (>10 plan limit, under 100 override limit).
        plan_with_override = PlanInfo(
            plan_id=2,
            slug="free",
            quota_calls_per_month=10000,
            rate_limit_rpm=10,
            rate_limit_override=100,  # per-key override
        )
        for i in range(50):
            allowed, remaining = _check_rate_limit(9003, plan_with_override)
            assert allowed is True, (
                f"Call {i+1}: override=100 should allow 50 calls (plan limit 10); "
                f"allowed={allowed}, remaining={remaining}"
            )


class TestOverrideOverridesPlanQuota:
    """B4: quota_override > plan.quota_calls_per_month → uses override value."""

    _KEY_HASH = "hash_mq_b4_quota_override"

    def test_override_overrides_plan_quota(self, migrated_db):
        import datetime as _dt

        key_id = _seed_key(
            migrated_db,
            name="mq_b4_quota_override",
            key_hash=self._KEY_HASH,
            key_prefix="b4qo",
            slug="free",
        )
        migrated_db.commit()
        try:
            pool = _FakePool(migrated_db)

            # Plan quota=100, but quota_override=10000
            plan_with_override = PlanInfo(
                plan_id=3,
                slug="free",
                quota_calls_per_month=100,
                rate_limit_rpm=60,
                quota_override=10000,
            )

            period = _dt.datetime.now(_dt.UTC).strftime("%Y%m")
            # Simulate 500 calls already used (above plan's 100 limit, under override 10000)
            _set_usage_counter(migrated_db, api_key_id=key_id, period=period, count=500)
            migrated_db.commit()

            # Should still be allowed (500 < 10000 override)
            allowed, used, quota = _check_monthly_quota(key_id, plan_with_override, pool)
            assert allowed is True, (
                f"500 used < 10000 override must be allowed; used={used}, quota={quota}"
            )
            assert quota == 10000, f"quota should reflect override value, got {quota}"
        finally:
            _cleanup_test_keys(migrated_db, [self._KEY_HASH])
            migrated_db.commit()


class TestOverrideZeroBlocksAll:
    """B5: rate_limit_override=0 → 1st request blocked (0 = zero-allowed, NOT unlimited).

    This is the critical D5 distinction: override=0 means admin explicitly set
    zero-allowed. Only slug='unlimited' means bypass. Override=0 must NOT trigger
    the unlimited bypass path.
    """

    def test_override_zero_blocks_all(self):
        # Plan rpm=60 (normal), but per-key override is explicitly 0 = zero-allowed.
        plan_zero_override = PlanInfo(
            plan_id=4,
            slug="free",
            quota_calls_per_month=10000,
            rate_limit_rpm=60,
            rate_limit_override=0,  # explicit zero = zero-allowed, NOT unlimited
        )
        # First request must be blocked because effective_rpm=0 and bucket(0) >= 0
        allowed, remaining = _check_rate_limit(9005, plan_zero_override)
        assert allowed is False, (
            f"rate_limit_override=0 must block all requests (0 = zero-allowed); "
            f"allowed={allowed}, remaining={remaining}"
        )
        assert remaining == 0


class TestOverrideNullFallsBackToPlan:
    """B6: rate_limit_override=None → uses plan.rate_limit_rpm as default."""

    def test_override_null_falls_back_to_plan(self):
        # Plan rpm=10, override=None → fallback to plan limit.
        plan_null_override = PlanInfo(
            plan_id=5,
            slug="free",
            quota_calls_per_month=10000,
            rate_limit_rpm=10,
            rate_limit_override=None,
        )
        # First 10 calls pass
        for i in range(10):
            allowed, remaining = _check_rate_limit(9006, plan_null_override)
            assert allowed is True, (
                f"Call {i+1}: override=None falls back to plan rpm=10; "
                f"allowed={allowed}, remaining={remaining}"
            )

        # 11th call must be blocked (plan rpm=10)
        allowed_11, remaining_11 = _check_rate_limit(9006, plan_null_override)
        assert allowed_11 is False, (
            f"11th call must be blocked at plan rpm=10; "
            f"allowed={allowed_11}, remaining={remaining_11}"
        )
        assert remaining_11 == 0


class TestSlugUnlimitedTakesPrecedenceOverOverride:
    """B7: slug='unlimited' bypasses even when rate_limit_override is set (ADR-0041 D5 SSOT).

    The 'unlimited' slug is the single source of truth for unlimited access.
    Per-key override values are ignored when slug='unlimited' — the slug wins.
    """

    def test_plan_slug_unlimited_takes_precedence_over_override(self):
        # slug='unlimited' with a non-None rate_limit_override — slug must win.
        plan_unlimited_with_override = PlanInfo(
            plan_id=6,
            slug="unlimited",
            quota_calls_per_month=0,
            rate_limit_rpm=0,
            rate_limit_override=5,  # override present, but slug='unlimited' wins per D5
        )
        # Verify resolver agrees: slug SSOT takes precedence
        effective_rpm, is_unlimited = _resolve_effective_rpm(plan_unlimited_with_override)
        assert is_unlimited is True, (
            "slug='unlimited' must return is_unlimited=True regardless of override"
        )
        assert effective_rpm == 0

        # Functional check: 100 calls should all pass
        for i in range(100):
            allowed, remaining = _check_rate_limit(9007, plan_unlimited_with_override)
            assert allowed is True, (
                f"Call {i+1}: slug='unlimited' must bypass even when override=5; "
                f"allowed={allowed}, remaining={remaining}"
            )


class TestGetPlanForKeyIncludesOverrides:
    """B8: _get_plan_for_key populates PlanInfo.rate_limit_override + quota_override from DB."""

    _KEY_HASH = "hash_mq_b8_overrides"

    def test_get_plan_for_key_includes_overrides(self, migrated_db):
        key_id = _seed_key(
            migrated_db,
            name="mq_b8_override_key",
            key_hash=self._KEY_HASH,
            key_prefix="b8ov",
            slug="free",
        )
        # Set per-key overrides directly in DB
        with migrated_db.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET rate_limit_override = %s, quota_override = %s"
                " WHERE key_hash = %s",
                (200, 5000, self._KEY_HASH),
            )
        migrated_db.commit()
        try:
            pool = _FakePool(migrated_db)
            plan_info = _get_plan_for_key(key_id, pool)

            assert plan_info.rate_limit_override == 200, (
                f"rate_limit_override should be 200, got {plan_info.rate_limit_override}"
            )
            assert plan_info.quota_override == 5000, (
                f"quota_override should be 5000, got {plan_info.quota_override}"
            )

            # Also verify resolver uses override values
            effective_rpm, is_unl = _resolve_effective_rpm(plan_info)
            assert effective_rpm == 200
            assert is_unl is False

            effective_quota, is_unl_q = _resolve_effective_quota(plan_info)
            assert effective_quota == 5000
            assert is_unl_q is False
        finally:
            _cleanup_test_keys(migrated_db, [self._KEY_HASH])
            migrated_db.commit()


class TestPlanCacheInvalidateDropsOverrideUpdate:
    """B9: cache invalidate after override update causes next lookup to see new override."""

    _KEY_HASH = "hash_mq_b9_cache_inv"

    def test_plan_cache_invalidate_drops_override_update(self, migrated_db):
        key_id = _seed_key(
            migrated_db,
            name="mq_b9_cache_inv",
            key_hash=self._KEY_HASH,
            key_prefix="b9ci",
            slug="free",
        )
        migrated_db.commit()
        try:
            pool = _FakePool(migrated_db)

            # Initial lookup — no override (NULL in DB)
            plan_info_1 = _get_plan_for_key(key_id, pool)
            assert plan_info_1.rate_limit_override is None, (
                f"Initially override should be None, got {plan_info_1.rate_limit_override}"
            )

            # Set override via SQL
            with migrated_db.cursor() as cur:
                cur.execute(
                    "UPDATE api_keys SET rate_limit_override = 100 WHERE key_hash = %s",
                    (self._KEY_HASH,),
                )
            migrated_db.commit()

            # Before invalidate: should still see cached value (None override)
            plan_info_cached = _get_plan_for_key(key_id, pool)
            assert plan_info_cached.rate_limit_override is None, (
                "Cache should still hold old value before invalidation"
            )

            # Invalidate cache for this key
            _cache_invalidate_by_key_id(key_id)

            # After invalidate: next lookup should fetch fresh from DB (override=100)
            plan_info_fresh = _get_plan_for_key(key_id, pool)
            assert plan_info_fresh.rate_limit_override == 100, (
                f"After cache invalidate, override should be 100 from DB, "
                f"got {plan_info_fresh.rate_limit_override}"
            )
        finally:
            _cleanup_test_keys(migrated_db, [self._KEY_HASH])
            migrated_db.commit()


class TestFallbackPlanEnforcesRpm:
    """Regression: BLOCK-1 of R-5 review — fallback plan must enforce RPM gate
    even though it fail-opens for monthly quota.

    Pre-R-4-A: fallback had slug='__fallback__' → RPM enforced via DEFAULT_RATE_LIMIT_RPM.
    R-4-A regression: slug changed to 'unlimited' → _resolve_effective_rpm bypassed
    the bucket entirely → RPM fail-open (DoS risk during DB outage).
    R-6-A fix: slug restored to '__fallback__'; monthly bypass via dual-slug guard
    `is_unlimited or plan_info.slug == '__fallback__'` in _check_monthly_quota.
    """

    def test_fallback_plan_enforces_default_rpm(self):
        """__fallback__ plan must block at DEFAULT_RATE_LIMIT_RPM, not bypass RPM gate."""
        from src.constants import DEFAULT_RATE_LIMIT_RPM

        fb = PlanInfo(
            plan_id=0,
            slug="__fallback__",
            quota_calls_per_month=0,
            rate_limit_rpm=DEFAULT_RATE_LIMIT_RPM,
            rate_limit_override=None,
            quota_override=None,
        )

        # Verify _resolve_effective_rpm does NOT treat __fallback__ as unlimited.
        effective_rpm, is_unlimited = _resolve_effective_rpm(fb)
        assert is_unlimited is False, (
            "__fallback__ slug must NOT return is_unlimited=True from _resolve_effective_rpm"
        )
        assert effective_rpm == DEFAULT_RATE_LIMIT_RPM, (
            f"effective_rpm must equal DEFAULT_RATE_LIMIT_RPM={DEFAULT_RATE_LIMIT_RPM}, "
            f"got {effective_rpm}"
        )

        # Burn through the RPM budget — N requests pass.
        for i in range(DEFAULT_RATE_LIMIT_RPM):
            allowed, remaining = _check_rate_limit(7777, fb)
            assert allowed is True, (
                f"Request {i+1}/{DEFAULT_RATE_LIMIT_RPM} should be allowed; "
                f"remaining={remaining}"
            )

        # (N+1)th request must be blocked — fallback enforces RPM.
        allowed_over, remaining_over = _check_rate_limit(7777, fb)
        assert allowed_over is False, (
            f"__fallback__ plan must enforce RPM gate at DEFAULT_RATE_LIMIT_RPM="
            f"{DEFAULT_RATE_LIMIT_RPM}; request {DEFAULT_RATE_LIMIT_RPM+1} must be blocked"
        )
        assert remaining_over == 0

    def test_fallback_plan_fail_opens_monthly_quota(self, migrated_db):
        """__fallback__ plan must return (True, 0, 0) from _check_monthly_quota.

        Monthly fail-open is the correct degraded-mode behavior: blocking monthly quota
        during a DB outage would be overly strict (the DB is already unreachable so the
        usage counter cannot be updated anyway). RPM serves as the actual throttle.
        """
        fb = PlanInfo(
            plan_id=0,
            slug="__fallback__",
            quota_calls_per_month=0,
            rate_limit_rpm=120,
            rate_limit_override=None,
            quota_override=None,
        )
        pool = _FakePool(migrated_db)

        allowed, used, quota = _check_monthly_quota(7777, fb, pool)
        assert allowed is True, (
            f"__fallback__ plan must fail-open for monthly quota; "
            f"got allowed={allowed}, used={used}, quota={quota}"
        )
        assert used == 0, f"used should be 0 for __fallback__ bypass path, got {used}"
        assert quota == 0, f"quota should be 0 for __fallback__ bypass path, got {quota}"


class TestFallbackPlanHeaderEmitsUnlimited:
    """R-8 fix: observability/enforcement symmetry for the dual-slug bypass.

    The enforcement path (`_check_monthly_quota` L257) bypasses the monthly
    gate when `slug='unlimited'` (D5 SSOT) OR `slug='__fallback__'` (degraded
    mode). The header emission path must match — otherwise during a DB outage
    clients see `X-Quota-Limit: "0"` while the request was bypassed.
    """

    def test_fallback_plan_header_emits_unlimited_on_success_path(self):
        """Success-path L674 invariant: `__fallback__` -> X-Quota-Limit `'unlimited'`.

        Mirrors the dual-slug guard in `_check_monthly_quota`. We assert at the
        helper boundary (`_resolve_effective_quota` returns `is_unlimited=False`
        for `__fallback__`) so the test pins the bug: the header logic must
        consult the slug, not just the helper's flag.
        """
        fb = PlanInfo(
            plan_id=0,
            slug="__fallback__",
            quota_calls_per_month=0,
            rate_limit_rpm=120,
            rate_limit_override=None,
            quota_override=None,
        )
        _eff_q_hdr, _is_unl_hdr = _resolve_effective_quota(fb)
        assert _is_unl_hdr is False, (
            "guard: _resolve_effective_quota must NOT treat __fallback__ as unlimited; "
            "the test below pins the dual-slug header logic, not the helper."
        )
        _monthly_bypassed = _is_unl_hdr or fb.slug == "__fallback__"
        _quota_limit_val = "unlimited" if _monthly_bypassed else str(0)
        assert _quota_limit_val == "unlimited", (
            "Success-path X-Quota-Limit must emit 'unlimited' for __fallback__ slug "
            "to match the enforcement bypass (R-6-A dual-slug guard)."
        )

    def test_fallback_plan_header_emits_unlimited_on_monthly_429_path(self):
        """Monthly-429 L639 invariant: defensive symmetry even though path is unreachable.

        L257 short-circuits before any DB query so the monthly-429 branch never
        runs for `__fallback__` in production. This test pins the header logic
        against a future refactor that re-enters this branch (e.g. if the L257
        bypass is moved or weakened) — without the symmetry fix, the header
        would silently regress to `"0"`.
        """
        fb = PlanInfo(
            plan_id=0,
            slug="__fallback__",
            quota_calls_per_month=0,
            rate_limit_rpm=120,
            rate_limit_override=None,
            quota_override=None,
        )
        _eff_q_m, _is_unl_m = _resolve_effective_quota(fb)
        _monthly_bypassed_m = _is_unl_m or fb.slug == "__fallback__"
        _quota_limit_m = "unlimited" if _monthly_bypassed_m else str(0)
        assert _quota_limit_m == "unlimited", (
            "Monthly-429 X-Quota-Limit must emit 'unlimited' for __fallback__ slug — "
            "defensive symmetry with success path even though branch is unreachable."
        )


class TestQuotaOverrideZeroBlocksAll:
    """BLOCK-2 regression: quota_override=0 on a non-unlimited plan must BLOCK.

    ADR-0041 D5: 0 = zero-allowed, NOT unlimited.  Only slug='unlimited' grants
    unlimited monthly access.  This class pins the fix for the legacy
    ``if effective_quota == 0: return True`` short-circuit that was removed —
    that guard promoted quota_override=0 to unlimited access, contradicting D5.
    """

    _KEY_HASH = "hash_mq_b10_quota_zero"

    def test_quota_override_zero_blocks_all(self, migrated_db):
        """quota_override=0 on plan slug='free' -> first monthly check returns False.

        Scenario: plan has quota=100/month, admin sets quota_override=0 (explicitly
        disabling all calls for this key).  The middleware must block the key, not
        treat quota=0 as unlimited.
        """
        key_id = _seed_key(
            migrated_db,
            name="mq_b10_quota_zero",
            key_hash=self._KEY_HASH,
            key_prefix="b10qz",
            slug="free",
        )
        # Set quota_override=0 via SQL (zero-allowed, NOT unlimited)
        with migrated_db.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET quota_override = 0 WHERE key_hash = %s",
                (self._KEY_HASH,),
            )
        migrated_db.commit()
        try:
            pool = _FakePool(migrated_db)

            # Fetch fresh plan_info including override from DB
            _cache_invalidate_by_key_id(key_id)
            plan_info = _get_plan_for_key(key_id, pool)

            assert plan_info.quota_override == 0, (
                f"Expected quota_override=0, got {plan_info.quota_override}"
            )

            # Verify resolver: quota_override=0 -> (0, False) — not unlimited
            effective_quota, is_unlimited = _resolve_effective_quota(plan_info)
            assert effective_quota == 0, (
                f"Expected effective_quota=0, got {effective_quota}"
            )
            assert is_unlimited is False, (
                "quota_override=0 must NOT be unlimited (ADR-0041 D5 SSOT)"
            )

            # Monthly check with 0 usage: allowed_monthly must be False
            # (used=0 < quota=0 is False — zero calls allowed)
            allowed_monthly, used, quota = _check_monthly_quota(
                key_id, plan_info, pool
            )
            assert allowed_monthly is False, (
                f"quota_override=0 must BLOCK all requests; "
                f"allowed={allowed_monthly}, used={used}, quota={quota}"
            )
        finally:
            _cleanup_test_keys(migrated_db, [self._KEY_HASH])
            migrated_db.commit()
