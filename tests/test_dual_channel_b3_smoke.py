# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for superset tool text channels + surviving dual-channel tools (M12 v0.6).

After v0.6 removed the 10 flat shims (resolve_model, resolve_field, resolve_method,
resolve_view, list_fields, list_methods, list_views, list_owl_components,
list_qweb_templates, list_js_patches), the dual-channel contract for the 5 surviving
dual-channel tools (describe_module + list_fields/methods/resolve_view/model structured
companions) is verified by:
  (a) describe_module — still registered as @mcp.tool with structured output.
  (b) The 6 structured-companion functions (_resolve_model_structured, _resolve_field_structured,
      _resolve_method_structured, _resolve_view_structured, _list_fields_structured,
      _list_methods_structured) are called directly to confirm the DTO round-trips still work.

For the new superset tools (model_inspect, module_inspect, entity_lookup), only the
text channel is verified (they are text-only per design — no structured_content).

DB version: TEST_VERSION = "95.0" (distinct from 99.0/98.0/97.0/96.0
fixtures used by other test modules).

Runtime: ~10s (7 Neo4j round-trips).
"""
import os

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from src.mcp.dto import (
    DescribeModuleOutput,
    ListFieldsOutput,
    ListMethodsOutput,
    ResolveFieldOutput,
    ResolveMethodOutput,
    ResolveModelOutput,
)

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


def _assert_dual_channel(result, dto_class) -> None:
    """Assert both channels are present and structured_content validates as dto_class."""
    text = _assert_text_channel(result)
    assert text, "text channel must be non-empty"
    # Structured channel
    assert result.structured_content is not None, "structured_content must not be None"
    assert isinstance(result.structured_content, dict), (
        f"structured_content must be a dict, got {type(result.structured_content)}"
    )
    # Round-trip through the DTO to validate shape (AC-B3-2: structured channel
    # must validate against the tool's declared *Output DTO type).
    validated = dto_class.model_validate(result.structured_content)
    assert validated is not None, "DTO round-trip must succeed"


# ---------------------------------------------------------------------------
# 7 smoke tests — migrated to v0.6 superset tools.
#
# (a) describe_module — still has dual channel (unchanged).
# (b) model_inspect / entity_lookup — text-only channel (by design, v0.6+).
# (c) Structured companion functions (_resolve_*_structured, _list_*_structured)
#     are called directly to verify DTO round-trips without needing the wrapper.
#
# NOTE: @mcp.tool() wraps functions into FunctionTool objects (not directly
# callable per CLAUDE.md §FastMCP). We call .fn to reach the underlying
# Python function which returns the ToolResult we want to assert on.
# ---------------------------------------------------------------------------


def test_model_inspect_summary_text_channel(b3_db):
    """model_inspect(method='summary') returns non-empty text with model name."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = server.model_inspect.fn(model="b3.order", method="summary", odoo_version=TEST_VERSION)
    text = _assert_text_channel(result)
    assert "b3.order" in text
    assert "b3_sale" in text or "sale" in text.lower()


def test_resolve_model_structured_companion(b3_db):
    """_resolve_model_structured returns a valid ResolveModelOutput DTO."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    structured = server._resolve_model_structured("b3.order", TEST_VERSION)
    assert structured is not None, "_resolve_model_structured returned None"
    output = ResolveModelOutput.model_validate(structured.model_dump())
    assert output.ref.name == "b3.order"
    assert output.ref.odoo_version == TEST_VERSION
    assert isinstance(output.field_count, int)
    assert isinstance(output.method_count, int)
    assert output.next_step_hint


def test_resolve_field_structured_companion(b3_db):
    """_resolve_field_structured returns a valid ResolveFieldOutput DTO."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    structured = server._resolve_field_structured("b3.order", "amount_total", TEST_VERSION)
    assert structured is not None, "_resolve_field_structured returned None"
    output = ResolveFieldOutput.model_validate(structured.model_dump())
    assert output.ref.name == "amount_total"
    assert output.ref.model == "b3.order"
    assert output.ref.odoo_version == TEST_VERSION
    assert output.ttype
    assert output.next_step_hint


def test_resolve_method_structured_companion(b3_db):
    """_resolve_method_structured returns a valid ResolveMethodOutput DTO."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    structured = server._resolve_method_structured("b3.order", "action_confirm", TEST_VERSION)
    assert structured is not None, "_resolve_method_structured returned None"
    output = ResolveMethodOutput.model_validate(structured.model_dump())
    assert output.ref.name == "action_confirm"
    assert output.ref.model == "b3.order"
    assert output.ref.odoo_version == TEST_VERSION
    assert isinstance(output.override_chain, list)
    assert output.next_step_hint


def test_entity_lookup_view_text_channel(b3_db):
    """entity_lookup(kind='view') returns non-empty text with view xmlid (replaces resolve_view)."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = server.entity_lookup.fn(
        kind="view", xmlid="b3_sale.view_order_form", odoo_version=TEST_VERSION
    )
    text = _assert_text_channel(result)
    assert "b3_sale.view_order_form" in text


def test_describe_module_dual_channel(b3_db):
    """describe_module wrapper returns both text and structured DescribeModuleOutput."""
    from src.mcp.server import describe_module

    result = describe_module.fn("b3_sale", TEST_VERSION)
    _assert_dual_channel(result, DescribeModuleOutput)
    sc = result.structured_content
    assert sc["ref"]["name"] == "b3_sale"
    assert sc["ref"]["odoo_version"] == TEST_VERSION
    assert "edition" in sc
    assert isinstance(sc["view_total"], int)
    assert "next_step_hint" in sc


def test_list_fields_structured_companion(b3_db):
    """_list_fields_structured returns a valid ListFieldsOutput DTO."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    structured = server._list_fields_structured("b3.order", TEST_VERSION)
    assert structured is not None, "_list_fields_structured returned None"
    output = ListFieldsOutput.model_validate(structured.model_dump())
    assert output.model == "b3.order"
    assert output.odoo_version == TEST_VERSION
    assert isinstance(output.total, int)
    assert isinstance(output.shown, int)
    assert isinstance(output.fields, list)
    assert output.next_step_hint


def test_list_methods_structured_companion(b3_db):
    """_list_methods_structured returns a valid ListMethodsOutput DTO."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    structured = server._list_methods_structured("b3.order", TEST_VERSION)
    assert structured is not None, "_list_methods_structured returned None"
    output = ListMethodsOutput.model_validate(structured.model_dump())
    assert output.model == "b3.order"
    assert output.odoo_version == TEST_VERSION
    assert isinstance(output.total, int)
    assert isinstance(output.shown, int)
    assert isinstance(output.methods, list)
    assert output.next_step_hint
