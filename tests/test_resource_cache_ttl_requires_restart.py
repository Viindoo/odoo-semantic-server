# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_resource_cache_ttl_requires_restart.py
"""No-DB unit test for fix 1b: mcp.resource_cache_ttl_seconds requires restart.

The MCP resource ``_CACHE`` singleton is frozen at MCP process start; the
admin-settings PATCH runs in a SEPARATE webui process, so a new TTL cannot reach
the live MCP cache without an MCP restart. The SettingDef must therefore carry
``requires_restart=True`` so the admin UI surfaces the honest constraint instead
of a misleading propagation ETA.
"""

from __future__ import annotations

from src.settings_registry import SETTINGS_CATALOGUE


def _by_key(key: str):
    for sdef in SETTINGS_CATALOGUE:
        if sdef.key == key:
            return sdef
    raise AssertionError(f"{key} not found in SETTINGS_CATALOGUE")


def test_resource_cache_ttl_requires_restart():
    sdef = _by_key("mcp.resource_cache_ttl_seconds")
    assert sdef.requires_restart is True, (
        "mcp.resource_cache_ttl_seconds must be requires_restart=True — the MCP "
        "_CACHE singleton is frozen at process start and cannot be live-updated "
        "from the webui process."
    )
