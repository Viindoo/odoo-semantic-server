# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live constraint-enforcement tests for the __global__ sentinel schema.

After the migration squash, one-shot existence/introspection tests (column
NOT NULL, old CHECK absent, new CHECK present, idempotent re-apply) are covered
by tests/test_squashed_baseline.py.  This file keeps ONLY the five tests that
protect LIVE CONSTRAINT BEHAVIOUR by attempting invalid/valid INSERTs and
asserting the database raises (or allows) them.  These tests cannot be replaced
by introspection because they verify the constraints actually fire at runtime.

Constraints exercised:
  - ck_embeddings_global_sentinel_scope  (embeddings table)
  - NOT NULL on embeddings.profile_name
  - profiles_name_no_dunder              (profiles table)

All tests run against the baseline schema produced by migrations/0001_initial.sql.
Requires PostgreSQL + pgvector (pytestmark = pytest.mark.postgres).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.postgres

_SENTINEL_CHECK = "ck_embeddings_global_sentinel_scope"
_DUNDER_CHECK = "profiles_name_no_dunder"

# A zero-vector literal compatible with the default embedder dim (1024).
_ZERO_VEC = "[" + ",".join(["0.0"] * 1024) + "]"

# Unique version sentinel for this test module to avoid row collisions.
_TV = "98.0"


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
    """Apply all migrations (including squashed baseline) on a clean schema."""
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


class TestM13021LiveConstraintEnforcement:
    """Five enforcement tests: attempt bad/good INSERTs and assert the DB fires correctly."""

    def test_sentinel_check_rejects_non_pattern_global(self, migrated_pg_vec):
        """ck_embeddings_global_sentinel_scope rejects non-pattern '__global__' insert.

        A method-type row carrying profile_name='__global__' is a data error —
        the sentinel is reserved exclusively for pattern_example catalogue rows.
        The CHECK constraint must fire and raise CheckViolation.
        """
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
        """ck_embeddings_global_sentinel_scope allows pattern_example + __patterns__ + __global__.

        This is the ONE valid combination for the global pattern catalogue.
        Confirms the CHECK is not over-restrictive.
        """
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
        """embeddings.profile_name NOT NULL constraint rejects a NULL insert."""
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

    def test_dunder_check_rejects_global_profile(self, migrated_pg):
        """profiles_name_no_dunder CHECK rejects a '__global__' profile name.

        The profiles table must not allow dunder-named profiles; '__global__' is
        a sentinel reserved for the embeddings catalogue, not a real profile.
        """
        import psycopg2.errors

        with pytest.raises(psycopg2.errors.CheckViolation):
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO profiles (name, odoo_version) "
                    "VALUES ('__global__', '17.0')"
                )
        migrated_pg.rollback()

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
