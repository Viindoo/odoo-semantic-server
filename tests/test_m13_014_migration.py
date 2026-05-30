# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_m13_014_migration.py
"""Migration tests for m13_014_billing_p1.sql.

m13_014 is the single unified billing migration (gộp từ m13_014..m13_017).
It covers: commercial plans columns + CHECK constraints, subscriptions table,
billing_webhook_events table, pricing seed, cancel_at_period_end flag,
per-currency prices JSONB, terms_accepted_at consent column, and
waitlist_emails.plan CHECK drop.

Business intent (7 test classes):
  T1  plans table gains new commercial columns + CHECK constraints (incl. prices JSONB).
  T2  subscriptions table exists with right columns (incl. cancel_at_period_end),
      constraints, and indexes.
  T3  billing_webhook_events table exists with right columns + UNIQUE constraint.
  T4  external_ref UNIQUE constraint rejects duplicate inserts.
  T5  subscriptions_no_orphan_active CHECK rejects active row with all claim fields NULL.
  T6  Pricing seed applied correctly (free=200 calls, pro price_cents=1900, team=3900).
  T7  Migration is idempotent — running the m13_014 SQL a second time raises no error.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import psycopg2
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Module-level teardown: drop new tables that clean_pg does not know about.
# billing_webhook_events and subscriptions are created by m13_014, but they
# are not in conftest.clean_pg's _all_tables list. Without explicit cleanup
# here, the tables survive the clean_pg wipe phase and confuse yoyo on the
# next test (yoyo sees m13_014 as un-applied but the tables already exist,
# leading to _yoyo_log not found errors during the log phase).
# ---------------------------------------------------------------------------

_M13_014_TABLES = ["billing_webhook_events", "subscriptions"]


@pytest.fixture(autouse=True)
def _drop_m13_014_tables(pg_conn):
    """Drop m13_014 tables before AND after each test so clean_pg gets a blank slate."""
    for tbl in _M13_014_TABLES:
        with pg_conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    yield
    for tbl in _M13_014_TABLES:
        with pg_conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply all migrations on a clean DB, yield connection."""
    run_migrations(clean_pg)
    yield clean_pg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _column_exists(conn, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        return cur.fetchone() is not None


def _column_data_type(conn, table: str, column: str) -> str | None:
    """Return the data_type string from information_schema for a column."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _constraint_exists(conn, conname: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s",
            (conname,),
        )
        return cur.fetchone() is not None


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_name = %s AND table_schema = 'public'",
            (table,),
        )
        return cur.fetchone() is not None


def _index_exists(conn, indexname: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s",
            (indexname,),
        )
        return cur.fetchone() is not None


def _plan_row(conn, slug: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, quota_calls_per_month, price_cents, currency, billing_interval,"
            "       trial_days, is_archived"
            "  FROM plans WHERE slug = %s",
            (slug,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "quota_calls_per_month": row[1],
        "price_cents": row[2],
        "currency": row[3],
        "billing_interval": row[4],
        "trial_days": row[5],
        "is_archived": row[6],
    }


def _insert_plan(conn, slug: str) -> int:
    """Insert a minimal plan and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO plans (slug, display_name, quota_calls_per_month, rate_limit_rpm)"
            " VALUES (%s, %s, 1000, 60) ON CONFLICT (slug) DO NOTHING RETURNING id",
            (slug, f"Test Plan {slug}"),
        )
        row = cur.fetchone()
    if row:
        conn.commit()
        return row[0]
    # Plan already exists (conflict) — fetch existing id
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = %s", (slug,))
        return cur.fetchone()[0]


def _insert_subscription(conn, plan_id: int, **kwargs) -> int:
    """Insert a minimal subscription and return its id."""
    fields = ["plan_id"]
    values = [plan_id]
    for k, v in kwargs.items():
        fields.append(k)
        values.append(v)
    cols = ", ".join(fields)
    placeholders = ", ".join(["%s"] * len(values))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO subscriptions ({cols}) VALUES ({placeholders}) RETURNING id",
            values,
        )
        sub_id = cur.fetchone()[0]
    conn.commit()
    return sub_id


# ---------------------------------------------------------------------------
# T1: plans table gains new commercial columns + CHECK constraints
# ---------------------------------------------------------------------------


