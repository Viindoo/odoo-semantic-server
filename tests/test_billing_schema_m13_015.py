# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for schema additions gộp vào m13_014_billing_p1.sql
(previously in separate migrations m13_015 and m13_016 — now merged into m13_014)
— behaviour cases only.

One-shot catalog assertions (S1-S3 column existence/defaults via information_schema,
S6 terms_accepted_at existence/nullability) were removed — covered by
test_squashed_baseline.py golden snapshot.

Kept behaviour cases:
  S4  Prices seed applied: pro gets {"USD": 1900}, team gets {"USD": 3900},
      free/unlimited get {"USD": 0}.
  S6b terms_accepted_at can store a timestamp (write+read roundtrip).

The S5 / S6b per-file direct-re-run idempotency cases were removed after the
WI-2A squash folded m13_014_billing_p1.sql into 0001_initial.sql; baseline
idempotency is covered by test_migrate_is_idempotent (run_migrations twice) and
test_prod_sim_no_reapply (test_squashed_baseline.py).
  S7  schedule_cancellation sets cancel_at_period_end=TRUE + cancelled_at,
      status stays 'active'.
  S8  list_by_user returns plan_slug + plan_name + cancel_at_period_end.
  S9  cancel_at_period_end is in _ALLOWED_UPDATE_COLS; update_fields can set it.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.db.migrate import run_migrations
from src.db.pg import subscription_store
from src.db.subscription_registry import _ALLOWED_UPDATE_COLS

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BILLING_TABLES = ["billing_webhook_events", "subscriptions"]


@pytest.fixture(autouse=True)
def _reset_billing_tables(pg_conn):
    """TRUNCATE billing tables before/after each test (non-destructive)."""
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
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = 'free'")
        row = cur.fetchone()
    assert row is not None, "'free' plan must exist after migrations"
    return row[0]


