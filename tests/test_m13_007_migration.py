# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_m13_007_migration.py
"""m13_007 usage_counter ON DELETE CASCADE migration tests.

Business intent (3 cases):
  T1  usage_counter.api_key_id FK exists after m13_007.
  T2  FK has ON DELETE CASCADE action (`pg_constraint.confdeltype = 'c'`).
  T3  DELETE FROM api_keys actually cascades — usage_counter rows for that
      key are removed automatically (the behavioural guarantee m13_007 buys,
      and the one cross-test contamination relies on at teardown).

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


def _usage_counter_fk_row(conn) -> tuple[str, str] | None:
    """Return (constraint_name, confdeltype) for the FK on usage_counter.api_key_id.

    `confdeltype` codes (per pg docs):
      'a' = NO ACTION (default)
      'r' = RESTRICT
      'c' = CASCADE
      'n' = SET NULL
      'd' = SET DEFAULT
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT con.conname, con.confdeltype
              FROM pg_constraint con
              JOIN pg_class    src_cls ON src_cls.oid = con.conrelid
              JOIN pg_class    ref_cls ON ref_cls.oid = con.confrelid
              JOIN pg_attribute att    ON att.attrelid = src_cls.oid
                                      AND att.attnum   = ANY(con.conkey)
             WHERE con.contype = 'f'
               AND src_cls.relname = 'usage_counter'
               AND ref_cls.relname = 'api_keys'
               AND att.attname     = 'api_key_id'
             LIMIT 1
            """
        )
        row = cur.fetchone()
        return (row[0], row[1]) if row else None


class TestUsageCounterCascadeFk:
    """T1 + T2: FK is present AND declares ON DELETE CASCADE."""

    def test_fk_exists_after_m13_007(self, migrated_pg):
        row = _usage_counter_fk_row(migrated_pg)
        assert row is not None, (
            "usage_counter.api_key_id must have an FK referencing api_keys"
            " after m13_007 (lookup via pg_constraint returned no row)"
        )

    def test_fk_on_delete_cascade(self, migrated_pg):
        row = _usage_counter_fk_row(migrated_pg)
        assert row is not None, "Precondition: FK must exist"
        conname, confdeltype = row
        # 'c' is the pg_constraint code for CASCADE.
        assert confdeltype == "c", (
            f"usage_counter_api_key_id_fkey must declare ON DELETE CASCADE"
            f" (confdeltype='c'); got constraint={conname!r}"
            f" confdeltype={confdeltype!r}."
            " Without CASCADE, DELETE FROM api_keys leaves orphan usage_counter"
            " rows that re-bind to the next SERIAL id and cause cross-test"
            " quota contamination (PR #200 CI iter 3 root cause)."
        )


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
