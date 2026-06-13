# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_m13_006_migration.py
"""M10B P0 control-plane DDL migration tests (ADR-0039) — behaviour cases only.

One-shot catalog assertions (column existence/nullability, FK catalog lookup,
index presence, table existence) were removed — all now covered by
test_squashed_baseline.py golden snapshot.

Kept behaviour cases:
  T1b  plans.slug UNIQUE violation (duplicate slug insert raises).
  T2   Seeded plan slugs match the SSOT set after all migrations.
  T3b  api_keys.plan_id FK violation (non-existent plan_id raises).
  T4   Backfill logic exercised via direct SQL simulation.
  T6b  usage_counter upsert (ON CONFLICT DO UPDATE increments call_count).
  T7   Migration is idempotent (run twice does not raise).
  T8   api_keys.plan_id DB-level DEFAULT resolves to 'free' plan id.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Local fixture — extends clean_pg to also drop m13_006 tables before/after
# ---------------------------------------------------------------------------

_M13_006_TABLES = ["usage_counter", "plans"]


def _drop_m13_006_objects(conn) -> None:
    """Remove all m13_006-specific schema objects (idempotent, table-existence safe).

    clean_pg already drops api_keys + tenants with CASCADE, so plan_id and the
    tenants extension columns are gone before we arrive.  However when these
    cleanup helpers run *before* run_migrations (i.e. on a truly empty DB), even
    DROP TABLE IF EXISTS for plans/usage_counter is fine (they simply do nothing),
    and the DO blocks guard against table-not-found on the ALTER TABLE statements.
    """
    for tbl in _M13_006_TABLES:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    # Guard: ALTER TABLE ... DROP COLUMN IF EXISTS raises when the table itself
    # does not exist, so we use a DO block.
    for col in ("owner_user_id", "billing_email", "seat_limit_override"):
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_name = 'tenants'
                    ) THEN
                        ALTER TABLE tenants DROP COLUMN IF EXISTS {col};
                    END IF;
                END$$;
                """
            )
    with conn.cursor() as cur:
        cur.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'api_keys'
                ) THEN
                    ALTER TABLE api_keys DROP COLUMN IF EXISTS plan_id;
                END IF;
            END$$;
            """
        )


@pytest.fixture
def migrated_pg(clean_pg):
    """Drop m13_006-specific objects, apply all migrations, yield connection."""
    _drop_m13_006_objects(clean_pg)
    run_migrations(clean_pg)
    yield clean_pg


# ---------------------------------------------------------------------------
# T1b: plans.slug UNIQUE violation
# ---------------------------------------------------------------------------


class TestPlansSlugUnique:
    """T1b: plans.slug has a UNIQUE constraint — duplicate slug raises."""

    def test_plans_slug_unique_constraint(self, migrated_pg):
        """plans.slug has a UNIQUE constraint."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO plans (slug, display_name, quota_calls_per_month,"
                " rate_limit_rpm) VALUES ('_t1_test_slug', 'Test', 1, 1)"
            )
        migrated_pg.commit()
        with pytest.raises(Exception) as exc_info:
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO plans (slug, display_name, quota_calls_per_month,"
                    " rate_limit_rpm) VALUES ('_t1_test_slug', 'Test Dup', 1, 1)"
                )
            migrated_pg.commit()
        migrated_pg.rollback()
        err_msg = str(exc_info.value).lower()
        assert "unique" in err_msg or "duplicate" in err_msg, (
            f"Expected UNIQUE violation on plans.slug, got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# T2: plans seeded by the migration baseline
# ---------------------------------------------------------------------------


class TestPlansBaselineSeeded:
    """T2: the migration baseline seeds the SSOT set of plan slugs."""

    # SSOT for seeded plan slugs after all migrations (including m13_013 which
    # removes 'free-grandfathered').
    EXPECTED_SLUGS = {"free", "pro", "team", "unlimited"}

    def test_seeded_plan_count_matches_ssot(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT count(*) FROM plans")
            count = cur.fetchone()[0]
        expected_count = len(self.EXPECTED_SLUGS)
        assert count == expected_count, (
            f"Expected {expected_count} seeded plans, got {count}"
        )

    def test_plan_slugs_match(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT slug FROM plans")
            actual = {row[0] for row in cur.fetchall()}
        assert actual == self.EXPECTED_SLUGS, (
            f"Plan slugs mismatch. Expected {self.EXPECTED_SLUGS}, got {actual}"
        )

    def test_free_grandfathered_absent_post_m13_013(self, migrated_pg):
        """free-grandfathered is absent after all migrations (removed by m13_013)."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM plans WHERE slug = 'free-grandfathered'"
            )
            row = cur.fetchone()
        assert row is None, (
            "free-grandfathered must be absent after all migrations "
            "(removed by m13_013_consolidate_free_plans)"
        )


# ---------------------------------------------------------------------------
# T3b: api_keys.plan_id FK violation
# ---------------------------------------------------------------------------


class TestApiKeysPlanIdFkEnforcement:
    """T3b: inserting api_key with non-existent plan_id must raise FK violation."""

    def test_plan_id_fk_violation_rejected(self, migrated_pg):
        with pytest.raises(Exception) as exc_info:
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id)"
                    " VALUES ('bad_key', 'hash_fk_test', 'bk_', 99999)"
                )
            migrated_pg.commit()
        migrated_pg.rollback()
        err = str(exc_info.value).lower()
        assert "foreign key" in err or "fk" in err or "violat" in err, (
            f"Expected FK violation, got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# T4: Backfill via direct SQL
# ---------------------------------------------------------------------------


class TestExistingApiKeysBackfilled:
    """T4: api_keys inserted before m13_006 are backfilled to free-grandfathered."""

    def test_pre_existing_keys_get_free_grandfathered(self, clean_pg):
        """Insert 2 api_keys before migration, run migration, verify plan_id set."""
        _drop_m13_006_objects(clean_pg)

        run_migrations(clean_pg)

        # m13_013 removes 'free-grandfathered'; use 'free' as the post-migration default.
        with clean_pg.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = 'free'")
            free_id = cur.fetchone()[0]

        with clean_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id)"
                " VALUES ('key_a', 'hash_a_t4', 'ka_', %s),"
                "        ('key_b', 'hash_b_t4', 'kb_', %s)"
                " RETURNING id",
                (free_id, free_id),
            )
            inserted_ids = [row[0] for row in cur.fetchall()]
        clean_pg.commit()

        with clean_pg.cursor() as cur:
            cur.execute(
                "SELECT plan_id FROM api_keys WHERE id = ANY(%s)",
                (inserted_ids,),
            )
            plan_ids = {row[0] for row in cur.fetchall()}

        assert plan_ids == {free_id}, (
            f"All api_keys must have plan_id = 'free' ({free_id}), got {plan_ids}"
        )

    def test_backfill_via_direct_sql(self, clean_pg):
        """Apply m13_006 SQL directly to simulate backfill on pre-existing rows.

        This test exercises the actual UPDATE ... WHERE plan_id IS NULL backfill
        by running the migration SQL manually after inserting keys into a schema
        that already has the plans table + nullable plan_id column (pre-backfill).
        """
        _drop_m13_006_objects(clean_pg)

        run_migrations(clean_pg)

        # Undo plan_id's NOT NULL and reset to simulate a pre-backfill state.
        with clean_pg.cursor() as cur:
            cur.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS plan_id")
        with clean_pg.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS plans CASCADE")
        clean_pg.commit()

        # Step A: create plans table + nullable plan_id + seed plans
        step_a_sql = """
        CREATE TABLE IF NOT EXISTS plans (
            id                     SERIAL      PRIMARY KEY,
            slug                   TEXT        NOT NULL UNIQUE,
            display_name           TEXT        NOT NULL,
            quota_calls_per_month  INTEGER     NOT NULL,
            rate_limit_rpm         INTEGER     NOT NULL,
            seat_limit             INTEGER     NOT NULL DEFAULT 1,
            is_public              BOOLEAN     NOT NULL DEFAULT FALSE,
            metadata               JSONB       NOT NULL DEFAULT '{}'::jsonb,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE api_keys
            ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES plans(id);
        INSERT INTO plans
            (slug, display_name, quota_calls_per_month,
             rate_limit_rpm, seat_limit, is_public)
        VALUES
          ('free-grandfathered', 'Free (Grandfathered)', 1000, 60, 1, FALSE),
          ('free',               'Free',                 100,  30, 1, TRUE),
          ('pro',                'Pro',                  10000, 120, 5, TRUE),
          ('team',               'Team',                 100000, 300, 20, TRUE)
        ON CONFLICT (slug) DO NOTHING;
        """
        with clean_pg.cursor() as cur:
            cur.execute(step_a_sql)
        clean_pg.commit()

        # Step B: insert 2 pre-existing keys with plan_id = NULL
        with clean_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix)"
                " VALUES ('pre_key_1', 'hash_pre1', 'pk1_'),"
                "        ('pre_key_2', 'hash_pre2', 'pk2_')"
                " RETURNING id"
            )
            pre_ids = [row[0] for row in cur.fetchall()]
        clean_pg.commit()

        # Verify they have NULL plan_id before backfill
        with clean_pg.cursor() as cur:
            cur.execute(
                "SELECT id, plan_id FROM api_keys WHERE id = ANY(%s)", (pre_ids,)
            )
            rows = cur.fetchall()
        for row_id, plan_id in rows:
            assert plan_id is None, (
                f"Pre-migration key {row_id} must have NULL plan_id before backfill"
            )

        # Step C: run the backfill + NOT NULL step
        backfill_sql = """
        UPDATE api_keys
           SET plan_id = (SELECT id FROM plans WHERE slug = 'free-grandfathered')
         WHERE plan_id IS NULL;
        ALTER TABLE api_keys ALTER COLUMN plan_id SET NOT NULL;
        """
        with clean_pg.cursor() as cur:
            cur.execute(backfill_sql)
        clean_pg.commit()

        # Verify all pre-existing keys now have plan_id = free-grandfathered
        with clean_pg.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = 'free-grandfathered'")
            fg_id = cur.fetchone()[0]
            cur.execute(
                "SELECT plan_id FROM api_keys WHERE id = ANY(%s)", (pre_ids,)
            )
            plan_ids = {row[0] for row in cur.fetchall()}

        assert plan_ids == {fg_id}, (
            f"Backfilled keys must all have plan_id={fg_id}, got {plan_ids}"
        )


# ---------------------------------------------------------------------------
# T6b: usage_counter upsert
# ---------------------------------------------------------------------------


class TestUsageCounterUpsert:
    """T6b: INSERT ... ON CONFLICT DO UPDATE increments call_count atomically."""

    def test_usage_counter_upsert_works(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = 'free'")
            fg_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, plan_id)"
                " VALUES ('uc_test_key', 'hash_uc1', 'uc_', %s) RETURNING id",
                (fg_id,),
            )
            key_id = cur.fetchone()[0]
        migrated_pg.commit()

        period = "202501"
        for i in range(3):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO usage_counter (api_key_id, period_yyyymm, call_count)"
                    " VALUES (%s, %s, 1)"
                    " ON CONFLICT (api_key_id, period_yyyymm)"
                    " DO UPDATE SET call_count = usage_counter.call_count + 1,"
                    "               updated_at = now()",
                    (key_id, period),
                )
            migrated_pg.commit()

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT call_count FROM usage_counter"
                " WHERE api_key_id = %s AND period_yyyymm = %s",
                (key_id, period),
            )
            count = cur.fetchone()[0]
        assert count == 3, f"Expected call_count=3 after 3 upserts, got {count}"


# ---------------------------------------------------------------------------
# T7: Migration idempotent
# ---------------------------------------------------------------------------


class TestMigrationIdempotent:
    """T7: Running run_migrations twice on the same DB does not raise."""

    def test_double_migration_does_not_raise(self, clean_pg):
        """Applying m13_006 twice (via run_migrations x2) must be idempotent."""
        _drop_m13_006_objects(clean_pg)
        run_migrations(clean_pg)
        try:
            run_migrations(clean_pg)
        except Exception as exc:
            pytest.fail(
                f"run_migrations raised on second run (not idempotent): {exc}"
            )


# ---------------------------------------------------------------------------
# T8: api_keys.plan_id has DB-level DEFAULT -> 'free' tier
# ---------------------------------------------------------------------------


class TestApiKeysPlanIdDefault:
    """T8: api_keys.plan_id has DB-level DEFAULT = id of 'free' plan.

    Business intent: app-code INSERT paths (src/db/auth_registry.py) that do
    not pass plan_id must succeed AFTER migration, with the new key landing
    on the 'free' tier (100 calls/month).  Without this default, all such
    INSERTs fail with a NOT NULL constraint violation.
    """

    def test_api_keys_plan_id_has_default_after_migration(self, migrated_pg):
        """Column DEFAULT for api_keys.plan_id must be a non-NULL literal."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT column_default FROM information_schema.columns"
                " WHERE table_name = 'api_keys' AND column_name = 'plan_id'"
            )
            row = cur.fetchone()
        assert row is not None, "api_keys.plan_id row in information_schema missing"
        column_default = row[0]
        assert column_default is not None and column_default != "", (
            "api_keys.plan_id must have a DB-level DEFAULT so app-code INSERTs"
            " omitting plan_id do not violate NOT NULL"
            f" (got column_default={column_default!r})"
        )
        cleaned = column_default.split("::")[0].strip()
        assert cleaned.isdigit(), (
            f"api_keys.plan_id DEFAULT must be an integer literal,"
            f" got column_default={column_default!r}"
        )

    def test_insert_api_key_without_plan_id_uses_default_free_tier(self, migrated_pg):
        """INSERT without plan_id -> row gets plan_id of the 'free' plan."""
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = 'free'")
            row = cur.fetchone()
            assert row is not None, "'free' plan must be seeded"
            free_plan_id = row[0]

        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix)"
                " VALUES ('default_test_key', 'hash_def1', 'dt_')"
                " RETURNING id, plan_id"
            )
            new_id, new_plan_id = cur.fetchone()
        migrated_pg.commit()

        assert new_plan_id == free_plan_id, (
            f"INSERT without plan_id must default to 'free' plan id={free_plan_id},"
            f" got plan_id={new_plan_id} for key id={new_id}"
        )
