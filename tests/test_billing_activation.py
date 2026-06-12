# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for src/billing/activation.py (M10B P1, ADR-0039 D3).

Business intent:
  A1  grant_entitlement is idempotent on external_ref (twice → 1 sub, no double key).
  A2  grant with buyer_email matching a VERIFIED user → that user's free key is
      upgraded IN PLACE (plan_id changed, NO new key) + subscription claimed
      (api_key_id + claimed_user_id set).
  A3  grant with buyer_email NOT matching any user → sub stays active + unclaimed
      (claimed_user_id NULL); a later claim_subscription_for_user provisions it.
  A4  grant against an UNVERIFIED user → not claimed at grant time (verified-only).
  A5  update_entitlement plan change on a claimed sub → the linked key's plan updates.
  A6  revoke_entitlement → key downgraded to free, status='cancelled', key still active.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.billing import provisioning
from src.billing.activation import (
    EntitlementGrant,
    grant_entitlement,
    revoke_entitlement,
    update_entitlement,
)
from src.db.migrate import run_migrations
from src.db.pg import auth_store, subscription_store

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BILLING_TABLES = ["billing_webhook_events", "subscriptions"]


@pytest.fixture(autouse=True)
def _reset_billing_tables(pg_conn):
    """Non-destructive blank slate for the m13_014 tables before/after each test.

    I21: TRUNCATE (RESTART IDENTITY CASCADE) instead of DROP TABLE — a full-suite
    run must not tear down schema other modules rely on.  TRUNCATE is a no-op-safe
    if the tables don't yet exist (guarded), and resets the SERIAL sequences so
    ids stay deterministic per test.
    """
    _truncate_billing_tables(pg_conn)
    yield
    _truncate_billing_tables(pg_conn)


def _truncate_billing_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'subscriptions'"
        )
        exists = cur.fetchone() is not None
    if exists:
        with pg_conn.cursor() as cur:
            cur.execute(
                "TRUNCATE billing_webhook_events, subscriptions RESTART IDENTITY CASCADE"
            )
    pg_conn.commit()


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    yield clean_pg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plan_id(conn, slug: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = %s", (slug,))
        row = cur.fetchone()
    assert row is not None, f"plan slug={slug!r} must exist after migrations"
    return row[0]


def _make_user(conn, username: str, email: str, *, verified: bool = True) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, email, password_hash, email_verified)"
            " VALUES (%s, %s, 'x', %s) RETURNING id",
            (username, email, verified),
        )
        user_id = cur.fetchone()[0]
    conn.commit()
    return user_id


def _key_plan_id(conn, key_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT plan_id FROM api_keys WHERE id = %s", (key_id,))
        return cur.fetchone()[0]


def _key_active(conn, key_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT active FROM api_keys WHERE id = %s", (key_id,))
        return cur.fetchone()[0]


def _count_keys_for_user(conn, user_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM api_keys WHERE user_id = %s", (user_id,))
        return cur.fetchone()[0]


def _count_subs(conn, external_ref: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s", (external_ref,)
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# A1: idempotency
# ---------------------------------------------------------------------------

class TestGrantIdempotent:
    def test_grant_twice_same_ref_one_sub_no_double_key(self, migrated_pg):
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "a1user", "a1@example.com")
        _raw, _prefix, _key_id = auth_store().create_api_key(
            name="Default key (a1user)", user_id=user_id
        )

        grant = EntitlementGrant(
            plan_id=pro_id, external_ref="grant_a1", source="polar",
            buyer_email="a1@example.com",
        )
        sub_id1 = grant_entitlement(grant)
        sub_id2 = grant_entitlement(grant)

        assert sub_id1 == sub_id2, "same external_ref must resolve to the same sub"
        assert _count_subs(migrated_pg, "grant_a1") == 1, "no duplicate subscription"
        assert _count_keys_for_user(migrated_pg, user_id) == 1, "no double-provisioned key"


# ---------------------------------------------------------------------------
# A2: grant claims + upgrades a verified user in place
# ---------------------------------------------------------------------------

class TestGrantClaimsVerifiedUser:
    def test_verified_buyer_free_key_upgraded_in_place(self, migrated_pg):
        free_id = _plan_id(migrated_pg, "free")
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "a2user", "a2@example.com", verified=True)
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (a2user)", user_id=user_id
        )
        assert _key_plan_id(migrated_pg, key_id) == free_id

        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="grant_a2", source="polar",
            buyer_email="a2@example.com",
        ))

        assert _key_plan_id(migrated_pg, key_id) == pro_id, "free key upgraded to pro"
        assert _count_keys_for_user(migrated_pg, user_id) == 1, "no new key minted"
        sub = subscription_store().get_by_id(sub_id)
        assert sub["api_key_id"] == key_id, "sub linked to the upgraded key"
        assert sub["claimed_user_id"] == user_id, "sub claimed by the buyer"
        assert sub["status"] == "active"


