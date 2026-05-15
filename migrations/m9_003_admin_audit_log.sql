-- migrations/m9_003_admin_audit_log.sql
-- M9 Auth Wow: create admin_audit_log table for non-repudiation of admin operations.
--
-- Findings addressed:
--   F14 — No admin_audit_log table: non-repudiation gap across all admin operations.
--
-- Design notes:
--   * `actor` encodes the acting identity in a free-text format that is
--     self-describing regardless of actor type, e.g.:
--       'user:admin'          — authenticated web UI user
--       'api_key:osm_abcd12'  — API key (prefix only; never the full hash)
--       'cli:tuan'            — CLI invocation (login name)
--   * `action` uses dot-namespaced verbs, e.g. 'user.login', 'profile.delete'.
--   * `target` (nullable) identifies the resource acted upon, e.g. 'profile:42'.
--   * `detail` (JSONB, nullable) carries structured payload for forensics.
--   * Rows are append-only; no UPDATE/DELETE should ever touch this table.
--     Enforcement is left to application layer + DB role grants (out of scope).

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id         BIGSERIAL    PRIMARY KEY,
    actor      TEXT         NOT NULL,
    action     TEXT         NOT NULL,
    target     TEXT,
    success    BOOLEAN      NOT NULL,
    detail     JSONB,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_actor_created
    ON admin_audit_log (actor, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_action_created
    ON admin_audit_log (action, created_at DESC);
