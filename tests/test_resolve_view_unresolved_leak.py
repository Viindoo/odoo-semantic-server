# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test for Fix B — _resolve_view must not leak __unresolved__
placeholder View nodes.

A placeholder View (created when an inherit ref points at a not-yet-indexed
target) carries module='__unresolved__' / unresolved=true. When the ONLY node
matching an xmlid is such a placeholder, _resolve_view must report not-found
rather than rendering the empty placeholder.
"""
import pytest

from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


def test_resolve_view_ignores_unresolved_placeholder(clean_neo4j):
    driver = clean_neo4j
    xmlid = "some_mod.placeholder_only_view"

    # Seed ONLY an __unresolved__ placeholder for this xmlid.
    with driver.session() as session:
        session.run(
            """
            CREATE (v:View {
                xmlid: $xmlid,
                odoo_version: $ver,
                module: '__unresolved__',
                unresolved: true,
                profile: []
            })
            """,
            xmlid=xmlid, ver=TEST_VERSION,
        )

    from src.mcp.server import _resolve_view

    out = _resolve_view(xmlid, TEST_VERSION)
    assert "not found" in out, (
        f"placeholder-only xmlid must yield not-found, got: {out!r}"
    )
