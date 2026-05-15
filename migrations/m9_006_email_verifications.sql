-- migrations/m9_006_email_verifications.sql
-- M9 Auth Wow: create email_verifications table for email verify and password reset tokens.
--
-- Findings addressed:
--   F9 — Email verification token entropy not defined: must be secrets.token_urlsafe(32),
--        TTL ≤ 24 h, single-use (mark used_at on consume, reject if used_at IS NOT NULL).
--
-- Design notes:
--   * Token is stored as plaintext TEXT PRIMARY KEY because:
--       - It is 256-bit random (secrets.token_urlsafe(32) = 43 chars base64url).
--       - Single-use + short TTL (≤ 24 h) limits attack window.
--       - Hashing would require a secondary lookup; plaintext lookup is O(1) on PK.
--   * `purpose` column allows this table to serve both email verification and
--     password reset without schema duplication.  Additional purposes can be
--     added via a new CHECK constraint extension later.
--   * `user_id` FK references webui_users(id) added in m9_001_oauth_columns.sql.
--     ON DELETE CASCADE: deleting a user invalidates their pending tokens.
--   * Expired + used tokens should be cleaned up periodically.

CREATE TABLE IF NOT EXISTS email_verifications (
    token      TEXT        PRIMARY KEY,
    user_id    INTEGER     NOT NULL REFERENCES webui_users(id) ON DELETE CASCADE,
    purpose    TEXT        NOT NULL DEFAULT 'email_verify'
                           CHECK (purpose IN ('email_verify', 'password_reset')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_email_verify_user
    ON email_verifications (user_id);

CREATE INDEX IF NOT EXISTS idx_email_verify_expires
    ON email_verifications (expires_at);
