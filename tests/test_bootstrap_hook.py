# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/settings_registry.py — bootstrap_settings_safe() idempotency (ADR-0042).

Business intent (Option B — owner-only metadata convergence):
  B1  First run inserts all catalogue rows.
  B2  Second run inserts zero rows (idempotent — return value = inserted count).
  B3  converge_metadata=True (owner/webui path): admin-modified value is NOT
      reset on re-bootstrap, AND stale registry metadata on an existing row is
      RE-SYNCED from the catalogue (ON CONFLICT DO UPDATE propagates metadata
      while preserving value_json + not bumping updated_at).
  B3b converge_metadata=False (default reader/MCP path): an existing row is left
      ENTIRELY unchanged (ON CONFLICT DO NOTHING) — stale metadata and value
      both untouched, inserted==0.
  B4  bootstrap_settings_safe() swallows DB exception (non-blocking startup).
  B5  bootstrap_settings_safe() logs honestly in both modes; the flag is threaded
      (webui=True converges, MCP=False insert-missing-only).

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Shared fixture: schema + clean app_settings
# ---------------------------------------------------------------------------


@pytest.fixture
def bootstrap_db(pg_conn):
    """Apply migrations and wipe app_settings before each test.

    Returns the raw psycopg2 connection for direct assertions.
    """
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM app_settings")

    yield pg_conn

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM app_settings")


# ---------------------------------------------------------------------------
# B1: First run inserts all settings
# ---------------------------------------------------------------------------


def test_first_run_inserts_all_settings(bootstrap_db):
    """register_settings_idempotent inserts exactly len(SETTINGS_CATALOGUE) rows."""
    from src.settings_registry import SETTINGS_CATALOGUE, register_settings_idempotent

    bootstrap_db.autocommit = False
    inserted = register_settings_idempotent(bootstrap_db)
    bootstrap_db.autocommit = True

    assert inserted == len(SETTINGS_CATALOGUE)

    with bootstrap_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM app_settings")
        count = cur.fetchone()[0]
    assert count == len(SETTINGS_CATALOGUE)


# ---------------------------------------------------------------------------
# B2: Second run inserts zero (idempotent)
# ---------------------------------------------------------------------------


def test_second_run_inserts_zero(bootstrap_db):
    """A second register_settings_idempotent call inserts 0 rows."""
    from src.settings_registry import register_settings_idempotent

    bootstrap_db.autocommit = False
    register_settings_idempotent(bootstrap_db)
    second = register_settings_idempotent(bootstrap_db)
    bootstrap_db.autocommit = True

    assert second == 0


# ---------------------------------------------------------------------------
# B3: converge_metadata=True — admin value preserved AND stale metadata re-synced
# ---------------------------------------------------------------------------


def _seed_stale_row(cur, key: str, admin_value: int):
    """Insert a pre-existing row with a custom admin value AND stale metadata."""
    import json

    cur.execute(
        """
        INSERT INTO app_settings (
            key, value_json, category, scope, data_type,
            validation_json, default_value, requires_restart,
            requires_reseed, is_secret, description
        ) VALUES (%s, %s::jsonb, 'mcp', 'system', 'duration_seconds',
                  '{}'::jsonb, %s::jsonb, false, false, false, 'stale')
        """,
        (key, json.dumps({"v": admin_value}), json.dumps({"v": 300})),
    )


def test_converge_true_admin_value_preserved_metadata_resynced(bootstrap_db):
    """converge=True: re-bootstrap preserves the admin value AND re-syncs metadata.

    A row that already exists with STALE metadata (here requires_restart=false,
    which the catalogue now says must be true) must converge to the catalogue
    metadata on the next bootstrap, while the admin-tuned value_json is left
    untouched and updated_at is NOT bumped.
    """
    from src.settings_registry import register_settings_idempotent

    # This key's catalogue requires_restart is True (PR #334).
    key = "mcp.resource_cache_ttl_seconds"
    admin_value = 600  # ≠ catalogue default 300

    bootstrap_db.autocommit = False

    with bootstrap_db.cursor() as cur:
        _seed_stale_row(cur, key, admin_value)
        # Capture updated_at to prove a metadata re-sync does NOT bump it.
        cur.execute("SELECT updated_at FROM app_settings WHERE key = %s", (key,))
        updated_at_before = cur.fetchone()[0]

    # Re-bootstrap with convergence — must re-sync metadata but preserve value.
    inserted = register_settings_idempotent(bootstrap_db, converge_metadata=True)
    bootstrap_db.autocommit = True

    # The row pre-existed → it is NOT counted as inserted.
    assert inserted == 0, f"existing row must not be counted as inserted, got {inserted}"

    with bootstrap_db.cursor() as cur:
        cur.execute(
            "SELECT value_json, requires_restart, description, updated_at "
            "FROM app_settings WHERE key = %s",
            (key,),
        )
        stored, requires_restart, description, updated_at_after = cur.fetchone()

    # value_json is the admin's value, untouched (psycopg2 returns JSONB as dict).
    assert stored.get("v") == admin_value, "admin value_json must be preserved"
    # Metadata re-synced from the catalogue.
    assert requires_restart is True, "stale requires_restart must re-sync to catalogue True"
    assert description == "MCP odoo:// resource cache TTL.", "description must re-sync"
    # A metadata convergence is a system op, not a user edit → updated_at unchanged.
    assert updated_at_after == updated_at_before, "metadata re-sync must not bump updated_at"


