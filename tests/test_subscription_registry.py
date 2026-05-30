# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for src/db/subscription_registry.py.

Business intent:
  T1  upsert_by_external_ref: inserts a new row, then upserts same external_ref
      → exactly one row, fields updated (no duplicate).
  T2  record_webhook_event: first call is_new=True; replay (same vendor+event_id)
      → is_new=False, no second row, existing id returned. Now a 3-tuple
      (pk, is_new, already_processed) — pk NEVER None.
  T3  _safe_update_clause: rejects unknown column (ValueError before DB call);
      allowed columns use sql.Identifier via update_fields round-trip.
  T4  find_unclaimed_active_by_email: filters by status='active' AND
      claimed_user_id IS NULL (case-insensitive email).
  T5  update_fields sets updated_at and persists the value;
      link_to_* helpers set the correct FK columns; mark_cancelled sets
      status='cancelled' and cancelled_at IS NOT NULL.
  T6  mark_event_processed sets processed_at + subscription_id + processing_error.

WI-2 additions (money-critical / concurrency):
  #1   claim_unclaimed_for_user: atomic CAS — first claimer True, second False,
       already-claimed False, nonexistent id False; never re-points an owner.
  #10  record_webhook_event already_processed reflects a prior SUCCESSFUL run
       (mark_event_processed), distinct from mere row existence → reprocess guard.
  #5   monotonic upsert: an out-of-order OLDER last_event_at must not overwrite
       newer status; high-water-mark never regresses; NULL ts → last-write-wins.
  #11  list_all: explicit projection + plan_slug/plan_name enrichment + LIMIT/OFFSET.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import time
from datetime import UTC

import pytest

from src.db.migrate import run_migrations
from src.db.pg import subscription_store

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

