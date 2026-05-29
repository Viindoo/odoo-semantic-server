# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_plan_cache_invalidation.py
"""WI-RV F-B: verify quota PATCH actually clears MCP _PLAN_CACHE.

Business intent:
  Prior to WI-RV F-B, ``src/web_ui/routes/admin_settings.py`` and
  ``src/web_ui/routes/tenant_settings.py`` referenced
  ``src.mcp.middleware._plan_cache`` (lowercase) when the canonical symbol
  is ``_PLAN_CACHE`` (uppercase).  The bare ``except Exception: pass``
  swallowed the resulting ``AttributeError`` so quota PATCH never
  invalidated the cache — operators had to wait up to 300 s for the natural
  TTL before a quota change took effect.

  After WI-RV F-B, all three routes (admin_settings, tenant_settings,
  admin_plans) delegate to the shared
  ``src.web_ui.routes._admin_helpers.invalidate_plan_cache`` which uses the
  correct uppercase symbol under ``_cache_lock``.

  This test populates ``_PLAN_CACHE`` with a sentinel entry, calls PATCH
  /api/admin/settings/quota.free_rpm, and asserts the cache is empty.

All tests require PostgreSQL (pytestmark postgres).
"""
from __future__ import annotations

import httpx
import pytest

from src.db.migrate import run_migrations
from src.settings import invalidate_all
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


@pytest.fixture(autouse=True)
def _clear_caches():
    invalidate_all()
    yield
    invalidate_all()


def _client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _seed_plan_cache_entry() -> int:
    """Populate _PLAN_CACHE with one sentinel entry; return its key."""
    import time

    from src.mcp.middleware import _PLAN_CACHE, PlanInfo, _cache_lock

    sentinel_key = 999999
    plan = PlanInfo(
        plan_id=1,
        slug="sentinel",
        quota_calls_per_month=1,
        rate_limit_rpm=1,
    )
    with _cache_lock:
        _PLAN_CACHE[sentinel_key] = (plan, time.monotonic())
    return sentinel_key


def _peek_plan_cache_size() -> int:
    from src.mcp.middleware import _PLAN_CACHE, _cache_lock
    with _cache_lock:
        return len(_PLAN_CACHE)


@pytest.mark.asyncio
async def test_quota_patch_via_admin_settings_clears_plan_cache(migrated_pg):
    """PATCH /api/admin/settings/quota.free_rpm -> _PLAN_CACHE is empty.

    Proves the post-write hook reaches src.mcp.middleware._PLAN_CACHE (NOT
    the dead-letter ``_plan_cache`` lowercase symbol).  The sentinel entry
    must be dropped — TTL is irrelevant; the helper calls ``.clear()``.
    """
    sentinel_key = _seed_plan_cache_entry()
    assert _peek_plan_cache_size() >= 1, "Seed step failed"

    async with _client() as client:
        resp = await client.patch(
            "/api/admin/settings/quota.free_rpm",
            json={"value": 45, "reason": "F-B cache invalidation test"},
        )
    assert resp.status_code == 200, (
        f"PATCH expected 200, got {resp.status_code}: {resp.text}"
    )

    # Hook must have cleared the cache.
    assert _peek_plan_cache_size() == 0, (
        f"_PLAN_CACHE not cleared after quota PATCH "
        f"(sentinel key {sentinel_key} still present)"
    )

    # Cleanup the patched system row so other tests start clean.
    with migrated_pg.cursor() as cur:
        cur.execute(
            "DELETE FROM app_settings WHERE key = 'quota.free_rpm' "
            "AND scope = 'system' AND tenant_id IS NULL"
        )
        cur.execute(
            "DELETE FROM app_settings_history WHERE setting_key = 'quota.free_rpm'"
        )
    migrated_pg.commit()


@pytest.mark.asyncio
async def test_non_quota_patch_leaves_plan_cache_alone(migrated_pg):
    """PATCH a non-quota key does NOT touch _PLAN_CACHE.

    Sanity check: only ``quota.*`` keys trigger the MCP cache flush.  An
    unrelated PATCH (e.g. ``auth.session_ttl_seconds``) leaves the cache
    intact.  This guards against an over-broad invalidation that would
    cost MCP latency on every settings change.
    """
    _seed_plan_cache_entry()
    size_before = _peek_plan_cache_size()
    assert size_before >= 1

    async with _client() as client:
        resp = await client.patch(
            "/api/admin/settings/auth.session_ttl_seconds",
            json={"value": 7200, "reason": "F-B non-quota guard"},
        )
    assert resp.status_code == 200, (
        f"PATCH expected 200, got {resp.status_code}: {resp.text}"
    )

    assert _peek_plan_cache_size() == size_before, (
        "_PLAN_CACHE altered by an unrelated PATCH "
        f"(was {size_before}, now {_peek_plan_cache_size()})"
    )

    # Cleanup
    with migrated_pg.cursor() as cur:
        cur.execute(
            "DELETE FROM app_settings WHERE key = 'auth.session_ttl_seconds' "
            "AND scope = 'system' AND tenant_id IS NULL"
        )
        cur.execute(
            "DELETE FROM app_settings_history "
            "WHERE setting_key = 'auth.session_ttl_seconds'"
        )
    migrated_pg.commit()


def test_invalidate_plan_cache_helper_uses_correct_symbol():
    """src/web_ui/routes/_admin_helpers.invalidate_plan_cache touches _PLAN_CACHE.

    Unit-level guard against regression to the lowercase ``_plan_cache``
    bug: directly populate the cache, call the helper, and verify the cache
    is empty.  This test does NOT exercise the HTTP layer — failure here
    means the helper itself is broken (not a route wiring problem).
    """
    from src.web_ui.routes._admin_helpers import invalidate_plan_cache

    _seed_plan_cache_entry()
    assert _peek_plan_cache_size() >= 1

    invalidate_plan_cache()

    assert _peek_plan_cache_size() == 0, (
        "invalidate_plan_cache() did not clear _PLAN_CACHE — "
        "regression to the lowercase _plan_cache bug?"
    )
