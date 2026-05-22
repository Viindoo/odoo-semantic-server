-- m13_001_embeddings_profile_name.sql
-- WI-B (ADR-0034 WI-5 schema half): add profile_name column to embeddings table.
-- NULL = shared/global (pattern chunks, legacy rows before multi-tenant).
--
-- IMPORTANT: Row Level Security (RLS) is NOT enabled here.
-- Enabling RLS requires runtime `SET LOCAL app.allowed_profiles` wiring
-- which is deferred to a later migration. Enabling it now would default-deny
-- and break all queries.
--
-- Idempotent: all DDL is guarded so it is safe to re-run.
-- The embeddings table is created by migrate.py _EMBEDDINGS_SQL (requires
-- pgvector superuser extension). When pgvector is absent the table does not
-- exist; this migration is a no-op in that case.

DO $$
BEGIN
  -- Guard: skip everything when embeddings table does not exist (no pgvector).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_name = 'embeddings' AND table_schema = 'public'
  ) THEN
    RETURN;
  END IF;

  -- 1. Add column (idempotent via column-existence check).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'embeddings' AND column_name = 'profile_name'
  ) THEN
    ALTER TABLE embeddings ADD COLUMN profile_name TEXT;
  END IF;

  -- 2. Rebuild the unique constraint to include profile_name if absent.
  --    This mirrors the pattern in migrate.py _EMBEDDINGS_UPGRADE_SQL.
  --
  --    NULLS NOT DISTINCT (PG15+) is REQUIRED: profile_name is NULL for shared
  --    and pattern chunks. Under default SQL semantics two NULLs compare as
  --    distinct, so a plain UNIQUE key would let duplicate NULL-profile chunks
  --    coexist — silently regressing the dedup invariant the 6-column key
  --    guaranteed before profile_name existed. With NULLS NOT DISTINCT, NULL
  --    profile_names are treated as equal, so shared/pattern chunks still
  --    dedup on (chunk_type, module, odoo_version, entity_name, file_path,
  --    chunk_idx) exactly as they did pre-WI-B, while non-NULL profiles remain
  --    independently scoped.
  IF EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'ux_embeddings_chunk' AND table_name = 'embeddings'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.constraint_column_usage
    WHERE constraint_name = 'ux_embeddings_chunk' AND column_name = 'profile_name'
  ) THEN
    ALTER TABLE embeddings DROP CONSTRAINT ux_embeddings_chunk;
    ALTER TABLE embeddings ADD CONSTRAINT ux_embeddings_chunk
      UNIQUE NULLS NOT DISTINCT
      (chunk_type, module, odoo_version, entity_name, file_path, chunk_idx, profile_name);
  END IF;

  -- 3. Rebuild idx_embeddings_filter to include profile_name for
  --    profile-scoped ANN pre-filter queries. DROP + CREATE inside the block
  --    keeps all DDL guarded by the table-existence check above.
  DROP INDEX IF EXISTS idx_embeddings_filter;
  CREATE INDEX IF NOT EXISTS idx_embeddings_filter
      ON embeddings (odoo_version, chunk_type, module, profile_name);
END $$;
