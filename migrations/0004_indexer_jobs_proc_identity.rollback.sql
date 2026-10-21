-- migrations/0004_indexer_jobs_proc_identity.rollback.sql
-- Rollback of 0004_indexer_jobs_proc_identity.sql. Code from before #383 never
-- reads the columns; code from #383 on writes them, but falls back to writing
-- the job row without them when they are missing (job_registry.update_job), so
-- this rollback is safe with either. Rows lose their exact identity and are
-- judged by the 120 s wall-clock heuristic again.

-- Lock guard, same as the forward migration: fail fast instead of queueing
-- every query on the table behind an idle-in-transaction lock holder.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE indexer_jobs DROP COLUMN IF EXISTS boot_id;
ALTER TABLE indexer_jobs DROP COLUMN IF EXISTS proc_start_ticks;
