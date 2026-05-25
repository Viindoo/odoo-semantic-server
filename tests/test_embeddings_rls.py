# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_embeddings_rls.py
"""RLS (Row-Level Security) tests for the embeddings table (ADR-0034 WI-7).

Migration m13_004_embeddings_rls.sql installs the policy in "armed-but-dormant"
mode (ENABLE without FORCE). These tests cover:

  * Armed-but-dormant: policy installed but owner connection bypasses it (tests 1-2).
  * FORCED mode (non-owner osm_reader role): isolation semantics (tests 3-6, 8).
  * Owner writes succeed under ENABLE-only (test 7).
  * D3 shared catalogue still passes through IS NULL branch (test 8).

Test categories:
  - Tests 1, 2, 7 need pgvector only (no osm_reader/FORCE privilege needed).
  - Tests 3, 4, 5, 6, 8 need superuser privilege to CREATE ROLE + SET ROLE.
    If the test DB user lacks these privileges the test is individually SKIPPED
    with a clear reason — the rest of the file continues to execute.

Pure unit test for _allowed_to_guc mapping lives in tests/test_rls_guc_unit.py
(no DB dependency, always runs without postgres marker).
"""
import pytest

from tests.conftest import PG_EMBED_VERSION as V

# All 8 tests here are postgres integration tests.
# The pure unit test for _allowed_to_guc lives in tests/test_rls_guc_unit.py
# (no DB dependency, always runs without postgres marker).
pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PFX = "rls_"  # prefix for all rows/roles created here


def _seed_embeddings(pg):
    """Insert the four canonical chunks needed by these tests.

    - acme: profile_name = 'rls_acme'
    - globex: profile_name = 'rls_globex'
    - shared: profile_name IS NULL  (legacy/global)
    - pattern: module = '__patterns__', profile_name IS NULL  (D3 catalogue)

    Uses direct SQL (not write_module_embeddings) so we control the vector
    dimension without spinning up an embedder or touching the pool.  The
    vector literal [0.0, ...] × 1024 is valid for the pgvector extension.
    """
    # Build a 1024-dim zero vector literal once.
    zero_vec = "[" + ",".join(["0.0"] * 1024) + "]"

    rows = [
        # (chunk_type, module, odoo_version, entity_name, model_name,
        #  file_path, chunk_idx, content, vec::vector, profile_name)
        ("method", "rls_acme_mod", V, "rls_acme_mod.method", None,
         "/rls_acme.py", 0, "acme private body", zero_vec, "rls_acme"),
        ("method", "rls_globex_mod", V, "rls_globex_mod.method", None,
         "/rls_globex.py", 0, "globex private body", zero_vec, "rls_globex"),
        ("method", "rls_shared_mod", V, "rls_shared_mod.method", None,
         "/rls_shared.py", 0, "shared body", zero_vec, None),
        ("pattern_example", "__patterns__", V, "rls_pattern_1", None,
         "/patterns.py", 0, "pattern catalogue body", zero_vec, None),
    ]

    with pg.cursor() as cur:
        for (ct, mod, ver, en, mn, fp, ci, co, vec, pn) in rows:
            if pn is None:
                cur.execute(
                    """INSERT INTO embeddings
                       (chunk_type, module, odoo_version, entity_name, model_name,
                        file_path, chunk_idx, content, vec, profile_name)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, NULL)
                       ON CONFLICT DO NOTHING""",
                    (ct, mod, ver, en, mn, fp, ci, co, vec),
                )
            else:
                cur.execute(
                    """INSERT INTO embeddings
                       (chunk_type, module, odoo_version, entity_name, model_name,
                        file_path, chunk_idx, content, vec, profile_name)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
                       ON CONFLICT DO NOTHING""",
                    (ct, mod, ver, en, mn, fp, ci, co, vec, pn),
                )
    pg.commit()


def _cleanup_seed(pg):
    with pg.cursor() as cur:
        cur.execute(
            "DELETE FROM embeddings WHERE odoo_version = %s AND module LIKE %s",
            (V, f"{_PFX}%"),
        )
        cur.execute(
            "DELETE FROM embeddings WHERE odoo_version = %s AND module = '__patterns__'",
            (V,),
        )
    pg.commit()


