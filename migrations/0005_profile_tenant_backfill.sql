-- migrations/0005_profile_tenant_backfill.sql
-- Profile tenant backfill: no profile may be more visible than its parent.
--
-- Why: profiles were created without a tenant (the create form and the CLI
-- had no tenant field, RepoStore.add_profile never wrote tenant_id), and
-- tenant_id NULL means "shared with every API key". Two Viindoo profiles were
-- left shared that way (observed 2026-10-20: viindoo_internal_19, whose parent
-- standard_viindoo_19 is private to Viindoo; and standard_viindoo_20, under the
-- shared odoo_20). The owner confirmed on 2026-10-20 that both belong to
-- Viindoo Technology JSC. The code now refuses this shape (RepoStore
-- check_child_visibility); this migration repairs the rows already there.
--
-- Data-driven, no hardcoded ids or tenant numbers. Rules, in order:
--
--   1. nearest_private_ancestor: a shared profile whose ancestor chain holds a
--      private profile gets the tenant of the NEAREST private ancestor (the
--      only value the visibility invariant allows).
--   2. owner_decision: ONE named row. standard_viindoo_20 sits under the
--      legitimately shared odoo_20, so no structural rule can tell it belongs
--      to Viindoo. The owner decided on 2026-10-20 ("both belong to Viindoo;
--      the UI had no tenant field"). The row is changed only when a profile
--      named exactly 'standard_viindoo_20' is shared AND every other
--      standard_viindoo_<N> profile belongs to ONE single tenant T (on prod
--      T = "Viindoo Technology JSC"); it then gets T. On any other deployment
--      (no such profile, or the family is shared / mixed) nothing happens.
--      There is deliberately NO generic "versioned family" rule: guessing a
--      tenant from a profile NAME would hand one tenant another customer's
--      profile (and portal write on it).
--   3. nearest_private_ancestor again, for descendants of the rule-2 row.
--
--   4. repo_follows_profile: repos.tenant_id NULL under a private profile gets
--      the profile's tenant (repos added while the profile was still shared;
--      the tenant assignment never cascaded to repos before).
--
-- Every change is recorded in admin_audit_log (actor 'migration:0005', action
-- 'profile.tenant_backfill', detail = table, row_id, old/new tenant, rule) -
-- visible on the admin audit-log page - so the rollback restores exactly these
-- rows and nothing else. No new table: the schema is unchanged.
--
-- IDEMPOTENT: each rule only touches rows still in the broken shape, so a
-- re-run (or psql -f) changes nothing and logs nothing.
--
-- RLS / grants: data-only on profiles and repos; no policy or GRANT changes.
-- osm_reader keeps SELECT on profiles/repos (ops/rls_create_osm_reader.sql);
-- admin_audit_log is owner-only as before. The MCP read side sees the change
-- within ~5 s (session.scope_epoch polls the profile fingerprint) or on restart.
--
-- Verify after apply: python -m src.manager profile-visibility-audit (exit 0,
-- no shared_child_of_private / cross_tenant_chain / repo_tenant_mismatch row),
-- and the logged changes:
--   SELECT target, detail FROM admin_audit_log
--    WHERE action = 'profile.tenant_backfill' ORDER BY id;
--
-- Rollback: 0005_profile_tenant_backfill.rollback.sql restores the logged
-- rows to their old (shared) tenant - which RE-OPENS the exposure. Take the
-- ADR-0018 backup first and prefer fixing forward.

-- Rule 1: nearest private ancestor.
WITH RECURSIVE chain AS (
    SELECT p.id AS leaf_id, p.parent_profile_id AS anc_id, 1 AS depth
    FROM profiles p
    WHERE p.parent_profile_id IS NOT NULL AND p.tenant_id IS NULL
    UNION ALL
    SELECT c.leaf_id, a.parent_profile_id, c.depth + 1
    FROM chain c JOIN profiles a ON a.id = c.anc_id
    WHERE a.parent_profile_id IS NOT NULL AND c.depth < 64
),
nearest AS (
    SELECT DISTINCT ON (c.leaf_id) c.leaf_id, a.tenant_id AS anc_tenant
    FROM chain c JOIN profiles a ON a.id = c.anc_id
    WHERE a.tenant_id IS NOT NULL
    ORDER BY c.leaf_id, c.depth
),
upd AS (
    UPDATE profiles p SET tenant_id = n.anc_tenant
    FROM nearest n
    WHERE p.id = n.leaf_id AND p.tenant_id IS NULL
    RETURNING p.id, n.anc_tenant
)
INSERT INTO admin_audit_log (actor, action, target, success, detail)
SELECT 'migration:0005', 'profile.tenant_backfill', 'profiles:' || id, TRUE,
       jsonb_build_object('table', 'profiles', 'row_id', id, 'old_tenant_id', NULL,
                          'new_tenant_id', anc_tenant, 'rule', 'nearest_private_ancestor')
FROM upd;

