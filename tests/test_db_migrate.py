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


def test_repos_ssh_key_id_fk_ordering(clean_pg):
    """Regression guard: repos.ssh_key_id FK must reference ssh_key_pairs after fresh migrate.

    Earlier bug: _BASE_SQL declared the FK inline, causing PostgreSQL to error
    'relation ssh_key_pairs does not exist' on fresh DB because _AUTH_SQL ran later.
    Fix: split into _REPOS_SSH_LINK_SQL executed AFTER _AUTH_SQL.
    """
    run_migrations(clean_pg)

    with clean_pg.cursor() as cur:
        # Confirm column exists
        cur.execute("""
            SELECT column_name
              FROM information_schema.columns
             WHERE table_name = 'repos' AND column_name = 'ssh_key_id'
        """)
        assert cur.fetchone() is not None, "repos.ssh_key_id column missing"

        # Confirm FK constraint with delete_rule = SET NULL
        cur.execute("""
            SELECT rc.delete_rule, ccu.table_name AS referenced_table
              FROM information_schema.referential_constraints rc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = rc.constraint_name
              JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = rc.constraint_name
             WHERE kcu.table_name = 'repos' AND kcu.column_name = 'ssh_key_id'
        """)
        rows = cur.fetchall()
        assert len(rows) == 1, f"expected 1 FK on repos.ssh_key_id, found {len(rows)}"
        delete_rule, referenced_table = rows[0]
        assert delete_rule == "SET NULL"
        assert referenced_table == "ssh_key_pairs"

        # Confirm clone_status column with proper default
        cur.execute("""
            SELECT column_default, is_nullable
              FROM information_schema.columns
             WHERE table_name = 'repos' AND column_name = 'clone_status'
        """)
        row = cur.fetchone()
        assert row is not None
        assert "manual" in str(row[0])
        assert row[1] == "NO"


def test_schema_sql_alias_includes_w4_columns():
    """SCHEMA_SQL alias must include _REPOS_SSH_LINK_SQL columns.

    External consumers who import SCHEMA_SQL must see the full schema including
    ssh_key_id, clone_status, and clone_error_msg columns added in M6 W4.
    """
    from src.db.migrate import SCHEMA_SQL
    assert "ssh_key_id" in SCHEMA_SQL
    assert "clone_status" in SCHEMA_SQL
    assert "clone_error_msg" in SCHEMA_SQL
