-- migrations/m13_019_public_tenant_isolation.sql
-- Close the free-signup tenant-isolation hole (SECURITY).
--
-- Context (ADR-0034):
--   Free-signup API keys were minted with api_keys.tenant_id = NULL, which is the
--   "admin / unrestricted" sentinel in the ADR-0034 choke point. A NULL-tenant key
--   bypasses tenant scoping entirely and can therefore read EVERY profile —
--   including the private 'standard_viindoo_*' (×12) and 'viindoo_internal_*' (×2)
--   profiles that were never meant to be public. The choke point itself is correct;
--   the hole is mint-time scope (the keys are bound to the unrestricted sentinel).
--
--   Decided policy:
--     - Free signup may read ONLY Odoo core/CE/EE: the 'odoo_*' base profiles
--       (tenant_id IS NULL = globally shared) plus the global spec data.
--     - 'standard_viindoo_*' and 'viindoo_internal_*' become restricted: they move
--       OUT of the shared (NULL) set and INTO the Viindoo tenant.
--     - A new 'public' tenant owns no profiles → its keys see only the shared
--       'odoo_*' base set (deny-everything-else, see-shared-base).
--
--   This migration:
--     1. Resolves (or fail-closed creates) the Viindoo tenant + a 'public' tenant.
--     2. Moves the 14 viindoo profiles from the shared (NULL) set into the
--        Viindoo tenant. 'odoo_*' base profiles are NEVER matched (LIKE-pattern
--        guard) and STAY tenant_id IS NULL = shared.
--     3. Backfills the already-minted NULL-tenant keys by email domain:
--          - @viindoo.com non-admin keys  → bound to the Viindoo tenant.
--          - other non-admin keys (the already-exposed external/gmail keys)
--            → DEACTIVATED (active=false; tenant left NULL).
--          - admin keys (user_id=1) + system/CLI keys (user_id IS NULL)
--            → untouched (stay NULL = unrestricted, by design).
--
-- No new tables created → no osm_reader GRANT changes needed (RLS unaffected;
-- the choke point reads profiles + api_keys, both already granted).
--
-- Idempotent — safe to re-run:
--   - tenants INSERT uses ON CONFLICT (name) DO NOTHING.
--   - the profile UPDATE only touches rows still in the shared (NULL) set, so a
--     second run finds them already assigned and is a no-op.
--   - the key backfill is scoped to NULL-tenant rows (the domain UPDATE) and to
--     still-active non-viindoo non-admin keys (the deactivate UPDATE); both
--     converge after the first run.
--
-- Fail-closed: if either tenant id cannot be resolved, RAISE EXCEPTION rather
-- than leave the isolation half-applied.

DO $$
DECLARE
    v_viindoo  INTEGER;
    v_public   INTEGER;
BEGIN
    -- ----- 1. Resolve the Viindoo tenant (create fail-closed if absent) -----
    SELECT id INTO v_viindoo FROM tenants WHERE name = 'Viindoo Technology JSC';
    IF v_viindoo IS NULL THEN
        INSERT INTO tenants (name) VALUES ('Viindoo Technology JSC')
            ON CONFLICT (name) DO NOTHING;
        SELECT id INTO v_viindoo FROM tenants WHERE name = 'Viindoo Technology JSC';
    END IF;

    -- ----- 2. Create the 'public' tenant (Odoo-only free-signup scope) -----
    INSERT INTO tenants (name) VALUES ('public')
        ON CONFLICT (name) DO NOTHING;
    SELECT id INTO v_public FROM tenants WHERE name = 'public';

    -- ----- 3. Fail-closed guard -------------------------------------------
    IF v_viindoo IS NULL OR v_public IS NULL THEN
        RAISE EXCEPTION
            'm13_019: could not resolve required tenants '
            '(viindoo=%, public=%) — refusing to half-apply isolation.',
            v_viindoo, v_public;
    END IF;

    -- ----- 4. Move viindoo profiles out of the shared (NULL) set -----------
    -- Only profiles still in the shared set are moved. 'odoo_*' base profiles
    -- are never matched by these LIKE patterns and therefore STAY NULL = shared.
    UPDATE profiles
       SET tenant_id = v_viindoo
     WHERE tenant_id IS NULL
       AND (
            name LIKE 'standard\_viindoo\_%' ESCAPE '\'
         OR name LIKE 'viindoo\_internal\_%' ESCAPE '\'
       );

    -- ----- 5. Backfill already-minted NULL-tenant keys by email domain -----
    -- Guarded on api_keys.user_id existence. yoyo applies migrations in
    -- id-string order, which places this file (m13_019) BEFORE m9_002 (which
    -- adds api_keys.user_id) on a fresh install. On a fresh install there are
    -- no keys yet, so the backfill is a no-op anyway; we skip the block to
    -- avoid referencing a not-yet-existing column. On an already-migrated DB
    -- (prod) the column exists and the backfill runs. Re-running after m9_002
    -- has been applied (the prod case) executes the backfill idempotently.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'api_keys' AND column_name = 'user_id'
    ) THEN
        -- 5a. @viindoo.com non-admin keys → bind to the Viindoo tenant.
        UPDATE api_keys k
           SET tenant_id = v_viindoo
          FROM webui_users u
         WHERE k.user_id = u.id
           AND k.tenant_id IS NULL
           AND NOT COALESCE(u.is_admin, false)
           AND lower(u.email) LIKE '%@viindoo.com';

        -- 5b. Other non-admin NULL-tenant keys (already-exposed external/gmail)
        --     → DEACTIVATE (active=false). Tenant left NULL; the key can no
        --     longer authenticate, so its old unrestricted reach is closed.
        UPDATE api_keys k
           SET active = false
          FROM webui_users u
         WHERE k.user_id = u.id
           AND k.tenant_id IS NULL
           AND NOT COALESCE(u.is_admin, false)
           AND lower(u.email) NOT LIKE '%@viindoo.com';

        -- 5c. admin keys (user_id=1) + system/CLI keys (user_id IS NULL) are
        --     NOT touched by either UPDATE above (admin filtered out by NOT
        --     is_admin; CLI keys have no matching webui_users row to join).
        --     They stay tenant_id IS NULL = unrestricted, by design.
    END IF;
END
$$;
