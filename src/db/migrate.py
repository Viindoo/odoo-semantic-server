# src/db/migrate.py
"""PostgreSQL schema bootstrap via yoyo-migrations.

Usage:
    python -m src.db.migrate

Migration files live in  <repo_root>/migrations/  and are numbered sequentially:
    0001_initial.sql   — full baseline schema (all tables up to M6 Wave 4)
    0002_*.sql         — future additive/ALTER changes go here

Baseline safety for existing databases
---------------------------------------
On a database already bootstrapped by the legacy SCHEMA_SQL approach, the
schema objects already exist but yoyo's internal _yoyo_migration table does
not.  Without intervention yoyo would attempt to re-apply 0001_initial.sql,
which would silently no-op for most CREATE TABLE IF NOT EXISTS statements but
would *fail* on the ALTER TABLE ADD COLUMN IF NOT EXISTS for embeddings'
unique-constraint upgrade (the DO $$ block is not fully idempotent against an
already-migrated constraint).

Baseline strategy: before applying, we detect whether the schema already
exists via a sentinel query (SELECT 1 FROM api_keys LIMIT 1).  If the schema
is present but 0001_initial has not been recorded as applied, we call
backend.mark_migrations([initial_migration]) to register it as applied
without re-executing it.  Subsequent numbered migrations are then applied
normally.

ADR-0001 Revision: adopted in M7 W15.  Baseline = migrations/0001_initial.sql.
"""

import sys
from pathlib import Path

import psycopg2

from src import config
from src.db._types import PgConn

# ---------------------------------------------------------------------------
# Legacy constants — kept for callers that import SCHEMA_SQL directly
# (e.g. test_db_migrate.py::test_schema_sql_alias_includes_w4_columns).
# ---------------------------------------------------------------------------

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
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (url, branch)
);

CREATE INDEX IF NOT EXISTS idx_repos_profile_id ON repos(profile_id);

-- M6 Wave 2: head_sha column for incremental indexer (idempotent ALTER for upgrade path)
ALTER TABLE repos ADD COLUMN IF NOT EXISTS head_sha TEXT;

-- M6 Wave 4 ssh_key_id + clone_status columns are added in _REPOS_SSH_LINK_SQL,
-- executed AFTER _AUTH_SQL creates ssh_key_pairs (FK ordering constraint).
"""

_REPOS_SSH_LINK_SQL = """
-- M6 Wave 4: link repos → ssh_key_pairs. Must run AFTER _AUTH_SQL creates ssh_key_pairs.
ALTER TABLE repos ADD COLUMN IF NOT EXISTS ssh_key_id INTEGER
    REFERENCES ssh_key_pairs(id) ON DELETE SET NULL;
ALTER TABLE repos ADD COLUMN IF NOT EXISTS clone_status TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE repos ADD COLUMN IF NOT EXISTS clone_error_msg TEXT;
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
SCHEMA_SQL = (
    _BASE_SQL
    + _EMBEDDINGS_SQL
    + _AUTH_SQL
    + _REPOS_SSH_LINK_SQL
    + _FEEDBACK_SQL
    + _INDEXER_JOBS_SQL
)

# ---------------------------------------------------------------------------
# Helpers (used by conftest fixtures and tests directly)
# ---------------------------------------------------------------------------

_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


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


def _schema_already_exists(conn: PgConn) -> bool:
    """Return True if the schema was previously bootstrapped by the legacy SCHEMA_SQL approach.

    Detection criteria (both must be true):
    1. yoyo's internal _yoyo_migration table does NOT yet exist (i.e. yoyo has never run).
    2. The api_keys table DOES exist (sentinel for the full M2.5–M6 schema).

    This combination means: "bootstrapped by legacy migrate.py, yoyo never ran before".
    If _yoyo_migration already exists (yoyo has run at least once), we defer entirely
    to yoyo's own state tracking — no need for the baseline mark.
    """
    with conn.cursor() as cur:
        # Check if yoyo's internal table already exists.
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = '_yoyo_migration' LIMIT 1"
        )
        yoyo_table_exists = cur.fetchone() is not None

    if yoyo_table_exists:
        # yoyo has run before; trust its state.  No baseline marking needed.
        return False

    # yoyo never ran: check for the api_keys sentinel.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'api_keys' LIMIT 1"
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# run_migrations — psycopg2-connection entry point (used by tests / conftest)
# ---------------------------------------------------------------------------

