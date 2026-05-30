# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for src/billing/provisioning.py (M10B P1, ADR-0039 D3).

Business intent:
  P1  provision_or_upgrade upgrades an existing free key IN PLACE (no new key),
      and links the subscription to that key + user.
  P2  provision_or_upgrade with seats>1 creates a tenant + tenant_admin
      membership + links the tenant to the subscription.
  P3  HIGHEST-TIER-WINS: a user already on a higher-price plan is NOT downgraded.
  P4  provision_or_upgrade for a user with no key mints exactly one key on the
      purchased plan.
  P5  claim_subscription_for_user provisions an unclaimed active sub for the
      matching email and is best-effort (never raises on bad data).

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.billing import provisioning
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
    run must not tear down schema other modules rely on.
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
    """Apply all migrations on a clean DB, yield connection."""
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


def _count_keys_for_user(conn, user_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM api_keys WHERE user_id = %s", (user_id,))
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# P1 + P4: provision_or_upgrade key handling
# ---------------------------------------------------------------------------

class TestProvisionKeyHandling:
    def test_upgrades_existing_free_key_in_place(self, migrated_pg):
        """A user with a free key is upgraded in place — no new key minted."""
        free_id = _plan_id(migrated_pg, "free")
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "p1user", "p1@example.com")
        # Auto-mint the free key the way signup does.
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (p1user)", user_id=user_id
        )
        assert _key_plan_id(migrated_pg, key_id) == free_id

        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="prov_p1", plan_id=pro_id, source="polar",
            status="active", buyer_email="p1@example.com",
        )
        returned_key = provisioning.provision_or_upgrade(sub_id, user_id)

        assert returned_key == key_id, "must upgrade the SAME key, not mint a new one"
        assert _count_keys_for_user(migrated_pg, user_id) == 1, "no extra key created"
        assert _key_plan_id(migrated_pg, key_id) == pro_id, "key plan upgraded to pro"
        # Subscription linked to the key + user.
        sub = subscription_store().get_by_id(sub_id)
        assert sub["api_key_id"] == key_id
        assert sub["claimed_user_id"] == user_id

    def test_mints_one_key_when_user_has_none(self, migrated_pg):
        """A user with no key gets exactly one key, on the purchased plan."""
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "p4user", "p4@example.com")
        assert _count_keys_for_user(migrated_pg, user_id) == 0

        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="prov_p4", plan_id=pro_id, source="polar",
            status="active", buyer_email="p4@example.com",
        )
        key_id = provisioning.provision_or_upgrade(sub_id, user_id)

        assert _count_keys_for_user(migrated_pg, user_id) == 1
        assert _key_plan_id(migrated_pg, key_id) == pro_id


# ---------------------------------------------------------------------------
# P2: seats > 1 → tenant + tenant_admin membership
# ---------------------------------------------------------------------------

class TestSeatsProvisionTenant:
    def test_seats_gt_one_creates_tenant_and_admin_membership(self, migrated_pg):
        team_id = _plan_id(migrated_pg, "team")
        user_id = _make_user(migrated_pg, "teamuser", "team@example.com")
        auth_store().create_api_key(name="Default key (teamuser)", user_id=user_id)

        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="prov_team", plan_id=team_id, source="polar",
            status="active", seats=3, buyer_email="team@example.com",
        )
        provisioning.provision_or_upgrade(sub_id, user_id)

        sub = subscription_store().get_by_id(sub_id)
        tenant_id = sub["tenant_id"]
        assert tenant_id is not None, "seats>1 must provision a tenant"

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT name FROM tenants WHERE id = %s", (tenant_id,))
            assert cur.fetchone()[0] == f"sub-{sub_id}"
            cur.execute(
                "SELECT role FROM tenant_members WHERE user_id = %s AND tenant_id = %s",
                (user_id, tenant_id),
            )
            row = cur.fetchone()
        assert row is not None and row[0] == "tenant_admin", (
            "buyer must be tenant_admin of the provisioned tenant"
        )

    def test_seats_eq_one_creates_no_tenant(self, migrated_pg):
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "soloseat", "solo@example.com")
        auth_store().create_api_key(name="Default key (soloseat)", user_id=user_id)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="prov_solo", plan_id=pro_id, source="polar",
            status="active", seats=1, buyer_email="solo@example.com",
        )
        provisioning.provision_or_upgrade(sub_id, user_id)
        sub = subscription_store().get_by_id(sub_id)
        assert sub["tenant_id"] is None, "single-seat purchase must not create a tenant"


# ---------------------------------------------------------------------------
# P3: highest-tier-wins
# ---------------------------------------------------------------------------