def _free_plan_id(conn) -> int:
    """Return the id of the 'free' plan (seeded by migrations)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = 'free'")
        row = cur.fetchone()
    assert row is not None, "'free' plan must exist after migrations"
    return row[0]


def _count_rows(conn, table: str, **where) -> int:
    """Count rows in table matching keyword-argument equality conditions."""
    if not where:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            return cur.fetchone()[0]
    clauses = " AND ".join(f"{k} = %s" for k in where)
    vals = list(where.values())
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {clauses}", vals)  # noqa: S608
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# T1: upsert_by_external_ref — insert then upsert same ref → one row
# ---------------------------------------------------------------------------

class TestUpsertByExternalRef:
    """T1: upsert_by_external_ref is idempotent on external_ref."""

    def test_insert_creates_one_row(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_001",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="buyer@example.com",
        )
        assert sub_id > 0
        assert _count_rows(migrated_pg, "subscriptions", external_ref="polar_sub_001") == 1

    def test_upsert_same_ref_returns_same_id(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        id1 = subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_002",
            plan_id=plan_id,
            source="polar",
            status="pending",
            buyer_email="buyer2@example.com",
        )
        id2 = subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_002",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="buyer2@example.com",
        )
        # Same row — same id, no duplicate
        assert id1 == id2
        assert _count_rows(migrated_pg, "subscriptions", external_ref="polar_sub_002") == 1

    def test_upsert_updates_status(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_003",
            plan_id=plan_id,
            source="polar",
            status="pending",
            buyer_email="buyer3@example.com",
        )
        subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_003",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="buyer3@example.com",
        )
        row = subscription_store().get_by_id(sub_id)
        assert row is not None
        assert row["status"] == "active", (
            f"upsert should update status to 'active', got {row['status']!r}"
        )

    def test_upsert_updates_amount_cents(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_004",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="buyer4@example.com",
            amount_cents=1900,
            currency="USD",
        )
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_004",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="buyer4@example.com",
            amount_cents=3900,
            currency="USD",
        )
        row = subscription_store().get_by_id(sub_id)
        assert row["amount_cents"] == 3900


# ---------------------------------------------------------------------------
# T2: record_webhook_event — idempotency ledger
# ---------------------------------------------------------------------------

class TestRecordWebhookEvent:
    """T2: record_webhook_event (vendor, event_id) is the idempotency key."""

    def test_first_call_is_new(self, migrated_pg):
        event_pk, is_new, already_processed = subscription_store().record_webhook_event(
            vendor="polar",
            event_id="evt_aaa001",
            event_type="subscription.created",
            signature_valid=True,
            payload={"data": {"id": "sub_001"}},
        )
        assert is_new is True
        assert already_processed is False
        assert event_pk is not None and event_pk > 0

    def test_replay_returns_is_new_false(self, migrated_pg):
        pk1, is_new1, proc1 = subscription_store().record_webhook_event(
            vendor="polar",
            event_id="evt_bbb001",
            event_type="subscription.created",
            signature_valid=True,
            payload={"data": {"id": "sub_002"}},
        )
        pk2, is_new2, proc2 = subscription_store().record_webhook_event(
            vendor="polar",
            event_id="evt_bbb001",
            event_type="subscription.created",
            signature_valid=True,
            payload={"data": {"id": "sub_002"}},
        )
        assert is_new1 is True
        assert is_new2 is False
        assert proc1 is False
        assert proc2 is False, "replay of an UN-processed row is not already_processed"
        assert pk1 == pk2, "replay must return the id of the existing row"
        assert pk2 is not None, "pk must never be None on replay (no-op DO UPDATE)"

    def test_replay_does_not_create_second_row(self, migrated_pg):
        subscription_store().record_webhook_event(
            vendor="polar",
            event_id="evt_ccc001",
            event_type="subscription.updated",
            signature_valid=True,
            payload={},
        )
        subscription_store().record_webhook_event(
            vendor="polar",
            event_id="evt_ccc001",
            event_type="subscription.updated",
            signature_valid=True,
            payload={},
        )
        assert (
            _count_rows(migrated_pg, "billing_webhook_events", event_id="evt_ccc001") == 1
        ), "replay must not create a second row"

    def test_same_event_id_different_vendor_is_new(self, migrated_pg):
        """Same event_id but different vendor → separate rows (idempotency key is composite)."""
        _, is_new1, _ = subscription_store().record_webhook_event(
            vendor="polar",
            event_id="evt_ddd001",
            event_type="order.paid",
            signature_valid=True,
            payload={},
        )
        _, is_new2, _ = subscription_store().record_webhook_event(
            vendor="test",
            event_id="evt_ddd001",
            event_type="order.paid",
            signature_valid=True,
            payload={},
        )
        assert is_new1 is True
        assert is_new2 is True
        assert (
            _count_rows(migrated_pg, "billing_webhook_events", event_id="evt_ddd001") == 2
        )


# ---------------------------------------------------------------------------
# T3: _safe_update_clause — frozenset gate + sql.Identifier
# ---------------------------------------------------------------------------

class TestSafeUpdateClause:
    """T3: _safe_update_clause uses frozenset gate and sql.Identifier for allowed cols."""

    def test_unknown_column_raises_value_error_before_db(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_safe_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="safe1@example.com",
        )
        # external_ref is intentionally NOT in _ALLOWED_UPDATE_COLS (idempotency key)
        with pytest.raises(ValueError, match="unknown column"):
            subscription_store().update_fields(
                sub_id,
                {"external_ref": "should_not_update"},
            )

    def test_unknown_column_sql_injection_attempt(self, migrated_pg):
        """A classic SQLi identifier pattern is rejected by the frozenset gate."""
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_safe_02",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="safe2@example.com",
        )
        with pytest.raises(ValueError, match="unknown column"):
            subscription_store().update_fields(
                sub_id,
                {"status = 'cancelled'; DROP TABLE subscriptions; --": "x"},
            )
        # Table must still exist
        assert _count_rows(migrated_pg, "subscriptions") >= 1

    def test_allowed_column_update_persists(self, migrated_pg):
        """update_fields with an allowed column round-trips correctly."""
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_safe_03",
            plan_id=plan_id,
            source="polar",
            status="pending",
            buyer_email="safe3@example.com",
        )
        result = subscription_store().update_fields(sub_id, {"status": "active"})
        assert result is True
        row = subscription_store().get_by_id(sub_id)
        assert row["status"] == "active"

    def test_empty_updates_raises_value_error(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sub_safe_04",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="safe4@example.com",
        )
        with pytest.raises(ValueError):
            subscription_store().update_fields(sub_id, {})

    def test_nonexistent_id_returns_false(self, migrated_pg):
        result = subscription_store().update_fields(999_999_999, {"status": "cancelled"})
        assert result is False


# ---------------------------------------------------------------------------
# T4: find_unclaimed_active_by_email
# ---------------------------------------------------------------------------

class TestFindUnclaimedActiveByEmail:
    """T4: find_unclaimed_active_by_email filters correctly."""

    def test_returns_active_unclaimed_row(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_claim_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="claim@example.com",
        )
        rows = subscription_store().find_unclaimed_active_by_email("claim@example.com")
        assert any(r["id"] == sub_id for r in rows), (
            "should return the unclaimed active subscription"
        )

    def test_case_insensitive_email_match(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_claim_02",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="ClaimUser@Example.COM",
        )
        rows = subscription_store().find_unclaimed_active_by_email("claimuser@example.com")
        assert any(r["id"] == sub_id for r in rows), (
            "email match must be case-insensitive"
        )

    def test_does_not_return_claimed_rows(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_claim_03",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="claimed@example.com",
        )
        # Simulate claim: set claimed_user_id (we use update_fields with a
        # fake user_id; FK is nullable, but we need a real user_id to avoid
        # FK violation. Use link_to_user would also trigger FK.
        # Instead, directly insert a user then link.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users (username, email, password_hash)"
                " VALUES ('testclaim', 'claimed@example.com', 'x')"
                " RETURNING id"
            )
            user_id = cur.fetchone()[0]
        migrated_pg.commit()
        subscription_store().link_to_user(sub_id, user_id)

        rows = subscription_store().find_unclaimed_active_by_email("claimed@example.com")
        assert not any(r["id"] == sub_id for r in rows), (
            "should NOT return a claimed subscription"
        )

    def test_does_not_return_non_active_status(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        subscription_store().upsert_by_external_ref(
            external_ref="polar_claim_04",
            plan_id=plan_id,
            source="polar",
            status="cancelled",
            buyer_email="cancelled@example.com",
        )
        rows = subscription_store().find_unclaimed_active_by_email("cancelled@example.com")
        assert rows == [], "should NOT return a cancelled subscription"

    def test_empty_result_for_unknown_email(self, migrated_pg):
        rows = subscription_store().find_unclaimed_active_by_email("nobody@unknown.example")
        assert rows == []


# ---------------------------------------------------------------------------
# T5: update_fields, link_to_*, mark_cancelled
# ---------------------------------------------------------------------------

class TestUpdateAndLinkHelpers:
    """T5: update_fields sets updated_at; link_to_* set FK cols; mark_cancelled sets status."""

    def test_update_fields_sets_updated_at(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_upd_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="upd1@example.com",
        )
        row_before = subscription_store().get_by_id(sub_id)
        time.sleep(0.01)  # ensure clock advances
        subscription_store().update_fields(sub_id, {"seats": 3})
        row_after = subscription_store().get_by_id(sub_id)

        assert row_after["seats"] == 3
        assert row_after["updated_at"] >= row_before["updated_at"], (
            "updated_at must be refreshed after update_fields"
        )

    def test_link_to_api_key_sets_api_key_id(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_link_01",
            plan_id=plan_id,
            source="polar",
            status="pending",
            buyer_email="link1@example.com",
        )
        # Create a real api_key row to satisfy the FK.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id)"
                " VALUES ('test-link-key', 'hash_link1', 'osm_testlin', %s)"
                " RETURNING id",
                (plan_id,),
            )
            key_id = cur.fetchone()[0]
        migrated_pg.commit()

        subscription_store().link_to_api_key(sub_id, key_id)
        row = subscription_store().get_by_id(sub_id)
        assert row["api_key_id"] == key_id

    def test_link_to_tenant_sets_tenant_id(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_link_02",
            plan_id=plan_id,
            source="polar",
            status="pending",
            buyer_email="link2@example.com",
        )
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (name) VALUES ('test-tenant-link') RETURNING id"
            )
            tenant_id = cur.fetchone()[0]
        migrated_pg.commit()

        subscription_store().link_to_tenant(sub_id, tenant_id)
        row = subscription_store().get_by_id(sub_id)
        assert row["tenant_id"] == tenant_id

    def test_link_to_user_sets_claimed_user_id(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_link_03",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="link3@example.com",
        )
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users (username, email, password_hash)"
                " VALUES ('link3user', 'link3@example.com', 'x') RETURNING id"
            )
            user_id = cur.fetchone()[0]
        migrated_pg.commit()

        subscription_store().link_to_user(sub_id, user_id)
        row = subscription_store().get_by_id(sub_id)
        assert row["claimed_user_id"] == user_id

    def test_mark_cancelled_sets_status_and_cancelled_at(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_cancel_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="cancel1@example.com",
        )
        subscription_store().mark_cancelled(sub_id)
        row = subscription_store().get_by_id(sub_id)
        assert row["status"] == "cancelled", (
            f"status must be 'cancelled' after mark_cancelled, got {row['status']!r}"
        )
        assert row["cancelled_at"] is not None, (
            "cancelled_at must be set after mark_cancelled"
        )


# ---------------------------------------------------------------------------
# T6: mark_event_processed
# ---------------------------------------------------------------------------

class TestMarkEventProcessed:
    """T6: mark_event_processed sets processed_at, subscription_id, processing_error."""

    def test_mark_event_processed_sets_fields(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_proc_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="proc1@example.com",
        )
        event_pk, _, _ = subscription_store().record_webhook_event(
            vendor="polar",
            event_id="evt_proc_001",
            event_type="subscription.created",
            signature_valid=True,
            payload={},
        )
        subscription_store().mark_event_processed(event_pk, sub_id)

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT processed_at, subscription_id, processing_error"
                " FROM billing_webhook_events WHERE id = %s",
                (event_pk,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] is not None, "processed_at must be set"
        assert row[1] == sub_id, "subscription_id must match"
        assert row[2] is None, "processing_error must be NULL when no error"

    def test_mark_event_processed_with_error(self, migrated_pg):
        event_pk, _, _ = subscription_store().record_webhook_event(
            vendor="test",
            event_id="evt_proc_002",
            event_type="order.paid",
            signature_valid=False,
            payload={},
        )
        subscription_store().mark_event_processed(
            event_pk, None, error="unknown product"
        )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT processed_at, subscription_id, processing_error"
                " FROM billing_webhook_events WHERE id = %s",
                (event_pk,),
            )
            row = cur.fetchone()
        assert row[0] is not None, "processed_at must be set even on error"
        assert row[1] is None, "subscription_id must be NULL on error"
        assert row[2] == "unknown product"


# ---------------------------------------------------------------------------
# I9/I14: polar.resolve_plan_id is self-sufficient when conn is None
# ---------------------------------------------------------------------------

class TestResolvePlanIdConnOptional:
    """resolve_plan_id(conn=None) must open its OWN pool connection and resolve."""

    def _set_product_map(self, conn, mapping: dict) -> None:
        """Write the billing.polar_product_map SYSTEM row + bust the LRU cache."""
        import json

        import src.settings as settings_mod
        with conn.cursor() as cur:
            # Remove any prior system row, then insert (scope='system', tenant NULL).
            cur.execute(
                "DELETE FROM app_settings"
                " WHERE key = 'billing.polar_product_map'"
                "   AND scope = 'system' AND tenant_id IS NULL"
            )
            cur.execute(
                "INSERT INTO app_settings"
                " (key, value_json, category, scope, tenant_id, data_type, default_value)"
                " VALUES ('billing.polar_product_map', %s, 'billing', 'system',"
                "         NULL, 'struct', '{}'::jsonb)",
                (json.dumps(mapping),),
            )
        conn.commit()
        settings_mod._cache.clear()  # ensure the next get_setting reads L2/L3 fresh

    def test_resolves_with_no_conn_via_own_pool(self, migrated_pg):
        from src.billing.polar import resolve_plan_id

        pro_id = _plan_id_for(migrated_pg, "pro")
        self._set_product_map(migrated_pg, {"prod_pro_123": "pro"})

        payload = {"data": {"id": "sub_1", "product_id": "prod_pro_123"}}
        # No conn passed → helper must open its own pool connection.
        assert resolve_plan_id(payload) == pro_id

    def test_unknown_product_raises_without_conn(self, migrated_pg):
        from src.billing.polar import resolve_plan_id

        self._set_product_map(migrated_pg, {})  # empty map
        payload = {"data": {"id": "sub_2", "product_id": "prod_missing"}}
        with pytest.raises(ValueError, match="unknown Polar product_id"):
            resolve_plan_id(payload)


# ---------------------------------------------------------------------------
# WI-2 #1: claim_unclaimed_for_user — atomic compare-and-set (first-writer-wins)
# ---------------------------------------------------------------------------

class TestClaimUnclaimedForUser:
    """#1: claim_unclaimed_for_user is a one-way CAS — exactly one winner."""

    def _make_user(self, conn, username: str, email: str) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users (username, email, password_hash)"
                " VALUES (%s, %s, 'x') RETURNING id",
                (username, email),
            )
            uid = cur.fetchone()[0]
        conn.commit()
        return uid

    def test_first_claim_wins_second_loses(self, migrated_pg):
        """Two sequential claims on the SAME unclaimed sub: first True, second False.

        Simulates the claim race: both requests target the same row; the CAS
        WHERE claimed_user_id IS NULL means only the first UPDATE matches a row.
        """
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_claim_cas_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="cas1@example.com",
        )
        user_a = self._make_user(migrated_pg, "cas_user_a", "cas_a@example.com")
        user_b = self._make_user(migrated_pg, "cas_user_b", "cas_b@example.com")

        won_first = subscription_store().claim_unclaimed_for_user(sub_id, user_a)
        won_second = subscription_store().claim_unclaimed_for_user(sub_id, user_b)

        assert won_first is True, "first claimer must win the CAS"
        assert won_second is False, "second claimer must lose — sub already claimed"

        # The winner's user_id is the persisted owner; loser never overwrites it.
        row = subscription_store().get_by_id(sub_id)
        assert row["claimed_user_id"] == user_a

    def test_claim_already_claimed_returns_false(self, migrated_pg):
        """Claiming a sub that is ALREADY claimed returns False (no overwrite)."""
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_claim_cas_02",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="cas2@example.com",
        )
        owner = self._make_user(migrated_pg, "cas_owner", "cas_owner@example.com")
        other = self._make_user(migrated_pg, "cas_other", "cas_other@example.com")
        subscription_store().link_to_user(sub_id, owner)  # pre-claim

        result = subscription_store().claim_unclaimed_for_user(sub_id, other)
        assert result is False
        row = subscription_store().get_by_id(sub_id)
        assert row["claimed_user_id"] == owner, "claim must not re-point owner"

    def test_claim_nonexistent_id_returns_false(self, migrated_pg):
        user = self._make_user(migrated_pg, "cas_nx", "cas_nx@example.com")
        assert subscription_store().claim_unclaimed_for_user(999_999_999, user) is False