# ---------------------------------------------------------------------------
# B3b: converge_metadata=False (default) — existing row left ENTIRELY unchanged
# ---------------------------------------------------------------------------


def test_converge_false_leaves_existing_row_unchanged(bootstrap_db):
    """converge=False (reader/MCP path): DO NOTHING leaves a stale existing row as-is.

    The MCP server runs as osm_reader (no UPDATE on app_settings), so the
    default path must NOT touch an existing row — neither its stale metadata nor
    its value. Insert count is 0.
    """
    from src.settings_registry import register_settings_idempotent

    key = "mcp.resource_cache_ttl_seconds"
    admin_value = 600

    bootstrap_db.autocommit = False

    with bootstrap_db.cursor() as cur:
        _seed_stale_row(cur, key, admin_value)
        cur.execute("SELECT updated_at FROM app_settings WHERE key = %s", (key,))
        updated_at_before = cur.fetchone()[0]

    # Default path (converge_metadata=False) — DO NOTHING.
    inserted = register_settings_idempotent(bootstrap_db)  # no converge flag
    bootstrap_db.autocommit = True

    assert inserted == 0, f"existing row must not be counted as inserted, got {inserted}"

    with bootstrap_db.cursor() as cur:
        cur.execute(
            "SELECT value_json, requires_restart, description, updated_at "
            "FROM app_settings WHERE key = %s",
            (key,),
        )
        stored, requires_restart, description, updated_at_after = cur.fetchone()

    # NOTHING changed — stale metadata stays stale, value stays, updated_at stable.
    assert stored.get("v") == admin_value, "value must be untouched (DO NOTHING)"
    assert requires_restart is False, "stale metadata must NOT converge on the reader path"
    assert description == "stale", "stale description must NOT converge on the reader path"
    assert updated_at_after == updated_at_before, "DO NOTHING must not bump updated_at"


def test_idempotent_convergence_second_run_noop(bootstrap_db):
    """After convergence, a third bootstrap inserts 0 and leaves metadata stable."""
    from src.settings_registry import register_settings_idempotent

    bootstrap_db.autocommit = False
    register_settings_idempotent(bootstrap_db, converge_metadata=True)  # fresh: inserts all
    second = register_settings_idempotent(bootstrap_db, converge_metadata=True)  # converged: 0
    bootstrap_db.autocommit = True

    assert second == 0

    key = "mcp.resource_cache_ttl_seconds"
    with bootstrap_db.cursor() as cur:
        cur.execute("SELECT requires_restart FROM app_settings WHERE key = %s", (key,))
        assert cur.fetchone()[0] is True


# ---------------------------------------------------------------------------
# B4: bootstrap_settings_safe swallows exception (non-blocking startup)
# ---------------------------------------------------------------------------


def test_bootstrap_safe_swallows_exception(bootstrap_db, monkeypatch):
    """bootstrap_settings_safe() does not propagate exceptions from the DB layer."""
    import src.settings_registry as _reg

    def _explode():
        raise RuntimeError("simulated pool failure")

    # Patch get_pool to raise — simulates DB unavailable at startup
    monkeypatch.setattr(
        "src.settings_registry.bootstrap_settings_safe",
        lambda: _explode(),
    )

    # Calling the real bootstrap_settings_safe after patching inner helper
    # We need to patch at the right level — test that wrapper catches arbitrary exc
    # Restore real function, then patch pool
    monkeypatch.undo()

    import src.db.pg as _pg_mod

    def _bad_pool():
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(_pg_mod, "get_pool", _bad_pool)

    # Must not raise — swallows silently
    _reg.bootstrap_settings_safe()  # no exception expected


# ---------------------------------------------------------------------------
# B5: bootstrap_settings_safe logs honestly + threads the converge flag
# ---------------------------------------------------------------------------


def test_converge_true_logs_inserted_and_resynced_counts(bootstrap_db, caplog):
    """webui path (converge=True): logs both inserted and re-synced counts honestly."""
    from src.settings_registry import bootstrap_settings_safe

    # Ensure pool points at test DB (already done by pg_conn fixture)
    with caplog.at_level(logging.INFO, logger="src.settings_registry"):
        bootstrap_settings_safe(converge_metadata=True)

    bootstrap_msgs = [r.message for r in caplog.records if "Settings bootstrap" in r.message]
    assert len(bootstrap_msgs) >= 1
    msg = bootstrap_msgs[0]
    # The message must report BOTH inserts and metadata re-syncs (honest counting).
    assert "inserted" in msg
    assert "re-synced" in msg


def test_converge_false_logs_insert_only(bootstrap_db, caplog):
    """default/MCP path (converge=False): logs insert-missing-only, no re-sync claim."""
    from src.settings_registry import bootstrap_settings_safe

    with caplog.at_level(logging.INFO, logger="src.settings_registry"):
        bootstrap_settings_safe()  # default converge_metadata=False

    bootstrap_msgs = [r.message for r in caplog.records if "Settings bootstrap" in r.message]
    assert len(bootstrap_msgs) >= 1
    msg = bootstrap_msgs[0]
    assert "inserted" in msg
    # The reader path does NOT converge, so it must not claim a metadata re-sync.
    assert "re-synced" not in msg
    assert "insert-missing-only" in msg
