-- Initial schema: profiles, repos, api_keys, ssh_key_pairs, usage_log,
-- pattern_feedback, indexer_jobs.
--
-- All DDL uses IF NOT EXISTS so this migration is safe to apply against a database
-- that was bootstrapped by the legacy src/db/migrate.py SCHEMA_SQL approach.
-- On first run against a fresh database yoyo creates these objects normally.
-- On first run against an existing database the backend.mark_migrations() baseline
-- call in migrate.py marks this migration as already applied without re-executing it.
--
-- NOTE: pgvector / embeddings table is intentionally excluded from this file.
-- CREATE EXTENSION vector requires superuser privileges that the application user
-- may not have.  The extension + embeddings table are created (if available) by
-- src/db/migrate.py::_ensure_extension() before yoyo runs, with graceful
-- InsufficientPrivilege handling.  If pgvector is unavailable the embeddings table
-- is simply skipped, which is acceptable for deployments without vector search.

CREATE TABLE IF NOT EXISTS profiles (
    id           SERIAL PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,
    odoo_version TEXT NOT NULL,
    description  TEXT,
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS repos (
    id              SERIAL PRIMARY KEY,
    profile_id      INTEGER REFERENCES profiles(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    branch          TEXT NOT NULL,
    local_path      TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',
    last_indexed_at TIMESTAMP,
    head_sha        TEXT,
    error_msg       TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (url, branch)
);

CREATE INDEX IF NOT EXISTS idx_repos_profile_id ON repos(profile_id);

-- M6 Wave 2: head_sha column (idempotent ALTER for upgrade path from pre-Wave-2 deploys)
ALTER TABLE repos ADD COLUMN IF NOT EXISTS head_sha TEXT;

CREATE TABLE IF NOT EXISTS api_keys (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    key_hash     TEXT UNIQUE NOT NULL,
    key_prefix   TEXT NOT NULL,
    active       BOOLEAN DEFAULT TRUE,
    created_at   TIMESTAMP DEFAULT NOW(),
    last_used_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ssh_key_pairs (
    id                    SERIAL PRIMARY KEY,
    name                  TEXT NOT NULL,
    public_key            TEXT NOT NULL,
    private_key_encrypted TEXT NOT NULL,
    key_version           INTEGER NOT NULL DEFAULT 1,
    created_at            TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage_log (
    id           BIGSERIAL PRIMARY KEY,
    api_key_id   INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    tool_name    TEXT NOT NULL,
    called_at    TIMESTAMP DEFAULT NOW(),
    response_ms  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_usage_log_api_key ON usage_log(api_key_id);
CREATE INDEX IF NOT EXISTS idx_usage_log_called_at ON usage_log(called_at);

-- M6 Wave 4: link repos → ssh_key_pairs (must run after ssh_key_pairs is created above)
ALTER TABLE repos ADD COLUMN IF NOT EXISTS ssh_key_id INTEGER
    REFERENCES ssh_key_pairs(id) ON DELETE SET NULL;
ALTER TABLE repos ADD COLUMN IF NOT EXISTS clone_status TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE repos ADD COLUMN IF NOT EXISTS clone_error_msg TEXT;

CREATE TABLE IF NOT EXISTS pattern_feedback (
    id               SERIAL PRIMARY KEY,
    pattern_node_id  TEXT NOT NULL,
    api_key_id       INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    rating           TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment          TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pattern_feedback_node ON pattern_feedback (pattern_node_id);

CREATE TABLE IF NOT EXISTS indexer_jobs (
    id           SERIAL PRIMARY KEY,
    profile_name TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','running','done','error')),
    pid          INTEGER,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    error_msg    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_indexer_jobs_profile ON indexer_jobs(profile_name);
CREATE INDEX IF NOT EXISTS ix_indexer_jobs_status  ON indexer_jobs(status);
CREATE INDEX IF NOT EXISTS ix_indexer_jobs_created ON indexer_jobs(created_at DESC);
