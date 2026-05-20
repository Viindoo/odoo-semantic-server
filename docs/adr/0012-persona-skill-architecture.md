# ADR-0012 — Persona Skill Architecture

**Date:** 2026-05-12
**Status:** Accepted

## Context

Odoo Semantic MCP exposes 14 tools that collectively cover model introspection, field and method resolution, view inheritance, semantic code search, impact analysis, upgrade auditing, and curated pattern guidance. However, adoption has revealed a routing gap:

1. **AI clients do not auto-route to the right tool.** When a user asks "does Odoo have a subscription module?", Claude Code, ChatGPT, and Gemini all default to answering from training data rather than calling `check_module_exists`. The tools are present but invisible to the routing model unless it receives strong priming.

2. **Non-technical personas cannot reach the tools via technical queries.** A CEO asking "what is our upgrade risk?" does not know to call `find_deprecated_usage`. A salesperson asking "can Odoo do X?" does not know to call `check_module_exists`. The gap between a role's natural vocabulary and the tool's invocation pattern prevents adoption by non-developer users.

3. **Each AI client has a different routing mechanism.** Claude Code uses skill invocation and MCP tool descriptions; Gemini Gems use system instructions; Custom GPTs use Action schemas and system prompts; Cursor uses `.cursorrules` files. There is no unified way to ship routing logic across all clients.

## Decision

### 1. TRIGGER/PREFER/SKIP docstring protocol

All 14 MCP tool docstrings follow a standard template:

```
TRIGGER: natural-language phrases that should invoke this tool
PREFER: conditions when this tool is the primary choice
SKIP: conditions when this tool should NOT be called
ARGS: required and optional parameters with descriptions
```

This protocol makes routing logic machine-readable to all inference clients. Models that respect system instructions (Claude, GPT-4, Gemini) will match user utterances against TRIGGER phrases embedded in the tool's own description. This is a zero-dependency routing mechanism: no middleware, no extra API calls.

The TRIGGER phrases are written in the user's natural vocabulary per persona:
- CEO vocabulary: "risk", "upgrade", "what breaks", "does Odoo have"
- Developer vocabulary: "override chain", "inheritance", "where to add", "safe to extend"
- Sales vocabulary: "does Odoo support", "can we show", "is this CE or EE"

### 2. Claude Code plugin package — persona skills

An optional plugin package (now at [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client)) ships 11 persona-aware Claude Code skills. Each skill pre-wires tool routing for a specific job-to-be-done:

| Skill | Persona | Tools orchestrated |
|-------|---------|-------------------|
| `odoo-risk-overview` | CEO | `impact_analysis`, `find_deprecated_usage`, `check_module_exists` |
| `odoo-customization-inventory` | CEO | `resolve_model`, `check_module_exists` |
| `odoo-override-finder` | Developer | `find_override_point`, `resolve_method`, `suggest_pattern` |
| `odoo-deprecation-audit` | Developer | `find_deprecated_usage`, `api_version_diff`, `lookup_core_api` |
| `odoo-version-diff` | Developer/Marketer | `api_version_diff`, `lookup_core_api` |
| `odoo-feature-check` | Consultant | `check_module_exists`, `resolve_model`, `find_examples` |
| `odoo-gap-analysis` | Consultant | `check_module_exists`, `find_examples`, `lookup_core_api` |
| `odoo-feature-highlights` | Marketer | `api_version_diff`, `find_examples`, `resolve_model` |
| `odoo-addon-diff` | Marketer | `check_module_exists`, `resolve_model` |
| `odoo-capability-proof` | Sales | `find_examples`, `check_module_exists`, `resolve_model` |
| `odoo-objection-handler` | Sales | `check_module_exists`, `find_examples`, `suggest_pattern` |

Each skill wraps tool calls in opinionated prompts that:
- Translate business-vocabulary input into correct tool arguments
- Present results in role-appropriate language (executive summary for CEO; code detail for devs)
- Surface the most relevant output signal first

### 3. Cross-vendor adapter files

Three adapter files ship in [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client):

- [`snippets/gemini-gem-instructions.md`](https://github.com/Viindoo/odoo-mcp-client/blob/master/snippets/gemini-gem-instructions.md) — System instructions for a Gemini Gem, including TRIGGER-matched routing rules for all 14 tools and persona-mode detection
- [`snippets/openai-gpt-instructions.md`](https://github.com/Viindoo/odoo-mcp-client/blob/master/snippets/openai-gpt-instructions.md) — System prompt for a Custom GPT, plus OpenAPI Action schema for the MCP endpoint
- [`snippets/cursor-rules.md`](https://github.com/Viindoo/odoo-mcp-client/blob/master/snippets/cursor-rules.md) — `.cursorrules` content for Cursor IDE, with file-type-based auto-trigger rules (Odoo Python files → resolve_model; XML files → resolve_view)

These files are maintained as plain Markdown that admins can copy-paste. They do not require programmatic generation.

### 4. Persona onboarding guides

Five role-specific guides in [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client) at `docs/personas/`:

- `ceo.md` — natural-language prompts, tool table, plugin skill reference
- `dev.md` — full tool list, development workflow, plugin skills
- `consultant.md` — feature gap workflow, CE/EE disambiguation, estimation guide
- `marketer.md` — content research workflow, accuracy checklist
- `sales.md` — capability proof workflow, objection handling, demo prep

Guides are written in English and target a non-developer audience for all non-dev personas.

## Consequences

### Positive

- Any AI client that respects system instructions (Claude, GPT, Gemini, Cursor) will route correctly without any server-side changes.
- Non-technical personas can now reach all 14 tools via natural-language questions.
- The plugin package is optional and additive — existing users are unaffected.
- Cross-vendor adapters require no maintenance unless the tool list changes.

### Negative / maintenance overhead

- **Docstring maintenance:** TRIGGER/PREFER/SKIP sections in tool docstrings must be updated whenever a tool is added, renamed, or its arguments change. Risk: divergence between docstring routing and actual tool behavior.
- **Plugin currency:** The 11 persona skills must be updated when new MCP tools are added (M8+). Skills referencing removed or renamed tools will silently fail to call the correct tool.
- **Disambiguation test gate:** A routing accuracy test (targeting ≥80% correct tool selection across a fixed 50-prompt benchmark) must pass in CI before any docstring change is merged. Test is in `tests/test_tool_routing_disambiguation.py`.

### Rejected alternatives

**System prompt injection via server response:** The MCP server could embed routing instructions in every tool call response. Rejected because: (1) it increases response payload size for all 14 tools; (2) clients that cache tool descriptions would not see updates until re-initialization; (3) it conflates routing metadata with tool output.

**Separate routing microservice:** A small HTTP service that accepts user utterances and returns the correct tool name. Rejected because: (1) adds an extra network hop and deployment dependency; (2) over-engineering for current scale (<100 concurrent users); (3) the TRIGGER-in-docstring approach achieves equivalent routing at zero deployment cost.

**Per-client system prompt scripts:** Generate client-specific system prompts from a template engine at deploy time. Rejected because: (1) adds a build step; (2) copy-paste adapter files are simpler to maintain and audit; (3) clients update their system prompt support independently — a generated file may become stale faster than a maintained template.

## References

- [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client) — Plugin package, cross-vendor adapters, persona guides
- `tests/test_tool_routing_disambiguation.py` — Routing accuracy gate (target ≥80%)
- ADR-0011 — Web UI session auth (M7 W16)
- ADR-0009 — Pattern catalogue community contribution (M6 Wave 3)
