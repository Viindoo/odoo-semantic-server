-- migrations/9001_signup_email_verify.sql
-- M9 W-SG: Public signup + email verification support (idempotent).
-- Extends webui_users with email + email_verified + is_admin + id columns.
-- Adds email_verifications table for token-based flows (verify + password reset).
-- NOTE: All ALTERs use IF NOT EXISTS for safe re-application on any base schema.

-- Extend webui_users with required columns (idempotent ALTERs)
ALTER TABLE webui_users ADD COLUMN IF NOT EXISTS id
    INTEGER DEFAULT nextval('webui_users_id_seq'::regclass);
ALTER TABLE webui_users ADD COLUMN IF NOT EXISTS email       VARCHAR(255);
ALTER TABLE webui_users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE webui_users ADD COLUMN IF NOT EXISTS is_admin    BOOLEAN NOT NULL DEFAULT FALSE;

-- Unique constraint on email (allow NULL for legacy rows without email)
CREATE UNIQUE INDEX IF NOT EXISTS ux_webui_users_email ON webui_users (email)
    WHERE email IS NOT NULL;

-- Sequence for webui_users.id (idempotent)
CREATE SEQUENCE IF NOT EXISTS webui_users_id_seq;

-- email_verifications: token store for email_verify + password_reset flows.
-- user_id is INTEGER FK to webui_users(id) (W-AC schema).
CREATE TABLE IF NOT EXISTS email_verifications (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    purpose    TEXT NOT NULL DEFAULT 'email_verify'
               CHECK (purpose IN ('email_verify', 'password_reset')),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_email_verif_token     ON email_verifications (token);
CREATE INDEX IF NOT EXISTS ix_email_verif_user      ON email_verifications (user_id);
CREATE INDEX IF NOT EXISTS ix_email_verif_created   ON email_verifications (created_at DESC);
