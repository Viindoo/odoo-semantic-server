# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for src.db.migrate — behaviour cases only.

One-shot catalog assertions (column/index/constraint existence via
information_schema, pg_indexes, pg_constraint) were removed — covered by
test_squashed_baseline.py golden snapshot.

Kept behaviour cases:
  - Idempotency (run twice, yoyo reports 0 pending on second run)
  - UNIQUE / CHECK / FK-cascade enforcement via INSERT+raises
  - Data preservation across baseline re-detection
  - REQUIRE_PGVECTOR fail-fast guard unit tests (no DB needed)

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
def test_migrate_is_idempotent(clean_pg):
    """Running migrate twice must not fail."""
    run_migrations(clean_pg)
    run_migrations(clean_pg)


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
# M9 behaviour tests — CHECK / FK-cascade enforcement
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# M13 behaviour tests — tenants table + tenant_id FKs + key_type + repos UNIQUE
# (m13_002_tenants_and_fks.sql, ADR-0034 D1 + D7)
# ---------------------------------------------------------------------------


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
# m13_021 sentinel tests — CHECK enforcement (FUFU-2 root fix)
# ---------------------------------------------------------------------------


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
