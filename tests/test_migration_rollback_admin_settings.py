# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migration_rollback_admin_settings.py
"""Round-trip rollback tests for m13_010 / m13_011 / m13_012 (Admin Settings).

Verifies that each migration can be:
  1. Applied cleanly (up)
  2. Rolled back (table drop + yoyo log purge — simulates "down")
  3. Re-applied cleanly (up again), producing identical final state

Six test functions (ADR-0041):
  test_m13_010_round_trip_up_down_up
  test_m13_011_round_trip_up_down_up
  test_m13_012_round_trip_up_down_up
  test_all_3_apply_clean_on_empty_db
  test_idempotent_re_apply
  test_combined_state_invariants_after_all_3

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Migration IDs — used to purge yoyo log so re-apply picks them up
# ---------------------------------------------------------------------------

_M13_MIGRATION_IDS = (
    "m13_010_app_settings",
    "m13_011_ee_modules",
    "m13_012_patterns",
)

# ---------------------------------------------------------------------------
# Drop helpers — simulate "rollback" (yoyo has no rollback scripts for these)
# ---------------------------------------------------------------------------


def _purge_yoyo_entries(conn, migration_ids: tuple) -> None:
    """Delete rows from _yoyo_migration for the given IDs, if the table exists.

    clean_pg drops _yoyo_migration entirely before each test, so on the first
    call inside admin_settings_pg setup the table may not exist yet.  We use a
    conditional DO block so the delete is a no-op when yoyo hasn't run yet
    (table absent) and removes the recorded entries when it has (mid-round-trip).
    """
    ids_list = list(migration_ids)
    with conn.cursor() as cur:
        cur.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                     WHERE table_name = '_yoyo_migration'
                ) THEN
                    DELETE FROM _yoyo_migration
                     WHERE migration_id = ANY(%s::text[]);
                END IF;
            END $$
            """,
            (ids_list,),
        )
    conn.commit()


def _drop_all_admin_settings_tables(conn) -> None:
    """Drop the three Admin Settings tables + purge their yoyo log entries.

    Order respects FK constraints:
      app_settings_history  — no outbound FKs from other tables referencing it
      app_settings          — no outbound FKs after history is gone
      ee_modules            — standalone
      patterns              — standalone

    After dropping tables we purge the corresponding _yoyo_migration rows so
    that run_migrations() treats these three migrations as pending again and
    re-executes them.  The other migrations remain recorded so yoyo skips them.

    _yoyo_migration may be absent on the first call (clean_pg dropped it);
    _purge_yoyo_entries() handles that gracefully.
    """
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS app_settings_history CASCADE")
        cur.execute("DROP TABLE IF EXISTS app_settings CASCADE")
        cur.execute("DROP TABLE IF EXISTS ee_modules CASCADE")
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")
    conn.commit()
    _purge_yoyo_entries(conn, _M13_MIGRATION_IDS)


def _drop_m13_010_tables(conn) -> None:
    """Drop only m13_010 tables + purge that migration's yoyo entry."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS app_settings_history CASCADE")
        cur.execute("DROP TABLE IF EXISTS app_settings CASCADE")
    conn.commit()
    _purge_yoyo_entries(conn, ("m13_010_app_settings",))


def _drop_m13_011_tables(conn) -> None:
    """Drop only m13_011 tables + purge that migration's yoyo entry."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ee_modules CASCADE")
    conn.commit()
    _purge_yoyo_entries(conn, ("m13_011_ee_modules",))


def _drop_m13_012_tables(conn) -> None:
    """Drop only m13_012 tables + purge that migration's yoyo entry."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")
    conn.commit()
    _purge_yoyo_entries(conn, ("m13_012_patterns",))


# ---------------------------------------------------------------------------
# State assertion helpers
# ---------------------------------------------------------------------------


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
            (table,),
        )
        return cur.fetchone() is not None


def _count_rows(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]


def _index_exists(conn, index_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s",
            (index_name,),
        )
        return cur.fetchone() is not None


def _check_constraint_exists(conn, constraint_name: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM pg_constraint
             WHERE conname = %s
               AND conrelid = %s::regclass
            """,
            (constraint_name, table),
        )
        return cur.fetchone() is not None


