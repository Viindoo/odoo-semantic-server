# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/settings.py — 3-tier setting resolver (ADR-0041).

Business intent:
  Each test validates one contract/rule of the resolution chain or cache behaviour.
  Tests are ordered L3->L2->L1 (code default -> DB -> cache).

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Shared fixture: schema + clean state
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_db(pg_conn):
    """Apply migrations, clean app_settings, and reset in-process cache.

    Returns the raw psycopg2 connection for direct assertions.
    """
    import src.settings as _settings_mod
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM app_settings")

    _settings_mod.invalidate_all()
    yield pg_conn
    # Cleanup after test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM app_settings")
    _settings_mod.invalidate_all()


# ---------------------------------------------------------------------------
# C1: Resolve default when DB is empty (L3 fallback)
# ---------------------------------------------------------------------------


def test_resolve_default_when_db_empty(settings_db):
    """When app_settings has no row for a key, return SETTINGS_CATALOGUE default."""
    from src.settings import get_setting
    from src.settings_registry import SETTINGS_CATALOGUE

    # Use first catalogue entry as reference
    first = SETTINGS_CATALOGUE[0]
    result = get_setting(first.key, conn=settings_db)
    assert result == first.default_value


# ---------------------------------------------------------------------------
# C2: Resolve system DB row overrides code default (L2 wins over L3)
# ---------------------------------------------------------------------------


def test_resolve_system_db_overrides_default(settings_db):
    """A system-scope DB row overrides the catalogue default."""
    import json

    from src.settings import get_setting, invalidate_setting

    key = "auth.session_ttl_seconds"
    new_value = 7200  # different from default 28800

    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'auth', 'system', 'duration_seconds',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": new_value}), json.dumps({"v": 28800})),
        )

    invalidate_setting(key)
    result = get_setting(key, conn=settings_db)
    assert result == new_value


# ---------------------------------------------------------------------------
# C3: Tenant resolution fallback — tenant_id supplied but no tenant row
#     Contract: when no tenant row exists, resolver falls back to system row.
# ---------------------------------------------------------------------------


def test_resolve_tenant_fallback_to_system_when_no_tenant_row(settings_db):
    """When tenant_id is supplied but no tenant row exists, system row is returned.

    Schema supports both system and tenant rows (partial unique indexes per ADR-0041/WI-1b).
    When a caller passes tenant_id but no tenant-scope row is present for that tenant,
    the resolver must fall back to the system-scope row.
    """
    import json

    from src.settings import get_setting, invalidate_setting

    key = "quota.free_rpm"
    system_value = 30

    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'quota', 'system', 'int',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": system_value}), json.dumps({"v": system_value})),
        )

    invalidate_setting(key)
    invalidate_setting(key, tenant_id=9001)

    # Without tenant_id: system value
    result_system = get_setting(key, conn=settings_db)
    assert result_system == system_value

    # With tenant_id=9001, no tenant row → falls back to system value
    result_tenant = get_setting(key, tenant_id=9001, conn=settings_db)
    assert result_tenant == system_value


# ---------------------------------------------------------------------------
# C4: Cache hit within TTL does not query DB again
# ---------------------------------------------------------------------------


def test_cache_hit_within_ttl(settings_db):
    """Second call within TTL returns cached value without hitting DB."""
    import json

    from src.settings import get_setting, invalidate_setting

    key = "embedding.max_batch_size"
    db_value = 99

    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'embedding', 'system', 'int',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": db_value}), json.dumps({"v": 50})),
        )

    invalidate_setting(key)

    # First call: populates cache
    result1 = get_setting(key, conn=settings_db)
    assert result1 == db_value

    # Now DELETE from DB — second call must still return cached value
    with settings_db.cursor() as cur:
        cur.execute("DELETE FROM app_settings WHERE key = %s", (key,))

    # Second call: cache hit (no DB query)
    result2 = get_setting(key, conn=settings_db)
    assert result2 == db_value


# ---------------------------------------------------------------------------
# C5: Cache expires after TTL
# ---------------------------------------------------------------------------


