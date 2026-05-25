-- migrations/m13_005_tenant_members.sql
-- W1 (UI plan) — Tenant RBAC web-UI write-side foundation. Three concerns folded
-- into ONE migration (all additive, idempotent, backward-compatible):
--
--   PART A — tenant_members(user_id, tenant_id, role, created_at): link
--            webui_users <-> tenants for the (b) multi-tenant-per-user model.
--   PART B — webui_users.password_hash DROP NOT NULL (issue #176, Option A):
--            repo schema drifted from prod; OAuth-only users already INSERT NULL.
--   PART C — profiles.name CHECK (name NOT LIKE '%,%') GUC-delimiter guard
--            (ADR-0034 A4, deferred-but-cheap): a comma in a profile name would
--            corrupt the RLS read-side GUC `string_to_array(allowed_profiles,',')`.
--
-- Idempotency: every statement uses IF NOT EXISTS / DO $$...END$$ guards. Safe to
-- re-run on a production-shape database (yoyo applies once; guards protect manual re-run).

-- ===========================================================================
-- PART A — tenant_members
-- ===========================================================================
CREATE TABLE IF NOT EXISTS tenant_members (
    user_id    INTEGER     NOT NULL
                 REFERENCES webui_users(id) ON DELETE CASCADE,
    tenant_id  INTEGER     NOT NULL
                 REFERENCES tenants(id)    ON DELETE CASCADE,
    role       TEXT        NOT NULL DEFAULT 'member'
                 CHECK (role IN ('member', 'tenant_admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, tenant_id)
);

-- Reverse-direction lookup index. The PK (user_id, tenant_id) already serves
-- "all tenants for a user" (resolve_tenant_scope_web); this index serves
-- "all members of a tenant" (admin tenant detail page).
CREATE INDEX IF NOT EXISTS idx_tenant_members_tenant ON tenant_members (tenant_id);

-- ===========================================================================
-- PART B — webui_users.password_hash DROP NOT NULL  (issue #176, Option A)
-- ===========================================================================
-- ALTER COLUMN DROP NOT NULL is a no-op if the column is already nullable, so
-- this is naturally idempotent. Wrapped in a guard only to skip cleanly if the
-- table somehow lacks the column (defensive — it always exists per 9000).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'webui_users' AND column_name = 'password_hash'
    ) THEN
        ALTER TABLE webui_users ALTER COLUMN password_hash DROP NOT NULL;
    END IF;
END $$;

-- ===========================================================================
-- PART C — profiles.name GUC-delimiter guard  (ADR-0034 A4)
-- ===========================================================================
-- A comma in profiles.name would corrupt the RLS read-side GUC parsing
-- `profile_name = ANY(string_to_array(current_setting('app.allowed_profiles'),','))`.
-- ADD CONSTRAINT has no IF NOT EXISTS in PG16 -> guard with pg_constraint check.
-- NOTE: this validates existing rows; if any current profile name already
-- contains ',', the ALTER will FAIL. Impl must run the pre-check below and
-- surface a clear error (see ADR-0038 data-migration safety).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'profiles_name_no_comma'
           AND conrelid = 'profiles'::regclass
    ) THEN
        ALTER TABLE profiles
            ADD CONSTRAINT profiles_name_no_comma
            CHECK (name NOT LIKE '%,%');
    END IF;
END $$;
