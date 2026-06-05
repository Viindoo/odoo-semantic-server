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
    # WI-4 profile introspection discriminator (ADR-0028/0029) — 25th tool.
    "profile_inspect",
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
    # WI-4 profile introspection query tool — TRIGGER keeps EN+VI for router accuracy.
    "profile_inspect",
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
    # WI-4 profile introspection (ADR-0028/0029): uses RequiredOdooVersion at the
    # tool boundary, so it must honour the same version-required + no-stale-auto
    # docstring convention as every other version-bearing tool (N7).
    "profile_inspect",
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


# ---------------------------------------------------------------------------
# WI-6 (#262-A) — model_inspect limit docstring discloses real cap + start_index
# ADR-0023 §3: caps (50/20/10) are intentional; continuation is via start_index,
# never by raising limit.
# ---------------------------------------------------------------------------


def test_model_inspect_limit_docstring_discloses_cap():
    """WI-6: model_inspect docstring must disclose the effective cap (50/20/10)
    and start_index as the pagination cursor, per ADR-0023 §3 + §5.5.

    Guards against a regression where the docstring says 'default 200' without
    disclosing that the effective max is 50 for fields, 20 for methods/views —
    which previously caused agents to pass limit=200 and receive only 50 rows
    with no explanation.
    """
    desc = _get_tool_description("model_inspect")
    # Must disclose the cap boundary values explicitly.
    assert "50" in desc, (
        "model_inspect docstring must disclose the cap=50 for fields (ADR-0023 §3). "
        f"Current desc: {desc!r}"
    )
    assert "20" in desc, (
        "model_inspect docstring must disclose the cap=20 for methods/views (ADR-0023 §3). "
        f"Current desc: {desc!r}"
    )
    # Must mention start_index as the pagination mechanism.
    assert "start_index" in desc, (
        "model_inspect docstring must name start_index as the pagination cursor. "
        f"Current desc: {desc!r}"
    )
    # The docstring MUST carry a `limit:` arg section (model_inspect is paged) —
    # asserting its presence keeps the next check non-vacuous (N4: the old
    # `... if 'limit: ' in desc else True` silently passed if the section was
    # ever renamed/dropped).
    assert "limit: " in desc, (
        "model_inspect docstring must document the limit: parameter so the cap "
        "(50/20) is disclosed at the argument site (ADR-0023 §3)."
    )
    # Within that limit: section, must NOT suggest raising limit= to get more rows
    # (start_index is the continuation mechanism per ADR-0023 §5.5 amendment).
    limit_section = desc.split("limit: ", 1)[1]
    assert "limit=" not in limit_section, (
        "model_inspect limit description must not suggest raising limit= as "
        "a continuation mechanism (use start_index instead per ADR-0023 §5.5 amendment). "
        f"limit: section was:\n{limit_section!r}"
    )


def test_model_inspect_limit_hint_no_limit_raising():
    """WI-6 code-side: server.py must not suggest raising limit= in field pagination.

    The _list_fields more_hint previously used a limit-doubling expression that
    was dead code (cap prevents it) AND contradicted the start_index amendment
    (ADR-0023 §5.5). Guard that this anti-pattern is gone from server.py.
    """
    from pathlib import Path
    src_path = Path(__file__).parent.parent / "src" / "mcp" / "server.py"
    source = src_path.read_text(encoding="utf-8")
    # The dead pattern was: limit={max(limit * 2, total)} — check by joining
    # the constituent parts so this test file itself does not match.
    bad_pattern = "limit" + " * " + "2"
    assert bad_pattern not in source, (
        f"WI-6: {bad_pattern!r} found in server.py — this was the dead limit-raising "
        "more_hint that contradicts ADR-0023 §5.5. It must be replaced with "
        "a start_index-based continuation hint."
    )


# ---------------------------------------------------------------------------
# WI-3 (#258, #259-B) — odoo_version='auto' + profile_name semantics alignment
# ADR-0029 WI-4 amendment: 'auto' resolves to session pin on version-bearing
# tools. ADR-0016 profile inheritance: profile filter is inheritance-resolved.
# ---------------------------------------------------------------------------


def test_required_odoo_version_field_mentions_auto_post_pin():
    """WI-3 (#258): RequiredOdooVersion field description must tell agents that
    'auto' is accepted on subsequent calls to reuse the session pin set by
    set_active_version (ADR-0029 WI-4 amendment).

    Guards the cross-tool guidance: an agent reading only the odoo_version
    parameter description of any version-bearing tool must learn about 'auto'.
    """
    schema = _get_tool_input_schema("model_inspect")
    props = schema.get("properties", {})
    ov_desc = props.get("odoo_version", {}).get("description", "")
    assert "'auto'" in ov_desc or "auto" in ov_desc, (
        "RequiredOdooVersion field description must mention 'auto' as the "
        "post-pin shorthand (ADR-0029 WI-4 amendment). "
        f"Current description: {ov_desc!r}"
    )
    # Specifically the 'set_active_version' reference so agents know the flow.
    assert "set_active_version" in ov_desc, (
        "RequiredOdooVersion field description must reference set_active_version "
        "so agents know how to obtain the pin. "
        f"Current description: {ov_desc!r}"
    )


def test_set_active_version_docstring_distinguishes_pin_vs_reuse():
    """WI-3 (#258): set_active_version docstring must clarify that sentinels are
    rejected AS THE VERSION TO PIN (not globally), while 'auto' IS accepted on
    subsequent tool calls to reuse the pin (ADR-0029).
    """
    desc = _get_tool_description("set_active_version")
    # Must still say sentinels are rejected (correct for the tool itself).
    assert "rejected" in desc, (
        "set_active_version docstring should mention sentinels are rejected "
        "as pin payloads. "
        f"Current desc: {desc!r}"
    )
    # Must clarify that 'auto' is accepted on SUBSEQUENT tool calls.
    assert "auto" in desc, (
        "set_active_version docstring must mention 'auto' as the mechanism "
        "for reusing the pin on subsequent tool calls (ADR-0029). "
        f"Current desc: {desc!r}"
    )
    # Must not leave agents with the impression 'auto' is globally rejected.
    assert "subsequent" in desc or "reuse" in desc, (
        "set_active_version docstring must clarify that 'auto' is accepted on "
        "subsequent calls after pinning (ADR-0029 WI-4 amendment). "
        f"Current desc: {desc!r}"
    )


@pytest.mark.parametrize("tool_name", [
    "model_inspect",
    "module_inspect",
    "check_module_exists",
    "find_deprecated_usage",
])
def test_profile_name_docstring_mentions_inheritance_resolved(tool_name):
    """WI-3 (#259-B): Tools with profile_name parameter must document that the
    filter is inheritance-resolved (includes parent profiles via ancestor chain).

    Guards the agent-facing semantics: an agent reading the profile_name param
    description must understand that a child profile also includes parent
    profile content (ADR-0016 profile inheritance).
    """
    desc = _get_tool_description(tool_name)
    assert "inheritance" in desc.lower() or "ancestor" in desc.lower(), (
        f"'{tool_name}' profile_name description must mention 'inheritance' or "
        "'ancestor' to convey that profile filtering is inheritance-resolved "
        "(ADR-0016). "
        f"Relevant fragment: {desc[desc.find('profile_name'):desc.find('profile_name')+200]!r}"
    )
