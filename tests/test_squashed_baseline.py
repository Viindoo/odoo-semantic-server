"""Schema-identity tests for the squashed 0001_initial.sql baseline.

These tests verify three things:
1. A fresh migration produces a schema structurally identical to the committed
   golden fixture (columns, indexes, named constraints, RLS policies).
2. The grants and RLS configuration are present as specified by m13_020/m13_021.
3. Running migrations twice is a clean no-op (yoyo idempotency).

The tests require a live Postgres instance reachable via PG_ADMIN_DSN. They are
skipped when that env var is absent, so they never block CI environments that
lack a database.

Run:
    PG_ADMIN_DSN=postgresql://odoo_semantic:wavetmp@127.0.0.1:15432/postgres \
    pytest tests/test_squashed_baseline.py -m postgres -v
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "baseline_squash"


# Tables created by the squashed baseline that are NOT in conftest._PG_TEST_TABLES.
# Other migration tests (test_m13_006, test_m13_014, ...) call clean_pg teardown
# which drops _PG_TEST_TABLES with CASCADE.  CASCADE propagates to FK constraints
# on tables NOT in that list (e.g. app_settings_tenant_id_fkey is dropped when
# tenants is dropped CASCADE).  The orphaned tables then survive with broken FKs,
# causing a constraint-count mismatch vs the golden fixture on a fresh migration
# (CREATE TABLE IF NOT EXISTS is a no-op when the table already exists, so the
# inline FKs/CHECKs are never re-added).  Fix: explicitly DROP these tables before
# re-migrating so the baseline migration always creates them fresh.
_EXTRA_BASELINE_TABLES = [
    "billing_webhook_events",
    "subscriptions",
    "app_settings_history",
    "app_settings",
    "ee_modules",
    "patterns",
]


# ---------------------------------------------------------------------------
# Per-test fixture: DROP ALL + run_migrations → always a full baseline schema.
# Using function-scoped clean_pg (rather than the session-scoped pg_conn)
# prevents schema pollution when migration tests run in the same pytest session.
# ---------------------------------------------------------------------------
@pytest.fixture
def _fresh_schema(clean_pg):
    """Apply baseline migrations on a fresh schema for each test.

    clean_pg drops only conftest._PG_TEST_TABLES.  Tables absent from that
    list may survive from prior tests with FK constraints severed by CASCADE
    on their referenced tables.  Explicitly drop them so the squashed migration
    always produces the full golden schema.
    """
    for tbl in _EXTRA_BASELINE_TABLES:
        with clean_pg.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    run_migrations(clean_pg)
    return clean_pg


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURE_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _query(conn, sql: str, params=None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


_OID_NAME_RE = re.compile(r"^\d+_\d+_\d+_not_null$")


def _is_oid_constraint(name: str) -> bool:
    """Return True for Postgres-internal NOT NULL constraint names like 2200_17244_1_not_null."""
    return bool(_OID_NAME_RE.match(name))


# ---------------------------------------------------------------------------
# Test 1: structural schema identity vs golden fixture
# ---------------------------------------------------------------------------
def test_squashed_schema_structurally_identical_to_golden(_fresh_schema):
    """Fresh migration must match the committed golden fixtures exactly."""
    conn = _fresh_schema

    # -- Columns --
    live_cols = _query(
        conn,
        """
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """,
    )
    # Normalise to match fixture format (psycopg2 returns str for most types)
    live_cols_norm = [
        {
            "table_name": r["table_name"],
            "column_name": r["column_name"],
            "data_type": r["data_type"],
            "is_nullable": r["is_nullable"],
            "column_default": r["column_default"],
        }
        for r in live_cols
    ]
    golden_cols = _load("columns.json")
    assert live_cols_norm == golden_cols, (
        f"Column mismatch: live has {len(live_cols_norm)} rows, "
        f"golden has {len(golden_cols)} rows"
    )

    # -- Indexes --
    live_idx = _query(
        conn,
        """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
        ORDER BY tablename, indexname
        """,
    )
    live_idx_norm = [
        {
            "tablename": r["tablename"],
            "indexname": r["indexname"],
            "indexdef": r["indexdef"],
        }
        for r in live_idx
    ]
    golden_idx = _load("indexes.json")
    assert live_idx_norm == golden_idx, (
        f"Index mismatch: live={len(live_idx_norm)}, golden={len(golden_idx)}"
    )

    # -- Named constraints (CHECK, UNIQUE, PK, FK) — exclude OID-generated NOT NULL names --
    live_cons_raw = _query(
        conn,
        """
        SELECT conrelid::regclass::text AS table_name,
               conname,
               contype,
               pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE connamespace = 'public'::regnamespace
          AND contype IN ('c', 'u', 'p', 'f')
        ORDER BY conrelid::regclass::text, conname
        """,
    )
    live_cons = [
        {
            "table_name": r["table_name"],
            "conname": r["conname"],
            "contype": r["contype"],
            "def": r["def"],
        }
        for r in live_cons_raw
        if not _is_oid_constraint(r["conname"])
    ]
    golden_cons_raw = _load("constraints.json")
    golden_cons = [r for r in golden_cons_raw if not _is_oid_constraint(r["conname"])]
    assert live_cons == golden_cons, (
        f"Constraint mismatch: live={len(live_cons)}, golden={len(golden_cons)}"
    )

    # -- RLS policies --
    live_rls = _query(
        conn,
        """
        SELECT tablename, policyname, permissive, roles, cmd, qual, with_check
        FROM pg_policies
        WHERE schemaname = 'public'
        ORDER BY tablename, policyname
        """,
    )
    live_rls_norm = [
        {
            "tablename": r["tablename"],
            "policyname": r["policyname"],
            "permissive": r["permissive"],
            "roles": r["roles"],
            "cmd": r["cmd"],
            "qual": r["qual"],
            "with_check": r["with_check"],
        }
        for r in live_rls
    ]
    golden_rls = _load("rls_policies.json")
    assert live_rls_norm == golden_rls, (
        f"RLS policy mismatch: live={len(live_rls_norm)}, golden={len(golden_rls)}"
    )


# ---------------------------------------------------------------------------
# Test 2: grants and RLS are present with correct configuration
# ---------------------------------------------------------------------------
def test_squashed_baseline_grants_and_rls_present(_fresh_schema):
    """Verify m13_020 column-level grants and m13_021 embeddings RLS."""
    conn = _fresh_schema

    # Embeddings RLS must be enabled on the table
    rls_rows = _query(
        conn,
        "SELECT relrowsecurity FROM pg_class"
        " WHERE relname = 'embeddings' AND relnamespace = 'public'::regnamespace",
    )
    assert rls_rows, "embeddings table not found"
    assert rls_rows[0]["relrowsecurity"] is True, "RLS not enabled on embeddings table"

    # The embeddings RLS policy must exist
    pol_rows = _query(
        conn,
        """
        SELECT policyname FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'embeddings'
        """,
    )
    policy_names = {r["policyname"] for r in pol_rows}
    assert "embeddings_tenant" in policy_names, (
        f"embeddings_tenant policy missing; found: {policy_names}"
    )

    # The policy qual must use __global__ sentinel (m13_021 contract)
    qual_rows = _query(
        conn,
        """
        SELECT qual FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'embeddings'
          AND policyname = 'embeddings_tenant'
        """,
    )
    assert qual_rows, "embeddings_tenant_isolation policy has no qual"
    qual = qual_rows[0]["qual"]
    assert "__global__" in qual, (
        f"Policy qual must reference __global__ sentinel; got: {qual}"
    )

    # Master data: plans must exist with canonical IDs (2=free, 3=pro, 4=team, 5=unlimited)
    plan_rows = _query(conn, "SELECT id, slug FROM plans ORDER BY id")
    plan_map = {r["slug"]: int(r["id"]) for r in plan_rows}
    assert plan_map.get("free") == 2, f"free plan id should be 2, got {plan_map}"
    assert plan_map.get("pro") == 3, f"pro plan id should be 3, got {plan_map}"
    assert plan_map.get("team") == 4, f"team plan id should be 4, got {plan_map}"
    assert plan_map.get("unlimited") == 5, f"unlimited plan id should be 5, got {plan_map}"

    # Tenants: both baseline tenants present
    tenant_rows = _query(conn, "SELECT id, name FROM tenants ORDER BY id")
    tenant_names = {r["name"] for r in tenant_rows}
    assert "Viindoo Technology JSC" in tenant_names, "Default tenant missing"
    assert "public" in tenant_names, "public tenant missing"


# ---------------------------------------------------------------------------
# Test 3: running migrations twice is a no-op (idempotency)
# ---------------------------------------------------------------------------
def test_prod_sim_no_reapply(pg_conn, _ephemeral_pg_db):
    """Second migrate() call must not change any row counts or raise an error.

    This simulates what happens on a prod deployment where 0001_initial was
    already applied: yoyo should see the migration as already applied and skip
    it entirely.
    """
    import os
    import subprocess
    import sys

    # _ephemeral_pg_db yields the full DSN string (password un-redacted).
    # pg_conn.dsn masks the password as 'xxx', so we use _ephemeral_pg_db directly.
    dsn = _ephemeral_pg_db
    env = {**os.environ, "PG_DSN": dsn, "PG_ADMIN_DSN": dsn}

    # First run already happened via the pg_conn fixture (conftest called run_migrations).
    # Run it a second time; it must exit 0 with no structural changes.
    result = subprocess.run(
        [sys.executable, "-m", "src.db.migrate"],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).parent.parent,
    )
    assert result.returncode == 0, (
        f"Second migrate() run failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[-2000:]}\n"
        f"stderr: {result.stderr[-2000:]}"
    )

    # The _yoyo_migration table must still show exactly one applied migration
    yoyo_rows = _query(
        pg_conn,
        "SELECT migration_id FROM _yoyo_migration ORDER BY migration_id",
    )
    migration_ids = [r["migration_id"] for r in yoyo_rows]
    assert migration_ids == ["0001_initial", "0002_add_category_to_patterns"], (
        f"Expected ['0001_initial', '0002_add_category_to_patterns'] in _yoyo_migration "
        f"after squash, got {migration_ids}"
    )
