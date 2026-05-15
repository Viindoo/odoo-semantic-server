-- migrations/m9_001_oauth_columns.sql
-- M9 Auth Wow: extend webui_users for multi-user admin, OAuth, and email verification.
--
-- Findings addressed:
--   F4  — No is_admin/active/email columns: every authenticated user = full admin.
--   F17 — webui_users.email column missing in migration but required by signup flow.
--   F18 — webui_users.created_at already exists — use ADD COLUMN IF NOT EXISTS.
--   F19 — create-webui-user needs --admin flag; bootstrap deadlock with is_admin DEFAULT FALSE.
--
-- Design notes:
--   * username remains PRIMARY KEY for backward compatibility with existing rows
--     and application code that references it directly.
--   * A new `id SERIAL UNIQUE NOT NULL` column provides a stable integer FK target
--     for tables added in later M9 migrations (api_keys.user_id, active_sessions,
--     email_verifications, totp_secrets).  Both keys coexist; downstream FKs
--     reference `id` rather than `username` to avoid cascading renames.
--   * created_at: guarded with IF NOT EXISTS because 9000_webui_users.sql already
--     creates this column.  Adding it again without the guard would fail on fresh
--     installs where 9000 has already run.

-- Stable integer PK for FK targets.
-- SERIAL generates a sequence; UNIQUE + NOT NULL give all PK semantics without
-- disrupting the existing username PRIMARY KEY constraint.
ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS id SERIAL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE constraint_name = 'webui_users_id_unique'
           AND table_name = 'webui_users'
    ) THEN
        ALTER TABLE webui_users ADD CONSTRAINT webui_users_id_unique UNIQUE (id);
    END IF;
END $$;

-- OAuth columns (NULL = password-based login).
ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS oauth_provider TEXT;
ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS oauth_id TEXT;

-- Email (UNIQUE across users; nullable for legacy rows created before M9).
ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS email VARCHAR(255);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE constraint_name = 'webui_users_email_unique'
           AND table_name = 'webui_users'
    ) THEN
        ALTER TABLE webui_users ADD CONSTRAINT webui_users_email_unique UNIQUE (email);
    END IF;
END $$;

ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;

-- Role & status flags.
ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'admin'
    CHECK (role IN ('admin', 'viewer'));

-- created_at is guarded because 9000_webui_users.sql already adds it.
ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