# ---------------------------------------------------------------------------
# A3: grant with no matching user stays unclaimed; later claim provisions it
# ---------------------------------------------------------------------------

class TestGrantUnclaimedThenClaim:
    def test_unmatched_email_stays_unclaimed_then_claim_provisions(self, migrated_pg):
        pro_id = _plan_id(migrated_pg, "pro")
        # No user exists for this email at grant time.
        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="grant_a3", source="polar",
            buyer_email="a3@example.com",
        ))
        sub = subscription_store().get_by_id(sub_id)
        assert sub["status"] == "active"
        assert sub["claimed_user_id"] is None, "no user → sub remains unclaimed"
        assert sub["api_key_id"] is None

        # Buyer signs up + verifies later, then claim-on-login fires.
        user_id = _make_user(migrated_pg, "a3user", "a3@example.com", verified=True)
        auth_store().create_api_key(name="Default key (a3user)", user_id=user_id)
        provisioned = provisioning.claim_subscription_for_user(user_id, "a3@example.com")

        assert len(provisioned) == 1
        sub = subscription_store().get_by_id(sub_id)
        assert sub["claimed_user_id"] == user_id, "claim links the buyer"
        assert sub["api_key_id"] is not None
        assert _key_plan_id(migrated_pg, sub["api_key_id"]) == pro_id


# ---------------------------------------------------------------------------
# A4: grant against an unverified user is NOT claimed at grant time
# ---------------------------------------------------------------------------

class TestGrantUnverifiedNotClaimed:
    def test_unverified_user_not_claimed_at_grant(self, migrated_pg):
        pro_id = _plan_id(migrated_pg, "pro")
        # An unverified account with the buyer email exists.
        _make_user(migrated_pg, "a4user", "a4@example.com", verified=False)

        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="grant_a4", source="polar",
            buyer_email="a4@example.com",
        ))
        sub = subscription_store().get_by_id(sub_id)
        assert sub["claimed_user_id"] is None, (
            "unverified user must not auto-claim a purchase at grant time"
        )


# ---------------------------------------------------------------------------
# A5: update_entitlement plan change propagates to the claimed key
# ---------------------------------------------------------------------------