def test_cache_expires_after_ttl(settings_db, monkeypatch):
    """After TTL expires, a subsequent call re-queries DB."""
    import json

    import src.settings as _mod
    from src.settings import get_setting, invalidate_setting

    key = "mcp.resource_cache_ttl_seconds"
    original_value = 111
    updated_value = 222

    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'mcp', 'system', 'duration_seconds',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": original_value}), json.dumps({"v": 300})),
        )

    invalidate_setting(key)
    result1 = get_setting(key, conn=settings_db)
    assert result1 == original_value

    # Update DB value
    with settings_db.cursor() as cur:
        cur.execute(
            "UPDATE app_settings SET value_json = %s::jsonb WHERE key = %s",
            (json.dumps({"v": updated_value}), key),
        )

    # Simulate TTL expiry by forcing TTL to 0 + rewriting the cache entry
    # timestamp below; the original monotonic reference is kept for the
    # cache-entry tuple constructor (entries are (value, expires_at_monotonic)).
    original_monotonic = time.monotonic
    monkeypatch.setattr(_mod, "_CACHE_TTL_SECONDS", 0.0)
    # Force expiry by directly manipulating cache entry
    cache_key = (key, None)
    if cache_key in _mod._cache:
        val, _ = _mod._cache[cache_key]
        _mod._cache[cache_key] = (val, original_monotonic() - 1.0)  # expired

    result2 = get_setting(key, conn=settings_db)
    assert result2 == updated_value


# ---------------------------------------------------------------------------
# C6: invalidate_setting clears specific cache entry
# ---------------------------------------------------------------------------


def test_invalidate_setting_clears_cache(settings_db):
    """invalidate_setting() removes only the targeted (key, tenant_id) entry."""
    import json

    import src.settings as _mod
    from src.settings import get_setting, invalidate_setting

    key = "indexer.git_clone_timeout_seconds"
    db_value = 7200

    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'indexer', 'system', 'duration_seconds',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": db_value}), json.dumps({"v": 3600})),
        )

    invalidate_setting(key)
    get_setting(key, conn=settings_db)  # populate cache
    assert (key, None) in _mod._cache

    invalidate_setting(key)
    assert (key, None) not in _mod._cache


# ---------------------------------------------------------------------------
# C7: invalidate_all clears the entire cache
# ---------------------------------------------------------------------------


def test_invalidate_all_clears_everything(settings_db):
    """invalidate_all() empties the entire in-process cache dict."""
    import src.settings as _mod
    from src.settings import get_setting, invalidate_all
    from src.settings_registry import SETTINGS_CATALOGUE

    # Populate cache with a couple entries
    for sdef in SETTINGS_CATALOGUE[:3]:
        get_setting(sdef.key, conn=settings_db)

    assert len(_mod._cache) >= 1

    invalidate_all()
    assert len(_mod._cache) == 0


# ---------------------------------------------------------------------------
# C8: get_setting_typed returns correctly typed value
# ---------------------------------------------------------------------------


def test_get_setting_typed_correct_type(settings_db):
    """get_setting_typed passes when DB/default returns the expected type."""
    from src.settings import get_setting_typed, invalidate_setting

    key = "auth.password_min_length"  # default 12, int
    invalidate_setting(key)
    result = get_setting_typed(key, int, conn=settings_db)
    assert isinstance(result, int)
    assert result == 12


# ---------------------------------------------------------------------------
# C9: get_setting_typed raises TypeError on type mismatch
# ---------------------------------------------------------------------------


def test_get_setting_typed_wrong_type_raises(settings_db):
    """get_setting_typed raises TypeError when actual type != expected type."""
    import json

    from src.settings import get_setting_typed, invalidate_setting

    key = "auth.mfa_grace_period_days"  # default int

    # Override DB to a string value
    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'auth', 'system', 'int',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)"
            " ON CONFLICT (key) WHERE scope = 'system' AND tenant_id IS NULL"
            " DO UPDATE SET value_json = EXCLUDED.value_json",
            (key, json.dumps({"v": "not-an-int"}), json.dumps({"v": 7})),
        )

    invalidate_setting(key)

    with pytest.raises(TypeError, match="expected int"):
        get_setting_typed(key, int, conn=settings_db)

    with settings_db.cursor() as cur:
        cur.execute("DELETE FROM app_settings WHERE key = %s", (key,))
    invalidate_setting(key)


