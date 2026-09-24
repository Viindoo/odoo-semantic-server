-- migrations/0003_module_presence.rollback.sql
-- Rollback of 0003_module_presence.sql. 0.18.x code never reads the ledger,
-- but its Web UI returns repo rows as SELECT r.* unredacted, so the new
-- repos.lifecycle_attention (it can name other tenants' repos) reaches
-- non-admins under 0.18: clear it (or run this rollback) before running 0.18
-- code on this database. Dropping the table discards the lifecycle history;
-- take the ADR-0018 backup first.

DROP POLICY IF EXISTS module_presence_tenant ON module_presence;
DROP TABLE IF EXISTS module_presence;

ALTER TABLE repos DROP COLUMN IF EXISTS lifecycle_attention_at;
ALTER TABLE repos DROP COLUMN IF EXISTS lifecycle_attention;
ALTER TABLE repos DROP COLUMN IF EXISTS presence_head_sha;