# ---------------------------------------------------------------------------
# WI-2 #2/#10: record_webhook_event already_processed flag (reprocess guard)
# ---------------------------------------------------------------------------

class TestRecordWebhookEventAlreadyProcessed:
    """#10: already_processed reflects a prior SUCCESSFUL run, not mere existence."""

    def test_lifecycle_new_then_replay_then_processed(self, migrated_pg):
        store = subscription_store()
        # First sighting: brand-new, not processed.
        pk1, is_new1, proc1 = store.record_webhook_event(
            vendor="polar",
            event_id="evt_reproc_01",
            event_type="subscription.created",
            signature_valid=True,
            payload={},
        )
        assert (is_new1, proc1) == (True, False)

        # Replay BEFORE processing: same pk, not new, still not processed
        # → pipeline should RE-process (recorded-but-never-finished).
        pk2, is_new2, proc2 = store.record_webhook_event(
            vendor="polar",
            event_id="evt_reproc_01",
            event_type="subscription.created",
            signature_valid=True,
            payload={},
        )
        assert pk2 == pk1
        assert (is_new2, proc2) == (False, False)

        # Mark the event processed, then replay again.
        store.mark_event_processed(pk1, None)
        pk3, is_new3, proc3 = store.record_webhook_event(
            vendor="polar",
            event_id="evt_reproc_01",
            event_type="subscription.created",
            signature_valid=True,
            payload={},
        )
        assert pk3 == pk1
        assert is_new3 is False
        assert proc3 is True, "after mark_event_processed, replay is already_processed"


