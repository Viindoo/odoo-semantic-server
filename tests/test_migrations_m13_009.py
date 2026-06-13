# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migrations_m13_009.py
"""Migration tests for m13_009_unlimited_plan_and_key_overrides.sql
— behaviour cases only.

One-shot catalog assertions (T1 column existence/type/nullability, T2 plan
seeded data values, T3 constraint name presence) were removed — covered by
test_squashed_baseline.py golden snapshot.

Kept behaviour cases:
  T3   Idempotency: double run does not raise, no duplicate rows/constraints.
  T4   CHECK constraint rejects negative override values; 0 and NULL are valid.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply all migrations on a clean DB, yield connection."""
    run_migrations(clean_pg)
    yield clean_pg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