class TestHighestTierWins:
    def test_does_not_downgrade_higher_tier_user(self, migrated_pg):
        """User already on team (higher price) buying pro is NOT downgraded."""
        pro_id = _plan_id(migrated_pg, "pro")
        team_id = _plan_id(migrated_pg, "team")
        user_id = _make_user(migrated_pg, "tieruser", "tier@example.com")
        # Existing key already on the pricier team plan.
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (tieruser)", user_id=user_id
        )
        from src.db.auth_registry import set_api_key_plan_and_overrides
        from src.db.pg import get_pool
        set_api_key_plan_and_overrides(get_pool(), key_id, team_id, None, None)
        assert _key_plan_id(migrated_pg, key_id) == team_id

        # A *cheaper* pro subscription arrives.
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="prov_tier", plan_id=pro_id, source="polar",
            status="active", buyer_email="tier@example.com",
        )
        returned = provisioning.provision_or_upgrade(sub_id, user_id)

        assert returned == key_id
        assert _key_plan_id(migrated_pg, key_id) == team_id, (
            "highest-tier-wins: must NOT downgrade team → pro"
        )

    def test_upgrades_when_new_plan_is_higher(self, migrated_pg):
        """User on free buying pro IS upgraded (new tier higher)."""
        free_id = _plan_id(migrated_pg, "free")
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "upuser", "up@example.com")
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (upuser)", user_id=user_id
        )
        assert _key_plan_id(migrated_pg, key_id) == free_id
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="prov_up", plan_id=pro_id, source="polar",
            status="active", buyer_email="up@example.com",
        )
        provisioning.provision_or_upgrade(sub_id, user_id)
        assert _key_plan_id(migrated_pg, key_id) == pro_id

    def test_unlimited_key_not_downgraded_by_paid_grant(self, migrated_pg):
        """I26(a): an admin-granted 'unlimited' key buying pro is NOT downgraded.

        'unlimited' and 'free' are both seeded at price_cents=0, so price alone
        cannot protect it — the plan-rank sentinel (ADR-0041 D5) must keep the
        unlimited slug above the priced pro plan.
        """
        unlimited_id = _plan_id(migrated_pg, "unlimited")
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "unluser", "unl@example.com")
        _raw, _prefix, key_id = auth_store().create_api_key(
            name="Default key (unluser)", user_id=user_id
        )
        # Admin grants unlimited.
        from src.db.auth_registry import set_api_key_plan_and_overrides
        from src.db.pg import get_pool
        set_api_key_plan_and_overrides(get_pool(), key_id, unlimited_id, None, None)
        assert _key_plan_id(migrated_pg, key_id) == unlimited_id

        # A paid pro subscription arrives (1900 > 0, but must NOT win over unlimited).
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="prov_unl", plan_id=pro_id, source="polar",
            status="active", buyer_email="unl@example.com",
        )
        returned = provisioning.provision_or_upgrade(sub_id, user_id)

        assert returned == key_id
        assert _key_plan_id(migrated_pg, key_id) == unlimited_id, (
            "unlimited sentinel must outrank a paid grant — never downgraded"
        )

    def test_active_key_upgraded_not_deactivated_one(self, migrated_pg):
        """I26(b): the user's oldest key is deactivated; a newer ACTIVE key exists.

        provision must upgrade the ACTIVE key, never the dead (active=FALSE) one.
        """
        free_id = _plan_id(migrated_pg, "free")
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "deaduser", "dead@example.com")
        # Oldest key (lowest id) — then deactivate it.
        _raw, _prefix, dead_key_id = auth_store().create_api_key(
            name="Old key (deaduser)", user_id=user_id
        )
        auth_store().deactivate_api_key(dead_key_id)
        # Newer, still-active key.
        _raw, _prefix, live_key_id = auth_store().create_api_key(
            name="Live key (deaduser)", user_id=user_id
        )
        assert dead_key_id < live_key_id, "dead key must be the id-ASC first"

        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="prov_dead", plan_id=pro_id, source="polar",
            status="active", buyer_email="dead@example.com",
        )
        returned = provisioning.provision_or_upgrade(sub_id, user_id)

        assert returned == live_key_id, "must upgrade the ACTIVE key, not the dead one"
        assert _key_plan_id(migrated_pg, live_key_id) == pro_id, "active key on pro"
        assert _key_plan_id(migrated_pg, dead_key_id) == free_id, (
            "deactivated key must stay on its original free plan — never touched"
        )

    def test_mints_new_key_when_only_key_is_deactivated(self, migrated_pg):
        """I26(b) corollary: if the user's ONLY key is deactivated, mint a fresh one."""
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "alldead", "alldead@example.com")
        _raw, _prefix, dead_key_id = auth_store().create_api_key(
            name="Only key (alldead)", user_id=user_id
        )
        auth_store().deactivate_api_key(dead_key_id)

        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="prov_alldead", plan_id=pro_id, source="polar",
            status="active", buyer_email="alldead@example.com",
        )
        returned = provisioning.provision_or_upgrade(sub_id, user_id)

        assert returned != dead_key_id, "must not provision onto the dead key"
        assert _key_plan_id(migrated_pg, returned) == pro_id
        assert _count_keys_for_user(migrated_pg, user_id) == 2, "a fresh key was minted"


