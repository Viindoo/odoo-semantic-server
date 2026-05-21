# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for src.db.migrate — requires PostgreSQL."""
import pytest

from src.db.migrate import (
    _MIGRATIONS_DIR,
    _conn_to_uri,
    _vector_extension_available,
    run_migrations,
)

pytestmark = pytest.mark.postgres


def test_migrate_creates_profiles_table(clean_pg):
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'profiles' ORDER BY ordinal_position
        """)
        cols = [r[0] for r in cur.fetchall()]
    assert cols == ["id", "name", "odoo_version", "description", "created_at", "parent_profile_id"]


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


# ---------------------------------------------------------------------------
# yoyo-specific tests (M7 W15)
# ---------------------------------------------------------------------------


def test_migrate_idempotent_zero_pending_on_second_run(clean_pg):
    """Second run of yoyo must report 0 migrations pending (all already applied).

    Verifies that yoyo's internal state table correctly tracks applied migrations
    so that re-running migrate is a no-op rather than re-executing DDL.
    """
    from yoyo import get_backend, read_migrations

    uri = _conn_to_uri(clean_pg)

    # First run — applies 0001_initial and records it in _yoyo_migration.
    run_migrations(clean_pg)

    # Second run — to_apply() must return an empty list.
    migrations = read_migrations(str(_MIGRATIONS_DIR))
    backend = get_backend(uri)
    try:
        pending = list(backend.to_apply(migrations))
    finally:
        backend.connection.close()

    assert pending == [], (
        f"Expected 0 pending migrations after second run, got: {[m.id for m in pending]}"
    )


def test_migrate_preserves_existing_data(clean_pg):
    """Migrate against a database with live data must not destroy rows.

    Simulates the production-safety scenario: api_keys row exists before
    migration runs (legacy bootstrap), and must survive the yoyo baseline
    marking + subsequent apply.
    """
    # Bootstrap schema directly so api_keys table exists without yoyo records.
    run_migrations(clean_pg)

    # Insert sentinel row.
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix) "
            "VALUES ('test-key', 'sha256hash', 'osm_test') RETURNING id"
        )
        sentinel_id = cur.fetchone()[0]

    # Drop all yoyo internal tables to simulate a legacy database (schema present,
    # no migration records) — forces baseline-detection code path.
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _yoyo_migration CASCADE")
        cur.execute("DROP TABLE IF EXISTS _yoyo_log CASCADE")
        cur.execute("DROP TABLE IF EXISTS _yoyo_version CASCADE")

    # Run migrate again — should mark baseline, apply 0 new migrations, preserve data.
    run_migrations(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute("SELECT id FROM api_keys WHERE id = %s", (sentinel_id,))
        row = cur.fetchone()

    assert row is not None, (
        f"api_keys row id={sentinel_id} was destroyed by migrate — production-safety failure"
    )


# ---------------------------------------------------------------------------
# M9 schema tests — verify all M9 migration columns and tables are present
# ---------------------------------------------------------------------------


def test_m9_001_oauth_columns_present(clean_pg):
    """m9_001: webui_users must have all M9 auth columns after migrate."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'webui_users'
        """)
        cols = {r[0] for r in cur.fetchall()}
    expected = {
        "oauth_provider",
        "oauth_id",
        "email",
        "email_verified",
        "is_admin",
        "is_active",
        "role",
        "created_at",
    }
    missing = expected - cols
    assert not missing, f"webui_users missing M9 columns: {sorted(missing)}"


def test_m9_001_webui_users_id_column(clean_pg):
    """m9_001: webui_users.id SERIAL UNIQUE must exist for downstream FK references."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name, is_nullable
              FROM information_schema.columns
             WHERE table_name = 'webui_users' AND column_name = 'id'
        """)
        row = cur.fetchone()
    assert row is not None, "webui_users.id column missing"


def test_m9_001_email_unique_constraint(clean_pg):
    """m9_001: webui_users.email must have a UNIQUE constraint."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT constraint_name
              FROM information_schema.table_constraints
             WHERE table_name = 'webui_users'
               AND constraint_type = 'UNIQUE'
               AND constraint_name = 'webui_users_email_unique'
        """)
        row = cur.fetchone()
    assert row is not None, "webui_users_email_unique constraint missing"


def test_m9_001_role_check_constraint(clean_pg):
    """m9_001: webui_users.role must only accept 'admin' or 'viewer'."""
    import psycopg2.errors
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash) "
            "VALUES ('check_role_test', 'hash')"
        )
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "UPDATE webui_users SET role = 'superuser' "
                "WHERE username = 'check_role_test'"
            )


def test_m9_002_api_keys_user_fk(clean_pg):
    """m9_002: api_keys must have user_id FK and expires_at column."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'api_keys'
        """)
        cols = {r[0] for r in cur.fetchall()}
    assert "user_id" in cols, "api_keys.user_id missing"
    assert "expires_at" in cols, "api_keys.expires_at missing"


def test_m9_002_api_keys_user_id_fk_references(clean_pg):
    """m9_002: api_keys.user_id must be a FK referencing webui_users(id) with CASCADE."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT rc.delete_rule, ccu.table_name
              FROM information_schema.referential_constraints rc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = rc.constraint_name
              JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = rc.constraint_name
             WHERE kcu.table_name = 'api_keys' AND kcu.column_name = 'user_id'
        """)
        rows = cur.fetchall()
    assert len(rows) == 1, f"Expected 1 FK on api_keys.user_id, found {len(rows)}"
    delete_rule, referenced_table = rows[0]
    assert delete_rule == "CASCADE"
    assert referenced_table == "webui_users"


def test_m9_002_api_keys_user_id_index(clean_pg):
    """m9_002: idx_api_keys_user_id index must exist for lookup performance."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT indexname FROM pg_indexes
             WHERE tablename = 'api_keys'
               AND indexname = 'idx_api_keys_user_id'
        """)
        row = cur.fetchone()
    assert row is not None, "idx_api_keys_user_id index missing"


def test_m9_003_admin_audit_log(clean_pg):
    """m9_003: admin_audit_log table must exist with all required columns."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'admin_audit_log'
        """)
        cols = {r[0] for r in cur.fetchall()}
    expected = {"id", "actor", "action", "target", "success", "detail", "created_at"}
    missing = expected - cols
    assert not missing, f"admin_audit_log missing columns: {sorted(missing)}"


def test_m9_003_admin_audit_log_indexes(clean_pg):
    """m9_003: both composite indexes on admin_audit_log must exist."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT indexname FROM pg_indexes
             WHERE tablename = 'admin_audit_log'
        """)
        indexes = {r[0] for r in cur.fetchall()}
    assert "idx_audit_actor_created" in indexes, "idx_audit_actor_created missing"
    assert "idx_audit_action_created" in indexes, "idx_audit_action_created missing"


def test_m9_004_login_attempts(clean_pg):
    """m9_004: login_attempts table must exist with all required columns."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'login_attempts'
        """)
        cols = {r[0] for r in cur.fetchall()}
    expected = {"id", "identifier", "attempted_at", "success", "ip_address", "user_agent"}
    missing = expected - cols
    assert not missing, f"login_attempts missing columns: {sorted(missing)}"


def test_m9_004_login_attempts_indexes(clean_pg):
    """m9_004: both composite indexes on login_attempts must exist."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT indexname FROM pg_indexes
             WHERE tablename = 'login_attempts'
        """)
        indexes = {r[0] for r in cur.fetchall()}
    assert "idx_login_attempts_identifier_time" in indexes, \
        "idx_login_attempts_identifier_time missing"
    assert "idx_login_attempts_ip_time" in indexes, \
        "idx_login_attempts_ip_time missing"


def test_m9_005_active_sessions(clean_pg):
    """m9_005: active_sessions table must exist with all required columns."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'active_sessions'
        """)
        cols = {r[0] for r in cur.fetchall()}
    expected = {
        "session_id", "user_id", "created_at", "expires_at",
        "last_seen", "ip_address", "user_agent", "mfa_verified_at",
    }
    missing = expected - cols
    assert not missing, f"active_sessions missing columns: {sorted(missing)}"


def test_m9_005_active_sessions_fk_cascade(clean_pg):
    """m9_005: active_sessions.user_id FK must reference webui_users with CASCADE."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT rc.delete_rule, ccu.table_name
              FROM information_schema.referential_constraints rc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = rc.constraint_name
              JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = rc.constraint_name
             WHERE kcu.table_name = 'active_sessions' AND kcu.column_name = 'user_id'
        """)
        rows = cur.fetchall()
    assert len(rows) == 1, f"Expected 1 FK on active_sessions.user_id, found {len(rows)}"
    delete_rule, referenced_table = rows[0]
    assert delete_rule == "CASCADE"
    assert referenced_table == "webui_users"


def test_m9_005_active_sessions_indexes(clean_pg):
    """m9_005: idx_sessions_user_id and idx_sessions_expires must exist."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT indexname FROM pg_indexes
             WHERE tablename = 'active_sessions'
        """)
        indexes = {r[0] for r in cur.fetchall()}
    assert "idx_sessions_user_id" in indexes, "idx_sessions_user_id missing"
    assert "idx_sessions_expires" in indexes, "idx_sessions_expires missing"


def test_m9_006_email_verifications(clean_pg):
    """m9_006: email_verifications table must exist with all required columns."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'email_verifications'
        """)
        cols = {r[0] for r in cur.fetchall()}
    expected = {"token", "user_id", "purpose", "created_at", "expires_at", "used_at"}
    missing = expected - cols
    assert not missing, f"email_verifications missing columns: {sorted(missing)}"


def test_m9_006_email_verifications_purpose_check(clean_pg):
    """m9_006: email_verifications.purpose must only accept defined values."""
    import psycopg2.errors
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash) "
            "VALUES ('ev_test_user', 'hash')"
        )
        cur.execute("SELECT id FROM webui_users WHERE username = 'ev_test_user'")
        uid = cur.fetchone()[0]
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO email_verifications "
                "(token, user_id, purpose, expires_at) "
                "VALUES ('tok1', %s, 'bad_purpose', NOW() + INTERVAL '1 hour')",
                (uid,),
            )


def test_m9_006_email_verifications_indexes(clean_pg):
    """m9_006: idx_email_verify_user and idx_email_verify_expires must exist."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT indexname FROM pg_indexes
             WHERE tablename = 'email_verifications'
        """)
        indexes = {r[0] for r in cur.fetchall()}
    assert "idx_email_verify_user" in indexes, "idx_email_verify_user missing"
    assert "idx_email_verify_expires" in indexes, "idx_email_verify_expires missing"


def test_m9_007_totp_secrets(clean_pg):
    """m9_007: totp_secrets table must exist with all required columns."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'totp_secrets'
        """)
        cols = {r[0] for r in cur.fetchall()}
    expected = {
        "user_id", "secret_encrypted", "enabled",
        "enrolled_at", "backup_codes_hash", "last_used_at",
    }
    missing = expected - cols
    assert not missing, f"totp_secrets missing columns: {sorted(missing)}"


def test_m9_007_totp_secrets_fk_cascade(clean_pg):
    """m9_007: totp_secrets.user_id FK must reference webui_users with CASCADE."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT rc.delete_rule, ccu.table_name
              FROM information_schema.referential_constraints rc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = rc.constraint_name
              JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = rc.constraint_name
             WHERE kcu.table_name = 'totp_secrets' AND kcu.column_name = 'user_id'
        """)
        rows = cur.fetchall()
    assert len(rows) == 1, f"Expected 1 FK on totp_secrets.user_id, found {len(rows)}"
    delete_rule, referenced_table = rows[0]
    assert delete_rule == "CASCADE"
    assert referenced_table == "webui_users"


def test_m9_all_new_tables_present(clean_pg):
    """m9: all 5 new M9 tables must exist after migrate."""
    run_migrations(clean_pg)
    expected_tables = {
        "webui_users",
        "admin_audit_log",
        "login_attempts",
        "active_sessions",
        "email_verifications",
        "totp_secrets",
    }
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        """)
        found = {row[0] for row in cur.fetchall()}
    missing = expected_tables - found
    assert not missing, f"M9 tables missing after migrate: {sorted(missing)}"


