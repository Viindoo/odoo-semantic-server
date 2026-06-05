# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_m13_018_embedding_model_dim.py
"""Tests for m13_018_embedding_model_dim migration + embedding_guard fail-fast helper.

Business rules protected by this test suite:
  1. Migration file m13_018 exists, is parseable SQL, and contains the two ALTER
     TABLE statements for embedding_model and embedding_dim.
  2. After run_migrations(), the embeddings table has both new columns.
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
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module-level markers
# ---------------------------------------------------------------------------

MIGRATION_PATH = Path(__file__).parent.parent / "migrations" / "m13_018_embedding_model_dim.sql"


def _split_sql_statements(sql: str) -> list[str]:
    """Split SQL into top-level statements, respecting ``$$``-dollar-quoted blocks.

    m13_018 is non-transactional (``-- transactional: false``): it mixes plain
    statements with ``DO $$ ... $$`` blocks whose bodies contain semicolons, plus
    a ``CREATE INDEX CONCURRENTLY``.  A naive ``;`` split would break the DO
    blocks, and a single ``execute(whole_file)`` cannot run CONCURRENTLY inside a
    transaction.  This mirrors how yoyo deploys it: one statement at a time in
    autocommit.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_dollar = False
    for line in sql.splitlines():
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        buf.append(line)
        if not in_dollar and line.strip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if any(ln.strip() and not ln.strip().startswith("--") for ln in stmt.splitlines()):
                statements.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail and any(ln.strip() and not ln.strip().startswith("--") for ln in tail.splitlines()):
        statements.append(tail)
    return statements


# ---------------------------------------------------------------------------
# Rule 1: File existence and SQL content (unit — no DB needed)
# ---------------------------------------------------------------------------


class TestMigrationFileContent:
    """Rule 1: migration file exists and contains the required ALTER TABLE statements."""

    def test_migration_file_exists(self):
        assert MIGRATION_PATH.exists(), (
            f"Migration file not found: {MIGRATION_PATH}. "
            "Must create migrations/m13_018_embedding_model_dim.sql."
        )

    def test_migration_adds_embedding_model_column(self):
        sql = MIGRATION_PATH.read_text()
        assert "embedding_model" in sql, (
            "m13_018 must ALTER TABLE embeddings ADD COLUMN ... embedding_model"
        )

    def test_migration_adds_embedding_dim_column(self):
        sql = MIGRATION_PATH.read_text()
        assert "embedding_dim" in sql, (
            "m13_018 must ALTER TABLE embeddings ADD COLUMN ... embedding_dim"
        )

    def test_migration_has_backfill_update(self):
        sql = MIGRATION_PATH.read_text()
        assert "UPDATE embeddings" in sql, (
            "m13_018 must contain an UPDATE embeddings backfill for pre-existing rows."
        )

    def test_migration_backfill_references_default_model(self):
        sql = MIGRATION_PATH.read_text()
        assert "qwen3-embedding-q5km" in sql, (
            "m13_018 backfill must set embedding_model = 'qwen3-embedding-q5km' "
            "(the model used for all pre-m13_018 vectors)."
        )

    def test_migration_backfill_references_default_dim(self):
        sql = MIGRATION_PATH.read_text()
        assert "1024" in sql, (
            "m13_018 backfill must set embedding_dim = 1024."
        )

    def test_migration_is_idempotent_by_if_not_exists(self):
        sql = MIGRATION_PATH.read_text()
        assert "IF NOT EXISTS" in sql.upper(), (
            "m13_018 must use IF NOT EXISTS for idempotent column additions."
        )

    def test_migration_is_non_transactional(self):
        sql = MIGRATION_PATH.read_text()
        # yoyo directive — required so CONCURRENTLY index does not fail inside a txn.
        assert "transactional: false" in sql.lower(), (
            "m13_018 must declare '-- transactional: false' so yoyo runs it outside "
            "a wrapping transaction (required for CONCURRENTLY index + batch COMMIT)."
        )

    def test_migration_uses_concurrently_index(self):
        sql = MIGRATION_PATH.read_text()
        assert "CONCURRENTLY" in sql.upper(), (
            "m13_018 must use CREATE INDEX CONCURRENTLY to avoid write-lock on production table."
        )

    def test_backfill_is_bounded_not_repeated_seqscan(self):
        """Business rule: the backfill must be O(n) bounded, never a repeated
        full seq-scan on the unindexed ``embedding_model IS NULL`` predicate.

        Issue #230: the original loop used
        ``WHERE ctid IN (SELECT ctid FROM embeddings WHERE embedding_model IS NULL LIMIT N)``.
        That predicate has no supporting index, so every batch re-scanned the
        whole table past an ever-growing filled prefix -> O(n^2).  The fix
        walks the BIGSERIAL primary key in id-ranges so each batch is an
        index-range scan visiting every row exactly once -> O(n).
        """
        import re

        sql = MIGRATION_PATH.read_text()

        # Assert against CODE only, never comments.  This migration's header
        # comment deliberately quotes BOTH the banned anti-pattern AND the
        # keyset shape ("id >= lo AND id < lo+step"), so matching raw text
        # would let every check below pass on the explanatory prose even if the
        # actual backfill regressed.  Strip "--" line comments once and share
        # the result across all assertions (positive + negative guard).
        code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).upper()

        # Positive: still batched (committed in chunks, not one giant UPDATE)...
        assert "LOOP" in code, (
            "m13_018 backfill must stay batched (LOOP) to bound lock duration + "
            "WAL burst on large tables (~591k rows)."
        )
        # ...and the batching mechanism must be a primary-key range scan
        # (half-open `id >= lo AND id < hi`, or `id BETWEEN lo AND hi`).
        assert re.search(r"\bID\s*(?:[<>]=?|BETWEEN\b)", code), (
            "m13_018 backfill must range-batch over the primary key "
            "(e.g. 'id >= lo AND id < lo + step') so each batch is an index-range "
            "scan -> O(n).  See issue #230."
        )

        # Negative regression guard (the important one): the O(n^2) signature
        # -- selecting ctids filtered by the unindexed IS NULL predicate with a
        # LIMIT -- must not reappear.
        has_ctid_select = "SELECT CTID" in code
        has_isnull_filter = "EMBEDDING_MODEL IS NULL" in code
        # LIMIT is only an anti-pattern signal when paired with the ctid+IS NULL
        # full-scan subquery; range-batching never needs LIMIT.
        assert not (has_ctid_select and has_isnull_filter and "LIMIT" in code), (
            "m13_018 backfill reintroduced the O(n^2) anti-pattern "
            "(SELECT ctid ... WHERE embedding_model IS NULL ... LIMIT). "
            "Range-batch over the primary key instead -- see issue #230."
        )


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
# Rules 2, 3 (Postgres integration — requires DB)
# ---------------------------------------------------------------------------

