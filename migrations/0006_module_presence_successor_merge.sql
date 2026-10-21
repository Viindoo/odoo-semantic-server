-- migrations/0006_module_presence_successor_merge.sql
-- A third successor source for the module lifecycle ledger (OSM #380 item 3).
--
-- 'git_merge': the commit that deleted the module's manifest moved at least
-- half of its files (git diff -M pairing, inclusive) into ONE module directory
-- that existed before the commit and still exists after it - an N:1 merge
-- (viin_ai_rag, viin_ai_agent, viin_ai_skill, viin_ai_search, viin_ai_memory
-- -> viin_ai at 17.0, tvtmaaddons). check_module_exists renders it as
-- "Merged into: <module>" (the two existing sources render "Renamed to").
-- The oracle is git alone (src/indexer/lifecycle.py merged_into); the commit
-- subject is never evidence.
--
-- Schema-only: the CHECK constraint is widened, no row changes, no backfill
-- here (rows retired before this release get their successor from
-- ops/backfill_successors.py, dry run by default). Until this migration is
-- applied, the indexer drops a git_merge successor with a WARNING instead of
-- failing the retirement (ModulePresenceStore._storable_successor).
--
-- Grants / RLS: unchanged. Only the indexer and the Web UI (owner DSN) write
-- module_presence; osm_reader keeps SELECT under the module_presence_tenant
-- policy (ops/rls_create_osm_reader.sql). DROP + ADD CONSTRAINT takes a brief
-- ACCESS EXCLUSIVE lock and re-validates the existing rows (all already pass).
--
-- IDEMPOTENT: DROP CONSTRAINT IF EXISTS + ADD in one statement; a re-run
-- replaces the constraint with the same definition.
--
-- Rollback: 0006_module_presence_successor_merge.rollback.sql.

-- Lock guard: ALTER TABLE needs ACCESS EXCLUSIVE. Behind an idle-in-transaction
-- connection that holds even ACCESS SHARE on the table it would wait forever and
-- queue every later query on the table behind it (the Web UI is live while
-- migrate runs). SET LOCAL scopes both limits to this migration's transaction
-- (yoyo applies each migration in one transaction): the migration fails fast
-- with "canceling statement due to lock timeout" and nothing applied - re-run
-- python -m src.db.migrate once the holder is gone.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE module_presence
    DROP CONSTRAINT IF EXISTS ck_module_presence_successor_source,
    ADD CONSTRAINT ck_module_presence_successor_source
        CHECK (successor_source IN ('git_rename', 'git_merge', 'old_technical_name'));