def _plan_prices(conn, slug: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT prices FROM plans WHERE slug = %s", (slug,))
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# S4: prices seed values applied correctly
# ---------------------------------------------------------------------------

class TestM13015PricesSeed:
    """S4: Seed data applied for pro/team/free/unlimited after migrations."""

    def test_pro_prices_seeded(self, migrated_pg):
        prices = _plan_prices(migrated_pg, "pro")
        assert prices is not None, "pro plan must exist"
        assert prices.get("USD") == 1900, (
            f"pro USD price must be 1900 cents, got {prices!r}"
        )
        # VND display deferred — prices seed is USD-only; no VND key expected.
        assert "VND" not in prices, (
            f"pro prices must not contain VND key (USD-only display), got {prices!r}"
        )

    def test_team_prices_seeded(self, migrated_pg):
        prices = _plan_prices(migrated_pg, "team")
        assert prices is not None, "team plan must exist"
        assert prices.get("USD") == 3900, (
            f"team USD price must be 3900 cents, got {prices!r}"
        )
        # VND display deferred — prices seed is USD-only; no VND key expected.
        assert "VND" not in prices, (
            f"team prices must not contain VND key (USD-only display), got {prices!r}"
        )

    def test_free_prices_seeded(self, migrated_pg):
        prices = _plan_prices(migrated_pg, "free")
        assert prices is not None, "free plan must exist"
        assert prices.get("USD") == 0, (
            f"free USD price must be 0, got {prices!r}"
        )

    def test_unlimited_prices_seeded(self, migrated_pg):
        prices = _plan_prices(migrated_pg, "unlimited")
        assert prices is not None, "unlimited plan must exist"
        assert prices.get("USD") == 0, (
            f"unlimited USD price must be 0, got {prices!r}"
        )


# ---------------------------------------------------------------------------
# S6b: m13_016 — terms_accepted_at write-read roundtrip
# ---------------------------------------------------------------------------

class TestM13016UserConsent:
    """S6b: terms_accepted_at column can store a timestamp.

    Originally verified by m13_016_user_consent.sql; now gộp vào m13_014_billing_p1.sql
    (section 7) and squashed into 0001_initial.sql. Catalog checks (column
    existence/nullability) removed — baseline covers. The per-file direct-re-run
    idempotency case was removed post-squash (m13_014_billing_p1.sql no longer
    exists; idempotency covered by test_migrate_is_idempotent +
    test_prod_sim_no_reapply in test_squashed_baseline.py).
    """

    def test_terms_accepted_at_can_store_timestamp(self, migrated_pg):
        """Write and read back a consent timestamp."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users (username, email, password_hash)"
                " VALUES ('consent_test_user', 'consent@example.com', 'x')"
                " RETURNING id"
            )
            user_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE webui_users SET terms_accepted_at = now() WHERE id = %s",
                (user_id,),
            )
        migrated_pg.commit()

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT terms_accepted_at FROM webui_users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] is not None, "terms_accepted_at must store a non-NULL timestamp"


# ---------------------------------------------------------------------------
# S7: schedule_cancellation — cancel_at_period_end=TRUE, cancelled_at set,
#     status stays 'active'
# ---------------------------------------------------------------------------

class TestScheduleCancellation:
    """S7: schedule_cancellation schedules voluntary cancel without changing status."""

    def test_schedule_cancellation_sets_flag(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sched_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="sched1@example.com",
        )
        subscription_store().schedule_cancellation(sub_id)

        row = subscription_store().get_by_id(sub_id)
        assert row["cancel_at_period_end"] is True, (
            "cancel_at_period_end must be TRUE after schedule_cancellation"
        )

    def test_schedule_cancellation_sets_cancelled_at(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sched_02",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="sched2@example.com",
        )
        subscription_store().schedule_cancellation(sub_id)

        row = subscription_store().get_by_id(sub_id)
        assert row["cancelled_at"] is not None, (
            "cancelled_at must record the schedule-cancellation timestamp"
        )

    def test_schedule_cancellation_status_stays_active(self, migrated_pg):
        """Voluntary cancel does NOT change status — key stays usable until period end."""
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sched_03",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="sched3@example.com",
        )
        subscription_store().schedule_cancellation(sub_id)

        row = subscription_store().get_by_id(sub_id)
        assert row["status"] == "active", (
            f"status must remain 'active' after schedule_cancellation, got {row['status']!r}"
        )

    def test_mark_cancelled_still_changes_status_to_cancelled(self, migrated_pg):
        """Regression: involuntary mark_cancelled must still set status='cancelled'."""
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_sched_04",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="sched4@example.com",
        )
        subscription_store().mark_cancelled(sub_id)

        row = subscription_store().get_by_id(sub_id)
        assert row["status"] == "cancelled", (
            "involuntary mark_cancelled must set status='cancelled'"
        )
        assert row["cancel_at_period_end"] is False, (
            "involuntary cancel must NOT set cancel_at_period_end (stays FALSE)"
        )


# ---------------------------------------------------------------------------
# S8: list_by_user — plan_slug + plan_name + cancel_at_period_end included
# ---------------------------------------------------------------------------

class TestListByUserEnrichment:
    """S8: list_by_user returns plan_slug, plan_name, and cancel_at_period_end."""

    def _create_user(self, conn, username: str, email: str) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users (username, email, password_hash)"
                " VALUES (%s, %s, 'x') RETURNING id",
                (username, email),
            )
            user_id = cur.fetchone()[0]
        conn.commit()
        return user_id

    def test_list_by_user_returns_plan_slug(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        user_id = self._create_user(migrated_pg, "list_test_1", "list1@example.com")
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_list_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="list1@example.com",
            claimed_user_id=user_id,
        )
        rows = subscription_store().list_by_user(user_id)
        assert any(r["id"] == sub_id for r in rows)
        sub = next(r for r in rows if r["id"] == sub_id)
        assert "plan_slug" in sub, "list_by_user must return plan_slug key"
        assert sub["plan_slug"] == "free", (
            f"plan_slug must be 'free', got {sub['plan_slug']!r}"
        )

    def test_list_by_user_returns_plan_name(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        user_id = self._create_user(migrated_pg, "list_test_2", "list2@example.com")
        subscription_store().upsert_by_external_ref(
            external_ref="polar_list_02",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="list2@example.com",
            claimed_user_id=user_id,
        )
        rows = subscription_store().list_by_user(user_id)
        assert rows, "list_by_user must return at least one row"
        assert "plan_name" in rows[0], "list_by_user must return plan_name key"
        assert rows[0]["plan_name"] is not None, "plan_name must not be None"

    def test_list_by_user_returns_cancel_at_period_end(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        user_id = self._create_user(migrated_pg, "list_test_3", "list3@example.com")
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_list_03",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="list3@example.com",
            claimed_user_id=user_id,
        )
        rows = subscription_store().list_by_user(user_id)
        sub = next(r for r in rows if r["id"] == sub_id)
        assert "cancel_at_period_end" in sub, (
            "list_by_user must include cancel_at_period_end"
        )
        assert sub["cancel_at_period_end"] is False, (
            "cancel_at_period_end must default to False for new subscriptions"
        )

    def test_list_by_user_returns_cancel_true_after_schedule(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        user_id = self._create_user(migrated_pg, "list_test_4", "list4@example.com")
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_list_04",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="list4@example.com",
            claimed_user_id=user_id,
        )
        subscription_store().schedule_cancellation(sub_id)
        rows = subscription_store().list_by_user(user_id)
        sub = next(r for r in rows if r["id"] == sub_id)
        assert sub["cancel_at_period_end"] is True, (
            "list_by_user must reflect cancel_at_period_end=TRUE after schedule_cancellation"
        )


# ---------------------------------------------------------------------------
# S9: cancel_at_period_end in _ALLOWED_UPDATE_COLS / update_fields round-trip
# ---------------------------------------------------------------------------

class TestCancelAtPeriodEndAllowed:
    """S9: cancel_at_period_end is in _ALLOWED_UPDATE_COLS and update_fields honours it."""

    def test_cancel_at_period_end_in_allowed_cols(self):
        assert "cancel_at_period_end" in _ALLOWED_UPDATE_COLS, (
            "cancel_at_period_end must be in _ALLOWED_UPDATE_COLS so update_fields can set it"
        )

    def test_update_fields_can_set_cancel_at_period_end(self, migrated_pg):
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_allowed_01",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="allowed1@example.com",
        )
        result = subscription_store().update_fields(
            sub_id, {"cancel_at_period_end": True}
        )
        assert result is True
        row = subscription_store().get_by_id(sub_id)
        assert row["cancel_at_period_end"] is True, (
            "update_fields must persist cancel_at_period_end=True"
        )

    def test_update_fields_can_reset_cancel_at_period_end(self, migrated_pg):
        """update_fields can also reset the flag (admin undo-cancel scenario)."""
        plan_id = _free_plan_id(migrated_pg)
        sub_id = subscription_store().upsert_by_external_ref(
            external_ref="polar_allowed_02",
            plan_id=plan_id,
            source="polar",
            status="active",
            buyer_email="allowed2@example.com",
        )
        subscription_store().schedule_cancellation(sub_id)
        subscription_store().update_fields(sub_id, {"cancel_at_period_end": False})
        row = subscription_store().get_by_id(sub_id)
        assert row["cancel_at_period_end"] is False, (
            "update_fields must be able to reset cancel_at_period_end to False"
        )
