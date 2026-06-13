# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for src.db.migrate — requires PostgreSQL.

Unit tests for REQUIRE_PGVECTOR fail-fast guard (no Docker needed) are at the
bottom of this file; they are NOT marked postgres/neo4j and run under make test-unit.
"""
import pytest

from src.db.migrate import (
    _MIGRATIONS_DIR,
    _check_pgvector_or_exit,
    _conn_to_uri,
    _vector_extension_available,
    run_migrations,
)


@pytest.mark.postgres
def test_migrate_creates_profiles_table(clean_pg):
    """profiles must expose its full column contract with correct type + nullability.

    RW-19: previously this snapshotted the exact declaration ORDER
    (`assert cols == [...]`), an implementation detail — a harmless column
    reorder (e.g. a future migration re-emitting the table) would break the test
    without any behavioral regression. The business contract is *which* columns
    exist and *what shape* they have (type + NOT NULL + UNIQUE), not their
    ordinal position. We assert that contract directly, order-independently.
    """
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_name = 'profiles'
        """)
        meta = {r[0]: {"type": r[1], "nullable": r[2]} for r in cur.fetchall()}

    # Contract 1: exactly these columns exist (no missing, no unexpected).
    # tenant_id added by m13_002_tenants_and_fks.sql (additive, nullable FK);
    # parent_profile_id by the profile-hierarchy migration (ADR-0016, nullable FK).
    expected_columns = {
        "id", "name", "odoo_version", "description", "created_at",
        "parent_profile_id", "tenant_id",
    }
    assert set(meta) == expected_columns, (
        f"profiles column set mismatch: "
        f"missing={sorted(expected_columns - set(meta))}, "
        f"unexpected={sorted(set(meta) - expected_columns)}"
    )

    # Contract 2: type + nullability per column (the real schema invariants).
    assert meta["id"]["type"] == "integer" and meta["id"]["nullable"] == "NO", meta["id"]
    assert meta["name"]["type"] == "text" and meta["name"]["nullable"] == "NO", meta["name"]
    assert meta["odoo_version"]["type"] == "text", meta["odoo_version"]
    assert meta["odoo_version"]["nullable"] == "NO", meta["odoo_version"]
    assert meta["description"]["type"] == "text", meta["description"]
    assert meta["description"]["nullable"] == "YES", meta["description"]
    assert meta["created_at"]["type"].startswith("timestamp"), meta["created_at"]
    # Additive FK columns are nullable by design.
    assert meta["parent_profile_id"]["nullable"] == "YES", meta["parent_profile_id"]
    assert meta["tenant_id"]["nullable"] == "YES", meta["tenant_id"]

    # Contract 3: name carries a UNIQUE constraint (business identity of a profile).
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT 1
              FROM information_schema.table_constraints tc
              JOIN information_schema.constraint_column_usage ccu
                ON tc.constraint_name = ccu.constraint_name
             WHERE tc.table_name = 'profiles'
               AND tc.constraint_type = 'UNIQUE'
               AND ccu.column_name = 'name'
        """)
        assert cur.fetchone() is not None, "profiles.name must carry a UNIQUE constraint"


@pytest.mark.postgres
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


@pytest.mark.postgres
def test_migrate_is_idempotent(clean_pg):
    """Running migrate twice must not fail."""
    run_migrations(clean_pg)
    run_migrations(clean_pg)


@pytest.mark.postgres
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


# NOTE: the former `test_migrate_embeddings_idempotent` (pure double-run no-raise)
# was removed — fully covered by the canonical `test_migrate_is_idempotent` above,
# which runs the full migration stack (incl. embeddings) twice.


@pytest.mark.postgres
def test_migrate_embeddings_unique_index(clean_pg):
    """UNIQUE NULLS NOT DISTINCT on (chunk_type, module, odoo_version, entity_name,
    file_path, chunk_idx, profile_name).

    Both rows supply the same profile_name to exercise the UNIQUE constraint:
    duplicating (chunk_type, module, odoo_version, entity_name, file_path,
    chunk_idx, profile_name) must raise UniqueViolation even though the rows
    differ in content. Post-m13_021 the column is NOT NULL; profile_name is
    required in the INSERT. The NULLS NOT DISTINCT clause (m13_001) is still
    relevant for the (unlikely) case where a future migration re-introduces
    a nullable profile — it prevents silent dedup regression.
    """
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
            "file_path, content, vec, profile_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("method", "sale", "99.0", "action_confirm",
             "models/sale.py", "def action_confirm(self):", vec, "test_profile"),
        )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO embeddings (chunk_type, module, odoo_version, entity_name, "
                "file_path, content, vec, profile_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                ("method", "sale", "99.0", "action_confirm",
                 "models/sale.py", "duplicate", vec, "test_profile"),
            )


@pytest.mark.postgres
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


@pytest.mark.postgres
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
# REQUIRE_PGVECTOR fail-fast guard — unit tests (no Docker / Postgres needed)
# ---------------------------------------------------------------------------

def test_require_pgvector_set_exits_when_extension_unavailable(monkeypatch):
    """REQUIRE_PGVECTOR=1 + pgvector unavailable must call sys.exit(1).

    When an operator sets REQUIRE_PGVECTOR=1 on a managed Postgres where the
    app-user cannot install the vector extension, migrate must fail loudly
    (sys.exit(1)) rather than silently skipping the embeddings table.
    """
    monkeypatch.setenv("REQUIRE_PGVECTOR", "1")

    with pytest.raises(SystemExit) as exc_info:
        _check_pgvector_or_exit(available=False)

    assert exc_info.value.code == 1


def test_default_pgvector_unavailable_is_fail_soft(monkeypatch):
    """REQUIRE_PGVECTOR unset (default) + pgvector unavailable must NOT raise SystemExit.

    The default behaviour is fail-soft: print a warning and continue so that
    existing deployments where pgvector is not installed are not broken.
    """
    monkeypatch.delenv("REQUIRE_PGVECTOR", raising=False)

    # Must not raise — fail-soft path only prints a warning (not tested here).
    _check_pgvector_or_exit(available=False)


# ---------------------------------------------------------------------------
# yoyo-specific tests (M7 W15)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


# NOTE: the former `test_m9_all_new_tables_present` (table-existence umbrella for
# the 6 M9 tables) was removed — each table's existence is already enforced by its
# individual column test (table missing → `expected - cols` fails):
#   webui_users → test_m9_001_oauth_columns_present
#   admin_audit_log → test_m9_003_admin_audit_log
#   login_attempts → test_m9_004_login_attempts
#   active_sessions → test_m9_005_active_sessions
#   email_verifications → test_m9_006_email_verifications
#   totp_secrets → test_m9_007_totp_secrets


@pytest.mark.postgres
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
        # M13 — tenants table added by m13_002_tenants_and_fks.sql
        "tenants",
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


# ---------------------------------------------------------------------------
# M13 schema tests — tenants table + tenant_id FKs + key_type + repos UNIQUE
# (m13_002_tenants_and_fks.sql, ADR-0034 D1 + D7)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_m13_tenants_table_exists(clean_pg):
    """m13_002: tenants table must exist with id, name, created_at, active columns."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'tenants'
             ORDER BY ordinal_position
        """)
        cols = [r[0] for r in cur.fetchall()]
    assert "id" in cols, "tenants.id missing"
    assert "name" in cols, "tenants.name missing"
    assert "created_at" in cols, "tenants.created_at missing"
    assert "active" in cols, "tenants.active missing"


@pytest.mark.postgres
def test_m13_tenants_name_unique(clean_pg):
    """m13_002: tenants.name must be UNIQUE — duplicate insert raises UniqueViolation."""
    import psycopg2.errors

    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("INSERT INTO tenants (name) VALUES ('acme')")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute("INSERT INTO tenants (name) VALUES ('acme')")


@pytest.mark.postgres
def test_m13_tenant_id_fk_columns_exist(clean_pg):
    """m13_002: tenant_id column must be present on api_keys, profiles, ssh_key_pairs, repos."""
    run_migrations(clean_pg)
    tables = ["api_keys", "profiles", "ssh_key_pairs", "repos"]
    for table in tables:
        with clean_pg.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = %s AND column_name = 'tenant_id'
            """, (table,))
            row = cur.fetchone()
        assert row is not None, f"{table}.tenant_id column missing after m13_002"