class TestPlansNewColumns:
    """T1: plans table has the commercial columns and their CHECK constraints.

    Includes prices JSONB (gộp từ m13_015 section 6.2).
    """

    def test_price_cents_column_exists(self, migrated_pg):
        assert _column_exists(migrated_pg, "plans", "price_cents"), (
            "plans.price_cents must exist after m13_014"
        )

    def test_currency_column_exists(self, migrated_pg):
        assert _column_exists(migrated_pg, "plans", "currency"), (
            "plans.currency must exist after m13_014"
        )

    def test_billing_interval_column_exists(self, migrated_pg):
        assert _column_exists(migrated_pg, "plans", "billing_interval"), (
            "plans.billing_interval must exist after m13_014"
        )

    def test_trial_days_column_exists(self, migrated_pg):
        assert _column_exists(migrated_pg, "plans", "trial_days"), (
            "plans.trial_days must exist after m13_014"
        )

    def test_is_archived_column_exists(self, migrated_pg):
        assert _column_exists(migrated_pg, "plans", "is_archived"), (
            "plans.is_archived must exist after m13_014"
        )

    def test_prices_jsonb_column_exists(self, migrated_pg):
        """plans.prices JSONB per-currency map — gộp từ m13_015 section 6.2."""
        assert _column_exists(migrated_pg, "plans", "prices"), (
            "plans.prices JSONB must exist after m13_014 (merged from m13_015)"
        )

    def test_billing_interval_check_constraint_exists(self, migrated_pg):
        assert _constraint_exists(migrated_pg, "plans_billing_interval_check"), (
            "plans_billing_interval_check constraint must exist"
        )

    def test_price_cents_nonneg_constraint_exists(self, migrated_pg):
        assert _constraint_exists(migrated_pg, "plans_price_cents_nonneg"), (
            "plans_price_cents_nonneg constraint must exist"
        )

    def test_trial_days_nonneg_constraint_exists(self, migrated_pg):
        assert _constraint_exists(migrated_pg, "plans_trial_days_nonneg"), (
            "plans_trial_days_nonneg constraint must exist"
        )

    def test_billing_interval_check_rejects_invalid_value(self, migrated_pg):
        """billing_interval CHECK must reject a value not in the allowed set."""
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO plans"
                    " (slug, display_name, quota_calls_per_month, rate_limit_rpm,"
                    "  billing_interval)"
                    " VALUES ('bad_interval_test', 'Bad Interval', 100, 30, 'weekly')"
                )
        migrated_pg.rollback()

    def test_price_cents_nonneg_check_rejects_negative(self, migrated_pg):
        """price_cents CHECK must reject a negative value."""
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO plans"
                    " (slug, display_name, quota_calls_per_month, rate_limit_rpm,"
                    "  price_cents)"
                    " VALUES ('neg_price_test', 'Neg Price', 100, 30, -1)"
                )
        migrated_pg.rollback()

    def test_trial_days_nonneg_check_rejects_negative(self, migrated_pg):
        """trial_days CHECK must reject a negative value."""
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO plans"
                    " (slug, display_name, quota_calls_per_month, rate_limit_rpm,"
                    "  trial_days)"
                    " VALUES ('neg_trial_test', 'Neg Trial', 100, 30, -1)"
                )
        migrated_pg.rollback()

    # --- #3 BIGINT assertions ---

    def test_price_cents_is_bigint(self, migrated_pg):
        """plans.price_cents must be BIGINT (not INTEGER) — #3 money-critical."""
        dtype = _column_data_type(migrated_pg, "plans", "price_cents")
        assert dtype == "bigint", (
            f"plans.price_cents must be BIGINT, got {dtype!r}. "
            "VND whole-units can exceed INT4 2.1B max."
        )

    # --- #9 currency CHECK assertions ---

    def test_plans_currency_iso4217_constraint_exists(self, migrated_pg):
        """plans_currency_iso4217 CHECK constraint must exist — #9."""
        assert _constraint_exists(migrated_pg, "plans_currency_iso4217"), (
            "plans_currency_iso4217 CHECK constraint must exist after m13_014"
        )

    def test_plans_currency_check_rejects_invalid_value(self, migrated_pg):
        """plans.currency CHECK must reject a value not matching ^[A-Z]{3}$ — #9."""
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO plans"
                    " (slug, display_name, quota_calls_per_month, rate_limit_rpm,"
                    "  currency)"
                    " VALUES ('bad_currency_test', 'Bad Currency', 100, 30, 'us')"
                )
        migrated_pg.rollback()

    def test_plans_currency_check_accepts_valid_iso(self, migrated_pg):
        """plans.currency CHECK must accept valid ISO 4217 codes (USD, VND)."""
        for code in ("USD", "VND", "EUR"):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO plans"
                    " (slug, display_name, quota_calls_per_month, rate_limit_rpm,"
                    "  currency)"
                    " VALUES (%s, %s, 100, 30, %s)",
                    (f"currency_ok_{code.lower()}", f"Test {code}", code),
                )
            migrated_pg.rollback()