class TestUpdateEntitlement:
    def test_plan_change_on_claimed_sub_updates_key(self, migrated_pg):
        pro_id = _plan_id(migrated_pg, "pro")
        team_id = _plan_id(migrated_pg, "team")
        user_id = _make_user(migrated_pg, "a5user", "a5@example.com", verified=True)
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (a5user)", user_id=user_id
        )
        grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="grant_a5", source="polar",
            buyer_email="a5@example.com",
        ))
        assert _key_plan_id(migrated_pg, key_id) == pro_id

        new_sub_id = update_entitlement("grant_a5", plan_id=team_id)
        sub = subscription_store().get_by_id(new_sub_id)
        assert sub["plan_id"] == team_id, "subscription plan_id updated"
        assert _key_plan_id(migrated_pg, key_id) == team_id, "linked key re-pointed to team"

    def test_update_unknown_ref_raises(self, migrated_pg):
        with pytest.raises(LookupError):
            update_entitlement("does_not_exist", status="past_due")

    def test_update_does_not_downgrade_higher_tier_key(self, migrated_pg):
        """I4 update path: a plan-change event must NOT downgrade a pricier key.

        Buyer on team (claimed) gets a subscription.updated → pro event.  The
        sub's snapshot plan_id may move to pro, but the LIVE key must stay on
        team (highest-tier-wins).
        """
        pro_id = _plan_id(migrated_pg, "pro")
        team_id = _plan_id(migrated_pg, "team")
        user_id = _make_user(migrated_pg, "uddown", "uddown@example.com", verified=True)
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (uddown)", user_id=user_id
        )
        # Claim on team (seats=3 to satisfy the enforced team minimum; the seat
        # count is incidental here — this test exercises the rank guard).
        grant_entitlement(EntitlementGrant(
            plan_id=team_id, external_ref="grant_uddown", source="polar",
            seats=3, buyer_email="uddown@example.com",
        ))
        assert _key_plan_id(migrated_pg, key_id) == team_id

        # A downgrade event to pro arrives.
        update_entitlement("grant_uddown", plan_id=pro_id)

        assert _key_plan_id(migrated_pg, key_id) == team_id, (
            "highest-tier-wins on update: team key must NOT be downgraded to pro"
        )

    def test_update_does_not_downgrade_unlimited_key(self, migrated_pg):
        """I4: an unlimited-granted key is never downgraded by a paid update event."""
        unlimited_id = _plan_id(migrated_pg, "unlimited")
        team_id = _plan_id(migrated_pg, "team")
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "udunl", "udunl@example.com", verified=True)
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (udunl)", user_id=user_id
        )
        # Grant team first so the sub.plan_id=team is claimed + linked to the key
        # (seats=3 satisfies the enforced team minimum; incidental to this test)...
        grant_entitlement(EntitlementGrant(
            plan_id=team_id, external_ref="grant_udunl", source="polar",
            seats=3, buyer_email="udunl@example.com",
        ))
        # ...then admin bumps the LIVE key to unlimited out-of-band.
        from src.db.auth_plans import set_api_key_plan_and_overrides
        from src.db.pg import get_pool
        set_api_key_plan_and_overrides(get_pool(), key_id, unlimited_id, None, None)
        assert _key_plan_id(migrated_pg, key_id) == unlimited_id

        # A subscription.updated → pro event (plan changes team→pro) must not
        # strip unlimited off the live key.
        update_entitlement("grant_udunl", plan_id=pro_id)
        assert _key_plan_id(migrated_pg, key_id) == unlimited_id, (
            "unlimited sentinel must survive a paid update event"
        )


# ---------------------------------------------------------------------------
# A6: revoke downgrades the key to free, marks sub cancelled, key stays active
# ---------------------------------------------------------------------------

class TestRevokeEntitlement:
    def test_revoke_downgrades_key_to_free_and_cancels(self, migrated_pg):
        free_id = _plan_id(migrated_pg, "free")
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "a6user", "a6@example.com", verified=True)
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (a6user)", user_id=user_id
        )
        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="grant_a6", source="polar",
            buyer_email="a6@example.com",
        ))
        assert _key_plan_id(migrated_pg, key_id) == pro_id

        revoke_entitlement("grant_a6", reason="cancelled")

        sub = subscription_store().get_by_id(sub_id)
        assert sub["status"] == "cancelled", "sub status must be cancelled"
        assert sub["cancelled_at"] is not None
        assert _key_plan_id(migrated_pg, key_id) == free_id, "key downgraded to free"
        assert _key_active(migrated_pg, key_id) is True, "key stays active on free tier"

    def test_revoke_unknown_ref_is_noop(self, migrated_pg):
        # Must not raise — unknown external_ref is a logged no-op.
        revoke_entitlement("never_existed", reason="cancelled")


# ---------------------------------------------------------------------------
# CR3: a TERMINAL status update downgrades the key, even with no plan change
# ---------------------------------------------------------------------------