-- Rule 2: owner decision 2026-10-20 for standard_viindoo_20 (see header).
WITH family AS (
    SELECT count(*)                                AS n,
           count(*) FILTER (WHERE tenant_id IS NULL) AS shared_n,
           count(DISTINCT tenant_id)               AS tenant_n,
           min(tenant_id)                          AS tenant_id
    FROM profiles
    WHERE name ~ '^standard_viindoo_[0-9]+$' AND name <> 'standard_viindoo_20'
),
upd AS (
    UPDATE profiles p SET tenant_id = f.tenant_id
    FROM family f
    WHERE p.name = 'standard_viindoo_20'
      AND p.tenant_id IS NULL
      AND f.n >= 1 AND f.shared_n = 0 AND f.tenant_n = 1
    RETURNING p.id, f.tenant_id AS new_tenant
)
INSERT INTO admin_audit_log (actor, action, target, success, detail)
SELECT 'migration:0005', 'profile.tenant_backfill', 'profiles:' || id, TRUE,
       jsonb_build_object('table', 'profiles', 'row_id', id, 'old_tenant_id', NULL,
                          'new_tenant_id', new_tenant, 'rule', 'owner_decision')
FROM upd;

-- Rule 3: nearest private ancestor again (descendants of the rule-2 row).
WITH RECURSIVE chain AS (
    SELECT p.id AS leaf_id, p.parent_profile_id AS anc_id, 1 AS depth
    FROM profiles p
    WHERE p.parent_profile_id IS NOT NULL AND p.tenant_id IS NULL
    UNION ALL
    SELECT c.leaf_id, a.parent_profile_id, c.depth + 1
    FROM chain c JOIN profiles a ON a.id = c.anc_id
    WHERE a.parent_profile_id IS NOT NULL AND c.depth < 64
),
nearest AS (
    SELECT DISTINCT ON (c.leaf_id) c.leaf_id, a.tenant_id AS anc_tenant
    FROM chain c JOIN profiles a ON a.id = c.anc_id
    WHERE a.tenant_id IS NOT NULL
    ORDER BY c.leaf_id, c.depth
),
upd AS (
    UPDATE profiles p SET tenant_id = n.anc_tenant
    FROM nearest n
    WHERE p.id = n.leaf_id AND p.tenant_id IS NULL
    RETURNING p.id, n.anc_tenant
)
INSERT INTO admin_audit_log (actor, action, target, success, detail)
SELECT 'migration:0005', 'profile.tenant_backfill', 'profiles:' || id, TRUE,
       jsonb_build_object('table', 'profiles', 'row_id', id, 'old_tenant_id', NULL,
                          'new_tenant_id', anc_tenant, 'rule', 'nearest_private_ancestor')
FROM upd;

-- Rule 4: a repo's tenant follows its private profile.
WITH upd AS (
    UPDATE repos r SET tenant_id = p.tenant_id
    FROM profiles p
    WHERE p.id = r.profile_id AND r.tenant_id IS NULL AND p.tenant_id IS NOT NULL
    RETURNING r.id, p.tenant_id AS new_tenant
)
INSERT INTO admin_audit_log (actor, action, target, success, detail)
SELECT 'migration:0005', 'profile.tenant_backfill', 'repos:' || id, TRUE,
       jsonb_build_object('table', 'repos', 'row_id', id, 'old_tenant_id', NULL,
                          'new_tenant_id', new_tenant, 'rule', 'repo_follows_profile')
FROM upd;

-- Postcondition: the visibility invariant holds for every profile - no shared
-- profile keeps a private ancestor (shared_child_of_private) and no private
-- profile sits under ANOTHER tenant's nearest private ancestor
-- (cross_tenant_chain). Same shape as RepoStore._VISIBILITY_AUDIT_SQL. Rules 1
-- and 3 make the first true by construction; the second is never repaired by a
-- guess - fail the migration loudly so an operator decides (the deploy plan
-- runs profile-visibility-audit before migrate to see it coming).
DO $$
DECLARE
    _bad TEXT;
BEGIN
    WITH RECURSIVE c AS (
        SELECT id AS leaf, parent_profile_id AS anc, 1 AS depth FROM profiles
        WHERE parent_profile_id IS NOT NULL
        UNION ALL
        SELECT c.leaf, p.parent_profile_id, c.depth + 1
        FROM c JOIN profiles p ON p.id = c.anc
        WHERE p.parent_profile_id IS NOT NULL AND c.depth < 64
    ),
    nearest AS (
        SELECT DISTINCT ON (c.leaf) c.leaf, a.name AS anc_name, a.tenant_id AS anc_tenant
        FROM c JOIN profiles a ON a.id = c.anc
        WHERE a.tenant_id IS NOT NULL
        ORDER BY c.leaf, c.depth
    )
    SELECT string_agg(
               CASE WHEN l.tenant_id IS NULL
                    THEN 'shared_child_of_private ' || l.name || ' (under ' || n.anc_name || ')'
                    ELSE 'cross_tenant_chain ' || l.name || ' (under ' || n.anc_name || ')'
               END, ', ' ORDER BY l.name) INTO _bad
    FROM nearest n JOIN profiles l ON l.id = n.leaf
    WHERE l.tenant_id IS DISTINCT FROM n.anc_tenant;
    IF _bad IS NOT NULL THEN
        RAISE EXCEPTION 'profile visibility invariant still broken: %', _bad;
    END IF;
END $$;
