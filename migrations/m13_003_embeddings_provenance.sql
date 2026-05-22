-- m13_003_embeddings_provenance.sql
-- WI-A3: add provenance columns to embeddings table.
-- line_start: 1-based source line of the entity (method def / field assign / XML record).
-- repo:       repo basename (ModuleInfo.repo), TEXT — no FK so schema stays additive.
-- repo_id:    FK to repos.id (ModuleInfo.repo_id), INTEGER — nullable, no FK constraint
--             (repos table may not exist in minimal test setups).
--
-- These are data columns — NOT part of the ux_embeddings_chunk unique constraint
-- and NOT part of idx_embeddings_filter.  Adding them to the unique key would break
-- existing duplicate-detection semantics and require a full-table constraint rebuild.
-- The ON CONFLICT DO UPDATE in writer_pgvector._INSERT_SQL updates line_start/repo/
-- repo_id on re-embed, so data stays current without a rekey.
--
-- Populated on re-index (REINDEX-FORCING).  Old rows retain NULL until re-indexed.
--
-- Idempotent: all DDL is guarded so it is safe to re-run.

DO $$
BEGIN
  -- Guard: skip everything when embeddings table does not exist (no pgvector).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_name = 'embeddings' AND table_schema = 'public'
  ) THEN
    RETURN;
  END IF;

  -- 1. line_start — 1-based source line of the entity.
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'embeddings' AND column_name = 'line_start'
  ) THEN
    ALTER TABLE embeddings ADD COLUMN line_start INTEGER;
  END IF;

  -- 2. repo — repo basename string (no FK, additive).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'embeddings' AND column_name = 'repo'
  ) THEN
    ALTER TABLE embeddings ADD COLUMN repo TEXT;
  END IF;

  -- 3. repo_id — FK to repos.id (nullable INTEGER, no constraint — test-safe).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'embeddings' AND column_name = 'repo_id'
  ) THEN
    ALTER TABLE embeddings ADD COLUMN repo_id INTEGER;
  END IF;

END $$;