class TestUpdateTerminalStatusDowngrades:
    @pytest.mark.parametrize("terminal", ["cancelled", "expired", "refunded", "past_due"])
    def test_terminal_status_downgrades_key_to_free(self, migrated_pg, terminal):
        """CR3: update_entitlement(status=<terminal>) on a claimed+paid sub must
        downgrade the live key to free, the same as an involuntary revoke —
        regardless of whether plan_id changed.  Before CR3 only a plan_id change
        touched the key, so a past_due/cancelled update silently kept paid access.
        """
        free_id = _plan_id(migrated_pg, "free")
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(
            migrated_pg, f"term{terminal}", f"term-{terminal}@example.com", verified=True
        )
        _raw, _prefix, key_id = auth_store().create_api_key(
            name=f"Default key (term{terminal})", user_id=user_id
        )
        grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref=f"grant_term_{terminal}", source="polar",
            buyer_email=f"term-{terminal}@example.com",
        ))
        assert _key_plan_id(migrated_pg, key_id) == pro_id

        # Terminal status arrives with NO plan_id change.
        update_entitlement(f"grant_term_{terminal}", status=terminal)

        assert _key_plan_id(migrated_pg, key_id) == free_id, (
            f"terminal status {terminal!r} must downgrade the paid key to free (CR3)"
        )
        assert _key_active(migrated_pg, key_id) is True, (
            "key stays active on the free tier after a terminal-status downgrade"
        )

    def test_non_terminal_status_does_not_downgrade(self, migrated_pg):
        """A benign status (e.g. 'active') with no plan change must NOT touch the key."""
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "actstat", "actstat@example.com", verified=True)
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (actstat)", user_id=user_id
        )
        grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="grant_actstat", source="polar",
            buyer_email="actstat@example.com",
        ))
        update_entitlement("grant_actstat", status="active")
        assert _key_plan_id(migrated_pg, key_id) == pro_id, (
            "a non-terminal status update must leave the paid key untouched"
        )


# ---------------------------------------------------------------------------
# Contract hardening: cancel_at_period_end reconciliation on the update path.
# A subscription.uncanceled (reactivation) arrives as an "update" carrying
# cancel_at_period_end=False → the locally-scheduled cancel must be CLEARED.
# A scheduling update carrying True re-records it; None leaves it untouched.
# See docs/reference/polar-contract-verification.md.
# ---------------------------------------------------------------------------

class TestUpdateReconcilesCancelAtPeriodEnd:
    def test_uncanceled_update_clears_scheduled_cancel(self, migrated_pg):
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "uncancel", "uncancel@example.com", verified=True)
        auth_store().create_api_key(name="Default key (uncancel)", user_id=user_id)
        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="grant_uncancel", source="polar",
            buyer_email="uncancel@example.com",
        ))
        subs = subscription_store()

        # User voluntarily scheduled a cancel-at-period-end.
        subs.schedule_cancellation(sub_id)
        assert subs.get_by_id(sub_id)["cancel_at_period_end"] is True

        # Polar fires subscription.uncanceled → update with status=active +
        # cancel_at_period_end=False → reconcile the local flag back to False.
        update_entitlement(
            "grant_uncancel", status="active", cancel_at_period_end=False
        )
        assert subs.get_by_id(sub_id)["cancel_at_period_end"] is False, (
            "subscription.uncanceled must clear the locally-scheduled cancel"
        )

    def test_update_records_scheduled_cancel_true(self, migrated_pg):
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "schedcap", "schedcap@example.com", verified=True)
        auth_store().create_api_key(name="Default key (schedcap)", user_id=user_id)
        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="grant_schedcap", source="polar",
            buyer_email="schedcap@example.com",
        ))
        subs = subscription_store()
        assert subs.get_by_id(sub_id)["cancel_at_period_end"] is False

        update_entitlement(
            "grant_schedcap", status="active", cancel_at_period_end=True
        )
        assert subs.get_by_id(sub_id)["cancel_at_period_end"] is True

    def test_update_without_flag_leaves_cancel_at_period_end_untouched(self, migrated_pg):
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "capnone", "capnone@example.com", verified=True)
        auth_store().create_api_key(name="Default key (capnone)", user_id=user_id)
        sub_id = grant_entitlement(EntitlementGrant(
            plan_id=pro_id, external_ref="grant_capnone", source="polar",
            buyer_email="capnone@example.com",
        ))
        subs = subscription_store()
        subs.schedule_cancellation(sub_id)
        assert subs.get_by_id(sub_id)["cancel_at_period_end"] is True

        # An update that omits the flag (cancel_at_period_end=None) must NOT
        # erase the stored schedule (partial-write contract).
        update_entitlement("grant_capnone", status="active")
        assert subs.get_by_id(sub_id)["cancel_at_period_end"] is True


