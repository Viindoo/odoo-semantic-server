# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_m13_008_migration.py
"""Migration tests for m13_008_waitlist_emails.sql — behaviour cases only.

One-shot catalog assertions (T1 table/column existence, T3 index existence)
were removed — covered by test_squashed_baseline.py golden snapshot.

Kept behaviour cases:
  T1b  Basic INSERT + SELECT confirms the table is fully functional.
  T2   email column UNIQUE constraint enforcement (duplicate raises).
  T2b  ON CONFLICT DO NOTHING silently absorbs a duplicate.
  T4   Migration is idempotent (run twice does not raise).
  T5   plan-column CHECK constraint REMOVED by C4 (ADR-0039) — the schema
       now accepts arbitrary plan values; application layer gates validity.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixture
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
# T1b: Basic INSERT + SELECT
# ---------------------------------------------------------------------------

class TestWaitlistEmailsInsertRead:
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

class TestPlanCheckConstraintRemovedByC4:
    """T5 (post-C4): `waitlist_emails.plan` has NO CHECK constraint.

    Validation of which plan slugs are acceptable is the application layer's job
    (`_public_plan_slugs`), not the schema's.  See ADR-0039 / m13_014 section 8.
    """

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