# ---------------------------------------------------------------------------
# WI-2 #5: monotonic upsert — out-of-order events must not regress authoritative state
# ---------------------------------------------------------------------------

class TestMonotonicUpsert:
    """#5: status/plan_id/seats only advance when last_event_at is non-decreasing."""

    def test_older_event_does_not_overwrite_newer_status(self, migrated_pg):
        from datetime import datetime, timedelta

        plan_id = _free_plan_id(migrated_pg)
        t2 = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
        t1 = t2 - timedelta(hours=1)  # older event
        t3 = t2 + timedelta(hours=1)  # newer event

        store = subscription_store()
        # Newest-known state first: cancelled at T2.
        sub_id = store.upsert_by_external_ref(
            external_ref="polar_mono_01",
            plan_id=plan_id,
            source="polar",
            status="cancelled",
            buyer_email="mono1@example.com",
            last_event_at=t2,
        )
        # An OLDER 'active' event (T1 < T2) arrives late — must NOT flip back.
        store.upsert_by_external_ref(
            external_ref="polar_mono_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="mono1@example.com",
            last_event_at=t1,
        )
        row = store.get_by_id(sub_id)
        assert row["status"] == "cancelled", (
            "an out-of-order older event must NOT overwrite the newer status"
        )

        # A genuinely newer 'active' event (T3 > T2) IS applied.
        store.upsert_by_external_ref(
            external_ref="polar_mono_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="mono1@example.com",
            last_event_at=t3,
        )
        row = store.get_by_id(sub_id)
        assert row["status"] == "active", "a newer event must advance the status"

    def test_high_water_mark_never_regresses(self, migrated_pg):
        from datetime import datetime, timedelta

        plan_id = _free_plan_id(migrated_pg)
        t2 = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
        t1 = t2 - timedelta(hours=1)

        store = subscription_store()
        sub_id = store.upsert_by_external_ref(
            external_ref="polar_mono_02",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="mono2@example.com",
            last_event_at=t2,
        )
        store.upsert_by_external_ref(
            external_ref="polar_mono_02",
            plan_id=plan_id,
            source="polar",
            status="cancelled",
            buyer_email="mono2@example.com",
            last_event_at=t1,  # older
        )
        row = store.get_by_id(sub_id)
        assert row["last_event_at"] == t2, (
            "last_event_at high-water-mark must stay at the newest timestamp"
        )

    def test_null_last_event_at_degrades_to_last_write_wins(self, migrated_pg):
        """Legacy callers omitting last_event_at keep last-write-wins semantics."""
        plan_id = _free_plan_id(migrated_pg)
        store = subscription_store()
        sub_id = store.upsert_by_external_ref(
            external_ref="polar_mono_03",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="mono3@example.com",
        )
        store.upsert_by_external_ref(
            external_ref="polar_mono_03",
            plan_id=plan_id,
            source="polar",
            status="cancelled",
            buyer_email="mono3@example.com",
        )
        row = store.get_by_id(sub_id)
        assert row["status"] == "cancelled", (
            "with NULL last_event_at the latest write must win (backward compat)"
        )