# ---------------------------------------------------------------------------
# P5: claim_subscription_for_user
# ---------------------------------------------------------------------------

class TestClaimSubscriptionForUser:
    def test_claims_unclaimed_active_sub_for_email(self, migrated_pg):
        pro_id = _plan_id(migrated_pg, "pro")
        user_id = _make_user(migrated_pg, "claimu", "claimu@example.com")
        auth_store().create_api_key(name="Default key (claimu)", user_id=user_id)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="claim_active", plan_id=pro_id, source="polar",
            status="active", buyer_email="claimu@example.com",
        )

        provisioned = provisioning.claim_subscription_for_user(
            user_id, "claimu@example.com"
        )
        assert len(provisioned) == 1
        sub = subscription_store().get_by_id(sub_id)
        assert sub["claimed_user_id"] == user_id
        assert sub["api_key_id"] is not None

    def test_no_unclaimed_subs_returns_empty(self, migrated_pg):
        user_id = _make_user(migrated_pg, "nosub", "nosub@example.com")
        provisioned = provisioning.claim_subscription_for_user(
            user_id, "nosub@example.com"
        )
        assert provisioned == []

    def test_best_effort_never_raises_on_bad_data(self, migrated_pg):
        """A non-existent user id (FK-violating link) must not raise into auth."""
        pro_id = _plan_id(migrated_pg, "pro")
        subscription_store().upsert_by_external_ref(
            external_ref="claim_bad", plan_id=pro_id, source="polar",
            status="active", buyer_email="bad@example.com",
        )
        # user id 9_999_999 does not exist → link_to_user/provision would FK-error;
        # claim_subscription_for_user must swallow it and return [] (best-effort).
        result = provisioning.claim_subscription_for_user(9_999_999, "bad@example.com")
        assert result == [], "best-effort claim must not raise on bad data"


# ---------------------------------------------------------------------------
# #1 CLAIM-FIRST concurrency: one seat → at most one paid key, never two
# ---------------------------------------------------------------------------

class TestClaimFirstConcurrency:
    def test_concurrent_claims_same_user_no_key_mint_only_one_paid_key(
        self, migrated_pg
    ):
        """#1: two CONCURRENT claim sweeps for the same keyless user + one sub →
        exactly ONE paid key minted, never two.

        Email is unique per account, so the dangerous race is the SAME buyer
        logging in twice in parallel (two tabs / retried request) before either
        has a key.  Without claim-FIRST both sweeps could each mint a fresh key
        and set it to the paid plan → two paid keys for one seat.  The atomic
        claim CAS (taken BEFORE provisioning) lets exactly one sweep proceed; the
        other loses the CAS and mints/upgrades nothing.

        Two real threads (each its own pooled connection) exercise the CAS at the
        DB row-lock level, not a serialized simulation.
        """
        import threading

        pro_id = _plan_id(migrated_pg, "pro")
        email = "race@example.com"
        user_id = _make_user(migrated_pg, "raceu", email)
        # Deliberately NO api key yet: each winning sweep would mint one.
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="race_sub", plan_id=pro_id, source="polar",
            status="active", buyer_email=email,
        )

        results: list[list[int]] = []
        barrier = threading.Barrier(2)

        def _claim():
            barrier.wait()  # maximise overlap on the CAS
            results.append(provisioning.claim_subscription_for_user(user_id, email))

        t1 = threading.Thread(target=_claim)
        t2 = threading.Thread(target=_claim)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one sweep provisioned the single seat.
        provisioned_total = sum(len(r) for r in results)
        assert provisioned_total == 1, (
            "exactly one of two concurrent claims may provision the single seat "
            f"(got {provisioned_total})"
        )

        sub = subscription_store().get_by_id(sub_id)
        assert sub["claimed_user_id"] == user_id

        # The seat yielded exactly ONE paid key — never two.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM api_keys WHERE user_id = %s AND plan_id = %s",
                (user_id, pro_id),
            )
            paid_count = cur.fetchone()[0]
        assert paid_count == 1, "one seat must never yield two paid keys (#1)"
        # And no orphan free keys minted by a losing race either.
        assert _count_keys_for_user(migrated_pg, user_id) == 1, (
            "a losing concurrent claim must not mint an extra key"
        )

    def test_claim_first_taken_before_provision(self, migrated_pg, monkeypatch):
        """#1 ordering: the claim CAS must run BEFORE provision_or_upgrade.

        If provisioning were attempted first (old invariant), a sub whose CAS we
        LOSE would still get a key.  Here we make provision_or_upgrade blow up; a
        correct claim-FIRST implementation only reaches provision AFTER a winning
        CAS, so the sub is left claimed (CAS won) but unprovisioned — never a key
        minted ahead of the claim.
        """
        pro_id = _plan_id(migrated_pg, "pro")
        email = "order@example.com"
        user_id = _make_user(migrated_pg, "orderu", email)
        auth_store().create_api_key(name="ord", user_id=user_id)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="order_sub", plan_id=pro_id, source="polar",
            status="active", buyer_email=email,
        )

        calls = {"claimed_before_provision": None}
        real_provision = provisioning.provision_or_upgrade

        def _spy_provision(subscription_id, uid):  # noqa: ANN001
            # Observe the sub state at the moment provision is invoked.
            s = subscription_store().get_by_id(subscription_id)
            calls["claimed_before_provision"] = s["claimed_user_id"]
            return real_provision(subscription_id, uid)

        monkeypatch.setattr(provisioning, "provision_or_upgrade", _spy_provision)
        provisioning.claim_subscription_for_user(user_id, email)

        assert calls["claimed_before_provision"] == user_id, (
            "claim CAS must be committed BEFORE provision_or_upgrade is called"
        )
        # The spy delegated to the real provision, so the sub is fully provisioned.
        sub = subscription_store().get_by_id(sub_id)
        assert sub["api_key_id"] is not None


