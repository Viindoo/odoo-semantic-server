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
--     3. Backfills the already-minted NULL-tenant keys by email domain. A NULL
--        email (webui_users.email is nullable) is treated as non-viindoo:
--          - @viindoo.com non-admin keys  → bound to the Viindoo tenant.
--          - other non-admin keys (the already-exposed external/gmail keys,
--            incl. NULL-email keys) split by plan:
--              · free plan        → DEACTIVATED (active=false; tenant left NULL).
--                                   Incident-response for free/abandoned external
--                                   keys (all current rows in prod are free).
--              · non-free (paid / unlimited / granted) → RE-SCOPED to the
--                                   'public' tenant (tenant_id = public, still
--                                   active). Pre-branch billing provisioning could
--                                   mint paid keys with tenant_id NULL; this keeps
--                                   any paying/granted customer working (Odoo-only)
--                                   instead of deactivating them — and they are
--                                   never left active+NULL-tenant.
--          - admin keys (is_admin=true) + system/CLI keys (user_id IS NULL)
--            → untouched (stay NULL = unrestricted, by design).
--        Net: no non-admin, non-viindoo key is ever left active+NULL-tenant.
--
-- No new tables created → no osm_reader GRANT changes needed (RLS unaffected;
-- the choke point reads profiles + api_keys, both already granted).
--
-- Idempotent — safe to re-run:
--   - tenants INSERT uses ON CONFLICT (name) DO NOTHING.
--   - the profile UPDATE only touches rows still in the shared (NULL) set, so a
--     second run finds them already assigned and is a no-op.
--   - the key backfill is scoped to NULL-tenant rows: the viindoo-domain UPDATE
--     and the non-free re-scope UPDATE both leave the row out of the NULL set
--     after the first run; the free deactivate UPDATE is scoped to still-active
--     rows. All three converge after the first run.
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
         WHERE table_schema = current_schema()
           AND table_name = 'api_keys' AND column_name = 'user_id'
    ) THEN
        -- 5a. @viindoo.com non-admin keys → bind to the Viindoo tenant.
        --     A NULL email is NOT a viindoo email, so it is excluded here (it
        --     falls through to the non-viindoo branches 5b/5c below). The IS NOT
        --     NULL guard makes the intent explicit even though a NULL email
        --     would already fail the LIKE.
        UPDATE api_keys k
           SET tenant_id = v_viindoo
          FROM webui_users u
         WHERE k.user_id = u.id
           AND k.tenant_id IS NULL
           AND NOT COALESCE(u.is_admin, false)
           AND u.email IS NOT NULL
           AND lower(u.email) LIKE '%@viindoo.com';

        -- Resolve the 'free' plan id once. COALESCE(..., -1) guarantees a
        -- non-NULL sentinel so that, if the 'free' slug were ever absent, the
        -- equality predicate in 5b/5c is FALSE (not NULL) for every key:
        -- branch 5b deactivates nothing and ALL non-viindoo keys re-scope to
        -- public (branch 5c) — still fail-closed (never left active+NULL).
        -- In prod the 'free' slug exists, so this resolves to the real id.

        -- 5b. Non-viindoo (incl. NULL-email) non-admin NULL-tenant keys on the
        --     FREE plan → DEACTIVATE (active=false). Tenant left NULL; the key
        --     can no longer authenticate, so its old unrestricted reach is
        --     closed. Incident-response for free/abandoned external keys.
        UPDATE api_keys k
           SET active = false
          FROM webui_users u
         WHERE k.user_id = u.id
           AND k.tenant_id IS NULL
           AND NOT COALESCE(u.is_admin, false)
           AND (u.email IS NULL OR lower(u.email) NOT LIKE '%@viindoo.com')
           AND k.plan_id = COALESCE(
                   (SELECT id FROM plans WHERE slug = 'free'), -1);

        -- 5c. Non-viindoo (incl. NULL-email) non-admin NULL-tenant keys on a
        --     NON-free plan (paid / unlimited / granted) → RE-SCOPE to the
        --     'public' tenant (still active). Pre-branch billing could mint paid
        --     keys with tenant_id NULL; this keeps paying/granted customers
        --     working (Odoo-only) rather than deactivating them, and never
        --     leaves them active+NULL-tenant.
        UPDATE api_keys k
           SET tenant_id = v_public
          FROM webui_users u
         WHERE k.user_id = u.id
           AND k.tenant_id IS NULL
           AND NOT COALESCE(u.is_admin, false)
           AND (u.email IS NULL OR lower(u.email) NOT LIKE '%@viindoo.com')
           AND k.plan_id IS DISTINCT FROM COALESCE(
                   (SELECT id FROM plans WHERE slug = 'free'), -1);

        -- 5d. admin keys (is_admin=true) + system/CLI keys (user_id IS NULL) are
        --     NOT touched by any UPDATE above (admin filtered out by NOT
        --     is_admin; CLI keys have no matching webui_users row to join).
        --     They stay tenant_id IS NULL = unrestricted, by design.
    END IF;
END
$$;
