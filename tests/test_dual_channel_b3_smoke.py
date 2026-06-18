# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for superset tool text channels (M12 v0.6, updated WI-5).

After v0.6 removed the 10 flat shims (resolve_model, resolve_field, resolve_method,
resolve_view, list_fields, list_methods, list_views, list_owl_components,
list_qweb_templates, list_js_patches), all MCP tools emit raw-text tree only.

WI-5 fix (#261/#265-Obs4): describe_module no longer declares output_schema= on its
@mcp.tool decorator. All tools (including describe_module) now emit uniform raw text
with structured_content=None (ADR-0023 §1). The dual-channel structured-companion
functions (_resolve_*_structured, _list_*_structured) were physically removed once
ADR-0028 made every tool text-only; only the text channel survives.

Verified:
  (a) describe_module — text-only channel (no structured_content), including not-found.
  (b) model_inspect / entity_lookup superset tools — text-only channel (per design).

DB version: TEST_VERSION = "95.0" (distinct from 99.0/98.0/97.0/96.0
fixtures used by other test modules).

Runtime: ~10s (7 Neo4j round-trips).
"""
import asyncio
import os

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

# Dedicated version — must not collide with any other test module fixture.
TEST_VERSION = "95.0"


# ---------------------------------------------------------------------------
# Fixture — seed minimal data for all 7 tools
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def b3_db(neo4j_driver, monkeypatch_module):
    """Seed test data for B3 smoke tests and patch Neo4j env vars."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Wipe any leftover data from previous runs at this version.
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION
        )

    mod = ModuleInfo(
        name="b3_sale",
        odoo_version=TEST_VERSION,
        repo="odoo_test",
        path="/tmp/b3_sale",
        depends=["base"],
        edition="community",
    )
    model = ModelInfo(
        name="b3.order",
        module="b3_sale",
        odoo_version=TEST_VERSION,
        fields=[
            FieldInfo("name", "char", required=True),
            FieldInfo("amount_total", "monetary", compute="_compute_total", stored=True),
        ],
        methods=[
            MethodInfo("action_confirm", has_super_call=True),
            MethodInfo("_compute_total"),
        ],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    # Seed a minimal View node directly (writer_neo4j does not expose view writing).
    with neo4j_driver.session() as session:
        session.run(
            """
            MERGE (v:View {xmlid: 'b3_sale.view_order_form', odoo_version: $ver})
            SET v.type = 'form', v.model = 'b3.order', v.module = 'b3_sale',
                v.xpaths_exprs = [], v.xpaths_positions = [], v.profile = []
            """,
            ver=TEST_VERSION,
        )

    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    )
    monkeypatch_module.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password")
    )

    import sys
    sys.modules.pop("src.mcp.server", None)

    yield

    # Teardown — clean up seeded nodes.
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION
        )


# ---------------------------------------------------------------------------
# Parametrized helper
# ---------------------------------------------------------------------------


def _assert_text_channel(result) -> str:
    """Assert text channel is present; return the text string."""
    from fastmcp.tools.tool import ToolResult
    from mcp.types import TextContent

    assert isinstance(result, ToolResult), (
        f"Expected ToolResult, got {type(result)}"
    )
    assert result.content is not None, "content must not be None"
    assert len(result.content) == 1, f"Expected 1 ContentBlock, got {len(result.content)}"
    block = result.content[0]
    assert isinstance(block, TextContent), (
        f"content[0] must be TextContent, got {type(block)}"
    )
    assert isinstance(block.text, str) and block.text, (
        "content[0].text must be a non-empty string"
    )
    return block.text


# ---------------------------------------------------------------------------
# Smoke tests — v0.6 superset tools, text-only channel (ADR-0028).
#
# (a) describe_module — text-only (no structured_content), found + not-found.
# (b) model_inspect / entity_lookup — text-only channel (by design, v0.6+).
#
# NOTE: FastMCP v3 — @mcp.tool() returns the original function unchanged, so
# module-level names (server.model_inspect, server.entity_lookup, ...) are
# directly callable. No .fn indirection needed.
# ---------------------------------------------------------------------------


def test_model_inspect_summary_text_channel(b3_db):
    """model_inspect(method='summary') returns non-empty text with model name."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = asyncio.run(server.model_inspect(
        model="b3.order", method="summary", odoo_version=TEST_VERSION
    ))
    text = _assert_text_channel(result)
    assert "b3.order" in text
    assert "b3_sale" in text or "sale" in text.lower()


def test_entity_lookup_view_text_channel(b3_db):
    """entity_lookup(kind='view') returns non-empty text with view xmlid (replaces resolve_view)."""
    import asyncio
    import importlib
    server = importlib.import_module("src.mcp.server")

    # entity_lookup is async (#227 — offloads blocking body off the event loop).
    result = asyncio.run(server.entity_lookup(
        kind="view", xmlid="b3_sale.view_order_form", odoo_version=TEST_VERSION
    ))
    text = _assert_text_channel(result)
    assert "b3_sale.view_order_form" in text


def test_describe_module_text_only_channel(b3_db):
    """describe_module wrapper returns raw text only (no structured_content).

    WI-5 fix (#261/#265-Obs4): describe_module no longer carries output_schema=;
    all tools emit a uniform raw-text tree (ADR-0023 §1). structured_content
    must be None so the MCP client never sees a schema-declared but absent payload.
    (The describe_module structured companion was removed in L9.)
    """
    from src.mcp.server import describe_module

    result = asyncio.run(describe_module("b3_sale", TEST_VERSION))
    text = _assert_text_channel(result)
    # Confirm the module name appears in the text channel.
    assert "b3_sale" in text
    # WI-5: no structured channel for describe_module any more.
    assert result.structured_content is None, (
        f"describe_module must not emit structured_content (WI-5). "
        f"Got: {result.structured_content!r}"
    )


def test_describe_module_not_found_returns_clean_text(b3_db):
    """describe_module(nonexistent) returns friendly text, no Output validation error.

    WI-5 fix (#261): previously, describe_module had output_schema= declared, so
    the not-found path (structured_content=None) triggered a client-side
    'Output validation error'. After dropping output_schema=, the tool returns
    the same ToolResult pattern as its siblings: plain text, structured_content=None.
    """
    from src.mcp.server import describe_module

    result = asyncio.run(describe_module("nonexistent_module_xyz_b3", TEST_VERSION))
    text = _assert_text_channel(result)
    assert "nonexistent_module_xyz_b3" in text, (
        f"Not-found text should contain the module name. Got: {text!r}"
    )
    assert "No module named" in text, (
        f"Not-found text should start with 'No module named'. Got: {text!r}"
    )
    assert result.structured_content is None, (
        f"Not-found path must not produce structured_content. Got: {result.structured_content!r}"
    )
