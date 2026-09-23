# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_mcp_resource_degraded_not_cached.py
"""A stylesheet resource read while its file is unreadable is not cached.

Business rule (lane-mcpfix defect 4): ``odoo://V/stylesheet/<module>/<path>``
caches for the resource TTL (300s). When the indexed file cannot be read on
this server (checkout being re-cloned, a mount not up yet), the body says
"indexed but file unreadable". That answer describes a transient server state,
so the next read after the file is back must serve the file, not the cached
error. A readable file is still cached as before.

Real shape: ``web/static/src/scss/primary_variables.scss`` of the Odoo web
module (repo-relative path, ADR-0037), indexed at TEST_VERSION.
"""
from __future__ import annotations

import pytest

from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

V = TEST_VERSION
MODULE = "web"
SCSS = "$o-brand-primary: #714B67 !default;\n"


@pytest.fixture
def indexed_stylesheet(clean_neo4j, tmp_path, monkeypatch):
    """A :Stylesheet node whose absolute on-disk path is under tmp_path (absent yet)."""
    from src.mcp import server as srv

    path = tmp_path / "addons" / MODULE / "static/src/scss/primary_variables.scss"
    with clean_neo4j.session() as s:
        s.run(
            """
            MERGE (mod:Module {name: $mod, odoo_version: $v})
            SET mod.profile = ['degraded_std']
            MERGE (ss:Stylesheet {file_path: $fp, module: $mod, odoo_version: $v})
            SET ss.language = 'scss', ss.profile = ['degraded_std']
            MERGE (ss)-[:DEFINED_IN]->(mod)
            """,
            mod=MODULE, v=V, fp=str(path),
        )
    monkeypatch.setattr(srv, "_driver", clean_neo4j)
    return path


def _read(cache, path) -> str:
    from src.mcp.resources import _render_stylesheet, _serve_resource_blocking

    uri_path = str(path).lstrip("/")  # the URI segment cannot carry the leading "/"
    return _serve_resource_blocking(
        cache, V, "stylesheet", f"{MODULE}/{uri_path}",
        lambda resolved: _render_stylesheet(resolved, MODULE, uri_path),
    )


def test_unreadable_stylesheet_is_retried_on_the_next_read(indexed_stylesheet):
    """FIX: pre-fix the "file unreadable" body was cached for the whole TTL."""
    from src.mcp.resources import ResourceCache

    cache = ResourceCache(ttl=300)
    first = _read(cache, indexed_stylesheet)
    assert "indexed but file unreadable on this server" in first, first

    indexed_stylesheet.parent.mkdir(parents=True)
    indexed_stylesheet.write_text(SCSS, encoding="utf-8")
    assert _read(cache, indexed_stylesheet) == SCSS


def test_readable_stylesheet_is_still_cached(indexed_stylesheet):
    """GUARD: a served file is cached; deleting it inside the TTL does not show."""
    # GUARD: pre-existing behaviour
    from src.mcp.resources import ResourceCache

    indexed_stylesheet.parent.mkdir(parents=True)
    indexed_stylesheet.write_text(SCSS, encoding="utf-8")
    cache = ResourceCache(ttl=300)
    assert _read(cache, indexed_stylesheet) == SCSS
    indexed_stylesheet.unlink()
    assert _read(cache, indexed_stylesheet) == SCSS
