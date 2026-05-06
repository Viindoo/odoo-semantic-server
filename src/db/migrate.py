# src/db/migrate.py
"""PostgreSQL schema bootstrap.

Usage:
    python -m src.db.migrate
"""
import sys

import psycopg2

from src import config

SCHEMA_SQL = """
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
    error_msg       TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (url, branch)
);

CREATE INDEX IF NOT EXISTS idx_repos_profile_id ON repos(profile_id);
"""


def run_migrations(conn) -> None:
    """Execute schema DDL on an open psycopg2 connection."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    if not conn.autocommit:
        conn.commit()


def main() -> int:
    dsn = config.get(
        "database", "pg_dsn",
        fallback="postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
    )
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as e:
        print(f"✗ Cannot connect to PostgreSQL ({dsn}): {e}", file=sys.stderr)
        return 1
    try:
        run_migrations(conn)
        print(f"✓ Migrations applied to {dsn}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
