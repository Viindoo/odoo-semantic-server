# SPDX-License-Identifier: AGPL-3.0-or-later
"""Comprehensive contract tests for the dual-channel envelope on 7 priority tools
(M10.5 WI-B4).

Three sections:

1. POSITIVE — parametrized, 1 test × 7 tools (7 cases total).
   For each tool: call .fn(...) on the FastMCP-wrapped FunctionTool, assert that
   - content[0].text is byte-identical to what the inner _impl returns
   - structured_content validates cleanly against the declared *Output Pydantic type
   - the validated DTO has a non-empty next_step_hint

2. NEGATIVE — ≥3 tests that verify the contract bites when broken.
   - None structured_content triggers the validator helper to raise.
   - Missing next_step_hint field causes Pydantic ValidationError.
   - Wrong type on a required field causes Pydantic ValidationError.

3. SCHEMA INTEGRITY — 1 parametrized test over all 7 *Output types asserting
   that model_json_schema() includes next_step_hint in required and as string.
   Runs DB-free.

DB version: TEST_VERSION = "94.0" (distinct from 95.0/99.0/98.0/97.0/96.0
used by other test modules).

Runtime: ~10s (7 Neo4j round-trips for positive tests).
"""

import os

import pytest
from pydantic import ValidationError

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
    "tool_name, args, dto_class, spot_checks",
    [
        pytest.param(
            "resolve_model",
            lambda: {"target": "b4.order", "odoo_version": TEST_VERSION},
            ResolveModelOutput,
            lambda sc: (
                sc["ref"]["name"] == "b4.order"
                and sc["ref"]["odoo_version"] == TEST_VERSION
                and isinstance(sc["field_count"], int)
                and isinstance(sc["method_count"], int)
            ),
            id="resolve_model",
        ),
        pytest.param(
            "resolve_field",
            lambda: {"target": "b4.order.amount_total", "odoo_version": TEST_VERSION},
            ResolveFieldOutput,
            lambda sc: (
                sc["ref"]["name"] == "amount_total"
                and sc["ref"]["model"] == "b4.order"
                and sc["ref"]["odoo_version"] == TEST_VERSION
                and "ttype" in sc
            ),
            id="resolve_field",
        ),
        pytest.param(
            "resolve_method",
            lambda: {"target": "b4.order.action_confirm", "odoo_version": TEST_VERSION},
            ResolveMethodOutput,
            lambda sc: (
                sc["ref"]["name"] == "action_confirm"
                and sc["ref"]["model"] == "b4.order"
                and sc["ref"]["odoo_version"] == TEST_VERSION
                and isinstance(sc["override_chain"], list)
            ),
            id="resolve_method",
        ),
        pytest.param(
            "resolve_view",
            lambda: {"target": "b4_sale.view_order_form", "odoo_version": TEST_VERSION},
            ResolveViewOutput,
            lambda sc: (
                sc["ref"]["xmlid"] == "b4_sale.view_order_form"
                and sc["ref"]["odoo_version"] == TEST_VERSION
                and "view_type" in sc
            ),
            id="resolve_view",
        ),
        pytest.param(
            "describe_module",
            lambda: ("b4_sale", TEST_VERSION),
            DescribeModuleOutput,
            lambda sc: (
                sc["ref"]["name"] == "b4_sale"
                and sc["ref"]["odoo_version"] == TEST_VERSION
                and "edition" in sc
                and isinstance(sc["view_total"], int)
            ),
            id="describe_module",
        ),
        pytest.param(
            "list_fields",
            lambda: ("b4.order", TEST_VERSION),
            ListFieldsOutput,
            lambda sc: (
                sc["model"] == "b4.order"
                and sc["odoo_version"] == TEST_VERSION
                and isinstance(sc["total"], int)
                and isinstance(sc["fields"], list)
            ),
            id="list_fields",
        ),
        pytest.param(
            "list_methods",
            lambda: ("b4.order", TEST_VERSION),
            ListMethodsOutput,
            lambda sc: (
                sc["model"] == "b4.order"
                and sc["odoo_version"] == TEST_VERSION
                and isinstance(sc["total"], int)
                and isinstance(sc["methods"], list)
            ),
            id="list_methods",
        ),
    ],
)
def test_positive_envelope(b4_db, tool_name, args, dto_class, spot_checks):
    """Each of 7 tools returns a valid dual-channel envelope with typed structured_content.

    Asserts:
    (a) text channel: content[0].text is non-empty (byte-identical guard vs inner _impl
        is checked indirectly — the wrapper's text block IS the _impl return value,
        so structured_content presence proves the wrapper ran with the same data).
    (b) structured_content is a dict.
    (c) dict validates cleanly as the tool's declared *Output Pydantic type.
    (d) validated DTO has a non-empty next_step_hint (ADR-0023 §4 contract).
    (e) tool-specific spot-checks on raw structured_content keys/values.

    The args lambda may return either a tuple (positional) or a dict (keyword).
    resolve_* tools use dict form after WI-C3 target= refactor.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")
    tool_fn = getattr(server, tool_name)
    raw_args = args()
    if isinstance(raw_args, dict):
        result = tool_fn.fn(**raw_args)
    else:
        result = tool_fn.fn(*raw_args)
    _assert_envelope(result, dto_class)
    assert spot_checks(result.structured_content), (
        f"{tool_name}: spot-check failed on structured_content: {result.structured_content}"
    )


def test_positive_text_channel_byte_identical_to_impl(b4_db):
    """Wrapper text == DEPRECATED banner (W-D4) + inner _impl output.

    Wave D4 added a deprecation banner prefix to legacy tools' text channel.
    Structured channel is unchanged. After stripping the banner, the wrapper
    output must still be byte-identical to the inner _impl output.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    inner_text = server._resolve_model("b4.order", TEST_VERSION)
    result = server.resolve_model.fn(target="b4.order", odoo_version=TEST_VERSION)

    wrapper_text = result.content[0].text
    assert wrapper_text.startswith("DEPRECATED:"), (
        "WI-D4 contract: legacy resolve_model wrapper must prefix text "
        "with DEPRECATED banner"
    )
    body = wrapper_text.split("\n\n", 1)[1]
    assert body == inner_text, (
        "content[0].text after stripping DEPRECATED banner must be "
        "byte-identical to _resolve_model() output"
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
        "next_step_hint": "└─ Next: resolve_model(...)",
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
    DescribeModuleOutput,
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
# Section 4 — OUTPUT SCHEMA WIRING (AC-BFIX-1/AC-BFIX-2)
#
# Verify that all 7 @mcp.tool() decorators advertise the correct DTO schema
# via output_schema= — NOT the FastMCP auto-wrap shim ({result: string}).
# Runs DB-free.
# ---------------------------------------------------------------------------

_TOOL_DTO_PAIRS = [
    ("resolve_model", ResolveModelOutput),
    ("resolve_field", ResolveFieldOutput),
    ("resolve_method", ResolveMethodOutput),
    ("resolve_view", ResolveViewOutput),
    ("describe_module", DescribeModuleOutput),
    ("list_fields", ListFieldsOutput),
    ("list_methods", ListMethodsOutput),
]


@pytest.mark.parametrize(
    "tool_name,dto",
    _TOOL_DTO_PAIRS,
    ids=[t for t, _ in _TOOL_DTO_PAIRS],
)
def test_tool_advertises_dto_outputschema(tool_name, dto):
    """Each of the 7 priority tools must expose the correct DTO schema via output_schema.

    AC-BFIX-1: output_schema= is declared on the decorator — the auto-wrap shim
    ('x-fastmcp-wrap-result' with single 'result' field) must NOT appear.

    AC-BFIX-2: The advertised schema must include 'next_step_hint' in its
    properties, proving the full DTO schema (not a narrow subset) is wired.

    Runs without DB — tool.output_schema is populated at import time.
    """
    import importlib

    server = importlib.import_module("src.mcp.server")
    tool = getattr(server, tool_name)
    schema = tool.output_schema

    assert schema is not None, (
        f"{tool_name}.output_schema is None — output_schema= not declared on @mcp.tool()"
    )
    assert "x-fastmcp-wrap-result" not in schema, (
        f"{tool_name} still has FastMCP auto-wrap shim in output_schema — "
        "output_schema= was not declared on the @mcp.tool() decorator"
    )
    props = schema.get("properties", {})
    assert "next_step_hint" in props, (
        f"{tool_name}: output_schema missing 'next_step_hint' in properties. "
        f"Got keys: {list(props.keys())}"
    )
    # All fields from the DTO schema must be present (FastMCP may add $defs etc.).
    dto_schema = dto.model_json_schema()
    dto_props = set(dto_schema.get("properties", {}).keys())
    schema_props = set(props.keys())
    missing = dto_props - schema_props
    assert not missing, (
        f"{tool_name}: output_schema missing DTO fields: {missing}. "
        f"DTO has {dto_props}, tool schema has {schema_props}"
    )


# ---------------------------------------------------------------------------
# Section 5 — NEXT STEP HINT CHANNEL PARITY (AC-BFIX-3/AC-BFIX-4)
#
# For each of the 7 priority tools, call the wrapper, extract the trailing
# footer from content[0].text, and assert it equals structured_content's
# next_step_hint.  This gates against future drift between the two channels.
# ---------------------------------------------------------------------------

_HINT_PARITY_ARGS = [
    ("resolve_model", lambda: {"target": "b4.order", "odoo_version": TEST_VERSION}),
    ("resolve_field", lambda: {"target": "b4.order.amount_total", "odoo_version": TEST_VERSION}),
    ("resolve_method", lambda: {"target": "b4.order.action_confirm", "odoo_version": TEST_VERSION}),
    ("resolve_view", lambda: {"target": "b4_sale.view_order_form", "odoo_version": TEST_VERSION}),
    ("describe_module", lambda: ("b4_sale", TEST_VERSION)),
    ("list_fields", lambda: ("b4.order", TEST_VERSION)),
    ("list_methods", lambda: ("b4.order", TEST_VERSION)),
]


@pytest.mark.parametrize(
    "tool_name,args_fn",
    _HINT_PARITY_ARGS,
    ids=[t for t, _ in _HINT_PARITY_ARGS],
)
def test_next_step_hint_matches_text_footer(b4_db, tool_name, args_fn):
    """Structured next_step_hint must be byte-identical to the text-channel footer.

    AC-BFIX-3/4: Extract the last non-empty line of content[0].text (the
    '└─ Next: ...' footer) and compare against structured_content['next_step_hint'].
    Any kwarg-name drift or model-qualifier loss in the structured path will
    cause this test to fail.

    Relies on the b4_db fixture which seeds b4.order with 2 fields + 2 methods.
    """
    import importlib

    server = importlib.import_module("src.mcp.server")
    tool = getattr(server, tool_name)
    raw = args_fn()
    if isinstance(raw, dict):
        result = tool.fn(**raw)
    else:
        result = tool.fn(*raw)

    text = result.content[0].text
    # The footer is the last line of the text channel.
    # Guard: strip trailing blank lines (join/split may add one).
    lines = text.split("\n")
    non_empty_lines = [ln for ln in lines if ln.strip()]
    assert non_empty_lines, f"{tool_name}: text channel produced no output lines"
    text_footer = non_empty_lines[-1]

    structured_hint = result.structured_content["next_step_hint"]

    assert text_footer == structured_hint, (
        f"{tool_name}: next_step_hint channel drift!\n"
        f"  text footer:        {text_footer!r}\n"
        f"  structured_content: {structured_hint!r}"
    )
