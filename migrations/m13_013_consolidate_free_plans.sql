-- migrations/m13_013_consolidate_free_plans.sql
-- Consolidate free-grandfathered plan — repoint its api_keys to 'unlimited',
-- then delete the plan row.
--
-- Context (ADR-0039, ADR-0041):
--   m13_006 seeded 'free-grandfathered' (is_public=FALSE, 1000 calls/month, 60 rpm)
--   as a one-time deploy snapshot for pre-commercialization API keys. Those keys all
--   belong to admin (user_id=1) or are system/CLI keys (user_id=NULL). There are no
--   self-service signup users on this plan. Keeping the plan creates UX confusion:
--   admin UI shows two overlapping "free" options in plan dropdowns.
--
--   Resolution:
--     1. Repoint all api_keys on free-grandfathered → unlimited (ADR-0041 D5 SSOT).
--        Admin/CLI keys should have unlimited access, not a capped free tier.
--     2. DELETE the free-grandfathered plan row.
--     3. api_keys.plan_id column DEFAULT stays pointing at 'free' (id set by m13_006
--        step 6) — new self-service signups continue to land on the public free tier.
--
-- No new tables created → no osm_reader GRANT changes needed (RLS unaffected).
--
-- Idempotent — safe to re-run:
--   The DO block checks for the existence of 'free-grandfathered' before acting.
--   Re-running on a DB where it is already gone is a safe no-op.

DO $$
DECLARE
    _fg_id      INTEGER;
    _unlim_id   INTEGER;
BEGIN
    -- Resolve plan ids; if 'free-grandfathered' is absent the whole block is a no-op.
    SELECT id INTO _fg_id    FROM plans WHERE slug = 'free-grandfathered';
    SELECT id INTO _unlim_id FROM plans WHERE slug = 'unlimited';

    IF _fg_id IS NULL THEN
        -- Plan already removed — nothing to do.
        RETURN;
    END IF;

    IF _unlim_id IS NULL THEN
        RAISE EXCEPTION
            'Cannot consolidate: slug=''unlimited'' plan not found. '
            'Ensure m13_009_unlimited_plan_and_key_overrides.sql has been applied.';
    END IF;

    -- Repoint all api_keys on free-grandfathered → unlimited.
    UPDATE api_keys
       SET plan_id = _unlim_id
     WHERE plan_id = _fg_id;

    -- Delete the now-unreferenced plan (FK satisfied, no remaining references).
    DELETE FROM plans WHERE id = _fg_id;
END
$$;