# ---------------------------------------------------------------------------
# #5 out-of-order events: last_event_at monotonic guard at the activation layer
# ---------------------------------------------------------------------------

class TestActivationMonotonicGuard:
    def test_stale_grant_does_not_revive_a_newer_cancellation(self, migrated_pg):
        """#5: a grant carrying an OLDER last_event_at must NOT flip a sub that a
        NEWER event already moved to cancelled.

        Drives the guard through the public activation API (grant_entitlement
        passes last_event_at to the registry upsert).  Order of arrival:
          1. grant at t0 (active).
          2. a NEWER cancellation lands (status=cancelled) at t2.
          3. a STALE grant replay at t1 (t0<t1<t2) arrives out of order — it must
             NOT resurrect 'active'.
        """
        from datetime import UTC, datetime, timedelta

        pro_id = _plan_id(migrated_pg, "pro")
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        t2 = t0 + timedelta(hours=2)

        # 1. initial grant (no claimable user → just the sub row).
        sub_id = grant_entitlement(
            EntitlementGrant(
                plan_id=pro_id, external_ref="ooo_ref", source="polar",
                buyer_email="ooo@example.com",
            ),
            last_event_at=t0,
        )

        # 2. a newer cancellation moves the sub to cancelled at t2.
        subscription_store().upsert_by_external_ref(
            external_ref="ooo_ref", plan_id=pro_id, source="polar",
            status="cancelled", buyer_email="ooo@example.com", last_event_at=t2,
        )
        assert subscription_store().get_by_id(sub_id)["status"] == "cancelled"

        # 3. a STALE grant replay at t1 must NOT revive 'active'.
        grant_entitlement(
            EntitlementGrant(
                plan_id=pro_id, external_ref="ooo_ref", source="polar",
                buyer_email="ooo@example.com",
            ),
            last_event_at=t1,
        )
        assert subscription_store().get_by_id(sub_id)["status"] == "cancelled", (
            "an out-of-order (older) grant must not overwrite a newer cancellation (#5)"
        )

    def test_stale_update_does_not_resurrect_a_cancelled_subscription(self, migrated_pg):
        """#5 update path (money-critical): a stale subscription.updated(active) must
        NOT resurrect access on a sub a newer subscription.canceled already revoked.

        Live scenario: the period-end cancel lands first (status=cancelled, key
        downgraded to free at T2); then an OLD subscription.updated(active) replay
        arrives out of order (T1<T2).  Without the update-path guard this flips the
        sub back to active AND re-points the key to the paid plan — illegitimate
        resurrection of paid access for a non-paying subscriber.  The guard must
        keep status='cancelled' and leave the key on free.
        """
        from datetime import UTC, datetime, timedelta

        free_id = _plan_id(migrated_pg, "free")
        pro_id = _plan_id(migrated_pg, "pro")
        t1 = datetime(2026, 2, 1, 10, 0, tzinfo=UTC)
        t2 = t1 + timedelta(hours=1)
        t3 = t1 + timedelta(hours=2)

        user_id = _make_user(migrated_pg, "resur", "resur@example.com", verified=True)
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (resur)", user_id=user_id
        )
        # Grant pro (claims + upgrades the key) at t1.
        grant_entitlement(
            EntitlementGrant(
                plan_id=pro_id, external_ref="resur_ref", source="polar",
                buyer_email="resur@example.com",
            ),
            last_event_at=t1,
        )
        assert _key_plan_id(migrated_pg, key_id) == pro_id

        # The period-end cancel lands at t2 → status=cancelled, key downgraded to free.
        revoke_entitlement("resur_ref", reason="cancelled", last_event_at=t2)
        sub = subscription_store().get_by_external_ref("resur_ref")
        assert sub["status"] == "cancelled"
        assert _key_plan_id(migrated_pg, key_id) == free_id

        # A STALE subscription.updated(active) replay arrives out of order at t1<t2.
        update_entitlement("resur_ref", status="active", plan_id=pro_id, last_event_at=t1)

        sub = subscription_store().get_by_external_ref("resur_ref")
        assert sub["status"] == "cancelled", (
            "#5: a stale update(active) must NOT resurrect a newer cancellation"
        )
        assert _key_plan_id(migrated_pg, key_id) == free_id, (
            "#5: a stale update must NOT re-point the key back to the paid plan"
        )

        # A genuinely NEWER update (t3>t2) IS applied normally — guard only drops stale.
        team_id = _plan_id(migrated_pg, "team")
        update_entitlement("resur_ref", status="active", plan_id=team_id, last_event_at=t3)
        sub = subscription_store().get_by_external_ref("resur_ref")
        assert sub["status"] == "active", "a newer event must be applied normally"
        assert sub["plan_id"] == team_id, "a newer event re-points the sub plan"
        assert _key_plan_id(migrated_pg, key_id) == team_id, (
            "a newer event re-points the live key (not stale)"
        )


