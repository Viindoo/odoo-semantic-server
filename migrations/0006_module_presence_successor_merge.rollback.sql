-- migrations/0006_module_presence_successor_merge.rollback.sql
-- Rollback of 0006_module_presence_successor_merge.sql: narrow the successor
-- source back to ('git_rename', 'old_technical_name').
--
-- Rows recorded with successor_source 'git_merge' would violate the narrower
-- CHECK, so their successor is cleared first (both columns together, keeping
-- ck_module_presence_successor_pair). That loses only the "Merged into"
-- answer; the removing commit and the rest of the history stay. Re-applying
-- 0006 and running ops/backfill_successors.py --apply restores it from git.
-- Code from before #380 item 3 never writes 'git_merge'; code from #380 on
-- skips it with a WARNING while this constraint is narrow.

-- Lock guard, same as the forward migration: fail fast instead of queueing
-- every query on the table behind an idle-in-transaction lock holder.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

UPDATE module_presence
   SET successor_names = NULL, successor_source = NULL, updated_at = now()
 WHERE successor_source = 'git_merge';

ALTER TABLE module_presence
    DROP CONSTRAINT IF EXISTS ck_module_presence_successor_source,
    ADD CONSTRAINT ck_module_presence_successor_source
        CHECK (successor_source IN ('git_rename', 'old_technical_name'));
