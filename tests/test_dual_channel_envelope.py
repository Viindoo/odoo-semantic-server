# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the text-only tool envelope on surviving tools (M12 v0.6).

After v0.6 removed the 10 flat shims and ADR-0028 made every tool text-only,
no tool emits a structured channel. The dual-channel structured-companion
functions and their *Output DTOs were physically removed; the surviving
contract is the raw-text envelope plus output_schema=None wiring.

Sections:

1. POSITIVE — model_inspect(method='summary') text == _resolve_model() impl.

2. NEGATIVE — the validator helper bites when structured_content is None
   (proves a future regression that re-introduces a structured channel is caught).

4. SERIALIZATION UNIFORMITY — describe_module + a representative -> str tool
   advertise output_schema=None and never emit structured_content (WI-5).

5. TEXT FOOTER — describe_module's text channel still ends with a Next: footer.

DB version: TEST_VERSION = "94.0" (distinct from 95.0/99.0/98.0/97.0/96.0
used by other test modules).

Runtime: ~10s (Neo4j round-trips for positive tests).
"""

import asyncio
import os

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

# Dedicated version — must not collide with any other test module fixture.
TEST_VERSION = "94.0"


# ---------------------------------------------------------------------------
# Fixture — seed minimal data for all 7 tools
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def b4_db(neo4j_driver, monkeypatch_module):
    """Seed test data for B4 envelope tests and patch Neo4j env vars."""
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
        name="b4_sale",
        odoo_version=TEST_VERSION,
        repo="odoo_test",
        path="/tmp/b4_sale",
        depends=["base"],
        edition="community",
    )
    model = ModelInfo(
        name="b4.order",
        module="b4_sale",
        odoo_version=TEST_VERSION,
        fields=[
            FieldInfo("name", "char", required=True),
            FieldInfo(
                "amount_total", "monetary", compute="_compute_total", stored=True
            ),
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
            MERGE (v:View {xmlid: 'b4_sale.view_order_form', odoo_version: $ver})
            SET v.type = 'form', v.model = 'b4.order', v.module = 'b4_sale',
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
# Contract validation helper
# ---------------------------------------------------------------------------


def _require_structured_content_is_dict(result) -> None:
    """Helper that raises AssertionError when structured_content is None or not a dict.

    Used by negative tests to prove the contract helper catches absent structured channel.
    """
    from fastmcp.tools.tool import ToolResult

    assert isinstance(result, ToolResult)
    assert result.structured_content is not None, "structured_content must not be None"
    assert isinstance(result.structured_content, dict), (
        f"structured_content must be dict, got {type(result.structured_content)}"
    )


# ---------------------------------------------------------------------------
# Section 1 — POSITIVE text-channel parity
# ---------------------------------------------------------------------------


def test_positive_text_channel_byte_identical_to_impl(b4_db):
    """model_inspect(method='summary') text == _resolve_model() impl output (text channel parity).

    The superset wrapper routes to the same impl function. The text channel must
    be byte-identical to what _resolve_model() returns directly.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    inner_text = server._resolve_model("b4.order", TEST_VERSION)
    result = asyncio.run(server.model_inspect(
        model="b4.order", method="summary", odoo_version=TEST_VERSION
    ))

    wrapper_text = result.content[0].text
    assert wrapper_text == inner_text, (
        "model_inspect(method='summary') text must be byte-identical to _resolve_model() output"
    )


# ---------------------------------------------------------------------------
# Section 2 — NEGATIVE tests (≥3) — contract bites when broken
# ---------------------------------------------------------------------------


def test_negative_structured_content_none_caught():
    """Validator raises AssertionError when structured_content is None.

    Constructs a ToolResult manually with structured_content=None to prove
    the _require_structured_content_is_dict helper catches it.  This proves
    the positive tests are real — they'd fail if the tool accidentally
    produced None.
    """
    from fastmcp.tools.tool import ToolResult
    from mcp.types import TextContent

    fake = ToolResult(
        content=[TextContent(type="text", text="some text")],
        structured_content=None,
    )
    with pytest.raises(AssertionError, match="structured_content must not be None"):
        _require_structured_content_is_dict(fake)


# ---------------------------------------------------------------------------
# Section 4 — SERIALIZATION UNIFORMITY (WI-5 fix for #261/#265-Obs4)
#
# After WI-5, ALL tools emit raw plain-text tree with NO structured_content.
# output_schema=None is set in READONLY_TOOL_KWARGS so FastMCP suppresses the
# auto-wrap {"result": "<tree>"} for -> str tools, and describe_module no
# longer declares an explicit output_schema= either.
#
# These tests verify:
#   AC-BFIX-1: describe_module.output_schema is None (no schema declared).
#   AC-BFIX-2: describe_module(found) -> structured_content is None.
#   AC-BFIX-3: describe_module(not-found) -> clean text, no validation error.
# Runs DB-free for schema checks; DB-bound for found/not-found.
# ---------------------------------------------------------------------------


