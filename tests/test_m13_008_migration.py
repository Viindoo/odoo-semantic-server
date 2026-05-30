# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_m13_008_migration.py
"""Migration tests for m13_008_waitlist_emails.sql.

Business intent (4 cases):
  T1  waitlist_emails table is created with correct columns.
  T2  email column has UNIQUE constraint.
  T3  created_at index exists for reporting queries.
  T4  Migration is idempotent (run twice does not raise).

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixture — drop m13_008 table before migration for a clean slate
# ---------------------------------------------------------------------------

def _drop_m13_008_objects(conn) -> None:
    """Drop waitlist_emails table and its index (idempotent)."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS waitlist_emails CASCADE")
    conn.commit()


@pytest.fixture
def migrated_pg(clean_pg):
    """Drop m13_008 objects, run all migrations, yield connection."""
    _drop_m13_008_objects(clean_pg)
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


def _index_exists(conn, index_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s",
            (index_name,),
        )
        return cur.fetchone() is not None


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
            (table,),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# T1: Table created with correct columns
# ---------------------------------------------------------------------------

class TestWaitlistEmailsTable:
    """T1: waitlist_emails table exists with correct schema after migration."""

    def test_table_exists(self, migrated_pg):
        assert _table_exists(migrated_pg, "waitlist_emails"), (
            "waitlist_emails table must exist after m13_008"
        )

    def test_required_columns_exist(self, migrated_pg):
        for col in ("id", "email", "plan", "source", "created_at"):
            assert _col_exists(migrated_pg, "waitlist_emails", col), (
                f"waitlist_emails.{col} must exist after m13_008"
            )

    def test_insert_and_read(self, migrated_pg):
        """Basic INSERT + SELECT to confirm table is fully functional."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO waitlist_emails (email, plan, source)"
                " VALUES ('test@example.com', 'pro', 'pricing-page')"
                " RETURNING id"
            )
            row_id = cur.fetchone()[0]
        migrated_pg.commit()

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT email, plan, source FROM waitlist_emails WHERE id = %s",
                (row_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "test@example.com"
        assert row[1] == "pro"
        assert row[2] == "pricing-page"


# ---------------------------------------------------------------------------
# T2: UNIQUE constraint on email
# ---------------------------------------------------------------------------

class TestEmailUniqueConstraint:
    """T2: email column has UNIQUE constraint — duplicate email must raise."""

    def test_duplicate_email_rejected(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO waitlist_emails (email) VALUES ('dup@example.com')"
            )
        migrated_pg.commit()

        with pytest.raises(Exception) as exc_info:
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO waitlist_emails (email) VALUES ('dup@example.com')"
                )
            migrated_pg.commit()
        migrated_pg.rollback()

        err = str(exc_info.value).lower()
        assert "unique" in err or "duplicate" in err, (
            f"Expected UNIQUE violation on waitlist_emails.email, got: {exc_info.value}"
        )

    def test_on_conflict_do_nothing_ignores_duplicate(self, migrated_pg):
        """ON CONFLICT DO NOTHING must succeed silently (rowcount=0) on dup."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO waitlist_emails (email) VALUES ('onconflict@example.com')"
            )
        migrated_pg.commit()

        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO waitlist_emails (email) VALUES ('onconflict@example.com')"
                " ON CONFLICT (email) DO NOTHING"
            )
            rowcount = cur.rowcount
        migrated_pg.commit()

        assert rowcount == 0, (
            "ON CONFLICT DO NOTHING must return rowcount=0 for a duplicate email"
        )


# ---------------------------------------------------------------------------
# T3: created_at index exists
# ---------------------------------------------------------------------------

class TestCreatedAtIndex:
    """T3: waitlist_emails_created_at_idx exists for reporting queries."""

    def test_created_at_index_exists(self, migrated_pg):
        assert _index_exists(migrated_pg, "waitlist_emails_created_at_idx"), (
            "waitlist_emails_created_at_idx must exist after m13_008"
        )


# ---------------------------------------------------------------------------
# T4: Idempotency
# ---------------------------------------------------------------------------

class TestMigrationIdempotent:
    """T4: Running run_migrations twice does not raise."""

    def test_double_run_idempotent(self, clean_pg):
        _drop_m13_008_objects(clean_pg)
        run_migrations(clean_pg)
        try:
            run_migrations(clean_pg)
        except Exception as exc:
            pytest.fail(
                f"run_migrations raised on second run (not idempotent): {exc}"
            )


# ---------------------------------------------------------------------------
# T5: plan-column CHECK constraint REMOVED by C4 (ADR-0039)
# ---------------------------------------------------------------------------
#
# Business rule change (C4 / ADR-0039, applied by migrations/m13_014_billing_p1.sql
# section 8 — "drop waitlist_emails.plan CHECK constraint"):
#
#   The original m13_008 migration added
#       CHECK (plan IS NULL OR plan IN ('free', 'pro', 'team'))
#   which froze the allowed-plan list at the schema level — adding a new public
#   plan would have required BOTH a code change AND a DB migration.  C4 moves the
#   allow-list to the application layer (`_public_plan_slugs` in waitlist.py,
#   derived from `plans WHERE is_public AND NOT is_archived`).  The DB CHECK was
#   intentionally DROPPED so plans become admin-editable without a migration.
#
# After the full migration chain runs (which is what `migrated_pg` gives us),
# the constraint no longer exists.  These tests now protect the NEW contract:
# the schema imposes NO plan allow-list — validation lives in the app layer.

class TestPlanCheckConstraintRemovedByC4:
    """T5 (post-C4): `waitlist_emails.plan` has NO CHECK constraint.

    Validation of which plan slugs are acceptable is the application layer's job
    (`_public_plan_slugs`), not the schema's.  See ADR-0039 / m13_014 section 8.
    """

    def test_constraint_no_longer_present_in_catalog(self, migrated_pg):
        """`waitlist_emails_plan_check` must be absent after m13_014 drops it."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_constraint"
                " WHERE conrelid = 'waitlist_emails'::regclass"
                "   AND contype = 'c'"
                "   AND conname = 'waitlist_emails_plan_check'"
            )
            row = cur.fetchone()
        assert row is None, (
            "C4 (ADR-0039) drops waitlist_emails_plan_check; the schema must no "
            "longer enforce a hard-coded plan allow-list"
        )

    def test_arbitrary_plan_value_is_accepted_by_schema(self, migrated_pg):
        """An off-list plan value must INSERT without error (no DB CHECK).

        The app layer (`_public_plan_slugs`) is the sole gate now, so the schema
        deliberately accepts any text — including a value that the old CHECK
        would have rejected.
        """
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO waitlist_emails (email, plan)"
                " VALUES ('post-c4-arbitrary@example.com', 'enterprise-2099')"
                " RETURNING plan"
            )
            stored = cur.fetchone()[0]
        migrated_pg.commit()
        assert stored == "enterprise-2099", (
            "schema must store the plan verbatim now that the CHECK is gone"
        )

    def test_previously_valid_values_still_insert(self, migrated_pg):
        """The slugs the old CHECK allowed still INSERT fine (regression guard)."""
        for plan, email_suffix in [
            ("free", "free"),
            ("pro", "pro"),
            ("team", "team"),
            (None, "null"),
        ]:
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO waitlist_emails (email, plan)"
                    " VALUES (%s, %s)",
                    (f"check-valid-{email_suffix}@example.com", plan),
                )
            migrated_pg.commit()