def _count_indexes_on_table(conn, table: str) -> int:
    """Return the number of indexes (including PK) on a table."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM pg_indexes WHERE tablename = %s",
            (table,),
        )
        return cur.fetchone()[0]


def _assert_m13_010_state(conn) -> None:
    """Assert app_settings + app_settings_history exist with expected structure."""
    assert _table_exists(conn, "app_settings"), "app_settings missing after apply"
    assert _table_exists(conn, "app_settings_history"), (
        "app_settings_history missing after apply"
    )
    # Partial unique indexes (5 indexes: PK + 2 partial unique + 2 btree)
    for idx in (
        "uq_app_settings_system_key",
        "uq_app_settings_tenant_key",
        "uq_app_settings_per_key",
        "idx_app_settings_category",
        "idx_app_settings_scope_tenant",
    ):
        assert _index_exists(conn, idx), f"Index {idx!r} missing after m13_010 apply"
    # History index
    assert _index_exists(conn, "idx_app_settings_history_key_time"), (
        "idx_app_settings_history_key_time missing after m13_010 apply"
    )
    # Scope-consistency CHECK constraint
    assert _check_constraint_exists(
        conn, "app_settings_tenant_scope_consistency", "app_settings"
    ), "app_settings_tenant_scope_consistency CHECK constraint missing"


def _assert_m13_011_state(conn) -> None:
    """Assert ee_modules exists with 16 backfilled rows and expected index."""
    assert _table_exists(conn, "ee_modules"), "ee_modules missing after apply"
    assert _index_exists(conn, "idx_ee_modules_name"), (
        "idx_ee_modules_name missing after m13_011 apply"
    )
    count = _count_rows(conn, "ee_modules")
    assert count == 16, (
        f"ee_modules expected 16 backfilled rows, got {count}"
    )


def _assert_m13_012_state(conn) -> None:
    """Assert patterns table exists with expected indexes (0 rows — backfill is separate)."""
    assert _table_exists(conn, "patterns"), "patterns missing after apply"
    for idx in (
        "idx_patterns_intent_keywords_gin",
        "idx_patterns_language",
        "idx_patterns_version_min",
    ):
        assert _index_exists(conn, idx), f"Index {idx!r} missing after m13_012 apply"


# ---------------------------------------------------------------------------
# Fixture: clean DB with all three Admin Settings tables pre-dropped.
# Builds on clean_pg (yoyo log is already wiped by clean_pg).
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_settings_pg(clean_pg):
    """Drop Admin Settings tables before + after, so run_migrations sees them pending."""
    _drop_all_admin_settings_tables(clean_pg)
    yield clean_pg
    _drop_all_admin_settings_tables(clean_pg)


# ---------------------------------------------------------------------------
# Test 1 — m13_010 round-trip up → down → up
# ---------------------------------------------------------------------------


def test_m13_010_round_trip_up_down_up(admin_settings_pg):
    """m13_010 apply → drop (simulate rollback) → re-apply produces same state."""
    conn = admin_settings_pg

    # --- First apply ---
    run_migrations(conn)
    _assert_m13_010_state(conn)

    # --- Simulate rollback: drop tables + purge yoyo log ---
    _drop_m13_010_tables(conn)
    assert not _table_exists(conn, "app_settings"), (
        "app_settings must be absent after simulated rollback"
    )
    assert not _table_exists(conn, "app_settings_history"), (
        "app_settings_history must be absent after simulated rollback"
    )

    # --- Re-apply ---
    run_migrations(conn)
    _assert_m13_010_state(conn)


# ---------------------------------------------------------------------------
# Test 2 — m13_011 round-trip up → down → up
# ---------------------------------------------------------------------------


def test_m13_011_round_trip_up_down_up(admin_settings_pg):
    """m13_011 apply → drop (simulate rollback) → re-apply produces same state (16 rows)."""
    conn = admin_settings_pg

    # --- First apply ---
    run_migrations(conn)
    _assert_m13_011_state(conn)

    # --- Simulate rollback ---
    _drop_m13_011_tables(conn)
    assert not _table_exists(conn, "ee_modules"), (
        "ee_modules must be absent after simulated rollback"
    )

    # --- Re-apply ---
    run_migrations(conn)
    _assert_m13_011_state(conn)


# ---------------------------------------------------------------------------
# Test 3 — m13_012 round-trip up → down → up
# ---------------------------------------------------------------------------


def test_m13_012_round_trip_up_down_up(admin_settings_pg):
    """m13_012 apply → drop (simulate rollback) → re-apply produces same state."""
    conn = admin_settings_pg

    # --- First apply ---
    run_migrations(conn)
    _assert_m13_012_state(conn)

    # --- Simulate rollback ---
    _drop_m13_012_tables(conn)
    assert not _table_exists(conn, "patterns"), (
        "patterns must be absent after simulated rollback"
    )

    # --- Re-apply ---
    run_migrations(conn)
    _assert_m13_012_state(conn)


# ---------------------------------------------------------------------------
# Test 4 — All 3 apply clean on empty DB
# ---------------------------------------------------------------------------


def test_all_3_apply_clean_on_empty_db(admin_settings_pg):
    """Applying all three migrations on a completely empty DB must produce correct state.

    admin_settings_pg fixture guarantees all three tables + their yoyo entries
    are absent before this test runs, simulating a fully fresh database.
    """
    conn = admin_settings_pg

    # Precondition: tables absent
    for tbl in ("app_settings", "app_settings_history", "ee_modules", "patterns"):
        assert not _table_exists(conn, tbl), (
            f"{tbl!r} should not exist before fresh apply"
        )

    run_migrations(conn)

    # All three migrations must have produced their tables
    _assert_m13_010_state(conn)
    _assert_m13_011_state(conn)
    _assert_m13_012_state(conn)


# ---------------------------------------------------------------------------
# Test 5 — Idempotent re-apply (apply twice, no error, no duplicate rows)
# ---------------------------------------------------------------------------


def test_idempotent_re_apply(admin_settings_pg):
    """Calling run_migrations() twice must not raise and must not create duplicate rows.

    yoyo tracks applied migrations, so the second call skips already-applied ones.
    The ON CONFLICT DO NOTHING in m13_011's INSERT means the row count stays at 16.
    """
    conn = admin_settings_pg

    # First apply
    run_migrations(conn)
    ee_count_first = _count_rows(conn, "ee_modules")

    # Second apply — must not raise
    try:
        run_migrations(conn)
    except Exception as exc:
        pytest.fail(f"run_migrations raised on second call: {exc}")

    # Row counts must be unchanged
    ee_count_second = _count_rows(conn, "ee_modules")
    assert ee_count_second == ee_count_first, (
        f"ee_modules row count changed after idempotent re-apply: "
        f"{ee_count_first} → {ee_count_second}"
    )

    app_count = _count_rows(conn, "app_settings")
    assert app_count == 0, (
        f"app_settings must have 0 rows after migration (bootstrap_settings_safe is "
        f"separate); got {app_count}"
    )

    patterns_count = _count_rows(conn, "patterns")
    assert patterns_count == 0, (
        f"patterns must have 0 rows after migration (backfill_patterns.py is separate); "
        f"got {patterns_count}"
    )


# ---------------------------------------------------------------------------
# Test 6 — Combined state invariants after all three migrations
# ---------------------------------------------------------------------------


def test_combined_state_invariants_after_all_3(admin_settings_pg):
    """After all three migrations: verify row counts, FKs valid, constraints, indexes.

    Invariants (ADR-0041 + migration comments):
      - app_settings:         0 rows  (bootstrap_settings_safe() is NOT part of migration)
      - app_settings_history: 0 rows  (populated at runtime)
      - ee_modules:           16 rows (backfilled by m13_011 INSERT ... ON CONFLICT DO NOTHING)
      - patterns:             0 rows  (backfill_patterns.py runs separately — not in migration)
    """
    conn = admin_settings_pg
    run_migrations(conn)

    # --- Row count invariants ---
    app_count = _count_rows(conn, "app_settings")
    assert app_count == 0, (
        f"app_settings must have 0 rows after migration (no default rows seeded by SQL); "
        f"got {app_count}"
    )

    history_count = _count_rows(conn, "app_settings_history")
    assert history_count == 0, (
        f"app_settings_history must have 0 rows after migration; got {history_count}"
    )

    ee_count = _count_rows(conn, "ee_modules")
    assert ee_count == 16, (
        f"ee_modules must have exactly 16 rows from m13_011 backfill; got {ee_count}"
    )

    patterns_count = _count_rows(conn, "patterns")
    assert patterns_count == 0, (
        f"patterns must have 0 rows after migration (ops/backfill_patterns.py is separate); "
        f"got {patterns_count}"
    )

    # --- FK integrity: all ee_modules.updated_by values must be valid (NULL = OK) ---
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM ee_modules e
             WHERE e.updated_by IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM webui_users u WHERE u.id = e.updated_by
               )
            """
        )
        fk_violations = cur.fetchone()[0]
    assert fk_violations == 0, (
        f"ee_modules.updated_by has {fk_violations} FK violations (dangling non-NULL refs)"
    )

    # --- CHECK constraint on app_settings exists ---
    assert _check_constraint_exists(
        conn, "app_settings_tenant_scope_consistency", "app_settings"
    ), "app_settings_tenant_scope_consistency CHECK constraint missing"

    # --- Index count regression guard ---
    # app_settings: PK + 3 partial unique + 2 btree = 6 indexes
    app_idx_count = _count_indexes_on_table(conn, "app_settings")
    assert app_idx_count >= 6, (
        f"app_settings should have >= 6 indexes (PK + 3 partial unique + 2 btree); "
        f"found {app_idx_count}"
    )

    # app_settings_history: PK + 1 btree = 2 indexes
    hist_idx_count = _count_indexes_on_table(conn, "app_settings_history")
    assert hist_idx_count >= 2, (
        f"app_settings_history should have >= 2 indexes (PK + key_time); "
        f"found {hist_idx_count}"
    )

    # ee_modules: PK + 1 name index = 2 indexes minimum
    ee_idx_count = _count_indexes_on_table(conn, "ee_modules")
    assert ee_idx_count >= 2, (
        f"ee_modules should have >= 2 indexes (PK + name); found {ee_idx_count}"
    )

    # patterns: PK + 3 indexes (GIN + 2 btree) = 4 indexes minimum
    patterns_idx_count = _count_indexes_on_table(conn, "patterns")
    assert patterns_idx_count >= 4, (
        f"patterns should have >= 4 indexes (PK + GIN + 2 btree); "
        f"found {patterns_idx_count}"
    )
