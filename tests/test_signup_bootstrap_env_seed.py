# SPDX-License-Identifier: AGPL-3.0-or-later
"""ISSUE-2 regression guard: signup.enabled seed value respects SIGNUP_ENABLED env.

Business intent (solution-design.md ISSUE-2 / Option B):
  On a FRESH install (no app_settings row yet) the bootstrap should capture the
  operator's deploy-time SIGNUP_ENABLED env var rather than always seeding
  {"v": false}.  Four invariants must hold:

  E1  SIGNUP_ENABLED=1 + no row  ->  seeded value_json = {"v": true}
  E2  SIGNUP_ENABLED absent + no row  ->  seeded value_json = {"v": false}
  E3  Row already exists ({"v": false}) + SIGNUP_ENABLED=1  ->
        ON CONFLICT DO NOTHING -- row stays false (existing-deploy safety)
  E4  default_value column always stores {"v": false} (catalogue default,
        reset-to-default stays invite-only) regardless of env

These tests are UNIT/mock -- they do NOT require a live PostgreSQL connection,
so they run under ``make test`` (no docker / PG_TEST_DSN needed).

The tests mock the DB cursor/connection at the psycopg2 boundary and assert
what value_json was passed to the INSERT statement, which is the SSOT for what
actually gets persisted.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn_mock(rowcount: int = 1):
    """Return a fake psycopg2 connection whose cursor tracks execute() calls."""
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _seed_value_for(cur: MagicMock, key: str) -> Any:
    """Extract the value_json parameter passed to INSERT for a given key.

    The INSERT is called as:
        cur.execute(sql, (sdef.key, value_json, ...))
    where value_json is arg index 1 (0-indexed).
    """
    for c in cur.execute.call_args_list:
        args = c.args  # positional args tuple: (sql, params_tuple)
        if len(args) >= 2:
            params = args[1]
            if isinstance(params, (list, tuple)) and len(params) >= 2:
                if params[0] == key:
                    raw = params[1]
                    # raw is the JSON string passed to psycopg2
                    return json.loads(raw)
    return None  # key not found in calls


def _default_value_for(cur: MagicMock, key: str) -> Any:
    """Extract the default_value (6th param, index 5) passed to INSERT for a given key."""
    for c in cur.execute.call_args_list:
        args = c.args
        if len(args) >= 2:
            params = args[1]
            if isinstance(params, (list, tuple)) and len(params) >= 6:
                if params[0] == key:
                    raw = params[5]
                    return json.loads(raw)
    return None


# ---------------------------------------------------------------------------
# E1: SIGNUP_ENABLED=1 seeds value_json = {"v": true}
# ---------------------------------------------------------------------------


def test_env_true_seeds_true(monkeypatch):
    """E1: fresh install with SIGNUP_ENABLED=1 -> signup.enabled seeded true."""
    monkeypatch.setenv("SIGNUP_ENABLED", "1")

    from src.settings_registry import register_settings_idempotent

    conn, cur = _make_conn_mock(rowcount=1)

    register_settings_idempotent(conn)

    seeded = _seed_value_for(cur, "signup.enabled")
    assert seeded is not None, "No INSERT call found for signup.enabled"
    assert seeded == {"v": True}, (
        f"Expected seed value {{\"v\": true}} when SIGNUP_ENABLED=1, got {seeded}"
    )


def test_env_true_variants(monkeypatch):
    """E1 variants: 'true' and 'yes' also seed true."""
    from src.settings_registry import register_settings_idempotent

    for val in ("true", "yes", "TRUE", "YES", "True", "Yes"):
        monkeypatch.setenv("SIGNUP_ENABLED", val)
        conn, cur = _make_conn_mock(rowcount=1)
        register_settings_idempotent(conn)
        seeded = _seed_value_for(cur, "signup.enabled")
        assert seeded == {"v": True}, (
            f"SIGNUP_ENABLED={val!r} should seed true, got {seeded}"
        )


# ---------------------------------------------------------------------------
# E2: SIGNUP_ENABLED absent (or "0"/"false") seeds value_json = {"v": false}
# ---------------------------------------------------------------------------


def test_env_absent_seeds_false(monkeypatch):
    """E2: SIGNUP_ENABLED not set -> signup.enabled seeded false (invite-only default)."""
    monkeypatch.delenv("SIGNUP_ENABLED", raising=False)

    from src.settings_registry import register_settings_idempotent

    conn, cur = _make_conn_mock(rowcount=1)

    register_settings_idempotent(conn)

    seeded = _seed_value_for(cur, "signup.enabled")
    assert seeded is not None, "No INSERT call found for signup.enabled"
    assert seeded == {"v": False}, (
        f"Expected seed value {{\"v\": false}} when env absent, got {seeded}"
    )


def test_env_zero_seeds_false(monkeypatch):
    """E2 variant: SIGNUP_ENABLED=0 seeds false."""
    monkeypatch.setenv("SIGNUP_ENABLED", "0")

    from src.settings_registry import register_settings_idempotent

    conn, cur = _make_conn_mock(rowcount=1)

    register_settings_idempotent(conn)

    seeded = _seed_value_for(cur, "signup.enabled")
    assert seeded == {"v": False}, (
        f"Expected {{\"v\": false}} for SIGNUP_ENABLED=0, got {seeded}"
    )


def test_env_false_string_seeds_false(monkeypatch):
    """E2 variant: SIGNUP_ENABLED=false seeds false."""
    monkeypatch.setenv("SIGNUP_ENABLED", "false")

    from src.settings_registry import register_settings_idempotent

    conn, cur = _make_conn_mock(rowcount=1)

    register_settings_idempotent(conn)

    seeded = _seed_value_for(cur, "signup.enabled")
    assert seeded == {"v": False}


# ---------------------------------------------------------------------------
# E3: Row already exists -> ON CONFLICT DO NOTHING (rowcount=0, value unchanged)
# ---------------------------------------------------------------------------


def test_existing_row_not_overwritten(monkeypatch):
    """E3: pre-existing row + SIGNUP_ENABLED=1 -> ON CONFLICT DO NOTHING, row stays false.

    We simulate this by returning rowcount=0 from the cursor (meaning the
    INSERT conflicted and was skipped).  The key assertion is that the
    return value of register_settings_idempotent is 0 for signup.enabled
    (no row inserted), proving ON CONFLICT DO NOTHING fired.
    """
    monkeypatch.setenv("SIGNUP_ENABLED", "1")

    from src.settings_registry import register_settings_idempotent

    # rowcount=0 simulates ON CONFLICT DO NOTHING
    conn, cur = _make_conn_mock(rowcount=0)

    inserted = register_settings_idempotent(conn)

    # None inserted (all 29 conflict-skipped when rowcount=0)
    assert inserted == 0, (
        f"Expected 0 rows inserted when all conflict, got {inserted}"
    )

    # The INSERT was still CALLED with the env-derived value (true) — the DB
    # itself decides to skip via ON CONFLICT DO NOTHING, not the Python layer.
    # This verifies we do NOT short-circuit before attempting the INSERT.
    seeded = _seed_value_for(cur, "signup.enabled")
    assert seeded == {"v": True}, (
        "Even on conflict, the attempted INSERT should carry the env-seeded value"
    )


# ---------------------------------------------------------------------------
# E4: default_value column always = {"v": false} regardless of env
# ---------------------------------------------------------------------------


def test_default_value_column_always_false(monkeypatch):
    """E4: default_value (catalogue default) is always {"v": false} even when env=1.

    The default_value column is the source of truth for 'reset to default'
    in the admin UI.  It must always be the catalogue default (invite-only),
    not the operator's env choice.
    """
    monkeypatch.setenv("SIGNUP_ENABLED", "1")

    from src.settings_registry import register_settings_idempotent

    conn, cur = _make_conn_mock(rowcount=1)

    register_settings_idempotent(conn)

    default = _default_value_for(cur, "signup.enabled")
    assert default is not None, "No INSERT call found for signup.enabled"
    assert default == {"v": False}, (
        f"default_value column must always be {{\"v\": false}}, got {default}"
    )


# ---------------------------------------------------------------------------
# E5: _env_seed_signup_enabled helper unit test
# ---------------------------------------------------------------------------


def test_env_seed_helper_true(monkeypatch):
    """_env_seed_signup_enabled returns True for canonical truthy values."""
    from src.settings_registry import _env_seed_signup_enabled

    for val in ("1", "true", "yes", "TRUE", "YES"):
        monkeypatch.setenv("SIGNUP_ENABLED", val)
        assert _env_seed_signup_enabled() is True, f"Expected True for SIGNUP_ENABLED={val!r}"


def test_env_seed_helper_false(monkeypatch):
    """_env_seed_signup_enabled returns False for falsy/absent values."""
    from src.settings_registry import _env_seed_signup_enabled

    for val in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("SIGNUP_ENABLED", val)
        assert _env_seed_signup_enabled() is False, f"Expected False for SIGNUP_ENABLED={val!r}"

    monkeypatch.delenv("SIGNUP_ENABLED", raising=False)
    assert _env_seed_signup_enabled() is False, "Expected False when SIGNUP_ENABLED absent"


# ---------------------------------------------------------------------------
# E6: Other settings are NOT affected by env-seed logic (no side effects)
# ---------------------------------------------------------------------------


def test_other_settings_use_catalogue_default(monkeypatch):
    """Other settings seed from their catalogue default regardless of env vars."""
    monkeypatch.setenv("SIGNUP_ENABLED", "1")

    from src.settings_registry import register_settings_idempotent

    conn, cur = _make_conn_mock(rowcount=1)

    register_settings_idempotent(conn)

    # auth.session_ttl_seconds default is 28800 (int)
    session_ttl = _seed_value_for(cur, "auth.session_ttl_seconds")
    assert session_ttl == {"v": 28800}, (
        f"auth.session_ttl_seconds should use catalogue default 28800, got {session_ttl}"
    )

    # auth.mfa_grace_period_days default is 7
    mfa_grace = _seed_value_for(cur, "auth.mfa_grace_period_days")
    assert mfa_grace == {"v": 7}, (
        f"auth.mfa_grace_period_days should use catalogue default 7, got {mfa_grace}"
    )
