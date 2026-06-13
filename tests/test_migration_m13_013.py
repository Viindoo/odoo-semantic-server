# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migration_m13_013.py
"""Migration tests for m13_013_consolidate_free_plans.sql — behaviour cases only.

One-shot catalog assertions (T4a: api_keys.plan_id column_default is an integer
literal via information_schema) were removed — covered by test_squashed_baseline.py.

Kept behaviour cases:
  T1  After migration, no plan with slug='free-grandfathered' exists (data check).
  T2  api_keys that were on free-grandfathered now point at the 'unlimited' plan.
  T3  The 'free' plan still exists and is_public=TRUE (data preservation check).
  T4  api_keys.plan_id DEFAULT equals the 'free' plan id; INSERT defaults correctly.
  T5  Migration is idempotent — running run_migrations twice does not raise or
      reintroduce the deleted plan.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixture — clean slate + full migration stack applied
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply all migrations on a clean DB, yield connection."""
    run_migrations(clean_pg)
    yield clean_pg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_exists(conn, slug: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM plans WHERE slug = %s", (slug,))
        return cur.fetchone() is not None


def _plan_row(conn, slug: str) -> dict | None:
    """Return {id, is_public} for the plan with the given slug, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, is_public FROM plans WHERE slug = %s",
            (slug,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "is_public": row[1]}


def _col_default(conn, table: str, column: str) -> str | None:
    """Return column_default string from information_schema, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_default FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# T1: free-grandfathered plan is removed
# ---------------------------------------------------------------------------


class TestFreeGrandfatheredRemoved:
    """T1: After all migrations, no plan with slug='free-grandfathered' exists."""

    def test_free_grandfathered_plan_absent(self, migrated_pg):
        assert not _plan_exists(migrated_pg, "free-grandfathered"), (
            "plans row with slug='free-grandfathered' must be deleted by m13_013"
        )


# ---------------------------------------------------------------------------
# T2: api_keys formerly on free-grandfathered now point at unlimited
# ---------------------------------------------------------------------------


class TestApiKeysRepointed:
    """T2: api_keys that were on free-grandfathered now point at 'unlimited'."""

    def test_no_api_keys_reference_free_grandfathered_after_migration(
        self, migrated_pg
    ):
        """After migration, zero api_keys reference the deleted plan (FK would fail
        the DELETE if any remained, but this assertion doubles as a regression guard).
        """
        # free-grandfathered no longer exists, so a join returns nothing.
        with migrated_pg.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM api_keys k
                  JOIN plans p ON p.id = k.plan_id
                 WHERE p.slug = 'free-grandfathered'
                """
            )
            count = cur.fetchone()[0]
        assert count == 0, (
            f"No api_keys should reference free-grandfathered after m13_013, got {count}"
        )

    def test_api_key_inserted_as_grandfathered_is_repointed_to_unlimited(
        self, clean_pg
    ):
        """Simulate a key on free-grandfathered BEFORE m13_013 is applied, then
        verify it moves to 'unlimited' after running all migrations.

        Strategy: run migrations up to m13_012 (i.e. all migrations, since m13_013
        is the new one being tested).  Then manually insert a key on
        free-grandfathered (which should NOT exist yet if m13_013 already ran).
        Because run_migrations applies ALL migrations including m13_013, we cannot
        partially apply — instead we verify the idempotency-safe invariant: after
        full migration, if a key WAS on free-grandfathered (simulated by inserting
        it with the plan id resolved pre-migration), it now sits on unlimited.

        Simpler equivalent: insert a key on 'unlimited' (which is where m13_013
        moves them), confirm it is on unlimited.  The actual repoint is exercised
        by the SQL migration itself; the test guards the outcome invariant.

        Direct simulation approach:
          1. Run all migrations → free-grandfathered is gone, unlimited exists.
          2. Manually restore free-grandfathered temporarily (outside yoyo tracking).
          3. Insert a key on it.
          4. Apply the m13_013 DO block manually.
          5. Assert key is on unlimited and free-grandfathered is gone again.
        """
        run_migrations(clean_pg)

        unlimited = _plan_row(clean_pg, "unlimited")
        assert unlimited is not None, "'unlimited' plan must exist after migrations"
        unlim_id = unlimited["id"]

        # Step 1: Temporarily re-seed free-grandfathered (simulates pre-m13_013 state).
        with clean_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO plans"
                " (slug, display_name, quota_calls_per_month,"
                "  rate_limit_rpm, seat_limit, is_public)"
                " VALUES"
                " ('free-grandfathered', 'Free (Grandfathered)', 1000, 60, 1, FALSE)"
                " RETURNING id"
            )
            fg_id = cur.fetchone()[0]
        clean_pg.commit()

        # Step 2: Insert a key on free-grandfathered.
        with clean_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id)"
                " VALUES ('sim_grandfathered_key', 'hash_sim_fg', 'sg_', %s)"
                " RETURNING id",
                (fg_id,),
            )
            key_id = cur.fetchone()[0]
        clean_pg.commit()

        # Step 3: Apply the m13_013 DO block manually (mirrors the migration exactly).
        consolidate_sql = """
        DO $$
        DECLARE
            _fg_id      INTEGER;
            _unlim_id   INTEGER;
        BEGIN
            SELECT id INTO _fg_id    FROM plans WHERE slug = 'free-grandfathered';
            SELECT id INTO _unlim_id FROM plans WHERE slug = 'unlimited';

            IF _fg_id IS NULL THEN
                RETURN;
            END IF;

            IF _unlim_id IS NULL THEN
                RAISE EXCEPTION 'unlimited plan not found';
            END IF;

            UPDATE api_keys
               SET plan_id = _unlim_id
             WHERE plan_id = _fg_id;

            DELETE FROM plans WHERE id = _fg_id;
        END
        $$;
        """
        with clean_pg.cursor() as cur:
            cur.execute(consolidate_sql)
        clean_pg.commit()

        # Step 4: Assert the key now points at unlimited.
        with clean_pg.cursor() as cur:
            cur.execute("SELECT plan_id FROM api_keys WHERE id = %s", (key_id,))
            actual_plan_id = cur.fetchone()[0]

        assert actual_plan_id == unlim_id, (
            f"Key formerly on free-grandfathered must be repointed to unlimited"
            f" (id={unlim_id}), got plan_id={actual_plan_id}"
        )

        # Step 5: Assert free-grandfathered is gone.
        assert not _plan_exists(clean_pg, "free-grandfathered"), (
            "free-grandfathered plan must be deleted after consolidation DO block"
        )


# ---------------------------------------------------------------------------
# T3: 'free' plan preserved with is_public=TRUE
# ---------------------------------------------------------------------------


class TestFreePlanPreserved:
    """T3: The 'free' plan still exists and is_public=TRUE after migration."""

    def test_free_plan_exists(self, migrated_pg):
        assert _plan_exists(migrated_pg, "free"), (
            "'free' plan must still exist after m13_013"
        )

    def test_free_plan_is_public(self, migrated_pg):
        row = _plan_row(migrated_pg, "free")
        assert row is not None and row["is_public"] is True, (
            "'free' plan must have is_public=TRUE after m13_013"
        )


# ---------------------------------------------------------------------------
# T4: api_keys.plan_id column DEFAULT still resolves to 'free' plan id
# ---------------------------------------------------------------------------


class TestPlanIdDefaultUnchanged:
    """T4: api_keys.plan_id DB-level DEFAULT still points at the 'free' plan id.

    m13_013 must NOT alter the column DEFAULT — new self-service signups
    continue to receive the 'free' plan (100 calls/month) automatically.
    """

    def test_api_keys_plan_id_default_equals_free_plan_id(self, migrated_pg):
        """The DEFAULT literal must match the id of the 'free' plan."""
        col_default = _col_default(migrated_pg, "api_keys", "plan_id")
        assert col_default is not None
        default_int = int(col_default.split("::")[0].strip())

        row = _plan_row(migrated_pg, "free")
        assert row is not None, "'free' plan must exist"
        assert default_int == row["id"], (
            f"api_keys.plan_id DEFAULT={default_int} must equal 'free' plan id={row['id']}"
        )

    def test_insert_without_plan_id_still_defaults_to_free(self, migrated_pg):
        """INSERT omitting plan_id must land the key on 'free', not on any deleted plan."""
        free = _plan_row(migrated_pg, "free")
        assert free is not None

        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix)"
                " VALUES ('m13013_default_test', 'hash_m13013_dt', 'm13_')"
                " RETURNING plan_id"
            )
            inserted_plan_id = cur.fetchone()[0]
        migrated_pg.commit()

        assert inserted_plan_id == free["id"], (
            f"INSERT without plan_id must default to 'free' id={free['id']},"
            f" got plan_id={inserted_plan_id}"
        )


# ---------------------------------------------------------------------------
# T5: Migration is idempotent
# ---------------------------------------------------------------------------


class TestMigrationIdempotent:
    """T5: Running run_migrations twice does not raise or reintroduce deleted plan."""

    def test_double_run_does_not_raise(self, clean_pg):
        run_migrations(clean_pg)
        try:
            run_migrations(clean_pg)
        except Exception as exc:
            pytest.fail(
                f"run_migrations raised on second run (not idempotent): {exc}"
            )

    def test_free_grandfathered_absent_after_double_run(self, clean_pg):
        run_migrations(clean_pg)
        run_migrations(clean_pg)
        assert not _plan_exists(clean_pg, "free-grandfathered"), (
            "free-grandfathered must remain absent after double run_migrations"
        )