# ---------------------------------------------------------------------------
# C10: Unknown key raises KeyError (no catalogue entry)
# ---------------------------------------------------------------------------


def test_unknown_key_raises_keyerror(settings_db):
    """get_setting() raises KeyError for a key not in SETTINGS_CATALOGUE."""
    from src.settings import get_setting

    with pytest.raises(KeyError, match="not in SETTINGS_CATALOGUE"):
        get_setting("does.not.exist", conn=settings_db)


# ---------------------------------------------------------------------------
# C11: DB error falls back to code default (L3)
# ---------------------------------------------------------------------------


def test_db_error_falls_back_to_default(settings_db):
    """When DB query raises, get_setting returns the catalogue default."""
    from src.settings import get_setting, invalidate_setting
    from src.settings_registry import SETTINGS_CATALOGUE

    key = SETTINGS_CATALOGUE[0].key
    expected_default = SETTINGS_CATALOGUE[0].default_value
    invalidate_setting(key)

    # Patch _resolve_from_db to simulate a DB error
    import src.settings as _mod
    original = _mod._resolve_from_db

    def _fail(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    try:
        _mod._resolve_from_db = _fail
        result = get_setting(key, conn=settings_db)
    finally:
        _mod._resolve_from_db = original
        invalidate_setting(key)

    assert result == expected_default


# ---------------------------------------------------------------------------
# C12: LRU eviction when cache exceeds _CACHE_MAX_ENTRIES
# ---------------------------------------------------------------------------


def test_lru_eviction_when_full(settings_db):
    """When cache grows past _CACHE_MAX_ENTRIES, eviction drops oldest entries."""
    import src.settings as _mod

    # Save original limit + cache
    orig_max = _mod._CACHE_MAX_ENTRIES
    orig_cache = dict(_mod._cache)
    _mod.invalidate_all()

    try:
        _mod._CACHE_MAX_ENTRIES = 10

        # Fill cache to limit + 1 so eviction triggers on next insert.
        # Entries below are direct dict writes (bypassing get_setting()) so we
        # don't need a real catalogue key here.
        now = _mod.time.monotonic()
        # Pre-fill with 10 entries using fake keys (not in catalogue, direct inject)
        for i in range(10):
            _mod._cache[(f"fake.key.{i}", None)] = (i, now + 60)

        assert len(_mod._cache) == _mod._CACHE_MAX_ENTRIES

        # Trigger eviction by calling _evict_if_full + adding one more
        _mod._evict_if_full()

        # After eviction, some entries should be removed
        assert len(_mod._cache) < 10

    finally:
        _mod._CACHE_MAX_ENTRIES = orig_max
        _mod._cache.clear()
        _mod._cache.update(orig_cache)


# ---------------------------------------------------------------------------
# C13: _unwrap handles legacy raw value without {"v": ...} wrapper
# ---------------------------------------------------------------------------


def test_unwrap_handles_legacy_raw_value(settings_db):
    """_unwrap returns the raw value when no {'v':...} wrapper is present."""
    from src.settings import _unwrap

    # Standard wrapped form
    assert _unwrap({"v": 42}) == 42
    assert _unwrap({"v": True}) is True
    assert _unwrap({"v": "hello"}) == "hello"

    # Legacy or non-wrapped dict (missing "v" key) — return as-is
    raw_dict = {"some": "other"}
    assert _unwrap(raw_dict) == raw_dict

    # Primitive (psycopg2 auto-decoded JSONB integer)
    assert _unwrap(99) == 99
    assert _unwrap(None) is None


# ---------------------------------------------------------------------------
# C14: Tenant override wins when system row + tenant row both exist (WI-1b)
# ---------------------------------------------------------------------------


def test_tenant_override_wins_when_both_exist(settings_db):
    """System row + tenant row coexist → tenant value returned for that tenant_id."""
    import json

    from src.settings import get_setting, invalidate_setting

    key = "quota.free_rpm"
    system_value = 30
    tenant_value = 99

    # Seed tenant
    with settings_db.cursor() as cur:
        cur.execute("INSERT INTO tenants (name) VALUES ('t14_override') RETURNING id")
        t1_id = cur.fetchone()[0]

    # Insert system row
    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'quota', 'system', 'int',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": system_value}), json.dumps({"v": system_value})),
        )
    # Insert tenant override row
    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, tenant_id, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'quota', 'tenant', %s, 'int',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": tenant_value}), t1_id, json.dumps({"v": system_value})),
        )

    invalidate_setting(key)
    invalidate_setting(key, tenant_id=t1_id)

    # Tenant sees override
    assert get_setting(key, tenant_id=t1_id, conn=settings_db) == tenant_value
    # System value unchanged
    assert get_setting(key, conn=settings_db) == system_value


