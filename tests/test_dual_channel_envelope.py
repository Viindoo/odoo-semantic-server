# SPDX-License-Identifier: AGPL-3.0-or-later
"""Comprehensive contract tests for the dual-channel envelope on surviving tools (M12 v0.6).

After v0.6 removed the 10 flat shims, only describe_module retains a full
dual-channel (text + structured) @mcp.tool wrapper.  The remaining *Output DTOs
are produced by internal structured-companion functions (_resolve_*_structured,
_list_*_structured) which still exist and must still satisfy the DTO contract.

Three sections:

1. POSITIVE — parametrized, tests over describe_module (dual-channel) + structured
   companions (DTO contract only, no wrapper).

2. NEGATIVE — ≥3 tests that verify the contract bites when broken.
   - None structured_content triggers the validator helper to raise.
   - Missing next_step_hint field causes Pydantic ValidationError.
   - Wrong type on a required field causes Pydantic ValidationError.

3. SCHEMA INTEGRITY — 1 parametrized test over all 7 *Output types asserting
   that model_json_schema() includes next_step_hint in required and as string.
   Runs DB-free.

4. OUTPUT SCHEMA WIRING — verify surviving dual-channel tool (describe_module)
   advertises the correct DTO schema via output_schema= on @mcp.tool().

5. NEXT STEP HINT CHANNEL PARITY — for describe_module (the one surviving
   full dual-channel tool), verify text footer == structured next_step_hint.

DB version: TEST_VERSION = "94.0" (distinct from 95.0/99.0/98.0/97.0/96.0
used by other test modules).

Runtime: ~10s (Neo4j round-trips for positive tests).
"""

import asyncio
import os

import pytest
from pydantic import ValidationError

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from src.mcp.dto import (
    ListFieldsOutput,
    ListMethodsOutput,
    ResolveFieldOutput,
    ResolveMethodOutput,
    ResolveModelOutput,
    ResolveViewOutput,
)

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