def test_migrate_fresh_db_creates_all_tables(clean_pg):
    """W15: run_migrations on empty schema must create all tables from 0001_initial.sql.

    Uses clean_pg (pre-wiped schema), runs migrate once, then asserts every
    non-embeddings table defined in 0001_initial.sql exists in information_schema.
    Embeddings table is conditional on pgvector — excluded from this assertion.
    """
    run_migrations(clean_pg)

    # Tables defined in 0001_initial.sql + additive migrations
    # (excluding embeddings which requires pgvector).
    expected_tables = {
        "profiles",
        "repos",
        "api_keys",
        "ssh_key_pairs",
        "usage_log",
        "pattern_feedback",
        "indexer_jobs",
        "key_rotation_log",
    }

    with clean_pg.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            """
        )
        found = {row[0] for row in cur.fetchall()}

    missing = expected_tables - found
    assert not missing, (
        f"Fresh migrate is missing expected tables: {sorted(missing)}. "
        f"Found: {sorted(found)}"
    )


def test_migrate_creates_key_rotation_log(clean_pg):
    """M9 W-FE: key_rotation_log audit table must be created by migration m9_008."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'key_rotation_log'
            ORDER BY ordinal_position
        """)
        cols = [r[0] for r in cur.fetchall()]
    assert cols, "key_rotation_log table not found after migrate"
    assert "id" in cols
    assert "rotated_at" in cols
    assert "actor" in cols
    assert "row_count" in cols
    assert "old_key_id" in cols
    assert "new_key_id" in cols


def test_key_rotation_log_index_exists(clean_pg):
    """M9 W-FE: idx_key_rotation_log_time index must be present after migrate."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'key_rotation_log'
        """)
        indexes = [r[0] for r in cur.fetchall()]
    assert "idx_key_rotation_log_time" in indexes, (
        f"Expected idx_key_rotation_log_time in {indexes}"
    )
