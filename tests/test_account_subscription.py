# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_account_subscription.py
"""Tests for the self-service subscription endpoints (M10B P1 W3).

  GET  /api/account/subscription        — real-time subscription state
  POST /api/account/subscription/cancel — cancel-at-period-end via Polar

Business intent (behaviour, not implementation):
  G1  No auth → 401 (both endpoints).
  G2  Auth user with a claimed sub → 200 returns plan_name/slug, status, seats,
      renewal date (current_period_end), cancel_at_period_end, manage_url.
  C-OK   Polar cancel succeeds → 200 cancellation_scheduled + the local
         cancel_at_period_end flag IS set (status stays 'active').
  C-503  POLAR_API_KEY not configured → 503; the local flag is NOT set
         (never tell a paying user "cancelled" while Polar still charges).
  C-502  Polar API error → 502; the local flag is NOT set.
  C-404  No active, not-yet-cancelling sub → 404; nothing scheduled.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
The outbound Polar call is ALWAYS mocked — no network.
"""
import httpx
import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def web_app(pg_conn):
    """Web UI app on the test DB with migrations + bypass user id=1.

    Auth bypass (WEBUI_AUTH_DISABLED=1) resolves current_user_id=1; we seed the
    matching webui_users row to satisfy FK constraints, and clear billing rows
    so each test starts clean.
    """
    from src.db.migrate import run_migrations
    from src.web_ui.app import create_app

    run_migrations(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM subscriptions")
        cur.execute("DELETE FROM admin_audit_log")
        # Isolation-safe seed of the bypass user id=1.  `webui_users` has
        # `username` as PK *and* a separate UNIQUE index `ux_webui_users_id`
        # on the SERIAL `id`.  ON CONFLICT on a single column cannot guard
        # against a collision on the *other* unique key, so we delete by BOTH
        # id and username first, then INSERT cleanly.  Without this, a row left
        # behind here (id=1, username='_sub_admin_id1') collides with the id=1
        # seed in test_account_usage_api.py (sorted after this file) →
        # UniqueViolation on ux_webui_users_id under the full suite.
        cur.execute(
            "DELETE FROM webui_users WHERE id = 1 OR username = '_sub_admin_id1'"
        )
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active, id)"
            " VALUES (%s, %s, TRUE, TRUE, 1)",
            ("_sub_admin_id1", "x"),
        )
    pg_conn.commit()

    app = create_app()
    yield app

    # Symmetric teardown: remove EXACTLY the rows this fixture created so the
    # next test file starts from a clean slate (no leftover id=1 webui_users).
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM subscriptions")
        cur.execute("DELETE FROM admin_audit_log")
        cur.execute(
            "DELETE FROM webui_users WHERE id = 1 OR username = '_sub_admin_id1'"
        )
    pg_conn.commit()


def _plan_id(conn, slug: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = %s", (slug,))
        row = cur.fetchone()
    assert row is not None, f"plan slug={slug!r} must be seeded"
    return row[0]


def _seed_active_sub(
    conn,
    *,
    external_ref: str = "polar_sub_x1",
    plan_slug: str = "pro",
    user_id: int = 1,
    cancel_at_period_end: bool = False,
):
    """Insert an active subscription claimed by ``user_id`` and return its id."""
    plan_id = _plan_id(conn, plan_slug)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO subscriptions"
            " (external_ref, plan_id, source, status, seats, buyer_email,"
            "  amount_cents, currency, billing_interval,"
            "  current_period_start, current_period_end, claimed_user_id,"
            "  cancel_at_period_end)"
            " VALUES (%s, %s, 'polar', 'active', 1, 'buyer@example.com',"
            "         1900, 'USD', 'monthly',"
            "         now() - interval '5 days', now() + interval '25 days', %s,"
            "         %s)"
            " RETURNING id",
            (external_ref, plan_id, user_id, cancel_at_period_end),
        )
        sub_id = cur.fetchone()[0]
    conn.commit()
    return sub_id


def _cancel_flag(conn, sub_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cancel_at_period_end FROM subscriptions WHERE id = %s", (sub_id,)
        )
        return cur.fetchone()[0]


def _status(conn, sub_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM subscriptions WHERE id = %s", (sub_id,))
        return cur.fetchone()[0]


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---------------------------------------------------------------------------
# G1: auth gate
# ---------------------------------------------------------------------------


class TestSubscriptionAuthGate:
    @pytest.mark.asyncio
    async def test_get_subscription_no_auth_returns_401(self, web_app, monkeypatch):
        monkeypatch.setattr("src.web_ui.auth.is_test_bypass_active", lambda: False)
        monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: None)
        async with _client(web_app) as client:
            resp = await client.get("/api/account/subscription")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_cancel_no_auth_returns_401(self, web_app, monkeypatch):
        monkeypatch.setattr("src.web_ui.auth.is_test_bypass_active", lambda: False)
        monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: None)
        async with _client(web_app) as client:
            resp = await client.post("/api/account/subscription/cancel")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# G2: GET /subscription returns the renewal date + cancel state + manage URL
