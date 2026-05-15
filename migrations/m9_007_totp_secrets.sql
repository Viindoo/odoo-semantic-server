-- migrations/m9_007_totp_secrets.sql
-- M9 Auth Wow: create totp_secrets table for MFA TOTP enrollment.
--
-- Design notes:
--   * One row per user; user_id is both PK and FK — 1:1 relationship with
--     webui_users.  ON DELETE CASCADE: deleting a user removes their TOTP config.
--   * `secret_encrypted` stores the TOTP base32 seed encrypted with the
--     application's FERNET_KEY.  Never stored plaintext.
--   * `enabled = FALSE` after enrollment until the user verifies their first
--     code — prevents locking out a user who scanned the wrong QR code.
--   * MFA status check: row EXISTS AND enabled = TRUE → MFA active.
--     No duplicate `mfa_enabled` column needed in webui_users.
--   * `backup_codes_hash` is a JSONB array of objects:
--       [{"hash": "<hmac-sha256>", "used_at": null}, ...]
--     Application verifies against hash; records used_at on redemption.
--   * `last_used_at` supports TOTP replay protection (reject codes re-used
--     within the same 30-second window).

CREATE TABLE IF NOT EXISTS totp_secrets (
    user_id             INTEGER     PRIMARY KEY REFERENCES webui_users(id) ON DELETE CASCADE,
    secret_encrypted    TEXT        NOT NULL,
    enabled             BOOLEAN     NOT NULL DEFAULT FALSE,
    enrolled_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    backup_codes_hash   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    last_used_at        TIMESTAMPTZ
);