def _assert_envelope(result, dto_class) -> None:
    """Full envelope contract: both channels present, structured validates, hint non-empty."""
    from fastmcp.tools.tool import ToolResult
    from mcp.types import TextContent

    assert isinstance(result, ToolResult), (
        f"Expected ToolResult, got {type(result)}"
    )
    # --- Text channel ---
    assert result.content is not None, "content must not be None"
    assert len(result.content) == 1, (
        f"Expected exactly 1 ContentBlock, got {len(result.content)}"
    )
    block = result.content[0]
    assert isinstance(block, TextContent), (
        f"content[0] must be TextContent, got {type(block)}"
    )
    assert isinstance(block.text, str) and block.text, (
        "content[0].text must be a non-empty string"
    )
    # --- Structured channel ---
    assert result.structured_content is not None, "structured_content must not be None"
    assert isinstance(result.structured_content, dict), (
        f"structured_content must be dict, got {type(result.structured_content)}"
    )
    # --- Pydantic validation (AC-B4-1c): raises on schema mismatch ---
    validated = dto_class.model_validate(result.structured_content)
    # --- next_step_hint non-empty (AC-B4-1d, ADR-0023 §4 contract) ---
    assert validated.next_step_hint, (
        f"next_step_hint must be non-empty string; got {validated.next_step_hint!r}"
    )


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
# Section 1 — POSITIVE parametrized tests (7 tools)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "companion_fn, args, dto_class, spot_checks",
    [
        pytest.param(
            "_resolve_model_structured",
            lambda: ("b4.order", TEST_VERSION),
            ResolveModelOutput,
            lambda out: (
                out.ref.name == "b4.order"
                and out.ref.odoo_version == TEST_VERSION
                and isinstance(out.field_count, int)
                and isinstance(out.method_count, int)
            ),
            id="resolve_model_companion",
        ),
        pytest.param(
            "_resolve_field_structured",
            lambda: ("b4.order", "amount_total", TEST_VERSION),
            ResolveFieldOutput,
            lambda out: (
                out.ref.name == "amount_total"
                and out.ref.model == "b4.order"
                and out.ref.odoo_version == TEST_VERSION
                and bool(out.ttype)
            ),
            id="resolve_field_companion",
        ),
        pytest.param(
            "_resolve_method_structured",
            lambda: ("b4.order", "action_confirm", TEST_VERSION),
            ResolveMethodOutput,
            lambda out: (
                out.ref.name == "action_confirm"
                and out.ref.model == "b4.order"
                and out.ref.odoo_version == TEST_VERSION
                and isinstance(out.override_chain, list)
            ),
            id="resolve_method_companion",
        ),
        pytest.param(
            "_resolve_view_structured",
            lambda: ("b4_sale.view_order_form", TEST_VERSION),
            ResolveViewOutput,
            lambda out: (
                out.ref.xmlid == "b4_sale.view_order_form"
                and out.ref.odoo_version == TEST_VERSION
                and bool(out.view_type)
            ),
            id="resolve_view_companion",
        ),
        pytest.param(
            "_list_fields_structured",
            lambda: ("b4.order", TEST_VERSION),
            ListFieldsOutput,
            lambda out: (
                out.model == "b4.order"
                and out.odoo_version == TEST_VERSION
                and isinstance(out.total, int)
                and isinstance(out.fields, list)
            ),
            id="list_fields_companion",
        ),
        pytest.param(
            "_list_methods_structured",
            lambda: ("b4.order", TEST_VERSION),
            ListMethodsOutput,
            lambda out: (
                out.model == "b4.order"
                and out.odoo_version == TEST_VERSION
                and isinstance(out.total, int)
                and isinstance(out.methods, list)
            ),
            id="list_methods_companion",
        ),
    ],
)
def test_positive_structured_companion(b4_db, companion_fn, args, dto_class, spot_checks):
    """Each structured-companion function returns a valid DTO with non-empty next_step_hint.

    Asserts:
    (a) companion function returns a non-None result.
    (b) result validates cleanly as the *Output Pydantic type.
    (c) validated DTO has a non-empty next_step_hint (ADR-0023 §4 contract).
    (d) tool-specific spot-checks on the validated DTO.

    The args lambda returns a tuple of positional args to the companion function.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")
    fn = getattr(server, companion_fn)
    raw_args = args()
    result = fn(*raw_args)
    assert result is not None, f"{companion_fn} returned None"
    output = dto_class.model_validate(result.model_dump())
    assert output.next_step_hint, (
        f"{companion_fn}: next_step_hint must be non-empty, got {output.next_step_hint!r}"
    )
    assert spot_checks(output), (
        f"{companion_fn}: spot-check failed on validated output: {output}"
    )


def test_positive_text_channel_byte_identical_to_impl(b4_db):
    """model_inspect(method='summary') text == _resolve_model() impl output (text channel parity).

    The superset wrapper routes to the same impl function. The text channel must
    be byte-identical to what _resolve_model() returns directly.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    inner_text = server._resolve_model("b4.order", TEST_VERSION)
    result = asyncio.run(server.model_inspect.fn(
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


def test_negative_missing_next_step_hint_raises_validation_error():
    """Pydantic raises ValidationError when next_step_hint is absent from the dict.

    Constructs a minimal dict that matches ResolveModelOutput EXCEPT it omits
    next_step_hint — model_validate must raise ValidationError.

    This is the AC-B4-4 mutation-experiment equivalent: removing next_step_hint
    from the dict is equivalent to removing it from the schema (for incoming
    data), and the schema-integrity test below catches structural removal.
    """
    minimal_dict = {
        "ref": {
            "name": "b4.order",
            "module": "b4_sale",
            "odoo_version": TEST_VERSION,
        },
        "is_definition": True,
        "defined_in": {
            "name": "b4_sale",
            "odoo_version": TEST_VERSION,
            "profile": None,
        },
        "extended_by": [],
        "inherits_from": [],
        "field_count": 2,
        "method_count": 2,
        # next_step_hint intentionally OMITTED
    }
    with pytest.raises(ValidationError) as exc_info:
        ResolveModelOutput.model_validate(minimal_dict)
    errors = exc_info.value.errors()
    field_names = [e["loc"][0] for e in errors]
    assert "next_step_hint" in field_names, (
        f"Expected ValidationError on next_step_hint, got errors on: {field_names}"
    )


def test_negative_wrong_type_on_required_field_raises_validation_error():
    """Pydantic raises ValidationError when ref is None (required, non-nullable field).

    ResolveFieldOutput.ref is a required FieldRef — passing None must raise.
    """
    bad_dict = {
        "ref": None,  # should be a FieldRef dict
        "ttype": "monetary",
        "computed": True,
        "compute_method": "_compute_total",
        "stored": True,
        "required": False,
        "related": None,
        "declared_in": [],
        "next_step_hint": "└─ Next: model_inspect(...)",
    }
    with pytest.raises(ValidationError) as exc_info:
        ResolveFieldOutput.model_validate(bad_dict)
    errors = exc_info.value.errors()
    field_names = [e["loc"][0] for e in errors]
    assert "ref" in field_names, (
        f"Expected ValidationError on 'ref', got errors on: {field_names}"
    )


def test_negative_extra_field_forbidden():
    """*Output types have extra='forbid' — unknown keys must raise ValidationError.

    This proves the DTOs use strict schema contracts — drift in the wrapper
    adding an undeclared key is caught immediately, not silently ignored.
    """
    with pytest.raises(ValidationError) as exc_info:
        ResolveMethodOutput.model_validate(
            {
                "ref": {
                    "model": "b4.order",
                    "name": "action_confirm",
                    "module": "b4_sale",
                    "odoo_version": TEST_VERSION,
                },
                "override_chain": [],
                "next_step_hint": "└─ Next: ...",
                "unexpected_field": "should trigger extra=forbid",
            }
        )
    assert any(
        e["type"] == "extra_forbidden" for e in exc_info.value.errors()
    ), "Expected 'extra_forbidden' ValidationError for unknown field"


# ---------------------------------------------------------------------------
# Section 3 — SCHEMA INTEGRITY (1 parametrized test, runs DB-free)
#
# AC-B4-4 rationale: removing next_step_hint from an *Output class directly
# causes this parametrized test to fail for that type — the "mutation experiment"
# is covered structurally, without needing to actually mutate and revert source.
# ---------------------------------------------------------------------------

_ALL_OUTPUT_TYPES = [
    ResolveModelOutput,
    ResolveFieldOutput,
    ResolveMethodOutput,
    ResolveViewOutput,
    ListFieldsOutput,
    ListMethodsOutput,
]


@pytest.mark.parametrize(
    "output_type",
    _ALL_OUTPUT_TYPES,
    ids=[t.__name__ for t in _ALL_OUTPUT_TYPES],
)
def test_schema_integrity_next_step_hint(output_type):
    """Each *Output type's JSON Schema includes next_step_hint as a required string.

    This is structural — runs without DB.  If next_step_hint is removed from any
    *Output class, this test fails immediately for that class.

    Three sub-assertions:
    (a) 'next_step_hint' appears in the schema's 'required' list.
    (b) 'next_step_hint' appears in the schema's 'properties' dict.
    (c) The property type is 'string'.
    """
    schema = output_type.model_json_schema()
    class_name = output_type.__name__

    # (a) required list
    required = schema.get("required", [])
    assert "next_step_hint" in required, (
        f"{class_name}.model_json_schema()['required'] is missing 'next_step_hint'. "
        f"Got: {required}"
    )

    # (b) properties dict
    properties = schema.get("properties", {})
    assert "next_step_hint" in properties, (
        f"{class_name}.model_json_schema()['properties'] is missing 'next_step_hint'. "
        f"Got keys: {list(properties.keys())}"
    )

    # (c) type is string
    hint_schema = properties["next_step_hint"]
    # Pydantic v2 emits {"type": "string", "description": "..."} for plain str fields.
    assert hint_schema.get("type") == "string", (
        f"{class_name}: expected next_step_hint.type='string', "
        f"got: {hint_schema}"
    )


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
    tool = server.describe_module
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
    result = asyncio.run(server.describe_module.fn("b4_sale", TEST_VERSION))

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
    result = asyncio.run(server.describe_module.fn("nonexistent_module_xyz_b4", TEST_VERSION))

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
    tool = server.check_module_exists
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
    result = asyncio.run(server.describe_module.fn("b4_sale", TEST_VERSION))

    text = result.content[0].text
    lines = text.split("\n")
    non_empty_lines = [ln for ln in lines if ln.strip()]
    assert non_empty_lines, "describe_module: text channel produced no output lines"
    text_footer = non_empty_lines[-1]

    assert "Next:" in text_footer or "└─" in text_footer, (
        f"describe_module text footer should contain 'Next:' or a tree connector. "
        f"Got last non-empty line: {text_footer!r}"
    )
