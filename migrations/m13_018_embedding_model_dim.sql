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
-- Backfill (batched, keyset-by-PK):
--   Existing rows are backfilled by walking the BIGSERIAL primary key in
--   fixed id-ranges (10 000 ids per batch), committing each batch, on a
--   large embeddings table (~591k rows in production).
--
--   Why keyset-by-PK and not "SELECT ctid ... WHERE embedding_model IS NULL
--   LIMIT N" (issue #230): the IS-NULL predicate has no supporting index, so
--   every such SELECT is a full sequential scan.  As rows get filled, each
--   batch must scan past an ever-growing prefix of already-filled rows ->
--   O(n^2 / batch_size) total (32+ min on prod).  Range-scanning the PK
--   (id >= lo AND id < lo+step) bounds each batch to an index-range scan that
--   visits every row exactly once across the whole loop -> O(n).
--
--   NOTE: per-batch COMMIT bounds lock duration + WAL burst; it does NOT
--   bound scan cost.  The two are independent -- batching COMMITs alone would
--   still leave the old loop O(n^2).  Future wide-table backfills must follow
--   the keyset-by-PK pattern, not just add COMMITs.
--
--   The WHERE clause stays idempotent (only touches NULL rows).  Rows written
--   concurrently by the indexer already carry embedding_model (writer_pgvector
--   stamps it on INSERT), so the one-shot max(id) snapshot cannot miss them.
--
--   2026-06-01: backfill loop rewritten ctid-seqscan -> PK-range (issue #230).
--   Prod m13_018 was already applied with the old loop and finished (idempotent),
--   so this is a forward-looking fix for fresh-install / restore / CI; it does
--   NOT require a re-deploy and does not re-run on instances already migrated
--   (yoyo tracks by migration id, not file content).
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

-- ===== 3. Backfill pre-existing rows (keyset-by-PK, idempotent) =====
-- Walk the primary key in fixed id-ranges so each batch is an index-range
-- scan (O(step)) instead of a repeated full seq-scan on the unindexed
-- "embedding_model IS NULL" predicate (issue #230 -- O(n^2) on the old loop).
-- Each batch COMMITs to bound lock duration + WAL burst.  The migration runs
-- outside a wrapping transaction (transactional: false), so COMMIT here runs
-- on an autocommit connection (valid in a DO block on PG11+).
DO $$
DECLARE
    batch_lo BIGINT;
    max_id   BIGINT;
    -- step caps WAL + lock footprint per COMMIT at <= step wide vector(1024)
    -- rows (~step * 4 KB).  A smaller step is the WAL-safe choice: empty
    -- batches over PK gaps (rows deleted on reindex) cost only a cheap bounded
    -- index-range scan, so there is no need to inflate step to "skip" gaps.
    step     BIGINT := 10000;
BEGIN
    SELECT min(id), max(id) INTO batch_lo, max_id FROM embeddings;
    IF max_id IS NULL THEN
        RETURN;  -- empty table (fresh install) -> nothing to backfill
    END IF;
    WHILE batch_lo <= max_id LOOP
        UPDATE embeddings
           SET embedding_model = 'qwen3-embedding-q5km',
               embedding_dim   = 1024
         WHERE id >= batch_lo
           AND id <  batch_lo + step
           AND embedding_model IS NULL;  -- idempotent: only touches NULL rows
        COMMIT;
        batch_lo := batch_lo + step;
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
