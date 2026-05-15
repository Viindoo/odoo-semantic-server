-- migrations/m9_002_api_keys_user_fk.sql
-- M9 Auth Wow: add user ownership and expiry support to api_keys.
--
-- Findings addressed:
--   F11 — API key has no expires_at column.
--
-- Design notes:
--   * user_id FK references webui_users(id) — the new integer column added in
--     m9_001_oauth_columns.sql.  NULL means the key is not yet associated with
--     a user (legacy keys created before M9).
--   * ON DELETE CASCADE: deleting a user revokes all their API keys.
--   * key_prefix is TEXT (variable length) — the plan mentions bumping prefix
--     length 8→12.  Because TEXT has no length constraint, new keys can simply
--     use a longer prefix without an ALTER; existing short prefixes remain valid.
--     No ALTER TABLE needed for this column.
--   * expires_at NULL = non-expiring key (existing behaviour preserved).

ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS user_id INTEGER
    REFERENCES webui_users(id) ON DELETE CASCADE;

ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
