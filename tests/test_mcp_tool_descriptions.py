# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/test_mcp_tool_descriptions.py — M7.5 T1

Verify that every MCP tool description (docstring exposed via FastMCP):
1. Contains TRIGGER when:, PREFER over:, SKIP when: routing blocks.
2. Is ≤1500 characters (FastMCP strips beyond that).
3. Contains ≥1 Vietnamese diacritic in the TRIGGER block (ADR-0012 §2 convention).
   Session-state tools (set_active_*, list_available_*) are exempt — they are
   internal plumbing utilities, not user-facing Odoo query tools.

FastMCP wraps @mcp.tool() functions into FunctionTool objects that expose
.description (= the full docstring text). We access them via mcp._tool_manager.
"""
import re

import pytest

from src.mcp.server import mcp

_TOOL_NAMES = [
    # M1–M5 core tools (10)
    "find_examples",
    "impact_analysis",
    "lookup_core_api",
    "find_deprecated_usage",
    "lint_check",
    "cli_help",
    "api_version_diff",
    "suggest_pattern",
    "check_module_exists",
    "find_override_point",
    # M9 Wave 1 entity-enumeration: describe_module (the 6 flat enumeration tools
    # list_fields/list_methods/list_views/list_owl_components/list_qweb_templates/
    # list_js_patches were removed in v0.6; use model_inspect/module_inspect instead)
    "describe_module",
    # D3 superset tools (ADR-0028)
    "model_inspect",
    "module_inspect",
    "entity_lookup",
    # E3 session-context tools (ADR-0029)
    "set_active_version",
    "set_active_profile",
    "list_available_versions",
    "list_available_profiles",
    # M10A stylesheet tools (ADR-0025, D5/D6)
    "resolve_stylesheet",
    "find_style_override",
    # M10.5 P2 ORM-validation tools
    "resolve_orm_chain",
    "validate_domain",
    "validate_depends",
    "validate_relation",
]

# Odoo-content query tools that MUST contain ≥1 Vietnamese diacritic in their
# TRIGGER block (ADR-0012 §2 exception — docstrings keep EN+VI for router accuracy).
# Session-state tools are intentionally excluded: they are internal plumbing
# utilities, not user-facing query tools where Vietnamese routing matters.
_VI_TRIGGER_TOOL_NAMES = [
    # M1–M5 core tools
    "find_examples",
    "impact_analysis",
    "lookup_core_api",
    "find_deprecated_usage",
    "lint_check",
    "cli_help",
    "api_version_diff",
    "suggest_pattern",
    "check_module_exists",
    "find_override_point",
    # M9 entity-enumeration (remaining after v0.6 flat-tool removal)
    "describe_module",
    # D3 superset tools (ADR-0028)
    "model_inspect",
    "module_inspect",
    "entity_lookup",
    # M10A stylesheet tools (ADR-0025, D5/D6)
    "resolve_stylesheet",
    "find_style_override",
    # M10.5 P2 ORM-validation tools
    "resolve_orm_chain",
    "validate_domain",
    "validate_depends",
    "validate_relation",
]

_REQUIRED_BLOCKS = ("TRIGGER when:", "PREFER over:", "SKIP when:")

# WI-4 (ADR-0029 amend): tools that carry an ``odoo_version`` parameter must
# mark it REQUIRED in their JSON-Schema so an LLM cannot silently omit it and
# get wrong-version data via the latest-fallback resolver. These are every
# version-bearing tool EXCEPT the bootstrap/session tools below.
_VERSION_REQUIRED_TOOL_NAMES = [
    "find_examples",
    "impact_analysis",
    "lookup_core_api",
    "find_deprecated_usage",
    "lint_check",
    "cli_help",
    "suggest_pattern",
    "check_module_exists",
    "find_override_point",
    "describe_module",
    "model_inspect",
    "module_inspect",
    "entity_lookup",
    "resolve_stylesheet",
    "find_style_override",
    "resolve_orm_chain",
    "validate_domain",
    "validate_depends",
    "validate_relation",
]

# Bootstrap/session + two-version tools that must NOT require ``odoo_version``
# (they are how a client discovers/sets the active version, or diff two
# explicit versions). list_available_* take no version at all; set_active_profile
# / set_active_version / api_version_diff use other required params.
_VERSION_NOT_REQUIRED_TOOL_NAMES = [
    "list_available_versions",
    "list_available_profiles",
    "set_active_profile",
    "api_version_diff",
]

# Regex matches any Unicode character in the Vietnamese extended Latin block.
_VI_DIACRITIC_RE = re.compile(r"[À-ỹ]")


def _get_tool_description(name: str) -> str:
    """Return the FunctionTool.description for a registered mcp tool."""
    tool = mcp._tool_manager._tools.get(name)
    assert tool is not None, (
        f"Tool '{name}' not found in mcp._tool_manager._tools. "
        f"Available tools: {list(mcp._tool_manager._tools.keys())}"
    )
    return tool.description or ""


def _get_tool_input_schema(name: str) -> dict:
    """Return the FunctionTool JSON inputSchema for a registered mcp tool."""
    tool = mcp._tool_manager._tools.get(name)
    assert tool is not None, (
        f"Tool '{name}' not found in mcp._tool_manager._tools. "
        f"Available tools: {list(mcp._tool_manager._tools.keys())}"
    )
    return tool.parameters or {}


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_tool_has_trigger_prefer_skip(tool_name):
    """Each tool description must contain TRIGGER when:, PREFER over:, SKIP when:."""
    desc = _get_tool_description(tool_name)
    for block in _REQUIRED_BLOCKS:
        assert block in desc, (
            f"'{tool_name}' description missing '{block}'. "
            f"First 200 chars: {desc[:200]!r}"
        )


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_tool_description_length(tool_name):
    """Each tool description must be ≤1500 chars (FastMCP budget)."""
    desc = _get_tool_description(tool_name)
    assert len(desc) <= 1500, (
        f"'{tool_name}' description is {len(desc)} chars, exceeds 1500-char budget. "
        "Trim the docstring."
    )


@pytest.mark.parametrize("tool_name", _VI_TRIGGER_TOOL_NAMES)
def test_tool_trigger_has_vietnamese(tool_name):
    """TRIGGER block of each Odoo-content query tool must contain ≥1 Vietnamese diacritic.

    Safeguard for ADR-0012 §2 convention: TRIGGER docstrings keep EN+VI so the
    MCP router (and LLM agents routing in Vietnamese) can match tool intents.
    This test fails if a future PR strips the Vietnamese trigger phrases.
    """
    desc = _get_tool_description(tool_name)
    # Extract the TRIGGER section: from "TRIGGER when:" up to the next block header.
    trigger_match = re.search(
        r"TRIGGER when:.*?(?=PREFER over:|SKIP when:|\Z)", desc, re.DOTALL
    )
    assert trigger_match is not None, (
        f"'{tool_name}' description has no TRIGGER section — "
        "check test_tool_has_trigger_prefer_skip."
    )
    trigger_text = trigger_match.group()
    assert _VI_DIACRITIC_RE.search(trigger_text) is not None, (
        f"'{tool_name}' TRIGGER block contains no Vietnamese diacritic character. "
        f"Add ≥1 VI phrase per ADR-0012 §2. TRIGGER text:\n{trigger_text!r}"
    )


# ---------------------------------------------------------------------------
# WI-4 (ADR-0029 amend) — odoo_version hard-required on version-bearing tools.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", _VERSION_REQUIRED_TOOL_NAMES)
def test_odoo_version_is_required(tool_name):
    """Every version-bearing tool must list ``odoo_version`` in its JSON-Schema
    ``required`` array, so an MCP client cannot silently omit it (which used to
    fall through to the latest-indexed version and return WRONG-version data).

    This test FAILS on the pre-WI-4 code where ``odoo_version`` had an ``"auto"``
    default (and was therefore NOT required).
    """
    schema = _get_tool_input_schema(tool_name)
    props = schema.get("properties", {})
    assert "odoo_version" in props, (
        f"'{tool_name}' has no odoo_version parameter at all — expected one."
    )
    required = schema.get("required", [])
    assert "odoo_version" in required, (
        f"'{tool_name}' does NOT mark odoo_version as required. "
        f"required={required!r}. Mark it RequiredOdooVersion (no default)."
    )


@pytest.mark.parametrize("tool_name", _VERSION_NOT_REQUIRED_TOOL_NAMES)
def test_odoo_version_not_required_for_bootstrap_tools(tool_name):
    """Bootstrap/session + two-version tools must NOT require ``odoo_version`` —
    they are how a client discovers/sets the version, or diff two explicit
    versions, so the call must succeed without an ``odoo_version`` argument.
    """
    schema = _get_tool_input_schema(tool_name)
    required = schema.get("required", [])
    assert "odoo_version" not in required, (
        f"'{tool_name}' must NOT require odoo_version (bootstrap/session tool). "
        f"required={required!r}."
    )


# ---------------------------------------------------------------------------
# WI-4 follow-up — docstring prose must not contradict the REQUIRED contract.
# ---------------------------------------------------------------------------

_STALE_AUTO_PATTERNS = [
    "Default 'auto'",
    'Default "auto"',
    "Defaults to 'auto'",
    "'auto' = latest indexed",
    '"auto" = latest indexed',
    "/ 'auto'",
]


@pytest.mark.parametrize("tool_name", _VERSION_REQUIRED_TOOL_NAMES)
def test_required_version_tool_docstring_no_stale_auto(tool_name):
    """Docstrings of version-required tools must NOT say 'Default auto' or
    similar phrases that contradict the REQUIRED contract (WI-4 follow-up).

    LLMs read tool docstrings surfaced by FastMCP. If the prose says
    "Default 'auto'" for a required parameter, the LLM infers it is optional
    and silently omits it, triggering a ValidationError loop.
    """
    desc = _get_tool_description(tool_name)
    for pattern in _STALE_AUTO_PATTERNS:
        assert pattern not in desc, (
            f"'{tool_name}' docstring contains stale phrase {pattern!r} for odoo_version "
            f"which is REQUIRED. Replace with 'REQUIRED — concrete Odoo version, e.g. "
            f"\"17.0\".' to match the contract."
        )


# ---------------------------------------------------------------------------
# Review #4 (code-review finding) — SSOT: odoo_version description lives in
# Field(description=...), NOT duplicated in docstring Args section.
# ---------------------------------------------------------------------------

# Generic phrases that belong only in the Field SSOT, not the docstring.
# When a tool re-states these in its Args block, the same prose drifts in two
# places and costs the LLM extra tokens to read the same constraint twice.
_GENERIC_VERSION_PROSE = "See list_available_versions"


@pytest.mark.parametrize("tool_name", _VERSION_REQUIRED_TOOL_NAMES)
def test_odoo_version_schema_description_not_empty(tool_name):
    """The JSON-Schema property description for odoo_version must be non-empty.

    FastMCP sources it from Field(description=...) — the single SSOT.  This
    guard fails if someone removes the Field description, losing the LLM hint.
    """
    schema = _get_tool_input_schema(tool_name)
    props = schema.get("properties", {})
    ov_desc = props.get("odoo_version", {}).get("description", "")
    assert ov_desc, (
        f"'{tool_name}' odoo_version schema property has no description. "
        "Restore the Field(description=...) in RequiredOdooVersion."
    )


@pytest.mark.parametrize("tool_name", _VERSION_REQUIRED_TOOL_NAMES)
def test_odoo_version_docstring_no_generic_prose(tool_name):
    """Docstring Args block must NOT duplicate the generic version prose from
    the Field SSOT.

    The phrase 'See list_available_versions' belongs exclusively in the Field
    description (JSON-Schema param description), not repeated in the docstring.
    Duplication creates two drift-prone sources for the same guidance.

    Exception: find_override_point keeps a tool-specific semantic
    ('From-version when in diff mode') — the guard targets only the generic
    prose, not all mentions of odoo_version in docstrings.
    """
    desc = _get_tool_description(tool_name)
    assert _GENERIC_VERSION_PROSE not in desc, (
        f"'{tool_name}' docstring contains {_GENERIC_VERSION_PROSE!r} which "
        "duplicates the Field SSOT. Remove the odoo_version Args line from the "
        "docstring; FastMCP already surfaces the Field description in the param "
        "schema."
    )
