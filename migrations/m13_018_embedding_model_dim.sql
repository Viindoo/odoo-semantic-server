-- migrations/m13_018_embedding_model_dim.sql
-- transactional: false
--
-- Track embedding model name + dimension per row in the embeddings table.
--
-- Context:
--   The embeddings.vec column was created with a hard-coded dimension
--   (vector(1024)) matching the default Qwen3-embedding-q5km model.  If the
--   operator switches to a different embedding model (different dimension or
--   different latent space) without a full reindex, existing and new vectors
--   would be in incompatible spaces, silently corrupting cosine-similarity
--   results.
--
--   This migration adds two tracking columns:
--     embedding_model TEXT   -- model name used to produce the vector
--                               (distinct from model_name = Odoo model e.g. 'sale.order')
--     embedding_dim   INT    -- dimensionality of the vector stored in vec
--
--   These columns let the read/write path detect dimension and model mismatches
--   at runtime and raise an error rather than silently mix incompatible vectors.
--   The fail-fast guard lives in src/db/embedding_guard.py.
--
-- Backfill (batched):
--   Existing rows are backfilled in batches of 10 000 rows to avoid a
--   single-statement table lock + WAL burst on large embeddings tables
--   (~591k rows in production).  Each batch is committed individually.
--   The WHERE clause is idempotent (only touches NULL rows).
--
-- Index:
--   Created with CONCURRENTLY so no write-lock is held on the live table.
--   Requires autocommit / non-transactional context -- ensured by the
--   "-- transactional: false" directive above (yoyo runs the file outside
--   a wrapping transaction).
--
-- Idempotent -- safe to re-run.

-- ===== 1. Add embedding_model column =====
ALTER TABLE embeddings
    ADD COLUMN IF NOT EXISTS embedding_model TEXT;

-- ===== 2. Add embedding_dim column =====
ALTER TABLE embeddings
    ADD COLUMN IF NOT EXISTS embedding_dim INT;

-- ===== 3. Backfill pre-existing rows (batched, idempotent) =====
-- Batches of 10 000 rows to prevent long-held locks and WAL burst on
-- large tables.  Each DO-block iteration is committed immediately because
-- the migration runs outside a wrapping transaction (transactional: false).
DO $$
DECLARE
    rows_updated INT;
BEGIN
    LOOP
        UPDATE embeddings
           SET embedding_model = 'qwen3-embedding-q5km',
               embedding_dim   = 1024
         WHERE ctid IN (
             SELECT ctid FROM embeddings
              WHERE embedding_model IS NULL
              LIMIT 10000
         );
        GET DIAGNOSTICS rows_updated = ROW_COUNT;
        EXIT WHEN rows_updated = 0;
        COMMIT;
    END LOOP;
END $$;

-- ===== 4. Index for model-aware filtering (CONCURRENTLY — no write lock) =====
-- A partial index on embedding_model speeds up the fail-fast guard's
-- per-model lookup and any future per-model cosine search filters.
-- CONCURRENTLY requires autocommit; guaranteed by transactional: false above.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_embeddings_model
    ON embeddings (embedding_model)
    WHERE embedding_model IS NOT NULL;

-- ===== 5. osm_reader grant =====
-- Covers the two new columns (new columns inherit table-level grants on
-- Postgres, but this explicit grant is belt-and-suspenders + idempotent).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'osm_reader') THEN
        GRANT SELECT ON embeddings TO osm_reader;
    END IF;
END $$;