# ---------------------------------------------------------------------------
# WI-2 #11: list_all — admin pagination + plan enrichment
# ---------------------------------------------------------------------------

class TestListAll:
    """#11: list_all returns an explicit projection with plan_slug enrichment."""

    def test_returns_projection_with_plan_slug(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        subscription_store().upsert_by_external_ref(
            external_ref="polar_listall_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="listall1@example.com",
        )
        rows = subscription_store().list_all()
        assert len(rows) >= 1
        target = next(r for r in rows if r["external_ref"] == "polar_listall_01")
        assert target["plan_slug"] == "free"
        assert "plan_name" in target
        # Explicit projection includes the WI-1 monotonic + cancel columns.
        assert "last_event_at" in target
        assert "cancel_at_period_end" in target

    def test_limit_offset_paginate(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        for i in range(3):
            subscription_store().upsert_by_external_ref(
                external_ref=f"polar_listall_pg_{i}",
                plan_id=plan_id,
                source="polar",
                status="active",
                buyer_email=f"listallpg{i}@example.com",
            )
        page1 = subscription_store().list_all(limit=2, offset=0)
        page2 = subscription_store().list_all(limit=2, offset=2)
        assert len(page1) == 2
        ids_page1 = {r["id"] for r in page1}
        ids_page2 = {r["id"] for r in page2}
        assert ids_page1.isdisjoint(ids_page2), "pages must not overlap"


def _plan_id_for(conn, slug: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = %s", (slug,))
        row = cur.fetchone()
    assert row is not None, f"plan slug={slug!r} must exist after migrations"
    return row[0]
