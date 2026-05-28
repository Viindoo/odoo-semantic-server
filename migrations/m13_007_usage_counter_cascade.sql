-- migrations/m13_007_usage_counter_cascade.sql
-- M10B P0 follow-up — convert usage_counter.api_key_id FK to ON DELETE CASCADE.
--
-- Why:
--   m13_006 declared `usage_counter.api_key_id INTEGER NOT NULL REFERENCES
--   api_keys(id)` WITHOUT `ON DELETE CASCADE`. As a result:
--     1. `DELETE FROM api_keys` on a key with rows in usage_counter raises a
--        foreign-key violation in any DB that *did* enforce the constraint.
--     2. On older DBs that received an early variant of m13_006 where
--        usage_counter was created via `CREATE TABLE IF NOT EXISTS` BEFORE the
--        FK was added, the constraint never got attached at all → orphan
--        usage_counter rows survive `DELETE FROM api_keys` and bind to the
--        next api_keys row that reuses the same SERIAL id → cross-test 429
--        contamination (see PR #200 CI iter 3 failure on
--        test_tenant_deploy_key).
--
-- Fix: drop whichever FK constraint name currently references api_keys, then
-- re-create it explicitly named `usage_counter_api_key_id_fkey` with the
-- CASCADE action. The DO block handles both scenarios:
--   (a) constraint present (declared inline) → drop + re-add with CASCADE.
--   (b) constraint absent (dropped by an older drift) → skip drop, just add.
--
-- Idempotent: re-running checks for the constraint by definition
-- (pg_get_constraintdef ... LIKE '%REFERENCES api_keys%') so a CASCADE-bearing
-- constraint added by a previous run is replaced in place with the same
-- CASCADE-bearing version (net-no-op for behaviour).

DO $$
DECLARE _fk_name TEXT;
BEGIN
    SELECT conname INTO _fk_name
      FROM pg_constraint
     WHERE conrelid = 'public.usage_counter'::regclass
       AND contype = 'f'
       AND pg_get_constraintdef(oid) LIKE '%REFERENCES api_keys%';
    IF _fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE usage_counter DROP CONSTRAINT %I', _fk_name);
    END IF;
END $$;

ALTER TABLE usage_counter
  ADD CONSTRAINT usage_counter_api_key_id_fkey
  FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE;