# ---------------------------------------------------------------------------
# #4 mid-sequence provision failure → claimed-but-unprovisioned recovery
# ---------------------------------------------------------------------------

class TestMidProvisionFailureRecovery:
    def test_claim_first_then_provision_fail_then_retry_converges(
        self, migrated_pg, monkeypatch
    ):
        """#4: with claim-FIRST, a provision crash AFTER the claim must still
        converge — the next login's claimed-but-unprovisioned scan re-provisions.

        Sequence:
          1. claim_subscription_for_user wins the CAS (sub.claimed_user_id=user).
          2. provision_or_upgrade raises mid-flight (link_to_api_key fails once).
             → sub is claimed but api_key_id IS NULL (no longer surfaced by
               find_unclaimed_active_by_email).
          3. A second login re-runs claim_subscription_for_user; the
             claimed-but-unprovisioned scan picks the sub up and finishes it.

        The test FAILS if recovery is missing (sub orphaned: claimed, no key).
        """
        pro_id = _plan_id(migrated_pg, "pro")
        email = "recover@example.com"
        user_id = _make_user(migrated_pg, "recu", email)
        auth_store().create_api_key(name="rec", user_id=user_id)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="recover_sub", plan_id=pro_id, source="polar",
            status="active", buyer_email=email,
        )

        real_link = subscription_store().link_to_api_key
        calls = {"n": 0}

        def _flaky_link(self, subscription_id, api_key_id):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated provision crash after claim")
            return real_link(subscription_id, api_key_id)

        from src.db.subscription_registry import SubscriptionStore
        monkeypatch.setattr(SubscriptionStore, "link_to_api_key", _flaky_link)

        # First login: CAS wins, provision crashes → claimed but unprovisioned.
        first = provisioning.claim_subscription_for_user(user_id, email)
        assert first == [], "the crashing provision must not report success"
        sub = subscription_store().get_by_id(sub_id)
        assert sub["claimed_user_id"] == user_id, (
            "claim-FIRST: the CAS committed the claim before the crash"
        )
        assert sub["api_key_id"] is None, "provision crashed before linking the key"
        # Crucially: it no longer surfaces as UNCLAIMED (claim already took).
        unclaimed = subscription_store().find_unclaimed_active_by_email(email)
        assert all(r["id"] != sub_id for r in unclaimed), (
            "a claimed sub must NOT be re-claimable via the unclaimed scan"
        )

        # Second login: claimed-but-unprovisioned scan must finish the job.
        second = provisioning.claim_subscription_for_user(user_id, email)
        assert len(second) == 1, "retry must re-provision the orphaned claimed sub"
        sub = subscription_store().get_by_id(sub_id)
        assert sub["api_key_id"] is not None, "key linked on retry"
        assert _key_plan_id(migrated_pg, sub["api_key_id"]) == pro_id, (
            "recovered key must be on the purchased plan"
        )