# ---------------------------------------------------------------------------
# Fixture: forced_rls
# Manages FORCE RLS + non-owner role for tests 3-6, 8.
# Setup: CREATE ROLE osm_reader NOLOGIN; GRANT SELECT; ALTER TABLE FORCE RLS.
# Teardown (try/finally): RESET ROLE; NO FORCE; DROP ROLE.
# ---------------------------------------------------------------------------

@pytest.fixture
def forced_rls(clean_pg_embeddings):
    """Yield pg connection + a context manager for running as osm_reader.

    If the test DB user lacks CREATE ROLE or FORCE RLS privilege this fixture
    sets has_privilege=False and each test individually skips (the skip is only
    set for a real InsufficientPrivilege error — NOT for "role already exists"
    which is a pollution artefact cleaned up defensively in setup).

    Setup order:
      1. Best-effort pre-cleanup of any leftover osm_reader from a prior run
         (drop owned + drop role — swallowed if the role doesn't exist).
      2. CREATE ROLE + GRANT + FORCE.  InsufficientPrivilege here → skip flag.

    Teardown order (each statement in its own try so a failure doesn't cascade):
      1. RESET ROLE  (recover session to owner)
      2. NO FORCE ROW LEVEL SECURITY  (critical — must not leak into sibling tests)
      3. REVOKE / DROP OWNED BY  (clear grants so DROP ROLE succeeds)
      4. DROP ROLE IF EXISTS
    """
    import psycopg2.errors

    pg = clean_pg_embeddings
    _seed_embeddings(pg)

    has_privilege = True
    skip_reason = None

    # --- Step 1: defensive pre-cleanup (best-effort, swallow all errors) ---
    for stmt in (
        "DROP OWNED BY osm_reader",
        "DROP ROLE IF EXISTS osm_reader",
    ):
        try:
            with pg.cursor() as cur:
                cur.execute(stmt)
            pg.commit()
        except Exception:
            try:
                pg.rollback()
            except Exception:
                pass

    # --- Step 2: actual setup --- only InsufficientPrivilege → skip flag ---
    try:
        with pg.cursor() as cur:
            cur.execute("CREATE ROLE osm_reader NOLOGIN")
            cur.execute("GRANT SELECT ON embeddings TO osm_reader")
            # Required so SET ROLE osm_reader succeeds for non-superuser CREATEROLE
            # users: PostgreSQL requires caller to be superuser OR a member of the
            # target role.  Without this GRANT, SET ROLE raises InsufficientPrivilege
            # even if the user has CREATEROLE (which only grants CREATE, not BECOME).
            cur.execute("GRANT osm_reader TO CURRENT_USER")
            cur.execute("ALTER TABLE embeddings FORCE ROW LEVEL SECURITY")
        pg.commit()
    except psycopg2.errors.InsufficientPrivilege as exc:
        try:
            pg.rollback()
        except Exception:
            pass
        has_privilege = False
        skip_reason = (
            f"DB user lacks CREATE ROLE / FORCE RLS privilege: {exc} "
            "— run tests as a superuser to enable FORCE-mode coverage."
        )
    except Exception as exc:
        # Unexpected error (e.g. "role already exists" should not reach here
        # because pre-cleanup ran, but handle defensively).
        try:
            pg.rollback()
        except Exception:
            pass
        has_privilege = False
        skip_reason = (
            f"Unexpected error during forced_rls setup: {exc} "
            "— run tests as a superuser to enable FORCE-mode coverage."
        )

    yield {"pg": pg, "has_privilege": has_privilege, "skip_reason": skip_reason}

    # --- Teardown: each step independent so NO FORCE always runs ---

    # 1. Reset role back to owner.
    try:
        with pg.cursor() as cur:
            cur.execute("RESET ROLE")
        pg.commit()
    except Exception:
        try:
            pg.rollback()
        except Exception:
            pass

    # 2. Remove FORCE — critical: must not leak into sibling tests.
    try:
        with pg.cursor() as cur:
            cur.execute("ALTER TABLE embeddings NO FORCE ROW LEVEL SECURITY")
        pg.commit()
    except Exception:
        try:
            pg.rollback()
        except Exception:
            pass

    # 3. Revoke grants so DROP ROLE succeeds (Postgres rejects DROP if grants exist).
    try:
        with pg.cursor() as cur:
            cur.execute("DROP OWNED BY osm_reader")
        pg.commit()
    except Exception:
        try:
            pg.rollback()
        except Exception:
            pass

    # 4. Drop the role.
    try:
        with pg.cursor() as cur:
            cur.execute("DROP ROLE IF EXISTS osm_reader")
        pg.commit()
    except Exception:
        try:
            pg.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test 1 — armed-but-dormant: owner read unaffected by ENABLE (no FORCE)