@pytest.mark.postgres
def test_m13_tenant_id_defaults_null(clean_pg):
    """m13_002: tenant_id must default to NULL — existing-row compatibility (ADR-0034 D1)."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        # Insert a profile without specifying tenant_id
        cur.execute(
            "INSERT INTO profiles (name, odoo_version) VALUES ('shared_base', '17.0') RETURNING id"
        )
        pid = cur.fetchone()[0]
        cur.execute("SELECT tenant_id FROM profiles WHERE id = %s", (pid,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] is None, "profiles.tenant_id should default to NULL for shared/global rows"


@pytest.mark.postgres
def test_m13_tenant_id_fk_references_tenants(clean_pg):
    """m13_002: api_keys.tenant_id must be a FK referencing tenants(id) with CASCADE."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT rc.delete_rule, ccu.table_name AS referenced_table
              FROM information_schema.referential_constraints rc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = rc.constraint_name
              JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = rc.constraint_name
             WHERE kcu.table_name = 'api_keys' AND kcu.column_name = 'tenant_id'
        """)
        rows = cur.fetchall()
    assert len(rows) == 1, f"Expected 1 FK on api_keys.tenant_id, found {len(rows)}"
    delete_rule, referenced_table = rows[0]
    assert delete_rule == "CASCADE"
    assert referenced_table == "tenants"


@pytest.mark.postgres
def test_m13_ssh_key_pairs_key_type_column(clean_pg):
    """m13_002: ssh_key_pairs.key_type must exist with default 'access_key' (ADR-0034 D7)."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_default, is_nullable
              FROM information_schema.columns
             WHERE table_name = 'ssh_key_pairs' AND column_name = 'key_type'
        """)
        row = cur.fetchone()
    assert row is not None, "ssh_key_pairs.key_type column missing"
    column_default, is_nullable = row
    assert "access_key" in str(column_default), (
        f"ssh_key_pairs.key_type default should be 'access_key', got: {column_default}"
    )
    assert is_nullable == "NO", "ssh_key_pairs.key_type must be NOT NULL"


@pytest.mark.postgres
def test_m13_ssh_key_pairs_key_type_check_constraint(clean_pg):
    """m13_002: ssh_key_pairs.key_type must only accept 'deploy_key' or 'access_key'."""
    import psycopg2.errors

    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        # Valid insert — 'deploy_key'
        cur.execute(
            "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted, key_type) "
            "VALUES ('k1', 'pub', 'enc', 'deploy_key')"
        )
        # Valid insert — 'access_key' (default)
        cur.execute(
            "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted) "
            "VALUES ('k2', 'pub2', 'enc2')"
        )
        cur.execute("SELECT key_type FROM ssh_key_pairs WHERE name = 'k2'")
        assert cur.fetchone()[0] == "access_key", "Default key_type should be 'access_key'"
        # Invalid insert — must raise CheckViolation
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted, key_type) "
                "VALUES ('k3', 'pub3', 'enc3', 'admin_key')"
            )


@pytest.mark.postgres
def test_m13_repos_unique_per_profile_not_global(clean_pg):
    """m13_002: same (url, branch) under DIFFERENT profiles must succeed (ADR-0034 D2)."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version) VALUES ('p_a', '17.0') RETURNING id"
        )
        pid_a = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO profiles (name, odoo_version) VALUES ('p_b', '17.0') RETURNING id"
        )
        pid_b = cur.fetchone()[0]

        # Same URL+branch under profile A
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path) "
            "VALUES (%s, 'github.com/odoo/odoo', '17.0', '/tmp/odoo_a')",
            (pid_a,),
        )
        # Same URL+branch under profile B — must NOT raise (cross-profile allowed)
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path) "
            "VALUES (%s, 'github.com/odoo/odoo', '17.0', '/tmp/odoo_b')",
            (pid_b,),
        )