# ---------------------------------------------------------------------------
# T2: subscriptions table exists with right columns and constraints
# ---------------------------------------------------------------------------


class TestSubscriptionsTable:
    """T2: subscriptions table has the required columns, constraints, and indexes."""

    def test_subscriptions_table_exists(self, migrated_pg):
        assert _table_exists(migrated_pg, "subscriptions"), (
            "subscriptions table must exist after m13_014"
        )

    def test_required_columns_exist(self, migrated_pg):
        required = [
            "id", "plan_id", "claimed_user_id", "api_key_id", "tenant_id",
            "buyer_email", "status", "seats", "source", "external_ref",
            "amount_cents", "currency", "billing_interval",
            "current_period_start", "current_period_end", "trial_ends_at",
            "cancelled_at", "created_at", "updated_at",
            # cancel_at_period_end — gộp từ m13_015 section 6.1
            "cancel_at_period_end",
            # last_event_at — #5 monotonic guard for out-of-order webhook events
            "last_event_at",
        ]
        for col in required:
            assert _column_exists(migrated_pg, "subscriptions", col), (
                f"subscriptions.{col} must exist after m13_014"
            )

    def test_status_check_constraint_exists(self, migrated_pg):
        assert _constraint_exists(migrated_pg, "subscriptions_status_check"), (
            "subscriptions_status_check must exist"
        )

    def test_seats_positive_constraint_exists(self, migrated_pg):
        assert _constraint_exists(migrated_pg, "subscriptions_seats_positive"), (
            "subscriptions_seats_positive must exist"
        )

    def test_source_check_constraint_exists(self, migrated_pg):
        assert _constraint_exists(migrated_pg, "subscriptions_source_check"), (
            "subscriptions_source_check must exist"
        )

    def test_no_orphan_active_constraint_exists(self, migrated_pg):
        assert _constraint_exists(migrated_pg, "subscriptions_no_orphan_active"), (
            "subscriptions_no_orphan_active must exist"
        )

    def test_billing_interval_check_constraint_exists(self, migrated_pg):
        assert _constraint_exists(migrated_pg, "subscriptions_billing_interval_check"), (
            "subscriptions_billing_interval_check must exist"
        )

    def test_index_user_id_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "idx_subscriptions_user_id"), (
            "idx_subscriptions_user_id must exist"
        )

    def test_index_api_key_id_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "idx_subscriptions_api_key_id"), (
            "idx_subscriptions_api_key_id must exist"
        )

    def test_index_tenant_id_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "idx_subscriptions_tenant_id"), (
            "idx_subscriptions_tenant_id must exist"
        )

    def test_index_plan_status_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "idx_subscriptions_plan_status"), (
            "idx_subscriptions_plan_status must exist"
        )

    def test_index_buyer_email_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "idx_subscriptions_buyer_email"), (
            "idx_subscriptions_buyer_email partial index must exist"
        )

    def test_status_check_rejects_invalid_value(self, migrated_pg):
        """status CHECK must reject a value not in the allowed set."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO subscriptions (plan_id, status)"
                    " VALUES (%s, 'unknown_status')",
                    (free["id"],),
                )
        migrated_pg.rollback()

    def test_seats_positive_check_rejects_zero(self, migrated_pg):
        """seats CHECK must reject seats=0."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO subscriptions (plan_id, seats)"
                    " VALUES (%s, 0)",
                    (free["id"],),
                )
        migrated_pg.rollback()

    # --- #3 BIGINT assertions ---

    def test_amount_cents_is_bigint(self, migrated_pg):
        """subscriptions.amount_cents must be BIGINT — #3 money-critical."""
        dtype = _column_data_type(migrated_pg, "subscriptions", "amount_cents")
        assert dtype == "bigint", (
            f"subscriptions.amount_cents must be BIGINT, got {dtype!r}. "
            "VND whole-units can exceed INT4 2.1B max."
        )

    # --- #5 last_event_at ---

    def test_last_event_at_column_exists(self, migrated_pg):
        """subscriptions.last_event_at TIMESTAMPTZ must exist — #5 monotonic guard."""
        assert _column_exists(migrated_pg, "subscriptions", "last_event_at"), (
            "subscriptions.last_event_at must exist after m13_014 (#5 out-of-order guard)"
        )

    def test_last_event_at_is_nullable(self, migrated_pg):
        """subscriptions.last_event_at must be nullable (NULL until first webhook)."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = 'subscriptions'"
                " AND column_name = 'last_event_at'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "YES", (
            "subscriptions.last_event_at must be nullable"
        )

    # --- #8 UNIQUE(source, external_ref) ---

    def test_source_external_ref_unique_constraint_exists(self, migrated_pg):
        """subscriptions_source_external_ref_key UNIQUE constraint must exist — #8."""
        assert _constraint_exists(
            migrated_pg, "subscriptions_source_external_ref_key"
        ), "subscriptions_source_external_ref_key UNIQUE constraint must exist"

    def test_composite_unique_rejects_same_source_and_ref(self, migrated_pg):
        """UNIQUE(source, external_ref) must reject duplicate (source, external_ref) — #8."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO subscriptions (plan_id, source, external_ref)"
                " VALUES (%s, 'polar', 'comp_uniq_001')",
                (free["id"],),
            )
        migrated_pg.commit()
        with pytest.raises(psycopg2.errors.UniqueViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO subscriptions (plan_id, source, external_ref)"
                    " VALUES (%s, 'polar', 'comp_uniq_001')",
                    (free["id"],),
                )
        migrated_pg.rollback()

    def test_composite_unique_allows_same_ref_different_source(self, migrated_pg):
        """UNIQUE(source, external_ref) must allow same external_ref across different sources."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO subscriptions (plan_id, source, external_ref)"
                " VALUES (%s, 'polar', 'cross_vendor_001')",
                (free["id"],),
            )
            cur.execute(
                "INSERT INTO subscriptions (plan_id, source, external_ref)"
                " VALUES (%s, 'erp', 'cross_vendor_001')",
                (free["id"],),
            )
        migrated_pg.commit()

    # --- #9 currency CHECK ---

    def test_subscriptions_currency_iso4217_constraint_exists(self, migrated_pg):
        """subscriptions_currency_iso4217 CHECK constraint must exist — #9."""
        assert _constraint_exists(
            migrated_pg, "subscriptions_currency_iso4217"
        ), "subscriptions_currency_iso4217 CHECK constraint must exist"

    def test_subscriptions_currency_check_rejects_invalid(self, migrated_pg):
        """subscriptions.currency CHECK must reject a value not matching ^[A-Z]{3}$ — #9."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO subscriptions (plan_id, currency)"
                    " VALUES (%s, 'usd')",
                    (free["id"],),
                )
        migrated_pg.rollback()

    def test_subscriptions_currency_check_accepts_null(self, migrated_pg):
        """subscriptions.currency is nullable — NULL must be accepted by the CHECK — #9."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO subscriptions (plan_id, currency)"
                " VALUES (%s, NULL) RETURNING id",
                (free["id"],),
            )
            row = cur.fetchone()
        migrated_pg.rollback()
        assert row is not None, "NULL currency must be accepted"


