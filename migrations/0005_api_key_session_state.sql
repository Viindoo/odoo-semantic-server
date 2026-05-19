-- migrations/0005_api_key_session_state.sql
-- M11 Wave E: Per-API-key sticky session state for implicit context.
--
-- Addresses W-E implicit-session-context pattern: once a user sets
-- odoo_version + profile_name via set_active_version() and
-- set_active_profile(), those choices are sticky across subsequent
-- MCP tool invocations on the same API key.
--
-- Design notes:
--   * `api_key_id` is an INTEGER PK referencing api_keys(id) with ON DELETE CASCADE.
--     Deleting an API key automatically purges its session state.
--   * `odoo_version` and `profile_name` store the currently-active context
--     set by set_active_version() and set_active_profile() MCP tools.
--     Both are TEXT (nullable) — NULL means not yet set, fallback to
--     _latest_version() and user's default profile.
--   * `updated_at` is TIMESTAMP WITH TIME ZONE; sliding window TTL is
--     implemented at the application layer: if updated_at < NOW() - interval '24h',
--     fallback to _latest_version() and ignore the stale context.
--   * One row per api_key_id (PK enforces 1:1 relationship).
--
-- See ADR-0029 — implicit-session-context for design details.

CREATE TABLE IF NOT EXISTS api_key_session_state (
    api_key_id    INTEGER PRIMARY KEY REFERENCES api_keys(id) ON DELETE CASCADE,
    odoo_version  TEXT,
    profile_name  TEXT,
    updated_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_api_key_session_state_updated_at
    ON api_key_session_state (updated_at);

COMMENT ON TABLE api_key_session_state IS
    'Per-API-key sticky session state — odoo_version + profile_name '
    'set via set_active_version()/set_active_profile() MCP tools. '
    '24h sliding TTL: updated_at older than 24h triggers fallback to '
    '_latest_version(). One row per api_key_id (PK). See ADR-0029.';

COMMENT ON COLUMN api_key_session_state.api_key_id IS
    'Foreign key to api_keys(id). ON DELETE CASCADE ensures automatic cleanup.';

COMMENT ON COLUMN api_key_session_state.odoo_version IS
    'Currently-active Odoo version context (e.g., "17.0", "16.0"). '
    'NULL = not yet set; fallback to _latest_version().';

COMMENT ON COLUMN api_key_session_state.profile_name IS
    'Currently-active profile name (e.g., "my-erp-prod", "custom-addon-lib"). '
    'NULL = not yet set; fallback to user''s default profile.';

COMMENT ON COLUMN api_key_session_state.updated_at IS
    'Timestamp of last state update. Used for 24h sliding TTL: '
    'if updated_at < NOW() - interval ''24h'', application treats state as expired.';
