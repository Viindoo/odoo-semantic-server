# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_server_instructions.py
"""Guard the server-level disambiguation surfaced to MCP clients.

`mcp = FastMCP("odoo-semantic", instructions=...)` ships a block that every
connecting client (Claude/Codex/Gemini/Cursor) reads at initialize. It must keep
two silent mis-routes from happening - both return plausible-but-wrong answers
with no error to self-correct on:

  1. Confusing this STATIC source index with a LIVE-instance Odoo MCP.
  2. Skipping this index and reading the huge Odoo codebase directly (burns
     context). OSM is the PRIMARY source; reading code is the FALLBACK.

The guidance lives ONLY in the server instructions (single carrier), NOT
duplicated per-tool: FastMCP enforces a ~1500-char per-description budget
(tests/test_mcp_tool_descriptions.py), and the superset tool descriptions are
already near that cap. So this file checks the instructions string, not tool
docstrings. Unit-level: no Neo4j/Postgres needed.
"""
import re

from src.mcp.server import INSTRUCTIONS, mcp

# Tools whose names look live but return STATIC indexed source here. The
# instructions must enumerate them so an agent considering one is steered right.
LOOK_LIVE_TOOLS = (
    "model_inspect",
    "module_inspect",
    "entity_lookup",
    "describe_module",
    "check_module_exists",
    "validate_domain",
    "validate_depends",
    "validate_relation",
    "resolve_orm_chain",
)


def _norm(text: str) -> str:
    """Collapse whitespace so wrapped lines match as one string."""
    return re.sub(r"\s+", " ", text or "").strip()


def test_server_instructions_wired_into_fastmcp():
    """Clients read mcp.instructions at initialize, not the module constant."""
    assert INSTRUCTIONS, "server instructions must be non-empty"
    assert _norm(mcp.instructions) == _norm(INSTRUCTIONS)


def test_instructions_carry_static_vs_live_boundary():
    """Boundary 1: do not confuse the STATIC index with a live instance."""
    norm = _norm(INSTRUCTIONS)
    assert "STATIC" in norm
    assert "LIVE DATA" in norm
    assert "live Odoo MCP server" in norm
    # read_record is a single token (never wraps) -> robust capability marker.
    assert "read_record" in norm


def test_instructions_carry_osm_first_precedence():
    """Boundary 2: OSM is PRIMARY; reading the codebase is the FALLBACK, not the
    first move. This is the contract a prior version got backwards."""
    norm = _norm(INSTRUCTIONS)
    assert "PRIMARY" in norm
    assert "precedence" in norm
    assert "FALLBACK" in norm
    # Must explicitly tell the agent to read source only as a fallback step.
    assert "read the source" in norm


def test_instructions_carry_unique_signature():
    """A unique positive identity so a generic/future 'Odoo code' tool cannot
    claim the same niche: indexed, cross-version, inheritance-resolved,
    checkout-free."""
    norm = _norm(INSTRUCTIONS)
    assert "INDEXED" in norm
    assert "cross-version" in norm
    assert "inheritance" in norm
    assert "checkout-free" in norm


def test_instructions_enumerate_look_live_tools():
    """The instructions are the single carrier of per-tool steering, so every
    look-live-but-static tool must be named in them."""
    norm = _norm(INSTRUCTIONS)
    missing = [t for t in LOOK_LIVE_TOOLS if t not in norm]
    assert not missing, "look-live tools not named in instructions: " + ", ".join(missing)
