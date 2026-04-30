-- Relax the mode<->inherit_id biconditional CHECK on `views`.
--
-- The view driver resolves `inherit_id` in a second pass after every view
-- row for a run is inserted (xmlid → id lookup is cross-schema). During the
-- first pass, extension rows carry `mode='extension'` with
-- `inherit_id=NULL` — the original strict biconditional
-- CHECK ((mode = 'extension') = (inherit_id IS NOT NULL))
-- rejected that state and blocked any fixture corpus containing extensions.
--
-- New invariant: primary views never carry an inherit_id; extensions *may*
-- temporarily have NULL inherit_id when the parent xmlid was not resolvable.
-- The driver emits a `view_inherit_unresolved` warning in that case, matching
-- the data-model/views.md tolerance for unresolved cross-module refs.
--
-- Idempotent: safe to re-run.

DO $$
DECLARE
    r record;
BEGIN
    -- Drop the auto-named biconditional CHECK created by 001_init. The
    -- generated name varies by PostgreSQL version (views_check, views_check1,
    -- ...), so match by definition text instead.
    FOR r IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'views'::regclass
           AND contype = 'c'
           AND pg_get_constraintdef(oid) ILIKE '%mode = ''extension''%inherit_id IS NOT NULL%'
    LOOP
        EXECUTE format('ALTER TABLE views DROP CONSTRAINT %I', r.conname);
    END LOOP;
END$$;

ALTER TABLE views DROP CONSTRAINT IF EXISTS views_mode_inherit_chk;
ALTER TABLE views
    ADD CONSTRAINT views_mode_inherit_chk
    CHECK (mode = 'extension' OR inherit_id IS NULL);