# ---------------------------------------------------------------------------
# T3: billing_webhook_events table exists with right columns + UNIQUE
# ---------------------------------------------------------------------------


class TestBillingWebhookEventsTable:
    """T3: billing_webhook_events table has the required columns and UNIQUE constraint."""

    def test_billing_webhook_events_table_exists(self, migrated_pg):
        assert _table_exists(migrated_pg, "billing_webhook_events"), (
            "billing_webhook_events table must exist after m13_014"
        )

    def test_required_columns_exist(self, migrated_pg):
        required = [
            "id", "vendor", "event_id", "event_type",
            "signature_valid", "payload", "received_at",
            "processed_at", "processing_error", "subscription_id",
        ]
        for col in required:
            assert _column_exists(migrated_pg, "billing_webhook_events", col), (
                f"billing_webhook_events.{col} must exist after m13_014"
            )

    def test_vendor_event_unique_constraint_exists(self, migrated_pg):
        assert _constraint_exists(
            migrated_pg, "billing_webhook_events_vendor_event_unique"
        ), "billing_webhook_events_vendor_event_unique must exist"

    def test_vendor_check_constraint_exists(self, migrated_pg):
        assert _constraint_exists(
            migrated_pg, "billing_webhook_events_vendor_check"
        ), "billing_webhook_events_vendor_check must exist"

    def test_vendor_check_rejects_invalid_vendor(self, migrated_pg):
        """vendor CHECK must reject a value not in the allowed set."""
        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO billing_webhook_events"
                    " (vendor, event_id, event_type, payload)"
                    " VALUES ('stripe', 'evt_test', 'charge.created', '{}'::jsonb)"
                )
        migrated_pg.rollback()

    def test_index_bwe_vendor_received_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "idx_bwe_vendor_received"), (
            "idx_bwe_vendor_received must exist"
        )

    def test_index_bwe_unprocessed_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "idx_bwe_unprocessed"), (
            "idx_bwe_unprocessed must exist"
        )

    def test_index_bwe_subscription_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "idx_bwe_subscription"), (
            "idx_bwe_subscription must exist"
        )