# ---------------------------------------------------------------------------


class TestGetSubscription:
    @pytest.mark.asyncio
    async def test_returns_plan_renewal_date_and_manage_url(self, web_app, pg_conn):
        _seed_active_sub(pg_conn, external_ref="polar_get_1", plan_slug="pro")

        async with _client(web_app) as client:
            resp = await client.get("/api/account/subscription")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "manage_url" in body and body["manage_url"]
        subs = body["subscriptions"]
        assert len(subs) == 1
        sub = subs[0]
        assert sub["plan_slug"] == "pro"
        assert sub["plan_name"] == "Pro"
        assert sub["status"] == "active"
        assert sub["seats"] == 1
        assert sub["billing_interval"] == "monthly"
        assert sub["current_period_end"] is not None, "renewal date must be present"
        assert sub["cancel_at_period_end"] is False
        assert sub["amount_cents"] == 1900
        assert sub["currency"] == "USD"

    @pytest.mark.asyncio
    async def test_no_subscriptions_returns_empty_list(self, web_app, pg_conn):
        async with _client(web_app) as client:
            resp = await client.get("/api/account/subscription")
        assert resp.status_code == 200
        assert resp.json()["subscriptions"] == []


# ---------------------------------------------------------------------------
# Cancel endpoint behaviour matrix
# ---------------------------------------------------------------------------


