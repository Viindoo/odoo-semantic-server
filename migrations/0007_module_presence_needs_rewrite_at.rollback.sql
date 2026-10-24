-- migrations/0007_module_presence_needs_rewrite_at.rollback.sql
-- Rollback of 0007_module_presence_needs_rewrite_at.sql. Code from before this
-- migration never reads either object; code from it on falls back to the clear
-- by name and records no pending entry while they are missing
-- (ModulePresenceStore._rewrite_stamps_ok, one WARNING), so this rollback is
-- safe with either. Lost: the timing of the current flags (the next run clears
-- them by name again) and the pending entries (a module that had no ledger
-- row when another repo walked may keep stale edges until it is re-written).

-- Lock guard, same as the forward migration: fail fast instead of queueing
-- every query on the table behind an idle-in-transaction lock holder.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DROP TABLE IF EXISTS module_presence_pending_rewrite;
ALTER TABLE module_presence DROP COLUMN IF EXISTS needs_rewrite_at;