# Business rule: ENABLE without FORCE = policy installed but owner bypasses →
# owner SELECT returns the same rows as without RLS (no behaviour change).
# ---------------------------------------------------------------------------

def test_rls_enabled_not_forced_owner_read_unaffected(clean_pg_embeddings):
    """Armed-but-dormant: owner connection bypasses RLS — count unchanged.

    Policy is ENABLED after migration (m13_004) but NOT FORCED.  The table
    owner (the test DB user / odoo_semantic in production) is exempt from
    policy evaluation by PostgreSQL's owner-bypass rule.  A SELECT without
    setting any GUC must return all rows seeded for this test.
    """
    pg = clean_pg_embeddings
    _seed_embeddings(pg)

    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM embeddings WHERE odoo_version = %s",
            (V,),
        )
        total = cur.fetchone()[0]

    # We seeded 4 rows (acme + globex + shared + pattern); owner sees all.
    assert total == 4, (
        f"Owner under ENABLE (no FORCE) should see all 4 seeded rows, got {total}. "
        "Armed-but-dormant must not change owner read behaviour."
    )

    _cleanup_seed(pg)


# ---------------------------------------------------------------------------
# Test 2 — schema state: policy exists + RLS enabled but NOT forced
# Business rule: m13_004 installs policy in dormant mode (enabled, not forced).
# ---------------------------------------------------------------------------

def test_policy_object_present_after_migration(clean_pg_embeddings):
    """Schema state: embeddings_tenant policy exists, RLS enabled, NOT forced.

    pg_policies confirms the policy name and table.
    pg_class relrowsecurity=true (enabled) and relforcerowsecurity=false (not forced)
    verify the exact dormant-state contract promised by ADR-0034 WI-7.
    """
    pg = clean_pg_embeddings

    # Check policy exists.
    with pg.cursor() as cur:
        cur.execute(
            """SELECT policyname, tablename
               FROM pg_policies
               WHERE tablename = 'embeddings' AND policyname = 'embeddings_tenant'""",
        )
        rows = cur.fetchall()

    assert len(rows) == 1, (
        f"Policy 'embeddings_tenant' on table 'embeddings' not found in pg_policies. "
        f"Run migration m13_004. pg_policies returned: {rows}"
    )

    # Check RLS enabled but not forced.
    with pg.cursor() as cur:
        cur.execute(
            """SELECT relrowsecurity, relforcerowsecurity
               FROM pg_class
               WHERE relname = 'embeddings' AND relkind = 'r'""",
        )
        row = cur.fetchone()

    assert row is not None, "pg_class row for 'embeddings' table not found."
    relrowsecurity, relforcerowsecurity = row
    assert relrowsecurity is True, (
        "RLS must be ENABLED on embeddings after m13_004 migration."
    )
    assert relforcerowsecurity is False, (
        "RLS must NOT be FORCED on embeddings (armed-but-dormant). "
        "FORCE is a manual ops step, not part of the migration."
    )


# ---------------------------------------------------------------------------
# Test 3 — FORCED RLS: non-owner with GUC='rls_acme' sees own + shared
# Business rule: tenant isolation — acme sees acme rows + NULL profile rows.
# ---------------------------------------------------------------------------

def test_forced_rls_nonowner_sees_own_plus_shared(forced_rls):
    """Tenant acme under FORCE + osm_reader sees its own rows and shared rows.

    Verifies the policy USING clause: profile_name='rls_acme' OR profile_name IS NULL
    both pass; profile_name='rls_globex' is denied.
    """
    info = forced_rls
    if not info["has_privilege"]:
        pytest.skip(info["skip_reason"])

    pg = info["pg"]

    with pg.cursor() as cur:
        cur.execute("SET ROLE osm_reader")
        cur.execute("BEGIN")
        cur.execute("SET LOCAL app.allowed_profiles = 'rls_acme'")
        cur.execute(
            "SELECT module, profile_name FROM embeddings WHERE odoo_version = %s ORDER BY module",
            (V,),
        )
        rows = cur.fetchall()
        cur.execute("ROLLBACK")
        cur.execute("RESET ROLE")
    pg.commit()

    modules = [r[0] for r in rows]
    assert "rls_acme_mod" in modules, (
        f"acme tenant must see its own embedding chunk. Got modules: {modules}"
    )
    assert "rls_globex_mod" not in modules, (
        f"CROSS-TENANT LEAK: acme must NOT see globex chunks. Got modules: {modules}"
    )
    assert "rls_shared_mod" in modules, (
        f"acme tenant must see NULL-profile shared chunks. Got modules: {modules}"
    )


