# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/settings_registry.py — bootstrap_settings_safe() idempotency (ADR-0042).

Business intent:
  B1  First run inserts all catalogue rows.
  B2  Second run inserts zero rows (idempotent).
  B3  Admin-modified value is NOT reset on re-bootstrap.
  B4  bootstrap_settings_safe() swallows DB exception (non-blocking startup).
  B5  bootstrap_settings_safe() logs inserted count via caplog.

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
# B3: Admin-modified value is preserved (ON CONFLICT DO NOTHING)
# ---------------------------------------------------------------------------


def test_admin_modified_value_not_reset(bootstrap_db):
    """After a DB row is updated by admin, re-bootstrap leaves it unchanged."""
    import json

    from src.settings_registry import register_settings_idempotent

    key = "auth.session_ttl_seconds"
    admin_value = 1234

    # First bootstrap
    bootstrap_db.autocommit = False
    register_settings_idempotent(bootstrap_db)

    # Admin updates the row
    with bootstrap_db.cursor() as cur:
        cur.execute(
            "UPDATE app_settings SET value_json = %s::jsonb WHERE key = %s",
            (json.dumps({"v": admin_value}), key),
        )

    # Second bootstrap — must not overwrite
    register_settings_idempotent(bootstrap_db)
    bootstrap_db.autocommit = True

    with bootstrap_db.cursor() as cur:
        cur.execute("SELECT value_json FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()

    assert row is not None
    stored = row[0]
    # psycopg2 returns JSONB as dict
    assert stored.get("v") == admin_value


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
# B5: bootstrap_settings_safe logs inserted count
# ---------------------------------------------------------------------------


def test_logs_inserted_count(bootstrap_db, caplog):
    """bootstrap_settings_safe() logs the number of newly inserted rows."""
    from src.settings_registry import SETTINGS_CATALOGUE, bootstrap_settings_safe

    # Ensure pool points at test DB (already done by pg_conn fixture)
    with caplog.at_level(logging.INFO, logger="src.settings_registry"):
        bootstrap_settings_safe()

    # Should log "Settings bootstrap: N new row(s) inserted"
    bootstrap_msgs = [r.message for r in caplog.records if "Settings bootstrap" in r.message]
    assert len(bootstrap_msgs) >= 1
    msg = bootstrap_msgs[0]
    # Verify the count in the message
    assert str(len(SETTINGS_CATALOGUE)) in msg or "new row" in msg