def test_describe_module_output_schema_is_none():
    """WI-5: describe_module must NOT declare output_schema= on its decorator.

    After removing output_schema=DescribeModuleOutput.model_json_schema(),
    FastMCP sets tool.output_schema = None (no schema advertised to clients).
    This prevents the 'Output validation error' when not-found returns no
    structured payload (the root cause of #261).
    """
    import importlib

    server = importlib.import_module("src.mcp.server")
    # fastmcp v3 default decorator-mode: the module-level name is the raw fn, so the
    # FunctionTool (which owns output_schema) is fetched via the public get_tool (#324).
    tool = asyncio.run(server.mcp.get_tool("describe_module"))
    assert tool.output_schema is None, (
        "describe_module must not declare output_schema= (WI-5). "
        f"Got: {tool.output_schema!r}"
    )


def test_describe_module_structured_content_is_none_on_found(b4_db):
    """WI-5: describe_module(found module) returns raw text only, no structured_content.

    The dual-channel structured companion for describe_module was removed (L9);
    the MCP wrapper now only ever emits the plain-text tree (ADR-0023 §1).
    """
    import importlib

    server = importlib.import_module("src.mcp.server")
    result = asyncio.run(server.describe_module("b4_sale", TEST_VERSION))

    assert result.content is not None and len(result.content) == 1
    text = result.content[0].text
    assert text, "describe_module must return non-empty text"
    assert "b4_sale" in text, f"Module name must appear in text. Got: {text!r}"
    assert result.structured_content is None, (
        "describe_module must not populate structured_content (WI-5). "
        f"Got: {result.structured_content!r}"
    )


def test_describe_module_not_found_returns_clean_text(b4_db):
    """WI-5 fix (#261): describe_module(nonexistent) returns friendly text, no error.

    Before WI-5, the not-found path returned structured_content=None while
    output_schema= was declared, triggering 'Output validation error' on
    MCP clients that validate tool output against the declared schema.
    After WI-5, output_schema= is removed; the not-found path returns the
    same ToolResult pattern as all siblings: plain text, structured_content=None.
    """
    import importlib

    server = importlib.import_module("src.mcp.server")
    result = asyncio.run(server.describe_module("nonexistent_module_xyz_b4", TEST_VERSION))

    assert result.content is not None and len(result.content) == 1
    text = result.content[0].text
    assert "nonexistent_module_xyz_b4" in text, (
        f"Not-found text should contain the module name. Got: {text!r}"
    )
    assert "No module named" in text, (
        f"Not-found text should contain 'No module named'. Got: {text!r}"
    )
    assert result.structured_content is None, (
        f"Not-found path must not produce structured_content. Got: {result.structured_content!r}"
    )


def test_str_tool_no_fastmcp_wrap_result_shim():
    """WI-5 (#265-Obs4): -> str tools must NOT have the FastMCP auto-wrap shim.

    Before WI-5, READONLY_TOOL_KWARGS lacked output_schema=None, so FastMCP
    auto-derived output_schema={'x-fastmcp-wrap-result':True,'properties':{'result':{...}}}
    for all -> str tools, producing {"result":"<tree>"} in structuredContent.
    After WI-5, output_schema=None in READONLY_TOOL_KWARGS suppresses auto-wrapping.

    This test checks check_module_exists (a representative -> str tool).
    DB-free: output_schema is populated at import time from the decorator.
    """
    import importlib

    server = importlib.import_module("src.mcp.server")
    # fastmcp v3 default decorator-mode: the module-level name is the raw fn, so the
    # FunctionTool (which owns output_schema) is fetched via the public get_tool (#324).
    tool = asyncio.run(server.mcp.get_tool("check_module_exists"))
    # With output_schema=None in READONLY_TOOL_KWARGS, FastMCP should not
    # auto-derive the wrap shim.
    schema = tool.output_schema
    assert schema is None, (
        "check_module_exists.output_schema must be None after WI-5. "
        f"Got: {schema!r}. "
        "If 'x-fastmcp-wrap-result' appears, READONLY_TOOL_KWARGS is missing output_schema=None."
    )


# ---------------------------------------------------------------------------
# Section 5 — NEXT STEP HINT IN TEXT CHANNEL (updated from dual-channel parity)
#
# WI-5: describe_module is now text-only (no structured_content). This section
# verifies the text-channel Next: footer is still present on found modules.
# (The describe_module structured companion was removed in L9.)
# ---------------------------------------------------------------------------


def test_describe_module_text_footer_present(b4_db):
    """describe_module(found) text channel ends with a Next: footer line.

    The footer is still required by ADR-0023 §4.3. WI-5 removed the dual-channel
    wrapper so the structured channel is gone, but the text footer must remain.
    """
    import importlib

    server = importlib.import_module("src.mcp.server")
    result = asyncio.run(server.describe_module("b4_sale", TEST_VERSION))

    text = result.content[0].text
    lines = text.split("\n")
    non_empty_lines = [ln for ln in lines if ln.strip()]
    assert non_empty_lines, "describe_module: text channel produced no output lines"
    text_footer = non_empty_lines[-1]

    assert "Next:" in text_footer or "└─" in text_footer, (
        f"describe_module text footer should contain 'Next:' or a tree connector. "
        f"Got last non-empty line: {text_footer!r}"
    )