# ---------------------------------------------------------------------------
# C15: Two tenants have independent overrides, no cross-tenant leak (WI-1b)
# ---------------------------------------------------------------------------


def test_two_tenants_independent_overrides(settings_db):
    """T1 override=500, T2 override=800. Each tenant sees only its own value."""
    import json

    from src.settings import get_setting, invalidate_setting

    key = "quota.team_rpm"
    system_value = 300

    with settings_db.cursor() as cur:
        cur.execute("INSERT INTO tenants (name) VALUES ('t15_t1') RETURNING id")
        t1 = cur.fetchone()[0]
        cur.execute("INSERT INTO tenants (name) VALUES ('t15_t2') RETURNING id")
        t2 = cur.fetchone()[0]

    # System row
    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'quota', 'system', 'int',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": system_value}), json.dumps({"v": system_value})),
        )
    # T1 override
    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, tenant_id, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'quota', 'tenant', %s, 'int',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": 500}), t1, json.dumps({"v": system_value})),
        )
    # T2 override
    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, tenant_id, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'quota', 'tenant', %s, 'int',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": 800}), t2, json.dumps({"v": system_value})),
        )

    invalidate_setting(key)
    invalidate_setting(key, tenant_id=t1)
    invalidate_setting(key, tenant_id=t2)

    assert get_setting(key, tenant_id=t1, conn=settings_db) == 500
    assert get_setting(key, tenant_id=t2, conn=settings_db) == 800
    # System (no tenant) still sees original
    assert get_setting(key, conn=settings_db) == system_value


# ---------------------------------------------------------------------------
# C16: Partial unique index rejects duplicate (key, tenant_id) insert (WI-1b)
# ---------------------------------------------------------------------------


def test_tenant_unique_per_key_constraint(settings_db):
    """INSERT two rows for same (key, tenant_id) with scope='tenant' → UniqueViolation."""
    import json

    import psycopg2.errors

    key = "quota.free_calls_per_month"

    with settings_db.cursor() as cur:
        cur.execute("INSERT INTO tenants (name) VALUES ('t16_dup') RETURNING id")
        t1 = cur.fetchone()[0]

    with settings_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value_json, category, scope, tenant_id, data_type,"
            " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
            " VALUES (%s, %s::jsonb, 'quota', 'tenant', %s, 'int',"
            " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
            (key, json.dumps({"v": 50}), t1, json.dumps({"v": 100})),
        )
    settings_db.commit()

    with pytest.raises(psycopg2.errors.UniqueViolation):
        with settings_db.cursor() as cur:
            cur.execute(
                "INSERT INTO app_settings (key, value_json, category, scope, tenant_id, data_type,"
                " validation_json, default_value, requires_restart, requires_reseed, is_secret)"
                " VALUES (%s, %s::jsonb, 'quota', 'tenant', %s, 'int',"
                " '{}'::jsonb, %s::jsonb, FALSE, FALSE, FALSE)",
                (key, json.dumps({"v": 75}), t1, json.dumps({"v": 100})),
            )
    settings_db.rollback()
