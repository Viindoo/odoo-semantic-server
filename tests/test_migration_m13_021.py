# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for migration m13_021_embeddings_global_sentinel.sql.

FUFU-2 (root fix, supersedes FU-2's ck_embeddings_null_profile_scope CHECK):
Replace the NULL-as-global overloading in embeddings.profile_name with an
explicit '__global__' sentinel and make the column NOT NULL.

Cases covered:
  1. Post-migration: 0 NULL profile_name rows (backfill succeeded).
  2. Column is NOT NULL after migration.
  3. Old ck_embeddings_null_profile_scope CHECK is absent (superseded).
  4. New ck_embeddings_global_sentinel_scope CHECK exists and is validated.
  5. Narrowed sentinel CHECK rejects a non-pattern '__global__' insert.
  6. Sentinel CHECK allows pattern_example/__patterns__/'__global__' insert.
  7. NOT NULL rejects a NULL profile_name insert.
  8. profiles_name_no_dunder CHECK exists and is validated.
  9. profiles_name_no_dunder CHECK rejects a '__global__' profile insert.
 10. Idempotent re-apply: run the migration SQL twice; constraint counts stable.
 11. RLS policy updated: embeddings_tenant policy body contains '__global__' sentinel
     branch and does NOT contain 'IS NULL'.

Requires PostgreSQL + pgvector (pytestmark = pytest.mark.postgres).
PROD-SAFETY: NEVER run against the default localhost DSN on this box — it points
at the prod database. Run in CI or against an isolated throwaway container only.
See docs/adr/0021 + conftest.py §PG_TEST_DSN for context.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.postgres

_MIGRATION_SQL = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "m13_021_embeddings_global_sentinel.sql"
)

_SENTINEL_CHECK = "ck_embeddings_global_sentinel_scope"
_OLD_CHECK = "ck_embeddings_null_profile_scope"
_DUNDER_CHECK = "profiles_name_no_dunder"

# A zero-vector literal compatible with the default embedder dim (1024).
_ZERO_VEC = "[" + ",".join(["0.0"] * 1024) + "]"

# Unique version sentinel for this test module to avoid row collisions.
_TV = "98.0"


def _apply_m13_021(conn) -> None:
    """Execute the migration SQL directly (for idempotency re-run checks)."""
    sql = _MIGRATION_SQL.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    if not conn.autocommit:
        conn.commit()


def _constraint_rows(conn, conname: str, table: str) -> list[dict]:
    """Return pg_constraint rows for the given constraint name + table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT conname, convalidated
              FROM pg_constraint
             WHERE conname = %s
               AND conrelid = %s::regclass
            """,
            (conname, f"public.{table}"),
        )
        return [{"conname": r[0], "convalidated": r[1]} for r in cur.fetchall()]


