-- migrations/0005_profile_tenant_backfill.rollback.sql
-- Rollback of 0005_profile_tenant_backfill.sql: restore exactly the rows the
-- migration logged in admin_audit_log (action 'profile.tenant_backfill') to
-- their old tenant (NULL = shared with every API key). It is rule-agnostic: rows
-- of every rule (nearest_private_ancestor, owner_decision, repo_follows_profile)
-- are restored alike, so it needs no change when a rule changes.
--
-- WARNING: this RE-OPENS the exposure the migration closed - the restored
-- profiles become readable by every key again, and the visibility audit
-- (python -m src.manager profile-visibility-audit) reports them. Take the
-- ADR-0018 backup first and prefer fixing forward. A row changed again after
-- the migration (its tenant no longer equals the logged new_tenant_id) is left
-- alone. The audit rows are kept (history); one 'profile.tenant_backfill.rollback'
-- row records the rollback.

UPDATE repos r SET tenant_id = (l.detail->>'old_tenant_id')::integer
FROM admin_audit_log l
WHERE l.action = 'profile.tenant_backfill' AND l.actor = 'migration:0005'
  AND l.detail->>'table' = 'repos'
  AND r.id = (l.detail->>'row_id')::integer
  AND r.tenant_id IS NOT DISTINCT FROM (l.detail->>'new_tenant_id')::integer;

UPDATE profiles p SET tenant_id = (l.detail->>'old_tenant_id')::integer
FROM admin_audit_log l
WHERE l.action = 'profile.tenant_backfill' AND l.actor = 'migration:0005'
  AND l.detail->>'table' = 'profiles'
  AND p.id = (l.detail->>'row_id')::integer
  AND p.tenant_id IS NOT DISTINCT FROM (l.detail->>'new_tenant_id')::integer;

INSERT INTO admin_audit_log (actor, action, target, success, detail)
VALUES ('migration:0005', 'profile.tenant_backfill.rollback', NULL, TRUE, '{}'::jsonb);
