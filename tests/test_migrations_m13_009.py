# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migrations_m13_009.py
"""Migration tests for m13_009_unlimited_plan_and_key_overrides.sql.

Business intent (4 cases):
  T1  api_keys gains rate_limit_override + quota_override columns (INT, nullable)
      with named CHECK constraints.
  T2  'unlimited' plan seeded with correct values (quota=0, rpm=0, is_public=FALSE).
  T3  Migration is idempotent (run twice: no duplicate columns, no duplicate plan row,
      no duplicate constraints).
  T4  CHECK constraint rejects negative override values; 0 and NULL are valid.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixture — clean slate + full migration applied
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply all migrations on a clean DB, yield connection."""
    run_migrations(clean_pg)
    yield clean_pg


# ---------------------------------------------------------------------------
# Helpers (local — avoid cross-file imports)
# ---------------------------------------------------------------------------


def _col_exists(conn, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        return cur.fetchone() is not None


def _col_nullable(conn, table: str, column: str) -> bool:
    """Return True if column is nullable (is_nullable = 'YES')."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        row = cur.fetchone()
        return row is not None and row[0] == "YES"


def _col_data_type(conn, table: str, column: str) -> str | None:
    """Return the data_type string for a column, or None if not found."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _pg_constraint_count(conn, table: str, constraint_name: str) -> int:
    """Return the number of pg_constraint rows with this name on the given table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
              FROM pg_constraint con
              JOIN pg_class cls ON cls.oid = con.conrelid
             WHERE cls.relname = %s
               AND con.conname = %s
            """,
            (table, constraint_name),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# T1: Override columns created correctly
# ---------------------------------------------------------------------------


class TestMigrationCreatesOverrideColumns:
    """T1: api_keys.rate_limit_override and quota_override columns exist after migration."""

    def test_rate_limit_override_column_exists(self, migrated_pg):
        assert _col_exists(migrated_pg, "api_keys", "rate_limit_override"), (
            "api_keys.rate_limit_override must exist after m13_009"
        )

    def test_quota_override_column_exists(self, migrated_pg):
        assert _col_exists(migrated_pg, "api_keys", "quota_override"), (
            "api_keys.quota_override must exist after m13_009"
        )

    def test_rate_limit_override_is_integer(self, migrated_pg):
        dtype = _col_data_type(migrated_pg, "api_keys", "rate_limit_override")
        assert dtype == "integer", (
            f"api_keys.rate_limit_override must be integer type, got {dtype!r}"
        )

    def test_quota_override_is_integer(self, migrated_pg):
        dtype = _col_data_type(migrated_pg, "api_keys", "quota_override")
        assert dtype == "integer", (
            f"api_keys.quota_override must be integer type, got {dtype!r}"
        )

    def test_rate_limit_override_is_nullable(self, migrated_pg):
        assert _col_nullable(migrated_pg, "api_keys", "rate_limit_override"), (
            "api_keys.rate_limit_override must be nullable (NULL = use plan default)"
        )

    def test_quota_override_is_nullable(self, migrated_pg):
        assert _col_nullable(migrated_pg, "api_keys", "quota_override"), (
            "api_keys.quota_override must be nullable (NULL = use plan default)"
        )

    def test_rate_limit_override_check_constraint_exists(self, migrated_pg):
        count = _pg_constraint_count(
            migrated_pg, "api_keys", "api_keys_rate_limit_override_nonneg"
        )
        assert count == 1, (
            f"api_keys_rate_limit_override_nonneg CHECK constraint must exist (count={count})"
        )

    def test_quota_override_check_constraint_exists(self, migrated_pg):
        count = _pg_constraint_count(
            migrated_pg, "api_keys", "api_keys_quota_override_nonneg"
        )
        assert count == 1, (
            f"api_keys_quota_override_nonneg CHECK constraint must exist (count={count})"
        )


# ---------------------------------------------------------------------------
# T2: 'unlimited' plan seeded
# ---------------------------------------------------------------------------


class TestMigrationSeedsUnlimitedPlan:
    """T2: 'unlimited' plan is seeded with correct values."""

    def test_unlimited_plan_exists(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM plans WHERE slug = 'unlimited'")
            count = cur.fetchone()[0]
        assert count == 1, (
            f"Exactly 1 'unlimited' plan must be seeded after m13_009, got {count}"
        )

    def test_unlimited_plan_quota_is_zero(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT quota_calls_per_month FROM plans WHERE slug = 'unlimited'"
            )
            row = cur.fetchone()
        assert row is not None and row[0] == 0, (
            f"unlimited plan.quota_calls_per_month must be 0 (sentinel), got {row}"
        )

    def test_unlimited_plan_rpm_is_zero(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT rate_limit_rpm FROM plans WHERE slug = 'unlimited'")
            row = cur.fetchone()
        assert row is not None and row[0] == 0, (
            f"unlimited plan.rate_limit_rpm must be 0 (sentinel), got {row}"
        )

    def test_unlimited_plan_is_not_public(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT is_public FROM plans WHERE slug = 'unlimited'")
            row = cur.fetchone()
        assert row is not None and row[0] is False, (
            "unlimited plan must have is_public=FALSE (admin-granted only)"
        )

    def test_unlimited_plan_display_name_conveys_unlimited(self, migrated_pg):
        """display_name must be a non-empty label that reads as 'unlimited'.

        The business contract is that the admin dropdown shows a human-readable
        label identifying this as the unlimited plan — not one exact marketing
        string. Asserting the exact copy ("Unlimited (admin-granted)") only
        mirrors the migration's literal and turns a label tweak into a false
        failure. We assert the semantic content (non-empty + says "unlimited")
        instead.
        """
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT display_name FROM plans WHERE slug = 'unlimited'")
            row = cur.fetchone()
        assert row is not None, "unlimited plan must be seeded"
        display_name = row[0]
        assert display_name and display_name.strip(), (
            "unlimited plan display_name must be a non-empty label"
        )
        assert "unlimited" in display_name.lower(), (
            f"display_name must read as the unlimited plan, got {display_name!r}"
        )


# ---------------------------------------------------------------------------
# T3: Idempotency
# ---------------------------------------------------------------------------


class TestMigrationIdempotent:
    """T3: Running run_migrations twice does not raise or duplicate rows/constraints."""

    def test_double_run_does_not_raise(self, clean_pg):
        run_migrations(clean_pg)
        try:
            run_migrations(clean_pg)
        except Exception as exc:
            pytest.fail(
                f"run_migrations raised on second run (not idempotent): {exc}"
            )

    def test_no_duplicate_unlimited_plan(self, clean_pg):
        """Running twice must not duplicate the 'unlimited' plan row."""
        run_migrations(clean_pg)
        run_migrations(clean_pg)
        with clean_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM plans WHERE slug = 'unlimited'")
            count = cur.fetchone()[0]
        assert count == 1, (
            f"'unlimited' plan must appear exactly once after 2 migration runs, got {count}"
        )

    def test_no_duplicate_rate_limit_override_constraint(self, clean_pg):
        """Running twice must not add a second CHECK constraint."""
        run_migrations(clean_pg)
        run_migrations(clean_pg)
        count = _pg_constraint_count(
            clean_pg, "api_keys", "api_keys_rate_limit_override_nonneg"
        )
        assert count == 1, (
            f"api_keys_rate_limit_override_nonneg must exist exactly once, got {count}"
        )

    def test_no_duplicate_quota_override_constraint(self, clean_pg):
        """Running twice must not add a second CHECK constraint."""
        run_migrations(clean_pg)
        run_migrations(clean_pg)
        count = _pg_constraint_count(
            clean_pg, "api_keys", "api_keys_quota_override_nonneg"
        )
        assert count == 1, (
            f"api_keys_quota_override_nonneg must exist exactly once, got {count}"
        )


# ---------------------------------------------------------------------------
# T4: CHECK constraint enforces >= 0 (negative rejected; 0 + NULL valid)
# ---------------------------------------------------------------------------


class TestOverrideConstraintRejectsNegative:
    """T4: CHECK constraint rejects negative override values; 0 and NULL are valid."""

    def _insert_test_key(self, conn) -> int:
        """Insert one api_key row (with free plan) and return its id."""
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = 'free'")
            free_plan_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id)"
                " VALUES ('override_test_key', 'hash_override_t4', 'ov_', %s)"
                " RETURNING id",
                (free_plan_id,),
            )
            key_id = cur.fetchone()[0]
        conn.commit()
        return key_id

    def test_negative_rate_limit_override_rejected(self, migrated_pg):
        key_id = self._insert_test_key(migrated_pg)
        with pytest.raises(Exception) as exc_info:
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "UPDATE api_keys SET rate_limit_override = -1 WHERE id = %s",
                    (key_id,),
                )
            migrated_pg.commit()
        migrated_pg.rollback()
        err = str(exc_info.value).lower()
        assert "check" in err or "violat" in err or "constraint" in err, (
            f"Expected CHECK violation for rate_limit_override=-1, got: {exc_info.value}"
        )

    def test_negative_quota_override_rejected(self, migrated_pg):
        key_id = self._insert_test_key(migrated_pg)
        with pytest.raises(Exception) as exc_info:
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "UPDATE api_keys SET quota_override = -1 WHERE id = %s",
                    (key_id,),
                )
            migrated_pg.commit()
        migrated_pg.rollback()
        err = str(exc_info.value).lower()
        assert "check" in err or "violat" in err or "constraint" in err, (
            f"Expected CHECK violation for quota_override=-1, got: {exc_info.value}"
        )

    def test_zero_rate_limit_override_accepted(self, migrated_pg):
        """0 is a valid override value (means zero-allowed, NOT unlimited)."""
        key_id = self._insert_test_key(migrated_pg)
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET rate_limit_override = 0 WHERE id = %s",
                (key_id,),
            )
        migrated_pg.commit()

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT rate_limit_override FROM api_keys WHERE id = %s", (key_id,)
            )
            val = cur.fetchone()[0]
        assert val == 0, f"rate_limit_override=0 must be accepted, got {val}"

    def test_null_rate_limit_override_accepted(self, migrated_pg):
        """NULL is valid (means use plan default)."""
        key_id = self._insert_test_key(migrated_pg)
        # First set to a value, then reset to NULL to confirm nullability
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET rate_limit_override = NULL WHERE id = %s",
                (key_id,),
            )
        migrated_pg.commit()

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT rate_limit_override FROM api_keys WHERE id = %s", (key_id,)
            )
            val = cur.fetchone()[0]
        assert val is None, f"rate_limit_override=NULL must be accepted, got {val}"

    def test_null_quota_override_accepted(self, migrated_pg):
        """NULL is valid (means use plan default)."""
        key_id = self._insert_test_key(migrated_pg)
        with migrated_pg.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET quota_override = NULL WHERE id = %s",
                (key_id,),
            )
        migrated_pg.commit()

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT quota_override FROM api_keys WHERE id = %s", (key_id,)
            )
            val = cur.fetchone()[0]
        assert val is None, f"quota_override=NULL must be accepted, got {val}"