class TestCancelSubscription:
    @pytest.mark.asyncio
    async def test_polar_success_schedules_and_sets_flag(
        self, web_app, pg_conn, monkeypatch
    ):
        """Polar confirms → 200 + the local cancel_at_period_end flag IS set."""
        sub_id = _seed_active_sub(pg_conn, external_ref="polar_cancel_ok")

        calls = {}

        async def _fake_cancel(external_ref, *, at_period_end=True):
            calls["ref"] = external_ref
            calls["at_period_end"] = at_period_end
            return {"id": external_ref, "status": "active"}

        monkeypatch.setattr(
            "src.billing.polar_api.cancel_subscription", _fake_cancel
        )

        async with _client(web_app) as client:
            resp = await client.post("/api/account/subscription/cancel")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "cancellation_scheduled"
        assert "manage_url" in body
        # Polar was called with the stored external_ref + cancel-at-period-end.
        assert calls["ref"] == "polar_cancel_ok"
        assert calls["at_period_end"] is True
        # Local flag IS set; status stays active (access until period end).
        pg_conn.rollback()  # refresh snapshot
        assert _cancel_flag(pg_conn, sub_id) is True
        assert _status(pg_conn, sub_id) == "active"

    @pytest.mark.asyncio
    async def test_polar_not_configured_returns_503_flag_not_set(
        self, web_app, pg_conn, monkeypatch
    ):
        """POLAR_API_KEY unset → 503; the local flag is NOT set."""
        from src.billing import polar_api

        sub_id = _seed_active_sub(pg_conn, external_ref="polar_cancel_503")

        async def _raise_not_configured(external_ref, *, at_period_end=True):
            raise polar_api.PolarApiNotConfigured("no key")

        monkeypatch.setattr(
            "src.billing.polar_api.cancel_subscription", _raise_not_configured
        )

        async with _client(web_app) as client:
            resp = await client.post("/api/account/subscription/cancel")

        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert "manage_url" in body, "503 must surface the portal link"
        pg_conn.rollback()
        assert _cancel_flag(pg_conn, sub_id) is False, (
            "the local cancel flag must NOT be set when Polar was not called"
        )

    @pytest.mark.asyncio
    async def test_polar_api_error_returns_502_flag_not_set(
        self, web_app, pg_conn, monkeypatch
    ):
        """Polar API error → 502; the local flag is NOT set."""
        from src.billing import polar_api

        sub_id = _seed_active_sub(pg_conn, external_ref="polar_cancel_502")

        async def _raise_api_error(external_ref, *, at_period_end=True):
            raise polar_api.PolarApiError(
                "boom", status_code=500, body="upstream error"
            )

        monkeypatch.setattr(
            "src.billing.polar_api.cancel_subscription", _raise_api_error
        )

        async with _client(web_app) as client:
            resp = await client.post("/api/account/subscription/cancel")

        assert resp.status_code == 502, resp.text
        body = resp.json()
        assert "manage_url" in body, "502 must surface the portal link as fallback"
        pg_conn.rollback()
        assert _cancel_flag(pg_conn, sub_id) is False, (
            "the local cancel flag must NOT be set after an upstream failure"
        )

    @pytest.mark.asyncio
    async def test_no_active_sub_returns_404(self, web_app, pg_conn, monkeypatch):
        """No active, not-yet-cancelling sub → 404; Polar never called."""
        # Seed a sub that is ALREADY cancelling (excluded by the active filter).
        _seed_active_sub(
            pg_conn, external_ref="polar_already_cancelling",
            cancel_at_period_end=True,
        )

        called = {"hit": False}

        async def _should_not_run(external_ref, *, at_period_end=True):
            called["hit"] = True
            return {}

        monkeypatch.setattr(
            "src.billing.polar_api.cancel_subscription", _should_not_run
        )

        async with _client(web_app) as client:
            resp = await client.post("/api/account/subscription/cancel")

        assert resp.status_code == 404, resp.text
        assert resp.json()["error"] == "no_active_subscription"
        assert called["hit"] is False, "Polar must not be called when there is no sub"

    @pytest.mark.asyncio
    async def test_cancel_calls_revoke_entitlement_voluntary(
        self, web_app, pg_conn, monkeypatch
    ):
        """CR5: account cancel MUST go through revoke_entitlement(voluntary=True).

        After Polar confirms, the route must call activation.revoke_entitlement
        with voluntary=True (schedule cancel-at-period-end, preserve access) rather
        than schedule_cancellation directly.  The observable outcome is the same:
        sub stays active, cancel_at_period_end=TRUE — but the sole-writer path
        (activation layer) is the contract we test.
        """
        sub_id = _seed_active_sub(pg_conn, external_ref="polar_cr5_voluntary")

        async def _fake_cancel(external_ref, *, at_period_end=True):
            return {"id": external_ref, "status": "active"}

        monkeypatch.setattr("src.billing.polar_api.cancel_subscription", _fake_cancel)

        revoke_calls: list[dict] = []

        import src.billing.activation as activation_mod
        orig_revoke = activation_mod.revoke_entitlement

        def _track_revoke(external_ref, **kwargs):
            revoke_calls.append({"ref": external_ref, **kwargs})
            return orig_revoke(external_ref, **kwargs)

        monkeypatch.setattr(activation_mod, "revoke_entitlement", _track_revoke)

        async with _client(web_app) as client:
            resp = await client.post("/api/account/subscription/cancel")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancellation_scheduled"

        # revoke_entitlement must have been called with voluntary=True.
        assert len(revoke_calls) == 1, (
            f"Expected exactly 1 revoke_entitlement call, got {len(revoke_calls)}"
        )
        call = revoke_calls[0]
        assert call["ref"] == "polar_cr5_voluntary", (
            f"Expected external_ref='polar_cr5_voluntary', got {call['ref']!r}"
        )
        assert call.get("voluntary") is True, (
            f"Expected voluntary=True (schedule cancel-at-period-end), got {call!r}"
        )
        assert call.get("reason") == "user-cancel", (
            f"Expected reason='user-cancel', got {call.get('reason')!r}"
        )

        # Observable: sub still active, local cancel flag is set.
        pg_conn.rollback()
        assert _cancel_flag(pg_conn, sub_id) is True
        assert _status(pg_conn, sub_id) == "active"

    @pytest.mark.asyncio
    async def test_cancel_audit_target_set_before_polar_call(
        self, web_app, pg_conn, monkeypatch
    ):
        """CR8: request.state.audit_target must be set BEFORE any side-effect.

        If Polar raises an error the audit log must still record which
        subscription was being cancelled (the sub id), not an empty target.
        We verify by raising PolarApiError and capturing the audit_target that
        was set on request.state.
        """
        from src.billing import polar_api

        _seed_active_sub(pg_conn, external_ref="polar_cr8_audit")

        # Patch polar_api so it raises — the route catches this and returns 502.
        # The audit_target is set BEFORE the polar call (CR8 fix), so the
        # @audit_action decorator records the sub id even for the 502 path.
        async def _raise_after_target(external_ref, *, at_period_end=True):
            raise polar_api.PolarApiError("test error", status_code=500, body="err")

        monkeypatch.setattr("src.billing.polar_api.cancel_subscription", _raise_after_target)

        # Wrap the audit_action decorator's target capture.  The route sets
        # request.state.audit_target before the polar_api call; we verify it
        # is set when the exception is raised and the route returns 502.
        async with _client(web_app) as client:
            resp = await client.post("/api/account/subscription/cancel")

        # Should have gotten a 502 from the Polar error.
        assert resp.status_code == 502, (
            f"Expected 502 from PolarApiError in CR8 test, got {resp.status_code}: {resp.text}"
        )
        # The audit_action decorator reads request.state.audit_target after the
        # route function returns (regardless of exception path).  Since the route
        # sets audit_target before calling polar_api, the audit log row must be
        # present with the correct sub id.
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT target FROM admin_audit_log"
                " WHERE action = 'account.subscription.cancel'"
                " ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        # If audit_target was set before the Polar call the decorator logs it.
        # If set after (old bug) the row either has NULL target or is absent.
        assert row is not None, (
            "Audit row for account.subscription.cancel must exist even when Polar fails"
        )
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM subscriptions WHERE external_ref = 'polar_cr8_audit'"
            )
            sub_row = cur.fetchone()
        assert sub_row is not None
        expected_target = str(sub_row[0])
        assert row[0] == expected_target, (
            f"audit_target must be the sub id ({expected_target!r}) set BEFORE "
            f"the Polar call; got {row[0]!r}"
        )
