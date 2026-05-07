"""Integration tests for src.db.migrate — requires PostgreSQL."""
import pytest

from src.db.migrate import _vector_extension_available, run_migrations

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


def test_migrate_creates_embeddings_table(clean_pg):
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed")
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'embeddings' ORDER BY ordinal_position
        """)
        cols = [r[0] for r in cur.fetchall()]
    assert "chunk_type" in cols
    assert "module" in cols
    assert "odoo_version" in cols
    assert "entity_name" in cols
    assert "content" in cols
    assert "vec" in cols


def test_migrate_embeddings_idempotent(clean_pg):
    """Running migrate twice must not fail (embeddings table included)."""
    run_migrations(clean_pg)
    run_migrations(clean_pg)


def test_migrate_embeddings_unique_index(clean_pg):
    """UNIQUE constraint on (chunk_type, module, odoo_version, entity_name, chunk_idx)."""
    import psycopg2.errors
    from pgvector.psycopg2 import register_vector
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed")
    register_vector(clean_pg)
    vec = [0.0] * 1024
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO embeddings (chunk_type, module, odoo_version, entity_name, "
            "file_path, content, vec) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            ("method", "sale", "99.0", "action_confirm",
             "models/sale.py", "def action_confirm(self):", vec),
        )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO embeddings (chunk_type, module, odoo_version, entity_name, "
                "file_path, content, vec) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                ("method", "sale", "99.0", "action_confirm", "models/sale.py", "duplicate", vec),
            )


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
