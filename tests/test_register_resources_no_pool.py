# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_register_resources_no_pool.py
"""No-DB unit tests for the lazy ``get_cache()`` fix (fix/startup-reseed-log-noise).

``register_resources(mcp)`` runs at ``server.py`` module-import time — BEFORE
``main()`` starts the PostgreSQL pool. Previously it called ``get_cache()``
eagerly, which resolved ``mcp.resource_cache_ttl_seconds`` against an
uninitialised pool and logged the spurious
``"... overlay-only resolve failed: PostgreSQL pool is not initialized"``
warning. The fix defers ``get_cache()`` into each resource handler (request
time). These tests prove registration no longer touches the pool / overlay.

All pure-Python (no DB, no neo4j) — runs in the ``not neo4j and not postgres``
lane.
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import FastMCP

import src.mcp.resources as resources
from src.mcp.resources import register_resources, reset_cache


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Drop the process-wide cache singleton before and after each test."""
    reset_cache()
    yield
    reset_cache()


def test_register_resources_does_not_resolve_overlay(monkeypatch):
    """register_resources must NOT call get_overlay_only at registration time."""
    calls: list[str] = []

    def _spy_get_overlay_only(key, *args, **kwargs):  # pragma: no cover - asserted not called
        calls.append(key)
        return None

    # Patch the symbol where _resolve_cache_ttl imports it from.
    import src.settings as settings
    monkeypatch.setattr(settings, "get_overlay_only", _spy_get_overlay_only)

    mcp = FastMCP("test-osm")
    register_resources(mcp)

    assert calls == [], (
        "register_resources resolved the settings overlay eagerly; the cache "
        "must be resolved lazily inside handlers (request time), not at "
        "registration/module-import time."
    )
    # And the cache singleton must still be unbuilt after registration.
    assert resources._CACHE is None


def test_register_resources_does_not_touch_pool(monkeypatch, caplog):
    """With get_pool() raising, register_resources must neither raise nor warn."""
    import src.db.pg as pg
    from src.db.exceptions import PoolNotInitializedError

    def _boom():
        raise PoolNotInitializedError(
            "PostgreSQL pool is not initialized. Call init_pool(dsn) at startup."
        )

    monkeypatch.setattr(pg, "get_pool", _boom)

    mcp = FastMCP("test-osm")
    with caplog.at_level(logging.WARNING, logger="src.settings"):
        register_resources(mcp)  # must not raise

    overlay_warnings = [
        r for r in caplog.records
        if "overlay-only resolve failed" in r.getMessage()
    ]
    assert overlay_warnings == [], (
        "register_resources triggered the overlay-resolve warning — the eager "
        "get_cache() call was not removed."
    )
    assert resources._CACHE is None
