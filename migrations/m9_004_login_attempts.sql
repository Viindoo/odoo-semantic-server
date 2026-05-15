-- migrations/m9_004_login_attempts.sql
-- M9 Auth Wow: create login_attempts table for Postgres-backed rate limiting.
--
-- Findings addressed:
--   F2 — Rate limit stored in in-process dict (login.py:22-23): multi-worker
--        uvicorn = N independent counters, restart = reset.  Fix: move tracking
--        to a shared Postgres table so all workers see the same state.
--   F3 — X-Forwarded-For trust without proxy allowlist: IP-based bucketing must
--        record raw ip_address so the application layer can apply proxy-aware
--        normalisation before querying.
--
-- Design notes:
--   * `identifier` stores whichever dimension is rate-limited (username or IP).
--     A single table supports both strategies; callers choose the identifier.
--   * `ip_address` uses the native INET type for efficient CIDR comparisons.
--   * `success = TRUE` rows allow positive-signal lockout reset.
--   * Row TTL is managed by a periodic cleanup job (out of scope for this
--     migration).  The application should purge rows older than 30 days.
--
-- TODO: cleanup job for rows older than 30 days.
--   Suggested: scheduled DELETE FROM login_attempts
--              WHERE attempted_at < NOW() - INTERVAL '30 days'
--   Run via pg_cron, a systemd timer, or the indexer maintenance routine.

CREATE TABLE IF NOT EXISTS login_attempts (
    id           BIGSERIAL   PRIMARY KEY,
    identifier   TEXT        NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    success      BOOLEAN     NOT NULL,
    ip_address   INET,
    user_agent   TEXT
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_identifier_time
    ON login_attempts (identifier, attempted_at DESC);

CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time
    ON login_attempts (ip_address, attempted_at DESC);