@pytest.mark.postgres
def test_m13_repos_unique_within_same_profile(clean_pg):
    """m13_002: same (url, branch, profile_id) under the SAME profile must raise UniqueViolation."""
    import psycopg2.errors

    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version) VALUES ('p_dup', '17.0') RETURNING id"
        )
        pid = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path) "
            "VALUES (%s, 'github.com/x/y', '17.0', '/tmp/xy_1')",
            (pid,),
        )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO repos (profile_id, url, branch, local_path) "
                "VALUES (%s, 'github.com/x/y', '17.0', '/tmp/xy_2')",
                (pid,),
            )


@pytest.mark.postgres
def test_m13_repos_old_global_unique_constraint_dropped(clean_pg):
    """m13_002 must DROP the old global UNIQUE(url, branch) (repos_url_branch_key).

    Asserts the business rule directly: if the drop silently failed, cross-profile
    registration of the same upstream URL would wrongly raise — and the positive
    test test_m13_repos_unique_per_profile_not_global would be the only signal.
    """
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_name = 'repos' AND constraint_type = 'UNIQUE'"
        )
        names = {r[0] for r in cur.fetchall()}
    assert "repos_url_branch_key" not in names, (
        "old global UNIQUE(url, branch) must be dropped by m13_002"
    )
    assert "repos_url_branch_profile_key" in names, (
        "per-profile UNIQUE(url, branch, profile_id) must be present"
    )


