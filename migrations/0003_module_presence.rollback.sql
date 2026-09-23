-- migrations/0003_module_presence.rollback.sql
-- Rollback of 0003_module_presence.sql (hygiene only: 0.18.x code never reads
-- the ledger or the new repos columns, so leaving them in place is harmless).
-- Dropping the table discards the lifecycle history; take the ADR-0018 backup
-- first.

DROP POLICY IF EXISTS module_presence_tenant ON module_presence;
DROP TABLE IF EXISTS module_presence;

ALTER TABLE repos DROP COLUMN IF EXISTS lifecycle_attention_at;
ALTER TABLE repos DROP COLUMN IF EXISTS lifecycle_attention;
ALTER TABLE repos DROP COLUMN IF EXISTS presence_head_sha;