def run_migrations(conn: PgConn) -> None:
    """Execute schema DDL on an open psycopg2 connection.

    Profiles and repos tables are always created (via yoyo migrations/0001_initial.sql).
    Embeddings table requires pgvector extension — attempted first via _ensure_extension();
    skipped with a warning if not available (superuser privilege required for extension).
    Auth tables (api_keys, ssh_key_pairs, usage_log) are always created.

    Raises RuntimeError if PostgreSQL version < 16 or pgvector < 0.8.

    This function delegates to yoyo-migrations for the main schema and handles
    pgvector/embeddings separately because CREATE EXTENSION requires superuser
    privileges that yoyo cannot gracefully skip.
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

    # pgvector / embeddings: handled separately before yoyo because CREATE EXTENSION
    # requires superuser and cannot be inside a transaction (yoyo would wrap it).
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

    # Reconstruct a URI yoyo can use from the psycopg2 connection's info attributes.
    # Must use _conn_to_uri (not _dsn_to_uri) because conn.dsn masks the password as 'xxx'.
    uri = _conn_to_uri(conn)
    _run_yoyo(uri, existing_conn=conn)


def _dsn_to_uri(dsn: str) -> str:
    """Convert a psycopg2-style DSN keyword string to a postgresql:// URI.

    Handles both URI form (already has '://') and keyword=value form.
    Note: psycopg2 masks passwords in conn.dsn as 'xxx'; prefer _conn_to_uri()
    when a live connection object is available.

    User + password are URL-encoded so secrets containing `@:/?#` (common in
    strong passwords) don't break URI parsing.
    """
    from urllib.parse import quote_plus

    if "://" in dsn:
        return dsn
    # Parse keyword=value pairs
    params: dict[str, str] = {}
    for token in dsn.split():
        if "=" in token:
            k, _, v = token.partition("=")
            params[k.strip()] = v.strip().strip("'")
    user = params.get("user", "")
    password = params.get("password", "")
    host = params.get("host", "localhost")
    port = params.get("port", "5432")
    dbname = params.get("dbname", "")
    if password:
        return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{dbname}"
    return f"postgresql://{quote_plus(user)}@{host}:{port}/{dbname}"


def _conn_to_uri(conn: PgConn) -> str:
    """Reconstruct a postgresql:// URI from an open psycopg2 connection.

    Uses conn.info to get the real (unmasked) password, which conn.dsn masks
    as 'xxx'. This is required to pass a valid URI to yoyo's get_backend().

    User + password are URL-encoded so secrets containing `@:/?#` don't break
    URI parsing (e.g. yoyo's get_backend would misinterpret host/port).
    """
    from urllib.parse import quote_plus

    info = conn.info
    user = info.user or ""
    password = info.password or ""
    host = info.host or "localhost"
    port = info.port or 5432
    dbname = info.dbname or ""
    if password:
        return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{dbname}"
    return f"postgresql://{quote_plus(user)}@{host}:{port}/{dbname}"


def _run_yoyo(dsn_uri: str, existing_conn: PgConn | None = None) -> None:
    """Apply pending yoyo migrations from _MIGRATIONS_DIR against dsn_uri.

    Baseline safety: if the schema already exists (detected via existing_conn
    or a fresh probe connection) but 0001_initial has not been recorded, mark
    it applied before running to_apply().

    existing_conn is used only for the schema-existence probe; yoyo manages
    its own connection internally.

    Advisory lock: pg_try_advisory_lock(0x05DA0E05) is acquired on a dedicated
    lock connection before the baseline-detection + apply phase to serialize
    concurrent migrate calls (e.g., two processes starting simultaneously on a
    fresh deploy).  The constant 0x05DA0E05 encodes "ODA005" (odoo schema 005).
    """
    import psycopg2 as _psycopg2
    from yoyo import get_backend, read_migrations

    # --- Acquire advisory lock ---
    # Use existing_conn if possible; otherwise open a dedicated lock connection.
    # The lock is held for the entire migrate duration and released on close.
    _lock_conn_owned = False
    lock_conn = existing_conn
    if lock_conn is None:
        try:
            lock_conn = _psycopg2.connect(dsn_uri)
            lock_conn.autocommit = True
            _lock_conn_owned = True
        except Exception:
            lock_conn = None

    _MIGRATE_ADVISORY_LOCK = 0x05DA0E05  # "ODA005" — odoo schema 005

    lock_acquired = False
    if lock_conn is not None:
        try:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATE_ADVISORY_LOCK,))
            lock_acquired = True
        except Exception:
            pass  # Advisory lock unavailable; proceed without serialization

    try:
        migrations = read_migrations(str(_MIGRATIONS_DIR))
        backend = get_backend(dsn_uri)

        try:
            # Detect legacy-bootstrapped database that predates yoyo adoption.
            if existing_conn is not None:
                schema_present = _schema_already_exists(existing_conn)
            else:
                # Open a probe connection to check schema existence.
                try:
                    probe = _psycopg2.connect(dsn_uri)
                    probe.autocommit = True
                    schema_present = _schema_already_exists(probe)
                    probe.close()
                except Exception:
                    schema_present = False

            if schema_present:
                # Mark 0001_initial as applied without re-executing it.
                # Uses backend.to_apply(migrations) — pass full MigrationList so yoyo
                # can compute hashes correctly; then filter by id.
                pending_ids = {m.id for m in backend.to_apply(migrations)}
                if "0001_initial" in pending_ids:
                    # Schema exists but 0001_initial not yet recorded — legacy baseline.
                    initial = [m for m in migrations if m.id == "0001_initial"]
                    backend.mark_migrations(initial)

            pending = backend.to_apply(migrations)
            backend.apply_migrations(pending)
        finally:
            backend.connection.close()
    finally:
        # Release advisory lock and close lock connection if we opened it.
        if lock_acquired and lock_conn is not None:
            try:
                with lock_conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATE_ADVISORY_LOCK,))
            except Exception:
                pass
        if _lock_conn_owned and lock_conn is not None:
            try:
                lock_conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

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
        conn.autocommit = True
        # PostgreSQL version check
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('server_version_num')::int")
            ver = cur.fetchone()[0]
            if ver < 160000:
                print(
                    f"✗ PostgreSQL 16+ required (found server_version_num={ver}). "
                    f"Update docker-compose.yml PG_IMAGE and re-run.",
                    file=sys.stderr,
                )
                return 1

        # pgvector / embeddings handled before yoyo (needs superuser; not transactional).
        if _ensure_extension(conn):
            with conn.cursor() as cur:
                cur.execute(_EMBEDDINGS_SQL)
                cur.execute(_EMBEDDINGS_UPGRADE_SQL)
        else:
            print(
                "⚠ pgvector extension not available — embeddings table skipped.\n"
                "  Run as superuser: CREATE EXTENSION vector; then re-run migrations.",
                file=sys.stderr,
            )

        _run_yoyo(dsn, existing_conn=conn)
        print(f"✓ Migrations applied to {safe_dsn}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