# ---------------------------------------------------------------------------
# H1: subscription.uncanceled must REACTIVATE a key that a prior involuntary
# cancel downgraded to free, even when sub.plan_id never changed.
# See docs/reference/polar-contract-verification.md.
# ---------------------------------------------------------------------------

class TestUncanceledReactivationRepoint:
    def test_uncanceled_update_restores_downgraded_key_to_paid_plan(self, migrated_pg):
        """H1: after an involuntary cancel downgrades the key to free (while the
        sub snapshot keeps plan_id=paid via mark_cancelled), a subscription.
        uncanceled-style update (status=active, SAME paid plan_id,
        cancel_at_period_end=False) must RESTORE the key to the paid plan AND
        clear the scheduled-cancel flag — even though plan_id is unchanged.

        Without the H1 fix this FAILS: plan_changed is False, so the old update
        path never re-points the key and the customer is stuck on free while the
        sub reads active/paid (under-serve + snapshot↔key divergence).
        """
        from datetime import UTC, datetime, timedelta

        free_id = _plan_id(migrated_pg, "free")
        pro_id = _plan_id(migrated_pg, "pro")
        t1 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
        t2 = t1 + timedelta(hours=1)
        t3 = t1 + timedelta(hours=2)

        user_id = _make_user(migrated_pg, "uncre", "uncre@example.com", verified=True)
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (uncre)", user_id=user_id
        )
        sub_id = grant_entitlement(
            EntitlementGrant(
                plan_id=pro_id, external_ref="grant_uncre", source="polar",
                buyer_email="uncre@example.com",
            ),
            last_event_at=t1,
        )
        assert _key_plan_id(migrated_pg, key_id) == pro_id

        # The period-end involuntary cancel lands at t2: status='cancelled',
        # key downgraded to free, but sub.plan_id stays on pro (mark_cancelled).
        revoke_entitlement("grant_uncre", reason="cancelled", last_event_at=t2)
        sub = subscription_store().get_by_id(sub_id)
        assert sub["status"] == "cancelled"
        assert sub["plan_id"] == pro_id, "mark_cancelled leaves plan_id on the paid plan"
        assert _key_plan_id(migrated_pg, key_id) == free_id, "cancel downgraded key to free"

        # subscription.uncanceled arrives (newer, t3) as an update: status=active,
        # SAME paid plan_id (pro), cancel_at_period_end=False.
        update_entitlement(
            "grant_uncre",
            plan_id=pro_id,
            status="active",
            cancel_at_period_end=False,
            last_event_at=t3,
        )

        assert _key_plan_id(migrated_pg, key_id) == pro_id, (
            "H1: uncanceled must restore the downgraded key to the paid plan "
            "even though plan_id did not change"
        )
        sub = subscription_store().get_by_id(sub_id)
        assert sub["status"] == "active", "uncanceled re-activates the sub"
        assert sub["cancel_at_period_end"] is False, (
            "uncanceled clears the locally-scheduled cancel"
        )

    def test_stale_uncanceled_does_not_reactivate_scheduled_cancel(self, migrated_pg):
        """#5 × cancel_at_period_end: a STALE uncanceled (older last_event_at)
        must be dropped wholesale, so a NEWER voluntary schedule-cancel survives.

        Order: schedule-cancel (cancel_at_period_end=True) at the NEWER t2; then
        replay an uncanceled (cancel_at_period_end=False, status=active) at the
        OLDER t1.  The monotonic guard must drop the stale event so the flag
        stays True and the key is NOT wrongly reactivated.
        """
        from datetime import UTC, datetime, timedelta

        pro_id = _plan_id(migrated_pg, "pro")
        t0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)   # stale uncanceled
        t2 = t0 + timedelta(hours=2)   # newer schedule-cancel

        user_id = _make_user(migrated_pg, "staleunc", "staleunc@example.com", verified=True)
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (staleunc)", user_id=user_id
        )
        sub_id = grant_entitlement(
            EntitlementGrant(
                plan_id=pro_id, external_ref="grant_staleunc", source="polar",
                buyer_email="staleunc@example.com",
            ),
            last_event_at=t0,
        )
        subs = subscription_store()

        # A schedule-cancel update lands at the NEWER t2 (cancel_at_period_end=True).
        update_entitlement(
            "grant_staleunc", status="active", cancel_at_period_end=True, last_event_at=t2
        )
        assert subs.get_by_id(sub_id)["cancel_at_period_end"] is True

        # A STALE uncanceled replay (cancel_at_period_end=False) arrives at the
        # OLDER t1 → the #5 guard must drop it BEFORE any snapshot/key change.
        update_entitlement(
            "grant_staleunc", status="active", cancel_at_period_end=False, last_event_at=t1
        )
        assert subs.get_by_id(sub_id)["cancel_at_period_end"] is True, (
            "#5: a stale uncanceled must NOT clear a newer scheduled cancel"
        )
        # Key stays on pro throughout (never downgraded here) — the point is the
        # stale event made no change at all.
        assert _key_plan_id(migrated_pg, key_id) == pro_id