# ---------------------------------------------------------------------------
# T4: external_ref UNIQUE constraint rejects duplicate inserts
# ---------------------------------------------------------------------------


class TestExternalRefUnique:
    """T4: UNIQUE(source, external_ref) composite constraint — #8.

    The old global external_ref UNIQUE has been replaced with a composite
    UNIQUE(source, external_ref) so the same vendor order ID cannot bleed
    across different billing sources (polar vs erp), while NULL external_ref
    rows (admin/promo grants) are always allowed.
    """

    def test_duplicate_source_and_external_ref_raises(self, migrated_pg):
        """Same (source, external_ref) pair must raise UniqueViolation — #8."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None

        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO subscriptions (plan_id, source, external_ref)"
                " VALUES (%s, 'polar', 'test_ext_ref_dup_001')",
                (free["id"],),
            )
        migrated_pg.commit()

        with pytest.raises(psycopg2.errors.UniqueViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO subscriptions (plan_id, source, external_ref)"
                    " VALUES (%s, 'polar', 'test_ext_ref_dup_001')",
                    (free["id"],),
                )
        migrated_pg.rollback()

    def test_same_external_ref_different_source_is_allowed(self, migrated_pg):
        """Same external_ref but different source must NOT raise — cross-vendor OK — #8."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None

        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO subscriptions (plan_id, source, external_ref)"
                " VALUES (%s, 'polar', 'test_ext_ref_cross_001')",
                (free["id"],),
            )
            cur.execute(
                "INSERT INTO subscriptions (plan_id, source, external_ref)"
                " VALUES (%s, 'erp', 'test_ext_ref_cross_001')",
                (free["id"],),
            )
        migrated_pg.commit()

    def test_null_external_ref_allows_multiple_rows(self, migrated_pg):
        """NULL external_ref must NOT trigger UNIQUE violation (admin/promo grants)."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None

        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO subscriptions (plan_id, external_ref)"
                " VALUES (%s, NULL)",
                (free["id"],),
            )
            cur.execute(
                "INSERT INTO subscriptions (plan_id, external_ref)"
                " VALUES (%s, NULL)",
                (free["id"],),
            )
        migrated_pg.commit()


# ---------------------------------------------------------------------------
# T5: subscriptions_no_orphan_active CHECK rejects invalid active rows
# ---------------------------------------------------------------------------


class TestNoOrphanActiveCheck:
    """T5: subscriptions_no_orphan_active CHECK rejects active row with all claim fields NULL."""

    def test_active_with_all_claims_null_raises(self, migrated_pg):
        """An active subscription with all claim targets NULL must raise CheckViolation."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None

        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO subscriptions"
                    " (plan_id, status, claimed_user_id, api_key_id, tenant_id, buyer_email)"
                    " VALUES (%s, 'active', NULL, NULL, NULL, NULL)",
                    (free["id"],),
                )
        migrated_pg.rollback()

    def test_trialing_with_all_claims_null_raises(self, migrated_pg):
        """A trialing subscription with all claim targets NULL must raise CheckViolation."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None

        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO subscriptions"
                    " (plan_id, status, claimed_user_id, api_key_id, tenant_id, buyer_email)"
                    " VALUES (%s, 'trialing', NULL, NULL, NULL, NULL)",
                    (free["id"],),
                )
        migrated_pg.rollback()

    def test_active_with_buyer_email_only_is_allowed(self, migrated_pg):
        """An active subscription with buyer_email set (unclaimed-paid transient)
        must be allowed."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None

        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO subscriptions"
                " (plan_id, status, claimed_user_id, api_key_id, tenant_id, buyer_email)"
                " VALUES (%s, 'active', NULL, NULL, NULL, 'buyer@example.com')"
                " RETURNING id",
                (free["id"],),
            )
            sub_id = cur.fetchone()[0]
        migrated_pg.commit()
        assert sub_id is not None, "active sub with buyer_email should be allowed"

    def test_pending_with_all_claims_null_is_allowed(self, migrated_pg):
        """A pending subscription with all claim targets NULL must be allowed (pre-payment)."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None

        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO subscriptions"
                " (plan_id, status, claimed_user_id, api_key_id, tenant_id, buyer_email)"
                " VALUES (%s, 'pending', NULL, NULL, NULL, NULL)"
                " RETURNING id",
                (free["id"],),
            )
            sub_id = cur.fetchone()[0]
        migrated_pg.commit()
        assert sub_id is not None, "pending sub with all claims NULL should be allowed"


# ---------------------------------------------------------------------------
# T6: Pricing seed applied correctly
# ---------------------------------------------------------------------------


class TestPricingSeed:
    """T6: Pricing seed applied — free=200 calls, pro price_cents=1900, team=3900."""

    def test_free_plan_quota_bumped_to_200(self, migrated_pg):
        row = _plan_row(migrated_pg, "free")
        assert row is not None, "'free' plan must exist"
        assert row["quota_calls_per_month"] == 200, (
            f"free plan quota_calls_per_month must be 200, got {row['quota_calls_per_month']}"
        )

    def test_free_plan_price_cents_is_zero(self, migrated_pg):
        row = _plan_row(migrated_pg, "free")
        assert row is not None
        assert row["price_cents"] == 0, (
            f"free plan price_cents must be 0, got {row['price_cents']}"
        )

    def test_free_plan_billing_interval_is_free(self, migrated_pg):
        row = _plan_row(migrated_pg, "free")
        assert row is not None
        assert row["billing_interval"] == "free", (
            f"free plan billing_interval must be 'free', got {row['billing_interval']!r}"
        )

    def test_pro_plan_price_cents_is_1900(self, migrated_pg):
        row = _plan_row(migrated_pg, "pro")
        assert row is not None, "'pro' plan must exist"
        assert row["price_cents"] == 1900, (
            f"pro plan price_cents must be 1900, got {row['price_cents']}"
        )

    def test_pro_plan_billing_interval_is_monthly(self, migrated_pg):
        row = _plan_row(migrated_pg, "pro")
        assert row is not None
        assert row["billing_interval"] == "monthly", (
            f"pro plan billing_interval must be 'monthly', got {row['billing_interval']!r}"
        )

    def test_team_plan_price_cents_is_3900(self, migrated_pg):
        row = _plan_row(migrated_pg, "team")
        assert row is not None, "'team' plan must exist"
        assert row["price_cents"] == 3900, (
            f"team plan price_cents must be 3900, got {row['price_cents']}"
        )

    def test_team_plan_billing_interval_is_monthly(self, migrated_pg):
        row = _plan_row(migrated_pg, "team")
        assert row is not None
        assert row["billing_interval"] == "monthly", (
            f"team plan billing_interval must be 'monthly', got {row['billing_interval']!r}"
        )

    def test_unlimited_plan_price_cents_is_zero(self, migrated_pg):
        row = _plan_row(migrated_pg, "unlimited")
        assert row is not None, "'unlimited' plan must exist"
        assert row["price_cents"] == 0, (
            f"unlimited plan price_cents must be 0, got {row['price_cents']}"
        )

    def test_unlimited_plan_billing_interval_is_free(self, migrated_pg):
        row = _plan_row(migrated_pg, "unlimited")
        assert row is not None
        assert row["billing_interval"] == "free", (
            f"unlimited plan billing_interval must be 'free', got {row['billing_interval']!r}"
        )


# ---------------------------------------------------------------------------
# T7: Migration is idempotent — running m13_014 SQL a second time raises no error
# ---------------------------------------------------------------------------


class TestMigrationIdempotent:
    """T7: Applying m13_014 SQL a second time against the already-migrated DB raises no error."""

    def test_double_run_via_run_migrations(self, clean_pg):
        """run_migrations is idempotent — yoyo tracks applied migrations
        and skips already-applied ones. Running twice must not raise."""
        run_migrations(clean_pg)
        try:
            run_migrations(clean_pg)
        except Exception as exc:
            pytest.fail(
                f"run_migrations raised on second run (not idempotent): {exc}"
            )

    def test_m13_014_sql_idempotent_when_run_directly(self, clean_pg):
        """Execute the m13_014 SQL file directly against an already-migrated DB.

        Strategy: run_migrations first (full stack including m13_014), then
        read and execute the m13_014 SQL file a second time. Every statement
        uses IF NOT EXISTS / guarded DO blocks so re-execution must be a no-op.
        """
        from pathlib import Path

        run_migrations(clean_pg)

        migration_path = (
            Path(__file__).parent.parent / "migrations" / "m13_014_billing_p1.sql"
        )
        sql = migration_path.read_text()

        try:
            with clean_pg.cursor() as cur:
                cur.execute(sql)
            clean_pg.commit()
        except Exception as exc:
            clean_pg.rollback()
            pytest.fail(
                f"m13_014_billing_p1.sql raised on second direct execution: {exc}"
            )

    def test_schema_intact_after_double_run(self, clean_pg):
        """After two run_migrations calls, schema objects are still present and correct."""
        run_migrations(clean_pg)
        run_migrations(clean_pg)

        assert _table_exists(clean_pg, "subscriptions"), (
            "subscriptions table must still exist after double run"
        )
        assert _table_exists(clean_pg, "billing_webhook_events"), (
            "billing_webhook_events table must still exist after double run"
        )
        row = _plan_row(clean_pg, "free")
        assert row is not None and row["price_cents"] == 0, (
            "free plan price_cents must still be 0 after double run"
        )

    def test_merged_columns_present_after_double_run(self, clean_pg):
        """Columns merged from m13_015/m13_016/m13_017 survive a second run_migrations."""
        run_migrations(clean_pg)
        run_migrations(clean_pg)

        assert _column_exists(clean_pg, "subscriptions", "cancel_at_period_end"), (
            "subscriptions.cancel_at_period_end must exist after double run (merged m13_015)"
        )
        assert _column_exists(clean_pg, "plans", "prices"), (
            "plans.prices must exist after double run (merged m13_015)"
        )
        assert _column_exists(clean_pg, "webui_users", "terms_accepted_at"), (
            "webui_users.terms_accepted_at must exist after double run (merged m13_016)"
        )
        # waitlist_emails.plan CHECK dropped by merged m13_017 — constraint must NOT exist
        with clean_pg.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_constraint"
                " WHERE conname = 'waitlist_emails_plan_check'"
            )
            row = cur.fetchone()
        assert row is None, (
            "waitlist_emails_plan_check must be dropped by m13_014 (merged m13_017)"
        )

    def test_schema_review_fixes_present_after_double_run(self, clean_pg):
        """Schema review fixes (#3/#5/#8/#9) survive a second run_migrations — idempotent."""
        run_migrations(clean_pg)
        run_migrations(clean_pg)

        # #3 BIGINT
        assert _column_data_type(clean_pg, "plans", "price_cents") == "bigint", (
            "plans.price_cents must be BIGINT after double run"
        )
        assert _column_data_type(clean_pg, "subscriptions", "amount_cents") == "bigint", (
            "subscriptions.amount_cents must be BIGINT after double run"
        )

        # #5 last_event_at
        assert _column_exists(clean_pg, "subscriptions", "last_event_at"), (
            "subscriptions.last_event_at must exist after double run"
        )

        # #8 UNIQUE(source, external_ref)
        assert _constraint_exists(clean_pg, "subscriptions_source_external_ref_key"), (
            "subscriptions_source_external_ref_key must exist after double run"
        )

        # #9 currency CHECKs
        assert _constraint_exists(clean_pg, "plans_currency_iso4217"), (
            "plans_currency_iso4217 must exist after double run"
        )
        assert _constraint_exists(clean_pg, "subscriptions_currency_iso4217"), (
            "subscriptions_currency_iso4217 must exist after double run"
        )
