# src/db/migrate.py
"""PostgreSQL schema bootstrap.

Usage:
    python -m src.db.migrate
"""

import sys

import psycopg2
import psycopg2.errors

from src import config
from src.db._types import PgConn

_EXTENSION_SQL = "CREATE EXTENSION IF NOT EXISTS vector;"

_BASE_SQL = """
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
    ssh_key_id      INTEGER REFERENCES ssh_key_pairs(id) ON DELETE SET NULL,
    clone_status    TEXT NOT NULL DEFAULT 'manual',
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (url, branch)
);

CREATE INDEX IF NOT EXISTS idx_repos_profile_id ON repos(profile_id);

-- M6 Wave 2: head_sha column for incremental indexer (idempotent ALTER for upgrade path)
ALTER TABLE repos ADD COLUMN IF NOT EXISTS head_sha TEXT;

-- M6 Wave 4: ssh_key_id + clone_status columns for SSH auto-clone support
ALTER TABLE repos ADD COLUMN IF NOT EXISTS ssh_key_id INTEGER
    REFERENCES ssh_key_pairs(id) ON DELETE SET NULL;
ALTER TABLE repos ADD COLUMN IF NOT EXISTS clone_status TEXT NOT NULL DEFAULT 'manual';
"""

_EMBEDDINGS_SQL = """
CREATE TABLE IF NOT EXISTS embeddings (
    id           BIGSERIAL PRIMARY KEY,
    chunk_type   TEXT NOT NULL,
    module       TEXT NOT NULL,
    odoo_version TEXT NOT NULL,
    entity_name  TEXT NOT NULL,
    model_name   TEXT,
    file_path    TEXT NOT NULL,
    chunk_idx    INTEGER NOT NULL DEFAULT 0,
    content      TEXT NOT NULL,
    vec          vector(1024) NOT NULL,
    indexed_at   TIMESTAMP DEFAULT NOW(),
    CONSTRAINT ux_embeddings_chunk
        UNIQUE (chunk_type, module, odoo_version, entity_name, file_path, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_vec
    ON embeddings USING hnsw (vec vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_embeddings_filter
    ON embeddings (odoo_version, chunk_type, module);
"""

# Upgrade existing installations: add file_path to the unique constraint if missing.
# Safe to re-run; the DO block is a no-op when the constraint already has file_path.
_EMBEDDINGS_UPGRADE_SQL = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'ux_embeddings_chunk' AND table_name = 'embeddings'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.constraint_column_usage
    WHERE constraint_name = 'ux_embeddings_chunk' AND column_name = 'file_path'
  ) THEN
    ALTER TABLE embeddings DROP CONSTRAINT ux_embeddings_chunk;
    ALTER TABLE embeddings ADD CONSTRAINT ux_embeddings_chunk
      UNIQUE (chunk_type, module, odoo_version, entity_name, file_path, chunk_idx);
  END IF;
END $$;
"""

_AUTH_SQL = """
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
"""

_FEEDBACK_SQL = """
CREATE TABLE IF NOT EXISTS pattern_feedback (
    id               SERIAL PRIMARY KEY,
    pattern_node_id  TEXT NOT NULL,
    api_key_id       INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    rating           TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment          TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pattern_feedback_node ON pattern_feedback (pattern_node_id);
"""

_INDEXER_JOBS_SQL = """
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
"""

# Public alias — tests and callers that import SCHEMA_SQL get the full DDL string
SCHEMA_SQL = _BASE_SQL + _EMBEDDINGS_SQL + _AUTH_SQL + _FEEDBACK_SQL + _INDEXER_JOBS_SQL


def _vector_extension_available(conn: PgConn) -> bool:
    """True if pgvector extension is installed (regardless of who created it)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        return cur.fetchone() is not None


def _ensure_extension(conn: PgConn) -> bool:
    """Attempt to create pgvector extension. Returns True if available after attempt.

    Raises RuntimeError if pgvector is available but version < 0.8.
    """
    if _vector_extension_available(conn):
        # Verify pgvector version is >= 0.8
        with conn.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            row = cur.fetchone()
            if row is not None:
                v = row[0]
                parts = v.split('.')
                major = int(parts[0])
                minor = int(parts[1]) if len(parts) > 1 else 0
                if (major, minor) < (0, 8):
                    raise RuntimeError(
                        f"pgvector 0.8+ required (found {v}). "
                        f"Update docker-compose.yml PG_IMAGE and re-run."
                    )
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(_EXTENSION_SQL)
        if not conn.autocommit:
            conn.commit()
        # Verify installed version is >= 0.8
        with conn.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            row = cur.fetchone()
            if row is not None:
                v = row[0]
                parts = v.split('.')
                major = int(parts[0])
                minor = int(parts[1]) if len(parts) > 1 else 0
                if (major, minor) < (0, 8):
                    raise RuntimeError(
                        f"pgvector 0.8+ required (found {v}). "
                        f"Update docker-compose.yml PG_IMAGE and re-run."
                    )
        return True
    except psycopg2.errors.InsufficientPrivilege:
        if not conn.autocommit:
            conn.rollback()
        return False


def run_migrations(conn: PgConn) -> None:
    """Execute schema DDL on an open psycopg2 connection.

    Profiles and repos tables are always created.
    Embeddings table requires pgvector extension — skipped with a warning if not available.
    Auth tables (api_keys, ssh_key_pairs, usage_log) are always created.

    Raises RuntimeError if PostgreSQL version < 16 or pgvector < 0.8.
    """
    # Check PostgreSQL version is >= 16
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('server_version_num')::int")
        ver = cur.fetchone()[0]
        if ver < 160000:
            raise RuntimeError(
                f"PostgreSQL 16+ required (found server_version_num={ver}). "
                f"Update docker-compose.yml PG_IMAGE and re-run."
            )

    with conn.cursor() as cur:
        cur.execute(_BASE_SQL)
    if not conn.autocommit:
        conn.commit()

    if _ensure_extension(conn):
        with conn.cursor() as cur:
            cur.execute(_EMBEDDINGS_SQL)
            cur.execute(_EMBEDDINGS_UPGRADE_SQL)
        if not conn.autocommit:
            conn.commit()
    else:
        print(
            "⚠ pgvector extension not available — embeddings table skipped.\n"
            "  Run as superuser: CREATE EXTENSION vector; then re-run migrations.",
            file=sys.stderr,
        )

    with conn.cursor() as cur:
        cur.execute(_AUTH_SQL)
    if not conn.autocommit:
        conn.commit()

    with conn.cursor() as cur:
        cur.execute(_FEEDBACK_SQL)
    if not conn.autocommit:
        conn.commit()

    with conn.cursor() as cur:
        cur.execute(_INDEXER_JOBS_SQL)
    if not conn.autocommit:
        conn.commit()


def main() -> int:
    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        print(
            "✗ PostgreSQL DSN missing. Set PG_DSN env var OR `pg_dsn` in "
            "[database] section of odoo-semantic.conf.",
            file=sys.stderr,
        )
        return 1
    safe_dsn = config.mask_dsn(dsn)
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as e:
        print(f"✗ Cannot connect to PostgreSQL ({safe_dsn}): {e}", file=sys.stderr)
        return 1
    try:
        run_migrations(conn)
        print(f"✓ Migrations applied to {safe_dsn}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