# ---------------------------------------------------------------------------
# I26(c): half-claimed retry — claimed_user_id is set ONLY with api_key_id
# ---------------------------------------------------------------------------

class TestHalfClaimRetry:
    def test_provision_failure_leaves_claimed_user_null_then_retry_succeeds(
        self, migrated_pg, monkeypatch
    ):
        """A failure mid-provision must NOT set claimed_user_id (sub stays retryable).

        Invariant (I7): claimed_user_id is written LAST, after api_key_id.  We
        force link_to_api_key to blow up on the first provision so the sub is
        left api_key_id=NULL AND claimed_user_id=NULL — then a clean retry must
        claim + provision it normally.
        """
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "halfuser", "half@example.com", verified=True)
        auth_store().create_api_key(name="Default key (halfuser)", user_id=user_id)

        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="grant_half", plan_id=pro_id, source="polar",
            status="active", buyer_email="half@example.com",
        )

        # Force link_to_api_key to fail exactly once (the first provision attempt).
        real_link = subscription_store().link_to_api_key
        calls = {"n": 0}

        def _flaky_link(self, subscription_id, api_key_id):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated provision failure before claim")
            return real_link(subscription_id, api_key_id)

        from src.db.subscription_registry import SubscriptionStore
        monkeypatch.setattr(SubscriptionStore, "link_to_api_key", _flaky_link)

        with pytest.raises(RuntimeError):
            provisioning.provision_or_upgrade(sub_id, user_id)

        sub = subscription_store().get_by_id(sub_id)
        assert sub["claimed_user_id"] is None, (
            "I7: a failure before the final step must leave claimed_user_id NULL"
        )
        assert sub["api_key_id"] is None, "no key linked on the failed attempt"

        # The sub is still discoverable as unclaimed → retry via claim-on-login.
        unclaimed = subscription_store().find_unclaimed_active_by_email("half@example.com")
        assert any(r["id"] == sub_id for r in unclaimed), (
            "failed half-claim must remain retryable (still unclaimed+active)"
        )

        # monkeypatch's _flaky_link now succeeds on the 2nd call → retry provisions.
        provisioned = provisioning.claim_subscription_for_user(user_id, "half@example.com")
        assert len(provisioned) == 1, "retry must provision the previously-failed sub"
        sub = subscription_store().get_by_id(sub_id)
        assert sub["claimed_user_id"] == user_id, "retry sets claimed_user_id"
        assert sub["api_key_id"] is not None, "retry links api_key together with claim"
