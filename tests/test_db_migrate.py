"""Integration tests for src.db.migrate — requires PostgreSQL."""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


def test_migrate_creates_profiles_table(clean_pg):
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'profiles' ORDER BY ordinal_position
        """)
        cols = [r[0] for r in cur.fetchall()]
    assert cols == ["id", "name", "odoo_version", "description", "created_at"]


def test_migrate_creates_repos_table(clean_pg):
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'repos' ORDER BY ordinal_position
        """)
        cols = [r[0] for r in cur.fetchall()]
    assert "profile_id" in cols
    assert "url" in cols
    assert "branch" in cols
    assert "local_path" in cols
    assert "status" in cols


def test_migrate_is_idempotent(clean_pg):
    """Running migrate twice must not fail."""
    run_migrations(clean_pg)
    run_migrations(clean_pg)


def test_repos_unique_constraint_on_url_branch(clean_pg):
    import psycopg2.errors
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version) VALUES ('p1', '17.0') RETURNING id"
        )
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path) "
            "VALUES (%s, 'github.com/x/y', '17.0', '/tmp/y')",
            (pid,),
        )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO repos (profile_id, url, branch, local_path) "
                "VALUES (%s, 'github.com/x/y', '17.0', '/tmp/other')",
                (pid,),
            )
