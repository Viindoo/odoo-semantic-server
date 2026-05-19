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
    "resolve_model",
    "resolve_field",
    "resolve_method",
    "resolve_view",
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
    # M9 Wave 1 entity-enumeration tools (ADR-0023)
    "describe_module",
    "list_fields",
    "list_methods",
    "list_views",
    "list_owl_components",
    "list_qweb_templates",
    "list_js_patches",
    # D3 superset tools (ADR-0028)
    "model_inspect",
    "module_inspect",
    "entity_lookup",
    # E3 session-context tools (ADR-0029)
    "set_active_version",
    "set_active_profile",
    "list_available_versions",
    "list_available_profiles",
]

# Odoo-content query tools that MUST contain ≥1 Vietnamese diacritic in their
# TRIGGER block (ADR-0012 §2 exception — docstrings keep EN+VI for router accuracy).
# Session-state tools are intentionally excluded: they are internal plumbing
# utilities, not user-facing query tools where Vietnamese routing matters.
_VI_TRIGGER_TOOL_NAMES = [
    "resolve_model",
    "resolve_field",
    "resolve_method",
    "resolve_view",
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
    "describe_module",
    "list_fields",
    "list_methods",
    "list_views",
    "list_owl_components",
    "list_qweb_templates",
    "list_js_patches",
    "model_inspect",
    "module_inspect",
    "entity_lookup",
]

_REQUIRED_BLOCKS = ("TRIGGER when:", "PREFER over:", "SKIP when:")

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
