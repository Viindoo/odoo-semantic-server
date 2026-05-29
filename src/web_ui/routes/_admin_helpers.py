# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for admin/tenant settings routes (WI-RV F-B + F-F).

Before WI-RV these helpers were duplicated across
``src/web_ui/routes/admin_settings.py`` and
``src/web_ui/routes/tenant_settings.py``:

  * ``_validate_value(sdef, value)`` — translates SettingValidationError -> 422
  * ``_catalogue_by_key()``         — dict view over SETTINGS_CATALOGUE
  * ``_post_write_hook(key, tid)``  — cache-invalidation cascade after PATCH/reset
  * ``_invalidate_plan_cache()``    — drops MCP middleware ``_PLAN_CACHE``

Two of those duplicates carried a real bug (F-B, score 95):
``_post_write_hook`` tried to clear ``src.mcp.middleware._plan_cache`` (lowercase)
when the actual module-level symbol is ``_PLAN_CACHE`` (uppercase) — the
``try/except: pass`` swallowed ``AttributeError`` so cache invalidation NEVER
ran after a quota PATCH.  Quota changes propagated only after the 300-second
cache TTL expired naturally.

This module consolidates the helpers into a single source of truth.  Both
routes import from here; the previously-correct
``src/web_ui/routes/admin_plans.py::_invalidate_plan_cache`` is also
re-routed through this module so the three call sites cannot drift.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from src.settings import invalidate_setting
from src.settings_registry import (
    SETTINGS_CATALOGUE,
    SettingValidationError,
    validate_setting_value,
)

log = logging.getLogger(__name__)


def catalogue_by_key() -> dict[str, Any]:
    """Return ``{key: SettingDef}`` view over :data:`SETTINGS_CATALOGUE`."""
    return {sdef.key: sdef for sdef in SETTINGS_CATALOGUE}


def validate_value_http(sdef: Any, value: Any) -> None:
    """Raise :class:`fastapi.HTTPException` (422) when validation fails.

    Thin HTTP-aware wrapper around
    :func:`src.settings_registry.validate_setting_value` so the route layer
    translates :class:`SettingValidationError` into a 422 response without
    leaking the underlying ValueError subclass into the API surface.
    """
    try:
        validate_setting_value(sdef, value)
    except SettingValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


def invalidate_plan_cache() -> None:
    """Best-effort: clear MCP ``_PLAN_CACHE`` under its ``_cache_lock``.

    Imports the canonical symbols from :mod:`src.mcp.middleware` — note
    the uppercase ``_PLAN_CACHE`` name.  Earlier in-line helpers in
    ``admin_settings`` + ``tenant_settings`` referenced ``_plan_cache``
    (lowercase) which silently raised ``AttributeError`` and was
    swallowed by ``except Exception: pass`` — see WI-RV F-B.

    The import is wrapped in ``try/except`` because the MCP middleware
    module may not be loaded in this process (split-tier deploy where
    the web UI runs separately, or unit tests that exercise route
    handlers without booting the MCP server).  Failure is logged at
    DEBUG and silently swallowed — the cache will TTL-expire within
    300 s anyway.
    """
    try:
        from src.mcp.middleware import _PLAN_CACHE, _cache_lock
        with _cache_lock:
            _PLAN_CACHE.clear()
        log.debug("Invalidated MCP _PLAN_CACHE after settings/plan write")
    except Exception as exc:
        # ImportError when MCP middleware is absent in this worker process,
        # or AttributeError if a future refactor renames the cache — log
        # at DEBUG so we have a forensic breadcrumb without spamming WARN.
        log.debug("MCP _PLAN_CACHE invalidation no-op: %s", exc)


def post_write_hook(key: str, tenant_id: int | None) -> None:
    """Dispatch cache-invalidation cascade after a setting write/reset.

    Steps:
      1. Drop the in-process :mod:`src.settings` overlay cache entry
         (other workers TTL-expire within 60 s).
      2. If ``key`` starts with ``"quota."`` also drop the MCP middleware
         ``_PLAN_CACHE`` so plan-aware quota gating picks up the new
         value immediately (previously this hop was BROKEN — F-B fix).
    """
    invalidate_setting(key, tenant_id=tenant_id)
    if key.startswith("quota."):
        invalidate_plan_cache()


def coerce_actor_id(actor_id: int | None, conn: Any) -> int | None:
    """Return ``actor_id`` only when the user exists in :table:`webui_users`.

    Mutating admin/tenant routes write ``updated_by = actor_id`` against
    columns whose FK is ``REFERENCES webui_users(id) ON DELETE SET NULL``.
    The FK accepts ``NULL`` (unknown/deleted actor) by design, but enforces
    referential integrity on INSERT/UPDATE — passing a non-existent
    ``actor_id`` raises ``psycopg2.errors.ForeignKeyViolation``.

    In test-bypass mode (``WEBUI_AUTH_DISABLED=1``), :func:`auth.current_user_id`
    returns the sentinel ``1`` even when no row with ``id=1`` exists in
    ``webui_users``.  Tests that exercise CRUD routes therefore trip the FK.
    Production callers always come from a real session → real user row, so
    this helper is effectively a no-op there.

    Implementation: 1 indexed ``SELECT 1`` on the primary key (~0.1 ms).
    Returns ``None`` (writing NULL is the consistent fallback — matches the
    "actor deleted" branch of ``ON DELETE SET NULL``) when the row is absent;
    otherwise returns ``actor_id`` unchanged.

    Args:
      actor_id: id returned by ``require_admin`` / ``require_admin_with_fresh_mfa``.
      conn:     a live psycopg2 connection (already checked out from the pool).

    Returns:
      ``actor_id`` if the row exists in :table:`webui_users`, else ``None``.
    """
    if actor_id is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM webui_users WHERE id = %s", (actor_id,))
        if cur.fetchone() is None:
            return None
    return actor_id
