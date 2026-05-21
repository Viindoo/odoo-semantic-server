# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for dual-channel ToolResult on 7 priority tools (M10.5 WI-B3).

AC-B3-2: For each of the 7 tools, assert that:
  - content[0].text is a non-empty string (text channel present).
  - structured_content is a non-None dict that validates against the
    declared *Output DTO type (structured channel present and typed).

These are integration tests (Neo4j required).  They intentionally avoid
detailed schema assertions — that is WI-B4's job.  Here we only verify
both channels are present and the structured dict round-trips through the
*Output Pydantic model without error.

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
    ResolveViewOutput,
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


def _assert_dual_channel(result, dto_class) -> None:
    """Assert both channels are present and structured_content validates as dto_class."""
    from fastmcp.tools.tool import ToolResult
    from mcp.types import TextContent

    assert isinstance(result, ToolResult), (
        f"Expected ToolResult, got {type(result)}"
    )
    # Text channel
    assert result.content is not None, "content must not be None"
    assert len(result.content) == 1, f"Expected 1 ContentBlock, got {len(result.content)}"
    block = result.content[0]
    assert isinstance(block, TextContent), (
        f"content[0] must be TextContent, got {type(block)}"
    )
    assert isinstance(block.text, str) and block.text, (
        "content[0].text must be a non-empty string"
    )
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
# 7 smoke tests — one per tool
#
# NOTE: @mcp.tool() wraps functions into FunctionTool objects (not directly
# callable per CLAUDE.md §FastMCP). We call .fn to reach the underlying
# Python function which returns the ToolResult we want to assert on.
# ---------------------------------------------------------------------------


def test_resolve_model_dual_channel(b3_db):
    """resolve_model wrapper returns both text and structured ResolveModelOutput."""
    from src.mcp.server import resolve_model

    result = resolve_model.fn(target="b3.order", odoo_version=TEST_VERSION)
    _assert_dual_channel(result, ResolveModelOutput)
    # Spot-check structured payload fields.
    sc = result.structured_content
    assert sc["ref"]["name"] == "b3.order"
    assert sc["ref"]["odoo_version"] == TEST_VERSION
    assert isinstance(sc["field_count"], int)
    assert isinstance(sc["method_count"], int)
    assert "next_step_hint" in sc


def test_resolve_field_dual_channel(b3_db):
    """resolve_field wrapper returns both text and structured ResolveFieldOutput."""
    from src.mcp.server import resolve_field

    result = resolve_field.fn(
        target="b3.order.amount_total", odoo_version=TEST_VERSION
    )
    _assert_dual_channel(result, ResolveFieldOutput)
    sc = result.structured_content
    assert sc["ref"]["name"] == "amount_total"
    assert sc["ref"]["model"] == "b3.order"
    assert sc["ref"]["odoo_version"] == TEST_VERSION
    assert "ttype" in sc
    assert "next_step_hint" in sc


def test_resolve_method_dual_channel(b3_db):
    """resolve_method wrapper returns both text and structured ResolveMethodOutput."""
    from src.mcp.server import resolve_method

    result = resolve_method.fn(
        target="b3.order.action_confirm", odoo_version=TEST_VERSION
    )
    _assert_dual_channel(result, ResolveMethodOutput)
    sc = result.structured_content
    assert sc["ref"]["name"] == "action_confirm"
    assert sc["ref"]["model"] == "b3.order"
    assert sc["ref"]["odoo_version"] == TEST_VERSION
    assert isinstance(sc["override_chain"], list)
    assert "next_step_hint" in sc


def test_resolve_view_dual_channel(b3_db):
    """resolve_view wrapper returns both text and structured ResolveViewOutput."""
    from src.mcp.server import resolve_view

    result = resolve_view.fn(
        target="b3_sale.view_order_form", odoo_version=TEST_VERSION
    )
    _assert_dual_channel(result, ResolveViewOutput)
    sc = result.structured_content
    assert sc["ref"]["xmlid"] == "b3_sale.view_order_form"
    assert sc["ref"]["odoo_version"] == TEST_VERSION
    assert "view_type" in sc
    assert "next_step_hint" in sc


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


def test_list_fields_dual_channel(b3_db):
    """list_fields wrapper returns both text and structured ListFieldsOutput."""
    from src.mcp.server import list_fields

    result = list_fields.fn("b3.order", TEST_VERSION)
    _assert_dual_channel(result, ListFieldsOutput)
    sc = result.structured_content
    assert sc["model"] == "b3.order"
    assert sc["odoo_version"] == TEST_VERSION
    assert isinstance(sc["total"], int)
    assert isinstance(sc["shown"], int)
    assert isinstance(sc["fields"], list)
    assert "next_step_hint" in sc


def test_list_methods_dual_channel(b3_db):
    """list_methods wrapper returns both text and structured ListMethodsOutput."""
    from src.mcp.server import list_methods

    result = list_methods.fn("b3.order", TEST_VERSION)
    _assert_dual_channel(result, ListMethodsOutput)
    sc = result.structured_content
    assert sc["model"] == "b3.order"
    assert sc["odoo_version"] == TEST_VERSION
    assert isinstance(sc["total"], int)
    assert isinstance(sc["shown"], int)
    assert isinstance(sc["methods"], list)
    assert "next_step_hint" in sc