@pytest.mark.postgres
def test_m13_deploy_key_unique_per_tenant(clean_pg):
    """m13_002 partial UNIQUE index allows at most ONE deploy_key per tenant.

    Closes the get_or_create_tenant_deploy_key check-then-insert TOCTOU, while
    still permitting multiple access_key rows for the same tenant.
    """
    import psycopg2.errors

    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("INSERT INTO tenants (name) VALUES ('t_dk') RETURNING id")
        tid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO ssh_key_pairs "
            "(name, public_key, private_key_encrypted, key_type, tenant_id) "
            "VALUES ('dk1', 'pub1', 'enc1', 'deploy_key', %s)",
            (tid,),
        )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO ssh_key_pairs "
            "(name, public_key, private_key_encrypted, key_type, tenant_id) "
                "VALUES ('dk2', 'pub2', 'enc2', 'deploy_key', %s)",
                (tid,),
            )

    with clean_pg.cursor() as cur:
        cur.execute("INSERT INTO tenants (name) VALUES ('t_ak') RETURNING id")
        tid2 = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO ssh_key_pairs "
            "(name, public_key, private_key_encrypted, key_type, tenant_id) "
            "VALUES ('ak1', 'pub1', 'enc1', 'access_key', %s)",
            (tid2,),
        )
        cur.execute(
            "INSERT INTO ssh_key_pairs "
            "(name, public_key, private_key_encrypted, key_type, tenant_id) "
            "VALUES ('ak2', 'pub2', 'enc2', 'access_key', %s)",
            (tid2,),
        )
        cur.execute(
            "SELECT count(*) FROM ssh_key_pairs WHERE tenant_id = %s AND key_type = 'access_key'",
            (tid2,),
        )
        assert cur.fetchone()[0] == 2, "multiple access_key rows per tenant must be allowed"


# NOTE: the former `test_m13_migration_idempotent` (pure double-run no-raise) was
# removed — fully covered by the canonical `test_migrate_is_idempotent` above, which
# runs the full migration stack (incl. all m13_* migrations) twice.


# ---------------------------------------------------------------------------
# m13_021 sentinel tests (FUFU-2 root fix — global sentinel + NOT NULL)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_m13_021_no_null_profile_name_after_migrate(clean_pg):
    """m13_021: post-migration embeddings must have 0 NULL profile_name rows.

    The migration backfills all NULLs to '__global__' before adding NOT NULL.
    On a fresh schema there are no rows, so the count is trivially 0.  The
    important assertion is that the NOT NULL column is in place (covered by
    test_m13_021_profile_name_not_null below).
    """
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed")
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM embeddings WHERE profile_name IS NULL"
        )
        count = cur.fetchone()[0]
    assert count == 0, (
        f"post-m13_021 migration must leave 0 NULL profile_name rows, got {count}"
    )


@pytest.mark.postgres
def test_m13_021_profile_name_not_null(clean_pg):
    """m13_021: embeddings.profile_name column must be NOT NULL after migrate."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed")
    with clean_pg.cursor() as cur:
        cur.execute(
            """SELECT is_nullable
                 FROM information_schema.columns
                WHERE table_name = 'embeddings'
                  AND column_name = 'profile_name'"""
        )
        row = cur.fetchone()
    assert row is not None, "embeddings.profile_name column not found"
    assert row[0] == "NO", (
        f"embeddings.profile_name must be NOT NULL after m13_021, got is_nullable={row[0]!r}"
    )


@pytest.mark.postgres
def test_m13_021_sentinel_check_present(clean_pg):
    """m13_021: ck_embeddings_global_sentinel_scope CHECK must be present and validated."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed")
    with clean_pg.cursor() as cur:
        cur.execute(
            """SELECT conname, convalidated
                 FROM pg_constraint
                WHERE conname = 'ck_embeddings_global_sentinel_scope'
                  AND conrelid = 'public.embeddings'::regclass"""
        )
        row = cur.fetchone()
    assert row is not None, (
        "ck_embeddings_global_sentinel_scope CHECK constraint missing after m13_021"
    )
    assert row[1] is True, (
        "ck_embeddings_global_sentinel_scope must be VALIDATED (convalidated=true)"
    )


@pytest.mark.postgres
def test_m13_021_dunder_check_present(clean_pg):
    """m13_021: profiles_name_no_dunder CHECK must be present and validated."""
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            """SELECT conname, convalidated
                 FROM pg_constraint
                WHERE conname = 'profiles_name_no_dunder'
                  AND conrelid = 'public.profiles'::regclass"""
        )
        row = cur.fetchone()
    assert row is not None, (
        "profiles_name_no_dunder CHECK constraint missing after m13_021"
    )
    assert row[1] is True, (
        "profiles_name_no_dunder must be VALIDATED (convalidated=true)"
    )


@pytest.mark.postgres
def test_m13_021_dunder_check_rejects_global_profile(clean_pg):
    """m13_021: profiles_name_no_dunder CHECK rejects a profile named '__global__'.

    The dunder-block prevents an admin from accidentally creating a profile
    whose embeddings would become globally visible via the RLS sentinel branch.
    """
    import psycopg2.errors

    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO profiles (name, odoo_version) VALUES ('__global__', '17.0')"
            )
    clean_pg.rollback()
