# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_m13_018_embedding_model_dim.py
"""Tests for m13_018 embedding_model/embedding_dim schema + embedding_guard fail-fast helper.

Business rules protected by this test suite:
  3. The backfill UPDATE is idempotent (re-run does not fail or re-set values).
  4. assert_dim_matches() is a no-op when the table is empty.
  5. assert_dim_matches() is a no-op when all rows have embedding_dim=NULL (pre-m13_018 rows).
  6. assert_dim_matches() raises EmbedderDimMismatch when stored_dim != configured_dim.
  7. assert_dim_matches() passes silently when stored_dim == configured_dim and model matches.
  8. EmbedderDimMismatch carries configured_dim, stored_dim, and stored_model attributes.
  9. assert_dim_matches() raises EmbedderModelMismatch when model differs but dim is the same.
     EmbedderModelMismatch carries configured_model, stored_model, stored_dim attributes.
     When configured_model=None (backward-compat), model check is skipped.
 10. assert_dim_matches() raises ValueError on invalid (non-positive) configured_dim.
 11. _build_embeddings_ddl() uses DEFAULT_EMBEDDER_DIM, not a literal 1024 string.

Rules 1-2 (migration-file content + post-migrate column existence) were removed
after the WI-2A squash folded m13_018_embedding_model_dim.sql into
0001_initial.sql: the per-file content assertions read a file that no longer
exists, and the column-existence catalog checks are covered by
test_squashed_baseline.py. The per-file direct-re-run idempotency case
(Rule 3 via _split_sql_statements) was removed for the same reason; baseline
idempotency is covered by test_migrate_is_idempotent (run_migrations twice) and
test_prod_sim_no_reapply (test_squashed_baseline.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Rule 11: migrate.py uses DEFAULT_EMBEDDER_DIM constant (unit — no DB needed)
# ---------------------------------------------------------------------------


class TestMigrateUsesConstant:
    """Rule 11: _build_embeddings_ddl uses the constant, not a literal '1024'."""

    def test_embeddings_sql_contains_dynamic_dim(self):
        """_EMBEDDINGS_SQL must include DDL from _build_embeddings_ddl(DEFAULT_EMBEDDER_DIM)."""
        from src.constants import DEFAULT_EMBEDDER_DIM
        from src.db.migrate import _EMBEDDINGS_SQL, _build_embeddings_ddl

        # _EMBEDDINGS_SQL starts with _build_embeddings_ddl() output (may be followed by
        # index DDL, upgrade SQL, etc. — startswith/contains check is sufficient).
        table_ddl = _build_embeddings_ddl(DEFAULT_EMBEDDER_DIM)
        ddl_present = (
            table_ddl in _EMBEDDINGS_SQL
            or _EMBEDDINGS_SQL.startswith(table_ddl.strip())
        )
        assert ddl_present, (
            "_EMBEDDINGS_SQL must contain the DDL from _build_embeddings_ddl(). "
            "Ensure _EMBEDDINGS_SQL is assembled from _build_embeddings_ddl() in migrate.py, "
            "not from a hard-coded string."
        )
        # Also verify the vector dim from the constant is present in _EMBEDDINGS_SQL
        assert f"vector({DEFAULT_EMBEDDER_DIM})" in _EMBEDDINGS_SQL, (
            f"_EMBEDDINGS_SQL must contain 'vector({DEFAULT_EMBEDDER_DIM})' "
            f"(from DEFAULT_EMBEDDER_DIM={DEFAULT_EMBEDDER_DIM})."
        )

    def test_build_embeddings_ddl_respects_dim_argument(self):
        """_build_embeddings_ddl(768) must produce vector(768), not vector(1024)."""
        from src.db.migrate import _build_embeddings_ddl

        ddl_768 = _build_embeddings_ddl(768)
        ddl_1024 = _build_embeddings_ddl(1024)

        assert "vector(768)" in ddl_768, (
            "_build_embeddings_ddl(768) must produce 'vector(768)' in DDL."
        )
        assert "vector(1024)" not in ddl_768, (
            "_build_embeddings_ddl(768) must NOT produce 'vector(1024)'."
        )
        assert "vector(1024)" in ddl_1024, (
            "_build_embeddings_ddl(1024) must produce 'vector(1024)' in DDL."
        )

    def test_migrate_py_imports_default_embedder_dim(self):
        """migrate.py must import DEFAULT_EMBEDDER_DIM from src.constants."""
        migrate_src = (
            Path(__file__).parent.parent / "src" / "db" / "migrate.py"
        ).read_text()
        assert "DEFAULT_EMBEDDER_DIM" in migrate_src, (
            "src/db/migrate.py must import and use DEFAULT_EMBEDDER_DIM from src.constants. "
            "Hard-coding 1024 in the DDL string is prohibited."
        )


# ---------------------------------------------------------------------------
# Rule 3 (Postgres integration — requires DB)
# One-shot catalog assertions (Rule 2: column existence via information_schema)
# were removed — covered by test_squashed_baseline.py golden snapshot.
# ---------------------------------------------------------------------------

pytestmark_postgres = pytest.mark.postgres


@pytest.mark.postgres
class TestMigrationSchema:
    """Rule 3: backfill is idempotent."""

    def test_migration_idempotent_double_run(self, clean_pg):
        """Rule 3: running run_migrations twice does not fail."""
        from src.db.migrate import run_migrations

        run_migrations(clean_pg)
        try:
            run_migrations(clean_pg)
        except Exception as exc:
            pytest.fail(
                f"run_migrations raised on second run (m13_018 is not idempotent): {exc}"
            )


# ---------------------------------------------------------------------------
# Rules 4-10: embedding_guard unit tests (no DB needed for most)
# ---------------------------------------------------------------------------


class TestAssertDimMatchesUnit:
    """Rules 4-10: fail-fast guard behavior (unit tests using in-memory fakes)."""

    # -----------------------------------------------------------------------
    # Fake psycopg2 connection helpers for unit testing without a live DB
    # -----------------------------------------------------------------------

    class _FakeCursor:
        """Minimal cursor stub that returns a pre-configured row."""

        def __init__(self, row):
            self._row = row

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params=None):
            pass

        def fetchone(self):
            return self._row

    class _FakeConn:
        def __init__(self, row):
            self._row = row

        def cursor(self):
            return TestAssertDimMatchesUnit._FakeCursor(self._row)

    def test_no_op_when_table_empty(self):
        """Rule 4: assert_dim_matches does not raise when query returns no row."""
        from src.db.embedding_guard import assert_dim_matches

        conn = self._FakeConn(row=None)
        # Should not raise
        assert_dim_matches(conn, configured_dim=1024)

    def test_no_op_when_stored_dim_matches(self):
        """Rule 7: assert_dim_matches passes silently when dims match."""
        from src.db.embedding_guard import assert_dim_matches

        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        assert_dim_matches(conn, configured_dim=1024)  # must not raise

    def test_raises_on_dim_mismatch(self):
        """Rule 6: assert_dim_matches raises EmbedderDimMismatch when dims differ."""
        from src.db.embedding_guard import EmbedderDimMismatch, assert_dim_matches

        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        with pytest.raises(EmbedderDimMismatch):
            assert_dim_matches(conn, configured_dim=768)

    def test_exception_attributes_are_populated(self):
        """Rule 8: EmbedderDimMismatch carries configured_dim, stored_dim, stored_model."""
        from src.db.embedding_guard import EmbedderDimMismatch, assert_dim_matches

        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        with pytest.raises(EmbedderDimMismatch) as exc_info:
            assert_dim_matches(conn, configured_dim=768)

        exc = exc_info.value
        assert exc.configured_dim == 768
        assert exc.stored_dim == 1024
        assert exc.stored_model == "qwen3-embedding-q5km"

    def test_error_message_mentions_reindex(self):
        """Rule 6: EmbedderDimMismatch message guides the operator to run a full reindex."""
        from src.db.embedding_guard import EmbedderDimMismatch, assert_dim_matches

        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        with pytest.raises(EmbedderDimMismatch) as exc_info:
            assert_dim_matches(conn, configured_dim=768)

        msg = str(exc_info.value)
        assert "reindex" in msg.lower(), (
            "EmbedderDimMismatch message must guide the operator to reindex."
        )
        assert "768" in msg
        assert "1024" in msg

    def test_raises_value_error_on_non_positive_dim(self):
        """Rule 10: assert_dim_matches raises ValueError on configured_dim <= 0."""
        from src.db.embedding_guard import assert_dim_matches

        conn = self._FakeConn(row=None)
        with pytest.raises(ValueError):
            assert_dim_matches(conn, configured_dim=0)

    def test_latest_tag_normalized_symmetrically(self):
        """An optional Ollama ':latest' tag must NOT read as a model switch,
        regardless of which side carries it — guard normalizes both operands."""
        from src.db.embedding_guard import assert_dim_matches

        # stored bare, configured carries :latest -> no mismatch
        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        assert_dim_matches(
            conn, configured_dim=1024, configured_model="qwen3-embedding-q5km:latest"
        )

        # stored carries :latest (e.g. a row written by a pre-fix run),
        # configured bare -> still no mismatch (symmetric normalization)
        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km:latest"))
        assert_dim_matches(
            conn, configured_dim=1024, configured_model="qwen3-embedding-q5km"
        )

    def test_genuine_model_switch_still_raises(self):
        """Normalization must not mask a real model change (different latent
        space at the same dim)."""
        from src.db.embedding_guard import EmbedderModelMismatch, assert_dim_matches

        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        with pytest.raises(EmbedderModelMismatch):
            assert_dim_matches(
                conn, configured_dim=1024, configured_model="text-embedding-3-small"
            )

        with pytest.raises(ValueError):
            assert_dim_matches(conn, configured_dim=-1)

    def test_raises_value_error_on_non_int_dim(self):
        """Rule 10: assert_dim_matches raises ValueError on non-int configured_dim."""
        from src.db.embedding_guard import assert_dim_matches

        conn = self._FakeConn(row=None)
        with pytest.raises(ValueError):
            assert_dim_matches(conn, configured_dim="1024")  # type: ignore[arg-type]


class TestAssertDimMatchesModelMismatch:
    """Rule 9: model-mismatch detection in assert_dim_matches.

    record_embedding_meta was removed (dead production code — writer uses
    EmbeddingChunk.as_tuple positional args, not a dict-merge helper).
    These tests cover the replacement behavior: assert_dim_matches raises
    EmbedderModelMismatch when the model name changes even if dim is identical.
    """

    class _FakeCursor:
        def __init__(self, row):
            self._row = row

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params=None):
            pass

        def fetchone(self):
            return self._row

    class _FakeConn:
        def __init__(self, row):
            self._row = row

        def cursor(self):
            return TestAssertDimMatchesModelMismatch._FakeCursor(self._row)

    def test_raises_model_mismatch_same_dim(self):
        """Rule 9: switching model at same dim raises EmbedderModelMismatch."""
        from src.db.embedding_guard import EmbedderModelMismatch, assert_dim_matches

        # stored: qwen3 at 1024; configured: openai model also at 1024
        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        with pytest.raises(EmbedderModelMismatch):
            assert_dim_matches(conn, 1024, "openai-text-embedding-3-small")

    def test_model_mismatch_attributes(self):
        """Rule 9: EmbedderModelMismatch carries configured_model, stored_model, stored_dim."""
        from src.db.embedding_guard import EmbedderModelMismatch, assert_dim_matches

        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        with pytest.raises(EmbedderModelMismatch) as exc_info:
            assert_dim_matches(conn, 1024, "openai-text-embedding-3-small")

        exc = exc_info.value
        assert exc.configured_model == "openai-text-embedding-3-small"
        assert exc.stored_model == "qwen3-embedding-q5km"
        assert exc.stored_dim == 1024

    def test_model_mismatch_message_guides_reindex(self):
        """Rule 9: EmbedderModelMismatch message mentions reindex."""
        from src.db.embedding_guard import EmbedderModelMismatch, assert_dim_matches

        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        with pytest.raises(EmbedderModelMismatch) as exc_info:
            assert_dim_matches(conn, 1024, "openai-text-embedding-3-small")

        msg = str(exc_info.value)
        assert "reindex" in msg.lower()
        assert "openai-text-embedding-3-small" in msg
        assert "qwen3-embedding-q5km" in msg

    def test_no_model_check_when_configured_model_is_none(self):
        """Rule 9 backward-compat: configured_model=None skips model check."""
        from src.db.embedding_guard import assert_dim_matches

        # stored: qwen3 at 1024; caller passes only dim (no model) — must not raise
        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        assert_dim_matches(conn, 1024)  # positional; configured_model defaults to None

    def test_no_model_check_when_stored_model_is_none(self):
        """Rule 9: if stored_model IS NULL, model check is skipped even when configured."""
        from src.db.embedding_guard import assert_dim_matches

        # Pre-m13_018 rows backfilled with NULL model; dim matches.
        conn = self._FakeConn(row=(1024, None))
        assert_dim_matches(conn, 1024, "qwen3-embedding-q5km")  # must not raise

    def test_dim_mismatch_takes_priority_over_model_mismatch(self):
        """Rule 6 vs 9: dim mismatch raises EmbedderDimMismatch, not EmbedderModelMismatch."""
        from src.db.embedding_guard import EmbedderDimMismatch, assert_dim_matches

        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        with pytest.raises(EmbedderDimMismatch):
            assert_dim_matches(conn, 768, "openai-text-embedding-3-small")

    def test_passes_when_both_dim_and_model_match(self):
        """Rule 7 extended: no exception when dim and model both match."""
        from src.db.embedding_guard import assert_dim_matches

        conn = self._FakeConn(row=(1024, "qwen3-embedding-q5km"))
        assert_dim_matches(conn, 1024, "qwen3-embedding-q5km")  # must not raise


# ---------------------------------------------------------------------------
# Rules 4-5 (Postgres integration — null-embedding_dim skip behavior)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestAssertDimMatchesIntegration:
    """Rules 4-5: skip behavior against a real (test) PostgreSQL DB."""

    def test_no_op_on_empty_embeddings_table(self, clean_pg):
        """Rule 4: guard is silent when the embeddings table is empty."""
        from src.db.embedding_guard import assert_dim_matches
        from src.db.migrate import _vector_extension_available, run_migrations

        run_migrations(clean_pg)
        if not _vector_extension_available(clean_pg):
            pytest.skip("pgvector not available")

        # Table is empty after fresh migrate — must not raise
        assert_dim_matches(clean_pg, configured_dim=1024)

    def test_raises_when_row_has_mismatched_dim(self, clean_pg):
        """Rule 6 (integration): guard raises when DB row has a different dim."""
        from src.db.embedding_guard import EmbedderDimMismatch, assert_dim_matches
        from src.db.migrate import _vector_extension_available, run_migrations

        run_migrations(clean_pg)
        if not _vector_extension_available(clean_pg):
            pytest.skip("pgvector not available")

        # Manually insert a row with embedding_dim=512 (simulates a different model)
        with clean_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO embeddings "
                "(chunk_type, module, odoo_version, entity_name, file_path, content, vec,"
                " embedding_model, embedding_dim, profile_name)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)",
                (
                    "method", "sale", "99.0", "action_confirm",
                    "models/sale.py", "def action_confirm(self):",
                    str([0.0] * 1024),  # vector matches DDL dim
                    "some-other-model", 512, "test_profile",
                ),
            )

        with pytest.raises(EmbedderDimMismatch):
            assert_dim_matches(clean_pg, configured_dim=1024)

    def test_passes_when_row_dim_matches_configured(self, clean_pg):
        """Rule 7 (integration): guard is silent when DB row dim matches."""
        from src.db.embedding_guard import assert_dim_matches
        from src.db.migrate import _vector_extension_available, run_migrations

        run_migrations(clean_pg)
        if not _vector_extension_available(clean_pg):
            pytest.skip("pgvector not available")

        with clean_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO embeddings "
                "(chunk_type, module, odoo_version, entity_name, file_path, content, vec,"
                " embedding_model, embedding_dim, profile_name)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)",
                (
                    "method", "sale", "99.0", "action_confirm2",
                    "models/sale.py", "def action_confirm(self):",
                    str([0.0] * 1024),
                    "qwen3-embedding-q5km", 1024, "test_profile",
                ),
            )

        # Should not raise
        assert_dim_matches(clean_pg, configured_dim=1024)