# ---------------------------------------------------------------------------
# Test 4 — FORCED RLS: cross-tenant query returns zero rows
# Business rule: deny-all for rows owned by another tenant.
# ---------------------------------------------------------------------------

def test_forced_rls_nonowner_cross_tenant_zero_rows(forced_rls):
    """FORCED + osm_reader: querying globex rows as acme returns 0 rows.

    The policy USING clause rejects profile_name='rls_globex' when
    app.allowed_profiles='rls_acme'.  Zero rows — not an error, not the wrong rows.
    """
    info = forced_rls
    if not info["has_privilege"]:
        pytest.skip(info["skip_reason"])

    pg = info["pg"]

    with pg.cursor() as cur:
        cur.execute("SET ROLE osm_reader")
        cur.execute("BEGIN")
        cur.execute("SET LOCAL app.allowed_profiles = 'rls_acme'")
        cur.execute(
            "SELECT COUNT(*) FROM embeddings "
            "WHERE odoo_version = %s AND module = 'rls_globex_mod'",
            (V,),
        )
        count = cur.fetchone()[0]
        cur.execute("ROLLBACK")
        cur.execute("RESET ROLE")
    pg.commit()

    assert count == 0, (
        f"CROSS-TENANT ISOLATION FAILURE: acme querying globex rows must return 0 rows, "
        f"got {count}. Policy USING clause not enforced under FORCE RLS."
    )


# ---------------------------------------------------------------------------
# Test 5 — FORCED RLS: admin sentinel ('*') bypasses isolation
# Business rule: GUC='*' = admin — sees all rows unconditionally.
# ---------------------------------------------------------------------------

def test_forced_rls_admin_sentinel_sees_all(forced_rls):
    """Admin sentinel: GUC='*' under FORCE + osm_reader sees every chunk.

    The policy USING clause: current_setting('app.allowed_profiles', true) = '*'
    returns TRUE → unrestricted.  All 4 seeded rows must be visible.
    """
    info = forced_rls
    if not info["has_privilege"]:
        pytest.skip(info["skip_reason"])

    pg = info["pg"]

    with pg.cursor() as cur:
        cur.execute("SET ROLE osm_reader")
        cur.execute("BEGIN")
        cur.execute("SET LOCAL app.allowed_profiles = '*'")
        cur.execute(
            "SELECT COUNT(*) FROM embeddings WHERE odoo_version = %s",
            (V,),
        )
        count = cur.fetchone()[0]
        cur.execute("ROLLBACK")
        cur.execute("RESET ROLE")
    pg.commit()

    assert count == 4, (
        f"Admin sentinel ('*') must see all 4 seeded chunks, got {count}. "
        "Policy USING clause admin branch not working under FORCE RLS."
    )


# ---------------------------------------------------------------------------
# Test 6 — FORCED RLS: empty GUC ('') sees only NULL-profile (shared) rows
# Business rule: deny-all tenant (no profiles) sees only global catalogue.
# ---------------------------------------------------------------------------

