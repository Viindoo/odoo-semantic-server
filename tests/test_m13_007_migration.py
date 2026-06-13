# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_m13_007_migration.py
"""m13_007 usage_counter ON DELETE CASCADE migration tests.

Business intent (1 behaviour case kept post-squash):
  T3  DELETE FROM api_keys actually cascades — usage_counter rows for that
      key are removed automatically (the behavioural guarantee m13_007 buys,
      and the one cross-test contamination relies on at teardown).

One-shot catalog assertions (T1: FK exists, T2: confdeltype='c') were removed
— both are now covered by test_squashed_baseline.py golden snapshot.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply all migrations on a clean DB and yield the connection."""
    run_migrations(clean_pg)
    yield clean_pg


class TestCascadeBehaviourEndToEnd:
    """T3: deleting an api_keys row really does remove its usage_counter rows."""

    def test_delete_api_key_cascades_to_usage_counter(self, migrated_pg):
        # NOTE (m13_013): m13_013_consolidate_free_plans.sql deletes 'free-grandfathered'.
        # The CASCADE FK behaviour being tested here is plan-agnostic; use 'free' instead.
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = 'free'")
            fg_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id)"
                " VALUES ('m13_007_cascade_key', 'hash_m13_007', 'm137_', %s)"
                " RETURNING id",
                (fg_id,),
            )
            key_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO usage_counter (api_key_id, period_yyyymm, call_count)"
                " VALUES (%s, '202501', 42),"
                "        (%s, '202502', 7)",
                (key_id, key_id),
            )
        migrated_pg.commit()

        # Sanity: counter rows present before delete.
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM usage_counter WHERE api_key_id = %s",
                (key_id,),
            )
            pre = cur.fetchone()[0]
        assert pre == 2, f"Setup must produce 2 usage_counter rows, got {pre}"

        # Delete the api_keys row — without CASCADE this raises FK violation;
        # with CASCADE both usage_counter rows must disappear.
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE id = %s", (key_id,))
        migrated_pg.commit()

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM usage_counter WHERE api_key_id = %s",
                (key_id,),
            )
            post = cur.fetchone()[0]
        assert post == 0, (
            f"DELETE FROM api_keys must cascade; expected 0 rows in"
            f" usage_counter for key_id={key_id}, got {post}"
        )
