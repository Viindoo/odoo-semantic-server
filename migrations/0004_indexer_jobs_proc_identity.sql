-- migrations/0004_indexer_jobs_proc_identity.sql
-- Exact process identity on indexer jobs (OSM #383 item 4, follow-up to #381 F1).
--
-- The indexer child writes, with its 'running' report:
--   proc_start_ticks  field 22 (starttime) of its own /proc/self/stat: clock
--                     ticks after boot at which the process started
--   boot_id           /proc/sys/kernel/random/boot_id of the boot it runs in
-- job_registry.job_staleness then decides "is this pid still the job's
-- process" by equality on (boot_id, start ticks): immune to wall-clock steps and
-- exact for a pid reused soon after the job started. Rows where either column is
-- NULL (written before this migration, or where /proc could not be read) keep
-- the 120 s wall-clock heuristic.
--
-- Pure additive: two nullable columns, no default, no backfill (no table
-- rewrite, a brief ACCESS EXCLUSIVE lock only). No grant change: only the Web
-- UI and the indexer (owner DSN) touch indexer_jobs; osm_reader has no grant on
-- it (ops/rls_create_osm_reader.sql).
--
-- IDEMPOTENCY: ADD COLUMN IF NOT EXISTS is safe to re-run.

ALTER TABLE indexer_jobs ADD COLUMN IF NOT EXISTS proc_start_ticks BIGINT;
ALTER TABLE indexer_jobs ADD COLUMN IF NOT EXISTS boot_id TEXT;