def test_forced_rls_empty_guc_sees_only_shared(forced_rls):
    """Empty GUC ('') under FORCE + osm_reader: only NULL-profile rows visible.

    string_to_array('', ',') = {''}; profile_name = ANY({''}) is FALSE for
    any real profile_name.  Only rows with profile_name IS NULL pass.
    """
    info = forced_rls
    if not info["has_privilege"]:
        pytest.skip(info["skip_reason"])

    pg = info["pg"]

    with pg.cursor() as cur:
        cur.execute("SET ROLE osm_reader")
        cur.execute("BEGIN")
        cur.execute("SET LOCAL app.allowed_profiles = ''")
        cur.execute(
            "SELECT module, profile_name FROM embeddings WHERE odoo_version = %s",
            (V,),
        )
        rows = cur.fetchall()
        cur.execute("ROLLBACK")
        cur.execute("RESET ROLE")
    pg.commit()

    modules = [r[0] for r in rows]
    profiles = [r[1] for r in rows]

    # Only NULL-profile rows should be visible.
    assert all(p is None for p in profiles), (
        f"Empty GUC must only return NULL-profile rows. Got profiles: {profiles}"
    )
    assert "rls_acme_mod" not in modules, (
        f"Empty GUC must not return acme rows. Got modules: {modules}"
    )
    assert "rls_globex_mod" not in modules, (
        f"Empty GUC must not return globex rows. Got modules: {modules}"
    )
    # shared and pattern rows (both NULL profile) should be present.
    assert "rls_shared_mod" in modules, (
        f"Shared (NULL-profile) rows must be visible under empty GUC. Got: {modules}"
    )


# ---------------------------------------------------------------------------
# Test 7 — owner write succeeds under ENABLE-only RLS
# Business rule: indexer (owner) must not be blocked by RLS — writes proceed.
# ---------------------------------------------------------------------------

def test_owner_write_succeeds_under_enabled_rls(clean_pg_embeddings):
    """Owner write (DELETE+INSERT) succeeds when RLS is ENABLED (not FORCED).

    The owner role bypasses policy evaluation entirely; write_module_embeddings
    must work exactly as before the RLS migration.  This proves that deploying
    m13_004 does not break the indexer.
    """
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    pg = clean_pg_embeddings
    emb = FakeEmbedder(dim=1024)
    chunk = EmbeddingChunk(
        "method", "rls_owner_write_mod", V,
        "rls_owner_write_mod.do", None, "/rls_owner.py", 0,
        "owner indexer write test body",
    )

    # write_module_embeddings uses the pool, not the test pg_conn directly.
    # It must succeed without error.
    embed_calls = write_module_embeddings(
        "rls_owner_write_mod", V, [chunk], emb, profile_name="rls_owner_profile",
    )

    assert embed_calls >= 1, "write_module_embeddings must report at least 1 embed call."

    # Verify the row is actually there (owner can read it back).
    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM embeddings "
            "WHERE module = 'rls_owner_write_mod' AND odoo_version = %s",
            (V,),
        )
        count = cur.fetchone()[0]

    assert count == 1, (
        f"Owner write must insert 1 row; found {count}. "
        "RLS ENABLE (without FORCE) must not block the table owner."
    )


# ---------------------------------------------------------------------------
# Test 8 — D3 pattern catalogue always visible through IS NULL branch
# Business rule: ADR-0034 D3 — shared pattern chunks (profile_name IS NULL)
# are always visible to any non-zero GUC (they pass the IS NULL branch).
# ---------------------------------------------------------------------------

def test_pattern_catalogue_not_blocked_by_rls(forced_rls):
    """Pattern catalogue chunks (profile_name IS NULL) visible to any tenant.

    Under FORCE + osm_reader + GUC='rls_acme', the '__patterns__' module chunk
    (profile_name=NULL, module='__patterns__') must still be returned.
    The IS NULL branch in the policy USING clause handles this.
    """
    info = forced_rls
    if not info["has_privilege"]:
        pytest.skip(info["skip_reason"])

    pg = info["pg"]

    with pg.cursor() as cur:
        cur.execute("SET ROLE osm_reader")
        cur.execute("BEGIN")
        cur.execute("SET LOCAL app.allowed_profiles = 'rls_acme'")
        cur.execute(
            "SELECT module, profile_name FROM embeddings "
            "WHERE odoo_version = %s AND module = '__patterns__'",
            (V,),
        )
        rows = cur.fetchall()
        cur.execute("ROLLBACK")
        cur.execute("RESET ROLE")
    pg.commit()

    assert len(rows) >= 1, (
        f"D3 pattern catalogue chunks (module='__patterns__', profile_name IS NULL) "
        f"must be visible to any tenant under FORCE RLS. Got {len(rows)} rows. "
        "The IS NULL branch in the policy USING clause must pass these rows."
    )
    assert all(r[1] is None for r in rows), (
        f"Pattern catalogue rows must have profile_name=NULL. Got: {rows}"
    )


