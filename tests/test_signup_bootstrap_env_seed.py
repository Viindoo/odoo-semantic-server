# SPDX-License-Identifier: AGPL-3.0-or-later
"""ISSUE-2 regression guard: signup.enabled seed value respects SIGNUP_ENABLED env.

Business intent (solution-design.md ISSUE-2 / Option B):
  On a FRESH install (no app_settings row yet) the bootstrap should capture the
  operator's deploy-time SIGNUP_ENABLED env var rather than always seeding
  {"v": false}.  Four invariants must hold:

  E1  SIGNUP_ENABLED=1 + no row  ->  seeded value_json = {"v": true}
  E2  SIGNUP_ENABLED absent + no row  ->  seeded value_json = {"v": false}
  E3  Row already exists ({"v": false}) + SIGNUP_ENABLED=1, converge_metadata=True
        ->  ON CONFLICT DO UPDATE re-syncs metadata but NEVER touches value_json,
        so the row's value stays false (existing-deploy safety)
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


def _make_conn_mock(was_inserted: bool = True):
    """Return a fake psycopg2 connection whose cursor tracks execute() calls.

    The mock supports BOTH counting modes of ``register_settings_idempotent``:

    * **converge_metadata=True** counts inserted-vs-updated from the
      ``RETURNING (xmax = 0) AS was_inserted`` row, so ``fetchone()`` returns
      ``(was_inserted,)``:
        - ``was_inserted=True``  → simulates a FRESH insert (xmax = 0).
        - ``was_inserted=False`` → simulates an existing row whose metadata was
          re-synced via ON CONFLICT DO UPDATE (xmax != 0).
    * **converge_metadata=False** (DO NOTHING) counts via ``cur.rowcount``:
      ``rowcount`` is wired to mirror ``was_inserted`` (1 = inserted, 0 = the
      row pre-existed and DO NOTHING skipped it).
    """
    cur = MagicMock()
    cur.rowcount = 1 if was_inserted else 0  # DO NOTHING path uses rowcount
    cur.fetchone.return_value = (was_inserted,)  # DO UPDATE path uses RETURNING
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

    conn, cur = _make_conn_mock(was_inserted=True)

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
        conn, cur = _make_conn_mock(was_inserted=True)
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

    conn, cur = _make_conn_mock(was_inserted=True)

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

    conn, cur = _make_conn_mock(was_inserted=True)

    register_settings_idempotent(conn)

    seeded = _seed_value_for(cur, "signup.enabled")
    assert seeded == {"v": False}, (
        f"Expected {{\"v\": false}} for SIGNUP_ENABLED=0, got {seeded}"
    )


def test_env_false_string_seeds_false(monkeypatch):
    """E2 variant: SIGNUP_ENABLED=false seeds false."""
    monkeypatch.setenv("SIGNUP_ENABLED", "false")

    from src.settings_registry import register_settings_idempotent

    conn, cur = _make_conn_mock(was_inserted=True)

    register_settings_idempotent(conn)

    seeded = _seed_value_for(cur, "signup.enabled")
    assert seeded == {"v": False}


# ---------------------------------------------------------------------------
# E3: Row already exists -> ON CONFLICT DO UPDATE (value preserved, count=0)
# ---------------------------------------------------------------------------


def test_existing_row_value_preserved_metadata_resynced(monkeypatch):
    """E3: converge=True pre-existing row -> DO UPDATE re-syncs metadata, value_json kept.

    We simulate "row already exists" via ``was_inserted=False`` (xmax != 0 on
    the RETURNING row).  Two things must hold:

    * The function counts this run as 0 inserted (the row pre-existed), so
      callers/tests expecting "0 on a converged DB" stay correct.
    * value_json is the single column that must NEVER appear in the SET clause,
      so the admin-set value can never be clobbered by a metadata re-sync.
    """
    monkeypatch.setenv("SIGNUP_ENABLED", "1")

    from src.settings_registry import register_settings_idempotent

    # was_inserted=False simulates an existing row (DO UPDATE metadata re-sync).
    conn, cur = _make_conn_mock(was_inserted=False)

    inserted = register_settings_idempotent(conn, converge_metadata=True)

    # Nothing newly inserted — every row pre-existed and was re-synced.
    assert inserted == 0, (
        f"Expected 0 rows inserted when every row pre-exists, got {inserted}"
    )

    # The DO UPDATE SET clause must NOT touch value_json (admin value preserved).
    sql = cur.execute.call_args.args[0]
    set_clause = sql.split("DO UPDATE SET", 1)[1]
    assert "value_json" not in set_clause, (
        "value_json must NEVER appear in the DO UPDATE SET clause — "
        "a metadata re-sync must not reset the admin-tuned value"
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

    conn, cur = _make_conn_mock(was_inserted=True)

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

    conn, cur = _make_conn_mock(was_inserted=True)

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


# ---------------------------------------------------------------------------
# B3-root: converge_metadata gates DO UPDATE vs DO NOTHING (Option B)
# ---------------------------------------------------------------------------
#
# The bug: bootstrap used DO NOTHING unconditionally, so a metadata change
# (e.g. flipping requires_restart for a key) never propagated to a deployment
# that already had the row.  The fix (Option B) re-syncs metadata via DO UPDATE
# ONLY when the caller passes converge_metadata=True (the owner/webui path); the
# default (reader/MCP path) stays DO NOTHING.  Rule #1 (the single most
# important invariant): value_json must NEVER be in the SET list, or every
# operator's tuned value silently resets on restart.  These structural tests
# guard the contract WITHOUT a database by scanning the SQL the function emits.

# The 8 registry-derived metadata columns that MUST re-sync from the catalogue.
_METADATA_COLUMNS = (
    "category",
    "data_type",
    "validation_json",
    "default_value",
    "requires_restart",
    "requires_reseed",
    "is_secret",
    "description",
)


def _captured_upsert_sql(*, converge_metadata: bool) -> str:
    """Run register_settings_idempotent against a fake cursor and return the SQL."""
    from src.settings_registry import register_settings_idempotent

    conn, cur = _make_conn_mock(was_inserted=True)
    register_settings_idempotent(conn, converge_metadata=converge_metadata)
    # All catalogue rows share the same parametrized SQL text.
    return cur.execute.call_args.args[0]


def test_converge_true_uses_do_update(monkeypatch):
    """converge_metadata=True -> ON CONFLICT ... DO UPDATE SET (not DO NOTHING)."""
    monkeypatch.delenv("SIGNUP_ENABLED", raising=False)
    sql = _captured_upsert_sql(converge_metadata=True)
    assert "DO UPDATE SET" in sql, "converge=True must DO UPDATE to propagate metadata"
    assert "DO NOTHING" not in sql, (
        "DO NOTHING would freeze metadata on existing rows (the B3-root bug)"
    )


def test_converge_false_uses_do_nothing(monkeypatch):
    """converge_metadata=False (default) -> ON CONFLICT ... DO NOTHING, no SET clause.

    Option B: the reader/MCP path must stay INSERT-missing-only so osm_reader
    needs no UPDATE privilege on app_settings.
    """
    monkeypatch.delenv("SIGNUP_ENABLED", raising=False)
    sql = _captured_upsert_sql(converge_metadata=False)
    assert "DO NOTHING" in sql, "converge=False (reader/MCP path) must DO NOTHING"
    assert "DO UPDATE" not in sql, (
        "converge=False must NOT emit DO UPDATE — osm_reader has no UPDATE grant"
    )
    assert "SET" not in sql.split("DO NOTHING", 1)[1], (
        "DO NOTHING must have no SET clause"
    )


def test_converge_true_resyncs_all_metadata_columns(monkeypatch):
    """Each of the 8 registry-derived metadata columns must be in the SET clause."""
    monkeypatch.delenv("SIGNUP_ENABLED", raising=False)
    sql = _captured_upsert_sql(converge_metadata=True)
    set_clause = sql.split("DO UPDATE SET", 1)[1]
    for col in _METADATA_COLUMNS:
        assert f"{col} = EXCLUDED.{col}" in set_clause, (
            f"metadata column {col!r} must re-sync from EXCLUDED in DO UPDATE"
        )


def test_converge_true_never_touches_value_json(monkeypatch):
    """RULE #1: value_json must NEVER appear in the DO UPDATE SET clause.

    A regression here silently resets every operator's tuned settings on every
    process restart — the most dangerous failure mode of this change.
    """
    monkeypatch.delenv("SIGNUP_ENABLED", raising=False)
    sql = _captured_upsert_sql(converge_metadata=True)
    set_clause = sql.split("DO UPDATE SET", 1)[1]
    assert "value_json" not in set_clause, (
        "value_json (admin-set value) must be preserved — never in the SET list"
    )
    # Audit/identity columns must also stay out of the SET clause: a metadata
    # convergence is a system op, not a user edit, so it must not bump these.
    for col in ("updated_at", "updated_by", "change_reason", "key", "tenant_id"):
        assert col not in set_clause, (
            f"{col!r} must not be re-synced — it is identity/audit, not metadata"
        )


# ---------------------------------------------------------------------------
# Option B: bootstrap_settings_safe threads converge_metadata + call-site wiring
# ---------------------------------------------------------------------------


def test_bootstrap_safe_threads_converge_flag(monkeypatch):
    """bootstrap_settings_safe passes converge_metadata straight to the registrar.

    No-DB structural guard: the wrapper must thread the flag through unchanged so
    the webui can pass True (owner converges) and the MCP server False (reader,
    insert-missing-only). We stub the pool + register_settings_idempotent and
    assert the kwarg each invocation receives.
    """
    import src.settings_registry as _reg

    captured: list[bool] = []

    def _fake_register(conn, *, converge_metadata=False):
        captured.append(converge_metadata)
        return 0

    class _FakeConn:
        autocommit = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakePool:
        def checkout(self):
            return _FakeConn()

    monkeypatch.setattr("src.db.pg.get_pool", lambda: _FakePool())
    monkeypatch.setattr(_reg, "register_settings_idempotent", _fake_register)

    _reg.bootstrap_settings_safe(converge_metadata=True)
    _reg.bootstrap_settings_safe(converge_metadata=False)
    _reg.bootstrap_settings_safe()  # default = False

    assert captured == [True, False, False], (
        "bootstrap_settings_safe must thread converge_metadata through unchanged "
        f"(webui=True, MCP=False, default=False); got {captured}"
    )


def test_call_sites_thread_expected_flag():
    """The two call sites pass the right flag: webui=True (owner), MCP=False (reader).

    Source-level guard (no import of the heavy app/server modules): the webui
    lifespan converges, the MCP lifespan does not. If a refactor flips either,
    osm_reader would either be denied UPDATE (webui broken) or asked for an
    UPDATE grant it deliberately lacks (MCP broken — the Option-A regression).
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]

    webui = (root / "src" / "web_ui" / "app.py").read_text()
    assert "bootstrap_settings_safe(converge_metadata=True)" in webui, (
        "webui (owner DSN) must converge metadata"
    )

    server = (root / "src" / "mcp" / "server.py").read_text()
    assert "converge_metadata=False" in server, (
        "MCP server (osm_reader DSN) must NOT converge — insert-missing-only"
    )
