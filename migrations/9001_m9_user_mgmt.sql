-- migrations/9001_m9_user_mgmt.sql
-- M9 W-UM: User management tables — expanded webui_users + sessions + email_verifications + audit log.
-- All statements use ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
-- for safe idempotent execution on both fresh and already-partially-migrated databases.

-- ----------------------------------------------------------------
-- Expand webui_users: add M9 columns idempotently.
-- The original M7 table has: username (PK), password_hash, created_at.
-- ----------------------------------------------------------------
ALTER TABLE webui_users
    ADD COLUMN IF NOT EXISTS id SERIAL,
    ADD COLUMN IF NOT EXISTS email TEXT,
    ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- id unique index (only needed if id is not already a PK/unique constraint)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'webui_users'::regclass
          AND conname = 'ux_webui_users_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'webui_users' AND indexname = 'ux_webui_users_id'
    ) THEN
        CREATE UNIQUE INDEX ux_webui_users_id ON webui_users(id);
    END IF;
END $$;

-- ----------------------------------------------------------------
-- active_sessions: server-side session store for instant revoke (F7).
--
-- IMPORTANT MERGE NOTE (M9 integration):
--   The canonical schema is W-AC's m9_005_active_sessions.sql:
--     session_id TEXT PRIMARY KEY, user_id INT, expires_at, last_seen, ...
--   This file (W-UM, alphabetically first) now creates the canonical schema
--   so later migrations using IF NOT EXISTS become no-ops. The previous W-UM
--   design (session_id absent; id BIGSERIAL PK) was incompatible with the
--   src/web_ui/routes/login.py session lookup code.
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS active_sessions (
    session_id      TEXT        PRIMARY KEY,
    user_id         INTEGER     NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '8 hours'),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip_address      INET,
    user_agent      TEXT,
    mfa_verified_at TIMESTAMPTZ
);

-- Defensive backfill if the table pre-existed in a different shape.
ALTER TABLE active_sessions ADD COLUMN IF NOT EXISTS session_id      TEXT;
ALTER TABLE active_sessions ADD COLUMN IF NOT EXISTS user_id         INTEGER;
ALTER TABLE active_sessions ADD COLUMN IF NOT EXISTS expires_at      TIMESTAMPTZ;
ALTER TABLE active_sessions ADD COLUMN IF NOT EXISTS last_seen       TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE active_sessions ADD COLUMN IF NOT EXISTS ip_address      INET;
ALTER TABLE active_sessions ADD COLUMN IF NOT EXISTS user_agent      TEXT;
ALTER TABLE active_sessions ADD COLUMN IF NOT EXISTS mfa_verified_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_active_sessions_user_id ON active_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id        ON active_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires        ON active_sessions(expires_at);

-- Add FK to webui_users(id) with CASCADE delete.
-- The constraint may already exist if m9_005 ran later on a fresh install
-- (which would no-op the CREATE TABLE above) — guarded with pg_constraint check.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'active_sessions'::regclass
          AND contype = 'f'
          AND conname = 'active_sessions_user_id_fkey'
    ) THEN
        ALTER TABLE active_sessions
            ADD CONSTRAINT active_sessions_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES webui_users(id) ON DELETE CASCADE;
    END IF;
EXCEPTION WHEN others THEN
    -- If webui_users.id isn't UNIQUE yet (unlikely; ux_webui_users_id above
    -- creates it), skip silently — later migrations will add the constraint.
    NULL;
END $$;

-- ----------------------------------------------------------------
-- email_verifications: tokens for email verify AND password reset.
--
-- IMPORTANT MERGE NOTE (M9 integration):
--   Three M9 worktrees independently designed this table:
--     - W-UM (this file)     → token_hash TEXT (SHA-256 of raw token)
--     - W-SG (9001_signup_email_verify.sql) → token TEXT PRIMARY KEY (raw plaintext)
--     - W-AC (m9_006_email_verifications.sql) → token TEXT PRIMARY KEY (raw plaintext)
--   The orchestrator merged all three. To keep both auth_registry (which uses
--   token_hash) and signup.py (which uses token) functional, the table must
--   carry BOTH columns. This migration (alphabetically first) creates the
--   table with `token` as the canonical PK + `token_hash` as a secondary
--   indexed column. Later migrations using IF NOT EXISTS become no-ops.
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_verifications (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    purpose    TEXT NOT NULL DEFAULT 'email_verify'
               CHECK (purpose IN ('email_verify', 'password_reset')),
    token_hash TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Backfill columns if the table pre-existed with a different shape
-- (defensive — protects against partial-migration scenarios).
ALTER TABLE email_verifications ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE email_verifications ADD COLUMN IF NOT EXISTS purpose TEXT;
ALTER TABLE email_verifications ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE email_verifications ADD COLUMN IF NOT EXISTS used_at TIMESTAMPTZ;
ALTER TABLE email_verifications ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE email_verifications ADD COLUMN IF NOT EXISTS token_hash TEXT;
-- 'token' column is the PK — guaranteed present by the CREATE TABLE above.

CREATE INDEX IF NOT EXISTS idx_email_verif_user_id ON email_verifications(user_id);
CREATE INDEX IF NOT EXISTS idx_email_verif_token_hash ON email_verifications(token_hash);

-- ----------------------------------------------------------------
-- admin_audit_log: records all privileged actions.
--
-- IMPORTANT MERGE NOTE (M9 integration):
--   Two M9 worktrees designed this table:
--     - W-UM (this file)         → actor_id INTEGER + detail TEXT
--     - W-AC (m9_003_admin_audit_log.sql) → actor TEXT + detail JSONB + success
--   The W-AC schema is canonical (matches the log_audit() API in
--   src/db/auth_registry.py and the audit decorator in W-AL). We adopt it
--   here so later migrations using IF NOT EXISTS become no-ops. The legacy
--   actor_id/target_id columns are kept additionally for W-UM call sites
--   that pass integer IDs directly.
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id         BIGSERIAL    PRIMARY KEY,
    actor      TEXT         NOT NULL,
    action     TEXT         NOT NULL,
    target     TEXT,
    success    BOOLEAN      NOT NULL DEFAULT TRUE,
    detail     JSONB,
    -- Legacy W-UM columns kept for compatibility:
    actor_id   INTEGER,
    target_id  INTEGER,
    detail_text TEXT,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Defensive backfill: if the table pre-existed in a different shape, add
-- the canonical columns. All NOT NULL columns are left nullable here on
-- ALTER (you cannot ALTER ... SET NOT NULL on an existing column with NULLs).
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS actor      TEXT;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS action     TEXT;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS target     TEXT;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS success    BOOLEAN DEFAULT TRUE;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS detail     JSONB;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS actor_id   INTEGER;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS target_id  INTEGER;
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS detail_text TEXT;

CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log(created_at DESC);