pytestmark_postgres = pytest.mark.postgres


@pytest.mark.postgres
class TestMigrationSchema:
    """Rules 2+3: schema columns present and backfill is idempotent."""

    def test_embedding_model_column_present(self, clean_pg):
        """Rule 2: embeddings.embedding_model column exists after run_migrations."""
        from src.db.migrate import _vector_extension_available, run_migrations

        run_migrations(clean_pg)
        if not _vector_extension_available(clean_pg):
            pytest.skip("pgvector not available — embeddings table skipped")

        with clean_pg.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'embeddings' AND column_name = 'embedding_model'"
            )
            row = cur.fetchone()
        assert row is not None, (
            "embeddings.embedding_model column missing after run_migrations + m13_018"
        )

    def test_embedding_dim_column_present(self, clean_pg):
        """Rule 2: embeddings.embedding_dim column exists after run_migrations."""
        from src.db.migrate import _vector_extension_available, run_migrations

        run_migrations(clean_pg)
        if not _vector_extension_available(clean_pg):
            pytest.skip("pgvector not available — embeddings table skipped")

        with clean_pg.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'embeddings' AND column_name = 'embedding_dim'"
            )
            row = cur.fetchone()
        assert row is not None, (
            "embeddings.embedding_dim column missing after run_migrations + m13_018"
        )

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

    def test_m13_018_sql_idempotent_when_run_directly(self, clean_pg):
        """Rule 3: re-running m13_018 on an already-migrated DB is a safe no-op.

        The migration is non-transactional (CREATE INDEX CONCURRENTLY + per-batch
        COMMIT), so it must be re-applied the way yoyo deploys it — statement by
        statement in autocommit — not as one execute() inside a transaction.
        """
        from src.db.migrate import _vector_extension_available, run_migrations

        run_migrations(clean_pg)
        if not _vector_extension_available(clean_pg):
            pytest.skip("pgvector not available — embeddings table skipped")

        statements = _split_sql_statements(MIGRATION_PATH.read_text())
        prev_autocommit = clean_pg.autocommit
        clean_pg.autocommit = True  # CONCURRENTLY + DO-block COMMIT need no wrapping txn
        try:
            with clean_pg.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)
        except Exception as exc:
            pytest.fail(
                f"m13_018_embedding_model_dim.sql raised on second direct execution: {exc}"
            )
        finally:
            clean_pg.autocommit = prev_autocommit


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
