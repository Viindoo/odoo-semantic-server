-- migrations/0007_module_presence_needs_rewrite_at.sql
-- When a needs_rewrite flag was set, and the flags a module had no row for yet
-- (ADR-0007 W14 note 3, cross-repo dependency propagation).
--
-- module_presence.needs_rewrite_at: the PostgreSQL clock (clock_timestamp())
-- at which needs_rewrite was last set TRUE, by ModulePresenceStore.
-- mark_needs_rewrite / mark_dependents_needs_rewrite (every writer of the flag:
-- cross-repo propagation, the incomplete-embedding and entity-prune retries,
-- reembed-stubs, the reconcile's owner hand-overs and prune rewrites). An index
-- run records the clock when it reads its flags (needs_rewrite_snapshot, under
-- the version ledger lock that every flag writer also takes) and its ledger
-- commit clears only the flags set at or before that read: a flag another
-- repo's propagation set while the run was in progress survives, where the
-- clear by name used to drop it. Meaningful only while needs_rewrite is TRUE.
--
-- module_presence_pending_rewrite: a flag for a module of repo S that has no
-- module_presence row yet when another repo's propagation walks (S wrote the
-- module to the graph but has not committed its ledger: a run in progress, or
-- the module is new). Keyed by (odoo_version, repo basename, name) - the walk
-- knows the Module node's repo basename, not a repo id. S's ledger commit
-- applies the entries for the names it commits present (needs_rewrite TRUE
-- when the flag was set after S read its flags, or S did not re-write the
-- module this run) and deletes them; entries nobody consumes are purged after
-- 30 days. No ledger state is added: a pending entry never claims that S
-- ships the module, so the retirement gates are untouched (ADR-0056).
--
-- Backfill: a row already flagged gets needs_rewrite_at = updated_at (the
-- flag set it, or something later did; either way at or before any read that
-- follows the migration, so the next run clears it as before).
--
-- Grants / RLS: unchanged for module_presence (osm_reader keeps table-level
-- SELECT under module_presence_tenant, ops/rls_create_osm_reader.sql; the new
-- column is covered). module_presence_pending_rewrite is read and written by
-- the indexer only (owner DSN); osm_reader gets no grant on it.
--
-- Until this migration is applied the indexer works as before (clear by name,
-- no pending entries) and logs one WARNING per store (ModulePresenceStore.
-- _rewrite_stamps_ok).
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS, CREATE TABLE / INDEX IF NOT EXISTS,
-- the backfill touches only flagged rows without a stamp.
--
-- Rollback: 0007_module_presence_needs_rewrite_at.rollback.sql.

-- Lock guard: ALTER TABLE needs ACCESS EXCLUSIVE. Behind an idle-in-transaction
-- connection that holds even ACCESS SHARE on the table it would wait forever and
-- queue every later query on the table behind it (the Web UI is live while
-- migrate runs). SET LOCAL scopes both limits to this migration's transaction
-- (yoyo applies each migration in one transaction): the migration fails fast
-- with "canceling statement due to lock timeout" and nothing applied - re-run
-- python -m src.db.migrate once the holder is gone.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE module_presence ADD COLUMN IF NOT EXISTS needs_rewrite_at TIMESTAMPTZ;

UPDATE module_presence
   SET needs_rewrite_at = updated_at
 WHERE needs_rewrite AND needs_rewrite_at IS NULL;

CREATE TABLE IF NOT EXISTS module_presence_pending_rewrite (
    odoo_version  TEXT        NOT NULL,
    repo_basename TEXT        NOT NULL,
    name          TEXT        NOT NULL,
    flagged_at    TIMESTAMPTZ NOT NULL,
    CONSTRAINT module_presence_pending_rewrite_pkey
        PRIMARY KEY (odoo_version, repo_basename, name)
);
CREATE INDEX IF NOT EXISTS ix_module_presence_pending_rewrite_flagged
    ON module_presence_pending_rewrite (flagged_at);
