# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_billing_cancel_voluntary.py
"""Voluntary vs involuntary revoke + configurable billing slugs (M10B P1 W3).

Business intent (behaviour-protecting):
  V1  revoke_entitlement(voluntary=True)  → schedules cancel-at-period-end:
      cancel_at_period_end=TRUE, status STAYS 'active', the key is NOT
      downgraded (access continues to period end — no refund policy).
  V2  revoke_entitlement(voluntary=False) → immediate downgrade: status
      'cancelled', the key drops to the free plan (existing behaviour, regression
      guard for the new voluntary branch).
  S1  free-slug setting honoured: with billing.free_plan_slug pointed at a
      renamed free plan, an involuntary revoke downgrades to THAT plan.
  S2  unlimited-sentinel setting honoured: a key on the configured sentinel plan
      is never downgraded by a lower-tier grant.
  T1  team_min_seats enforced: a team grant below the minimum raises ValueError;
      at/above the minimum it succeeds.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.billing import provisioning
from src.billing.activation import (
    EntitlementGrant,
    grant_entitlement,
    revoke_entitlement,
)
from src.db.migrate import run_migrations
from src.db.pg import auth_store, subscription_store

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures + helpers (mirror test_billing_activation.py conventions)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_billing_tables(pg_conn):
    _truncate(pg_conn)
    yield
    _truncate(pg_conn)


def _truncate(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'subscriptions'"
        )
        if cur.fetchone() is not None:
            cur.execute(
                "TRUNCATE billing_webhook_events, subscriptions RESTART IDENTITY CASCADE"
            )
    pg_conn.commit()


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    yield clean_pg


@pytest.fixture(autouse=True)
def _clear_setting_cache():
    """Settings have a 60s in-process TTL cache; clear it around each test so a
    monkeypatched billing.* setting takes effect immediately."""
    from src.settings import invalidate_all

    invalidate_all()
    yield
    invalidate_all()


def _plan_id(conn, slug: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = %s", (slug,))
        row = cur.fetchone()
    assert row is not None, f"plan slug={slug!r} must exist after migrations"
    return row[0]


def _make_user(conn, username: str, email: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, email, password_hash, email_verified)"
            " VALUES (%s, %s, 'x', TRUE) RETURNING id",
            (username, email),
        )
        uid = cur.fetchone()[0]
    conn.commit()
    return uid


def _key_plan_id(conn, key_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT plan_id FROM api_keys WHERE id = %s", (key_id,))
        return cur.fetchone()[0]


def _key_active(conn, key_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT active FROM api_keys WHERE id = %s", (key_id,))
        return cur.fetchone()[0]


def _set_plan_slug(conn, old_slug: str, new_slug: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE plans SET slug = %s WHERE slug = %s", (new_slug, old_slug)
        )
    conn.commit()


# ---------------------------------------------------------------------------
# V1: voluntary cancel keeps access until period end
# ---------------------------------------------------------------------------


class TestVoluntaryCancelKeepsAccess:
    def test_voluntary_revoke_schedules_and_keeps_paid_plan(self, migrated_pg):
        pro_id = _plan_id(migrated_pg, "pro")
        uid = _make_user(migrated_pg, "v1user", "v1@example.com")
        auth_store().create_api_key(name="Default (v1user)", user_id=uid)

        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="vol_v1", source="polar",
            buyer_email="v1@example.com",
        ))
        sub = subscription_store().get_by_id(sub_id)
        key_id = sub["api_key_id"]
        assert _key_plan_id(migrated_pg, key_id) == pro_id

        revoke_entitlement("vol_v1", reason="user-cancel", voluntary=True)

        migrated_pg.rollback()  # refresh snapshot
        sub = subscription_store().get_by_id(sub_id)
        assert sub["cancel_at_period_end"] is True, "voluntary → schedule flag set"
        assert sub["status"] == "active", "status STAYS active until period end"
        assert _key_plan_id(migrated_pg, key_id) == pro_id, (
            "key must NOT be downgraded on a voluntary cancel (no refund, "
            "access to period end)"
        )
        assert _key_active(migrated_pg, key_id) is True


# ---------------------------------------------------------------------------
# V2: involuntary revoke still downgrades immediately (regression guard)
# ---------------------------------------------------------------------------


class TestInvoluntaryRevokeImmediate:
    def test_involuntary_revoke_downgrades_now(self, migrated_pg):
        free_id = _plan_id(migrated_pg, "free")
        pro_id = _plan_id(migrated_pg, "pro")
        uid = _make_user(migrated_pg, "v2user", "v2@example.com")
        auth_store().create_api_key(name="Default (v2user)", user_id=uid)

        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="invol_v2", source="polar",
            buyer_email="v2@example.com",
        ))
        sub = subscription_store().get_by_id(sub_id)
        key_id = sub["api_key_id"]

        revoke_entitlement("invol_v2", reason="payment-failure")  # voluntary defaults False

        migrated_pg.rollback()
        sub = subscription_store().get_by_id(sub_id)
        assert sub["status"] == "cancelled", "involuntary → immediate cancel"
        assert sub["cancel_at_period_end"] is False
        assert _key_plan_id(migrated_pg, key_id) == free_id, "key downgraded to free now"
        assert _key_active(migrated_pg, key_id) is True


# ---------------------------------------------------------------------------
# S1: free-slug setting honoured on revoke
# ---------------------------------------------------------------------------


class TestFreeSlugSetting:
    def test_renamed_free_slug_is_downgrade_target(self, migrated_pg, monkeypatch):
        # Rename the seeded 'free' plan to 'starter' and point the setting at it.
        _set_plan_slug(migrated_pg, "free", "starter")
        starter_id = _plan_id(migrated_pg, "starter")
        pro_id = _plan_id(migrated_pg, "pro")

        def _fake_get_setting(key, **_kw):
            if key == "billing.free_plan_slug":
                return "starter"
            if key == "billing.unlimited_sentinel_slug":
                return "unlimited"
            return None

        monkeypatch.setattr("src.settings.get_setting", _fake_get_setting)

        uid = _make_user(migrated_pg, "s1user", "s1@example.com")
        auth_store().create_api_key(name="Default (s1user)", user_id=uid)
        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="slug_s1", source="polar",
            buyer_email="s1@example.com",
        ))
        key_id = subscription_store().get_by_id(sub_id)["api_key_id"]

        revoke_entitlement("slug_s1", reason="cancel")

        migrated_pg.rollback()
        assert _key_plan_id(migrated_pg, key_id) == starter_id, (
            "revoke must downgrade to the configured free slug, not a hardcoded 'free'"
        )
        assert _key_active(migrated_pg, key_id) is True, (
            "renamed free slug resolves → key stays active (no fail-safe deactivate)"
        )


# ---------------------------------------------------------------------------
# S2: unlimited-sentinel setting honoured (no downgrade of the top tier)
# ---------------------------------------------------------------------------


class TestUnlimitedSentinelSetting:
    def test_renamed_sentinel_still_protects_top_tier(self, migrated_pg, monkeypatch):
        # Rename 'unlimited' → 'enterprise'; point the sentinel setting at it.
        _set_plan_slug(migrated_pg, "unlimited", "enterprise")
        ent_id = _plan_id(migrated_pg, "enterprise")
        pro_id = _plan_id(migrated_pg, "pro")

        monkeypatch.setattr(
            "src.settings.get_setting",
            lambda key, **_kw: "enterprise"
            if key == "billing.unlimited_sentinel_slug"
            else None,
        )

        # A key already on the renamed sentinel plan must outrank pro.
        assert provisioning._plan_outranks(ent_id, pro_id) is True, (
            "the configured sentinel slug must outrank a priced plan even after rename"
        )
        assert provisioning._plan_outranks(pro_id, ent_id) is False


# ---------------------------------------------------------------------------
# T1: team_min_seats enforcement at grant
# ---------------------------------------------------------------------------


class TestTeamMinSeatsEnforced:
    def test_team_grant_below_min_raises(self, migrated_pg, monkeypatch):
        team_id = _plan_id(migrated_pg, "team")

        def _fake_get_setting(key, **_kw):
            return {
                "billing.team_plan_slug": "team",
                "billing.team_min_seats": 3,
            }.get(key)

        monkeypatch.setattr("src.settings.get_setting", _fake_get_setting)

        with pytest.raises(ValueError, match="team tier requires"):
            grant_entitlement(EntitlementGrant(
                plan_id=team_id, external_ref="team_t1_bad", source="polar",
                seats=2, buyer_email="t1@example.com",
            ))

    def test_team_grant_at_min_succeeds(self, migrated_pg, monkeypatch):
        team_id = _plan_id(migrated_pg, "team")

        def _fake_get_setting(key, **_kw):
            return {
                "billing.team_plan_slug": "team",
                "billing.team_min_seats": 3,
                "billing.free_plan_slug": "free",
                "billing.unlimited_sentinel_slug": "unlimited",
            }.get(key)

        monkeypatch.setattr("src.settings.get_setting", _fake_get_setting)

        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=team_id, external_ref="team_t1_ok", source="polar",
            seats=3, buyer_email="t1ok@example.com",
        ))
        sub = subscription_store().get_by_id(sub_id)
        assert sub["seats"] == 3
        assert sub["status"] == "active"

    def test_non_team_plan_ignores_min_seats(self, migrated_pg, monkeypatch):
        pro_id = _plan_id(migrated_pg, "pro")

        monkeypatch.setattr(
            "src.settings.get_setting",
            lambda key, **_kw: {
                "billing.team_plan_slug": "team",
                "billing.team_min_seats": 3,
            }.get(key),
        )

        # seats=1 on a non-team plan must NOT trip the team-min rule.
        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="pro_seat1", source="polar",
            seats=1, buyer_email="prouser@example.com",
        ))
        assert subscription_store().get_by_id(sub_id)["seats"] == 1
