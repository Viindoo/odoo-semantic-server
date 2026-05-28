# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_account_usage_api.py
"""Tests for GET /api/account/usage — quota dashboard endpoint (WI-B3, ADR-0039).

Business intent (5 cases):
  C1  No auth cookie → 401 Unauthorized.
  C2  Authenticated user with API key + usage_counter rows → 200, full data.
  C3  Authenticated user with API key + zero usage → percent=0.0, used=0.
  C4  Authenticated user with no API key → 200, null plan + empty history.
  C5  6-period history ordering (DESC) + current_period.yyyymm == server's current period.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""

import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Shared fixture: web app + seed data
# ---------------------------------------------------------------------------


@pytest.fixture
def web_app(pg_conn):
    """Web UI app on test DB with all migrations applied and a seeded admin user.

    The conftest auth bypass (WEBUI_AUTH_DISABLED=1) returns current_user_id=1.
    We must have a webui_users row with id=1 to satisfy FK constraints.
    """
    from src.db.migrate import run_migrations
    from src.web_ui.app import create_app

    run_migrations(pg_conn)

    with pg_conn.cursor() as cur:
        # Ensure a clean slate for api_keys / usage_counter owned by test users.
        cur.execute("DELETE FROM usage_counter")
        cur.execute("DELETE FROM api_keys WHERE user_id IN (1, 2)")
        # Seed admin user id=1 (auth bypass sentinel).
        cur.execute(
            "DELETE FROM webui_users WHERE username = '_usage_admin_id1'"
        )
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active, id)"
            " VALUES (%s, %s, TRUE, TRUE, 1) ON CONFLICT (username) DO NOTHING",
            ("_usage_admin_id1", "x"),
        )
        # Seed non-admin user id=2 (used by C4 tests via monkeypatch).
        cur.execute(
            "DELETE FROM webui_users WHERE username = '_usage_nonadmin_id2'"
        )
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active, id)"
            " VALUES (%s, %s, FALSE, TRUE, 2) ON CONFLICT (username) DO NOTHING",
            ("_usage_nonadmin_id2", "x"),
        )

    app = create_app()
    yield app

    # Cleanup.
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM usage_counter")
        cur.execute("DELETE FROM api_keys WHERE user_id IN (1, 2)")
        cur.execute(
            "DELETE FROM webui_users"
            " WHERE username IN ('_usage_admin_id1', '_usage_nonadmin_id2')"
        )


@pytest.fixture
def free_grandfathered_plan_id(pg_conn):
    """Return the id of the free-grandfathered plan (seeded by m13_006)."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = 'free-grandfathered'")
        row = cur.fetchone()
    assert row is not None, "free-grandfathered plan must be seeded by m13_006"
    return row[0]


# ---------------------------------------------------------------------------
# C1: No auth cookie → 401
# ---------------------------------------------------------------------------


class TestNoAuthReturns401:
    """C1: Unauthenticated request must receive 401 (auth required)."""

    @pytest.mark.asyncio
    async def test_no_auth_returns_401(self, web_app, monkeypatch):
        """GET /api/account/usage without a valid session returns 401."""
        import httpx

        # Disable auth bypass so the request is truly unauthenticated.
        monkeypatch.setattr("src.web_ui.auth.is_test_bypass_active", lambda: False)
        monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: None)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/account/usage")

        assert resp.status_code == 401, (
            f"Expected 401 for unauthenticated request, got {resp.status_code}"
        )
        body = resp.json()
        assert "detail" in body or "error" in body, (
            "401 response must include a detail/error field"
        )


# ---------------------------------------------------------------------------
# C2: Authenticated user + API key + usage rows → 200 full data
# ---------------------------------------------------------------------------


class TestAuthenticatedUserFullUsage:
    """C2: Valid session + api_key + usage_counter rows → full JSON response."""

    @pytest.mark.asyncio
    async def test_authenticated_user_full_usage(
        self, web_app, pg_conn, free_grandfathered_plan_id
    ):
        """200 response with plan info, current_period, and history."""
        import httpx

        fg_id = free_grandfathered_plan_id

        # Insert an API key owned by bypass user id=1.
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id, user_id)"
                " VALUES ('usage-test-key', 'hash_c2_full', 'ut_', %s, 1)"
                " RETURNING id",
                (fg_id,),
            )
            key_id = cur.fetchone()[0]

        # Insert usage_counter for 3 past periods + current.
        with pg_conn.cursor() as cur:
            cur.execute("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYYMM')")
            current_yyyymm = cur.fetchone()[0]

        rows = [
            (key_id, current_yyyymm, 87),
            (key_id, "202604", 1234),
            (key_id, "202603", 500),
        ]
        with pg_conn.cursor() as cur:
            for kid, period, count in rows:
                cur.execute(
                    "INSERT INTO usage_counter (api_key_id, period_yyyymm, call_count)"
                    " VALUES (%s, %s, %s)"
                    " ON CONFLICT (api_key_id, period_yyyymm)"
                    " DO UPDATE SET call_count = EXCLUDED.call_count",
                    (kid, period, count),
                )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/account/usage")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()

        # plan block
        assert body["plan"] is not None
        assert body["plan"]["slug"] == "free-grandfathered"
        assert body["plan"]["name"] == "Free (Grandfathered)"
        assert body["plan"]["quota_calls_per_month"] == 1000
        assert body["plan"]["rate_limit_rpm"] == 60

        # current_period block
        cp = body["current_period"]
        assert cp is not None
        assert cp["yyyymm"] == current_yyyymm
        assert cp["used"] == 87
        assert cp["remaining"] == 913
        assert cp["percent"] == 8.7

        # history — 3 periods, DESC
        history = body["history"]
        assert len(history) == 3
        assert history[0]["period"] == current_yyyymm
        assert history[0]["used"] == 87