def _insert_embedding(conn, *, chunk_type, module, profile_name, idx=0):
    """Insert a minimal embeddings row; raises on CHECK / NOT NULL violation."""
    with conn.cursor() as cur:
        if profile_name is None:
            cur.execute(
                """
                INSERT INTO embeddings
                    (chunk_type, module, odoo_version, entity_name, model_name,
                     file_path, chunk_idx, content, vec, profile_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, NULL)
                """,
                (
                    chunk_type, module, _TV,
                    f"test_entity_{chunk_type}_{module}_{idx}", None,
                    f"/test/{chunk_type}_{module}_{idx}.py", idx,
                    f"test content {chunk_type} {module} {idx}",
                    _ZERO_VEC,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO embeddings
                    (chunk_type, module, odoo_version, entity_name, model_name,
                     file_path, chunk_idx, content, vec, profile_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
                """,
                (
                    chunk_type, module, _TV,
                    f"test_entity_{chunk_type}_{module}_{idx}", None,
                    f"/test/{chunk_type}_{module}_{idx}.py", idx,
                    f"test content {chunk_type} {module} {idx}",
                    _ZERO_VEC, profile_name,
                ),
            )
    if not conn.autocommit:
        conn.commit()


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply all migrations (including m13_021) on a clean schema."""
    from src.db.migrate import run_migrations

    run_migrations(clean_pg)
    return clean_pg


@pytest.fixture
def migrated_pg_vec(migrated_pg):
    """Skip if pgvector is not available; return migrated connection."""
    from src.db.migrate import _vector_extension_available

    if not _vector_extension_available(migrated_pg):
        pytest.skip("pgvector extension not installed")
    return migrated_pg


class TestMigrationM13021GlobalSentinel:
    def test_zero_null_profile_name_rows(self, migrated_pg_vec):
        """Case 1: post-migration 0 NULL profile_name rows."""
        with migrated_pg_vec.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM embeddings WHERE profile_name IS NULL")
            count = cur.fetchone()[0]
        assert count == 0, (
            f"Expected 0 NULL profile_name rows post-migration, got {count}"
        )

    def test_column_not_null(self, migrated_pg_vec):
        """Case 2: embeddings.profile_name column is NOT NULL."""
        with migrated_pg_vec.cursor() as cur:
            cur.execute(
                """SELECT is_nullable FROM information_schema.columns
                    WHERE table_name = 'embeddings'
                      AND column_name = 'profile_name'"""
            )
            row = cur.fetchone()
        assert row is not None, "embeddings.profile_name column not found"
        assert row[0] == "NO", (
            f"embeddings.profile_name must be NOT NULL, got is_nullable={row[0]!r}"
        )

    def test_old_check_absent(self, migrated_pg_vec):
        """Case 3: ck_embeddings_null_profile_scope (FU-2) is absent."""
        rows = _constraint_rows(migrated_pg_vec, _OLD_CHECK, "embeddings")
        assert rows == [], (
            f"Old superseded CHECK {_OLD_CHECK!r} must not exist after m13_021; "
            f"got {rows}"
        )

    def test_sentinel_check_present_and_validated(self, migrated_pg_vec):
        """Case 4: ck_embeddings_global_sentinel_scope exists and convalidated=true."""
        rows = _constraint_rows(migrated_pg_vec, _SENTINEL_CHECK, "embeddings")
        assert len(rows) == 1, (
            f"Expected exactly 1 constraint {_SENTINEL_CHECK!r}, got {len(rows)}"
        )
        assert rows[0]["convalidated"] is True, (
            "Sentinel CHECK must be VALIDATED (convalidated=true)"
        )

    def test_sentinel_check_rejects_non_pattern_global(self, migrated_pg_vec):
        """Case 5: non-pattern '__global__' insert raises CheckViolation."""
        import psycopg2.errors

        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_embedding(
                migrated_pg_vec,
                chunk_type="method",
                module="sale",
                profile_name="__global__",
                idx=1,
            )
        migrated_pg_vec.rollback()

    def test_sentinel_check_allows_pattern_global(self, migrated_pg_vec):
        """Case 6: pattern_example/__patterns__/'__global__' row is allowed."""
        _insert_embedding(
            migrated_pg_vec,
            chunk_type="pattern_example",
            module="__patterns__",
            profile_name="__global__",
            idx=2,
        )
        with migrated_pg_vec.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM embeddings "
                "WHERE odoo_version = %s AND chunk_type = 'pattern_example' "
                "  AND module = '__patterns__' AND profile_name = '__global__'",
                (_TV,),
            )
            count = cur.fetchone()[0]
        assert count == 1, "Global pattern row must have been inserted"

    def test_not_null_rejects_null_profile(self, migrated_pg_vec):
        """Case 7: NULL profile_name raises NotNullViolation."""
        import psycopg2.errors

        with pytest.raises(psycopg2.errors.NotNullViolation):
            _insert_embedding(
                migrated_pg_vec,
                chunk_type="pattern_example",
                module="__patterns__",
                profile_name=None,
                idx=3,
            )
        migrated_pg_vec.rollback()

    def test_dunder_check_present_and_validated(self, migrated_pg):
        """Case 8: profiles_name_no_dunder CHECK exists and convalidated=true."""
        rows = _constraint_rows(migrated_pg, _DUNDER_CHECK, "profiles")
        assert len(rows) == 1, (
            f"Expected exactly 1 constraint {_DUNDER_CHECK!r}, got {len(rows)}"
        )
        assert rows[0]["convalidated"] is True, (
            "Dunder CHECK must be VALIDATED (convalidated=true)"
        )

    def test_dunder_check_rejects_global_profile(self, migrated_pg):
        """Case 9: profiles_name_no_dunder CHECK rejects '__global__' profile name."""
        import psycopg2.errors

        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO profiles (name, odoo_version) "
                    "VALUES ('__global__', '17.0')"
                )
        migrated_pg.rollback()

    def test_idempotent_reapply(self, migrated_pg_vec):
        """Case 10: running the migration SQL a second time is a no-op;
        constraint counts remain exactly 1 for each CHECK."""
        _apply_m13_021(migrated_pg_vec)
        # Sentinel CHECK must still be exactly 1.
        sentinel_rows = _constraint_rows(migrated_pg_vec, _SENTINEL_CHECK, "embeddings")
        assert len(sentinel_rows) == 1, (
            f"Idempotent re-run must not duplicate {_SENTINEL_CHECK!r}; "
            f"got {len(sentinel_rows)}"
        )
        assert sentinel_rows[0]["convalidated"] is True
        # Dunder CHECK must still be exactly 1.
        dunder_rows = _constraint_rows(migrated_pg_vec, _DUNDER_CHECK, "profiles")
        assert len(dunder_rows) == 1, (
            f"Idempotent re-run must not duplicate {_DUNDER_CHECK!r}; "
            f"got {len(dunder_rows)}"
        )
        assert dunder_rows[0]["convalidated"] is True

    def test_sentinel_check_rejects_pattern_global_wrong_module(self, migrated_pg_vec):
        """AND-boundary: chunk_type='pattern_example' but module != '__patterns__' must raise.

        Proves chunk_type alone is insufficient — BOTH chunk_type='pattern_example'
        AND module='__patterns__' are required for the '__global__' exemption.
        A row with the correct chunk_type but a non-catalogue module (e.g. 'sale_addon')
        is a data error and must be rejected by the sentinel CHECK.
        """
        import psycopg2.errors

        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_embedding(
                migrated_pg_vec,
                chunk_type="pattern_example",
                module="sale_addon",    # NOT '__patterns__'
                profile_name="__global__",
                idx=11,
            )
        migrated_pg_vec.rollback()

    def test_rls_policy_uses_sentinel_not_is_null(self, migrated_pg_vec):
        """Case 11: embeddings_tenant RLS policy uses '__global__' branch, not IS NULL.

        Confirms the policy body was updated by m13_021 Block 2.
        """
        with migrated_pg_vec.cursor() as cur:
            cur.execute(
                """SELECT pg_get_expr(polqual, polrelid)
                     FROM pg_policy
                    WHERE polname = 'embeddings_tenant'
                      AND polrelid = 'public.embeddings'::regclass"""
            )
            row = cur.fetchone()
        assert row is not None, "embeddings_tenant policy not found"
        policy_text = row[0]
        assert "__global__" in policy_text, (
            f"Policy must contain '__global__' sentinel branch. Got: {policy_text!r}"
        )
        assert "IS NULL" not in policy_text, (
            f"Policy must NOT contain 'IS NULL' branch post-m13_021. Got: {policy_text!r}"
        )
