"""tests/test_mcp_tool_descriptions.py — M7.5 T1

Verify that every MCP tool description (docstring exposed via FastMCP):
1. Contains TRIGGER when:, PREFER over:, SKIP when: routing blocks.
2. Is ≤1500 characters (FastMCP strips beyond that).

FastMCP wraps @mcp.tool() functions into FunctionTool objects that expose
.description (= the full docstring text). We access them via mcp._tool_manager.
"""
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
    # D3 superset tools (ADR-0028)
    "model_inspect",
    "module_inspect",
    "entity_lookup",
]

_REQUIRED_BLOCKS = ("TRIGGER when:", "PREFER over:", "SKIP when:")


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