# ---------------------------------------------------------------------------
# C3: Authenticated user + API key + zero usage → percent=0.0, used=0
# ---------------------------------------------------------------------------


class TestZeroUsage:
    """C3: API key with no usage_counter rows → used=0, percent=0.0."""

    @pytest.mark.asyncio
    async def test_zero_usage(self, web_app, pg_conn, free_grandfathered_plan_id):
        """When no usage_counter row exists, used=0 and percent=0.0."""
        import httpx

        fg_id = free_grandfathered_plan_id

        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id, user_id)"
                " VALUES ('zero-usage-key', 'hash_c3_zero', 'zu_', %s, 1)"
                " RETURNING id",
                (fg_id,),
            )
            # key created but no usage_counter rows

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/account/usage")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()

        cp = body["current_period"]
        assert cp is not None
        assert cp["used"] == 0
        assert cp["remaining"] == 1000  # quota_calls_per_month for free-grandfathered
        assert cp["percent"] == 0.0
        assert body["history"] == []


# ---------------------------------------------------------------------------
# C4: Authenticated user + no API key → 200 with null plan + empty history
# ---------------------------------------------------------------------------


class TestUserNoApiKey:
    """C4: Logged-in user with no api_key → graceful 200 with nulls."""

    @pytest.mark.asyncio
    async def test_user_no_api_key(self, web_app, pg_conn, monkeypatch):
        """User 2 (non-admin, no api_key) gets 200 with plan=null, history=[]."""
        import httpx

        # Patch current_user_id to return uid=2 (no api_keys for that user).
        monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: 2)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/account/usage")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()

        assert body["plan"] is None, "plan must be null when user has no API key"
        assert body["current_period"] is None, (
            "current_period must be null when user has no API key"
        )
        assert body["history"] == [], "history must be empty when user has no API key"


# ---------------------------------------------------------------------------
# C5: 6-period history ordering (DESC) + current_period.yyyymm == now()
# ---------------------------------------------------------------------------


class TestHistoryOrderingAndCurrentPeriod:
    """C5: History returned in DESC order; current_period.yyyymm matches server now()."""

    @pytest.mark.asyncio
    async def test_history_ordering_and_current_period(
        self, web_app, pg_conn, free_grandfathered_plan_id
    ):
        """7 usage rows inserted; only 6 returned, all in DESC order by period."""
        import httpx

        fg_id = free_grandfathered_plan_id

        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id, user_id)"
                " VALUES ('history-key', 'hash_c5_hist', 'hk_', %s, 1)"
                " RETURNING id",
                (fg_id,),
            )
            key_id = cur.fetchone()[0]

        # Get the server's current yyyymm.
        with pg_conn.cursor() as cur:
            cur.execute("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYYMM')")
            current_yyyymm = cur.fetchone()[0]

        # Build 7 synthetic periods: current + 6 past (all as YYYYMM strings).
        # We use purely synthetic periods based on current to avoid year-boundary issues.
        def _prev_period(yyyymm: str, n: int) -> str:
            """Subtract n months from a YYYYMM string."""
            year, month = int(yyyymm[:4]), int(yyyymm[4:])
            total = year * 12 + month - 1 - n  # 0-indexed
            return f"{total // 12:04d}{total % 12 + 1:02d}"

        periods = [current_yyyymm] + [_prev_period(current_yyyymm, i) for i in range(1, 7)]
        # periods[0] = current, periods[1..6] = 6 prior months (7 total)

        with pg_conn.cursor() as cur:
            for i, period in enumerate(periods):
                cur.execute(
                    "INSERT INTO usage_counter (api_key_id, period_yyyymm, call_count)"
                    " VALUES (%s, %s, %s)"
                    " ON CONFLICT (api_key_id, period_yyyymm)"
                    " DO UPDATE SET call_count = EXCLUDED.call_count",
                    (key_id, period, (i + 1) * 100),
                )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/account/usage")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()

        # current_period.yyyymm must match server's now().
        cp = body["current_period"]
        assert cp is not None
        assert cp["yyyymm"] == current_yyyymm, (
            f"current_period.yyyymm must equal server's current period "
            f"'{current_yyyymm}', got '{cp['yyyymm']}'"
        )

        # history: exactly 6 entries (LIMIT 6), ordered DESC.
        history = body["history"]
        assert len(history) == 6, (
            f"history must contain exactly 6 periods (LIMIT 6), got {len(history)}"
        )
        history_periods = [h["period"] for h in history]
        assert history_periods == sorted(history_periods, reverse=True), (
            f"history must be ordered DESC by period, got {history_periods}"
        )
        # The most-recent period in history must be the current one.
        assert history_periods[0] == current_yyyymm, (
            f"history[0].period must be the current period '{current_yyyymm}'"
        )
