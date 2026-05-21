# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schema migration upgrade-path tests (M6 W2 review fix).

Reproduces the Opus review HIGH finding: CREATE TABLE IF NOT EXISTS is a no-op
on existing tables; an ALTER is required for upgrade path.
"""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


def test_run_migrations_on_wave_1_schema_adds_head_sha(clean_pg):
    """Simulate Wave-1 deploy (no head_sha) → run migrate → assert column added.

    Reproduces the Opus review HIGH finding: CREATE TABLE IF NOT EXISTS
    is a no-op on existing tables; an ALTER is required for upgrade.
    """
    conn = clean_pg  # autocommit=True per conftest fixture

    # Build a Wave-1-ish schema manually (no head_sha column)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS repos CASCADE")
        cur.execute("DROP TABLE IF EXISTS profiles CASCADE")
        # Wave-1 schema (head_sha intentionally absent)
        cur.execute("""
            CREATE TABLE profiles (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                odoo_version TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE repos (
                id SERIAL PRIMARY KEY,
                profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                branch TEXT NOT NULL,
                local_path TEXT,
                last_indexed_at TIMESTAMP,
                UNIQUE(url, branch)
            )
        """)

    # Confirm column ABSENT before migrate
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='repos' AND column_name='head_sha'"
        )
        assert cur.fetchone() is None, "Wave-1 fixture should not have head_sha"

    # Run M6 Wave 2 migrations
    run_migrations(conn)

    # Confirm column NOW PRESENT
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='repos' AND column_name='head_sha'"
        )
        assert cur.fetchone() is not None, (
            "head_sha column missing after migrate — ALTER TABLE not idempotent"
        )

    # Idempotent: running migrate twice should not error
    run_migrations(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='repos' AND column_name='head_sha'"
        )
        assert cur.fetchone() is not None, (
            "head_sha column missing after second migrate run"
        )
