# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for schema additions gộp vào m13_014_billing_p1.sql
(previously in separate migrations m13_015 and m13_016 — now merged into m13_014).

Business intent:
  S1  m13_014 (merged) is idempotent: applying it twice produces no error and the
      expected columns/defaults exist (cancel_at_period_end, prices, terms_accepted_at).
  S2  cancel_at_period_end DEFAULT FALSE is present on subscriptions.
  S3  prices JSONB DEFAULT '{}' is present on plans.
  S4  Prices seed applied: pro gets {"USD": 1900}, team gets {"USD": 3900},
      free/unlimited get {"USD": 0}. VND display deferred — no VND key in seed.
  S5  Seed is NOT clobbered on re-run (guard condition check).
  S6  terms_accepted_at TIMESTAMPTZ column exists on webui_users, nullable, idempotent.
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

def _col_exists(conn, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        return cur.fetchone() is not None


def _col_default(conn, table: str, column: str) -> str | None:
    """Return column_default string from information_schema (may be None)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_default FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        row = cur.fetchone()
    return row[0] if row else None


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
# S1/S2/S3: column existence + defaults after first apply (via run_migrations)
# ---------------------------------------------------------------------------

class TestM13015ColumnExistence:
    """S1: Expected columns exist with correct defaults after migrations."""

    def test_cancel_at_period_end_exists_on_subscriptions(self, migrated_pg):
        assert _col_exists(migrated_pg, "subscriptions", "cancel_at_period_end"), (
            "cancel_at_period_end column must exist on subscriptions after m13_015"
        )

    def test_cancel_at_period_end_default_is_false(self, migrated_pg):
        default = _col_default(migrated_pg, "subscriptions", "cancel_at_period_end")
        assert default is not None, "cancel_at_period_end must have a column default"
        assert "false" in default.lower(), (
            f"cancel_at_period_end DEFAULT must be false, got {default!r}"
        )

    def test_prices_exists_on_plans(self, migrated_pg):
        assert _col_exists(migrated_pg, "plans", "prices"), (
            "prices column must exist on plans after m13_015"
        )

    def test_prices_default_is_empty_jsonb(self, migrated_pg):
        default = _col_default(migrated_pg, "plans", "prices")
        assert default is not None, "prices must have a column default"
        assert "{}" in default, (
            f"prices DEFAULT must be '{{}}'::jsonb, got {default!r}"
        )


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
# S5: idempotency — re-applying m13_015 SQL does not clobber seeded prices
# ---------------------------------------------------------------------------

class TestM13015Idempotency:
    """S5: Re-running m13_014 (merged) SQL is a no-op (seeds guarded by WHERE prices='{}').

    These tests re-apply the full merged m13_014_billing_p1.sql to confirm that
    the cancel_at_period_end / prices / terms_accepted_at / waitlist-check-drop sections
    (originally from m13_015/m13_016/m13_017) remain idempotent after the merge.
    """

    def test_rerun_does_not_clobber_prices(self, migrated_pg):
        """Apply the merged m13_014 SQL a second time; pro prices must remain seeded."""
        import pathlib
        sql = (
            pathlib.Path(__file__).parent.parent
            / "migrations"
            / "m13_014_billing_p1.sql"
        ).read_text()
        with migrated_pg.cursor() as cur:
            cur.execute(sql)
        migrated_pg.commit()

        prices = _plan_prices(migrated_pg, "pro")
        assert prices is not None
        assert prices.get("USD") == 1900, (
            "Re-running m13_014 must NOT reset pro prices (guard WHERE prices='{}' failed)"
        )

    def test_rerun_cancel_at_period_end_no_error(self, migrated_pg):
        """ADD COLUMN IF NOT EXISTS — second run of merged m13_014 must not raise."""
        import pathlib
        sql = (
            pathlib.Path(__file__).parent.parent
            / "migrations"
            / "m13_014_billing_p1.sql"
        ).read_text()
        with migrated_pg.cursor() as cur:
            cur.execute(sql)
        migrated_pg.commit()
        # If we got here with no exception, idempotency holds.
        assert _col_exists(migrated_pg, "subscriptions", "cancel_at_period_end")


# ---------------------------------------------------------------------------
# S6: m13_016 — terms_accepted_at column on webui_users
# ---------------------------------------------------------------------------

class TestM13016UserConsent:
    """S6: terms_accepted_at TIMESTAMPTZ column on webui_users, nullable, idempotent.

    Originally verified by m13_016_user_consent.sql; now gộp vào m13_014_billing_p1.sql
    (section 7). Tests are unchanged — they verify the same business contract.
    """

    def test_terms_accepted_at_exists(self, migrated_pg):
        assert _col_exists(migrated_pg, "webui_users", "terms_accepted_at"), (
            "terms_accepted_at column must exist on webui_users after m13_014 (merged)"
        )

    def test_terms_accepted_at_is_nullable(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = 'webui_users' AND column_name = 'terms_accepted_at'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "YES", (
            "terms_accepted_at must be nullable (NULL = pre-consent legacy user)"
        )

    def test_m13016_is_idempotent(self, migrated_pg):
        """ADD COLUMN IF NOT EXISTS — re-running merged m13_014 SQL must not raise."""
        import pathlib
        sql = (
            pathlib.Path(__file__).parent.parent
            / "migrations"
            / "m13_014_billing_p1.sql"
        ).read_text()
        with migrated_pg.cursor() as cur:
            cur.execute(sql)
        migrated_pg.commit()
        assert _col_exists(migrated_pg, "webui_users", "terms_accepted_at")

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
