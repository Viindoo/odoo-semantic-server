# Odoo Semantic — Cursor Rules

## Overview

These rules configure Cursor IDE to automatically route Odoo-related questions through the Odoo Semantic MCP server. Add them to `.cursorrules` in your project root, or paste into **Cursor Settings → Rules for AI** (applies globally).

---

## Add to `.cursorrules`

```
# Odoo Semantic MCP — Developer Rules
# Auto-triggers for Odoo codebase intelligence via MCP

## When to call Odoo Semantic tools

### Working with Python model files (models/*.py, *.py with `models.Model`)
- User asks about model structure → call resolve_model(model_name, odoo_version)
- User asks about a field → call resolve_field(field_name, model_name, odoo_version)
- User asks about a method → call resolve_method(method_name, model_name, odoo_version)
- User wants to add new behavior → call find_override_point(model_name, method_name, odoo_version)
- User wants code examples → call find_examples(natural_language_query)

### Working with XML view files (views/*.xml, *.xml with inherit_id)
- User asks about view structure → call resolve_view(view_id, odoo_version)
- User wants to override a view → call resolve_view first, then suggest XPath from the chain

### Before writing any new code
- Check for existing patterns → call suggest_pattern(description)
- Check module availability → call check_module_exists(module_name, odoo_version)

### Before using any Odoo core API
- Verify API status → call lookup_core_api(symbol_name, odoo_version)
- If writing upgrade code → call api_version_diff(symbol_name, from_version, to_version)

### Code review / pre-commit
- Scan for deprecated usage → call find_deprecated_usage(odoo_version)
- Check coding standards → call lint_check(module_name, odoo_version)

### Risk assessment before major changes
- Impact of field change → call impact_analysis("field", "model.field_name", odoo_version)
- Impact of method change → call impact_analysis("method", "model.method_name", odoo_version)

## Auto-trigger on file open
When a Python file with `class .*(models\.Model)` is opened:
- Silently resolve the model to pre-cache its structure
- Surface inheritance chain in a comment if the user asks "what is this model?"

## Odoo version detection
- Check pyproject.toml, setup.cfg, or __manifest__.py for version hints
- Default to "17.0" if version not found in project
- Always pass detected version to MCP tool calls

## Response formatting
- Inheritance chains: use ├─ └─ tree notation
- Field info: type, required, compute/related, string label
- Method info: full override chain with module names
- Risk levels: always bold HIGH, MEDIUM, LOW
- Module names: always in `backticks`

## Developer workflow
1. Open model file → resolve_model to understand inheritance
2. Find extension point → find_override_point before writing override
3. Check pattern → suggest_pattern for the implementation approach
4. Verify API → lookup_core_api for any core methods used
5. After writing → lint_check to verify standards
6. Before PR → find_deprecated_usage + impact_analysis for risky changes
```

---

## Global Rules (Cursor Settings → Rules for AI)

For workspace-agnostic use, paste this shorter version into **Cursor → Settings → Rules for AI**:

```
When working with Odoo Python or XML files, use the odoo-semantic MCP tools:
- Model questions → resolve_model
- Field questions → resolve_field  
- Method questions → resolve_method
- View questions → resolve_view
- "Where to add X" → find_override_point
- "Best practice for X" → suggest_pattern
- "Does Odoo have X" → check_module_exists
- "Is [API] deprecated" → lookup_core_api
- "What changed in upgrade" → api_version_diff
- "What breaks if I change X" → impact_analysis
Always call the tool before answering codebase-specific questions.
Default Odoo version: 17.0 (detect from project manifest if available).
```

---

## Example `.cursorrules` (Minimal)

For a project already configured for Odoo 17.0 development:

```
# .cursorrules — Odoo 17.0 project

## MCP tools (use odoo-semantic for all Odoo questions)

When user asks about Odoo models, fields, methods, views, or patterns:
- ALWAYS call the relevant odoo-semantic MCP tool first
- Default version: 17.0
- Never fabricate module names, field types, or method signatures
- After getting tool results, summarize clearly with tree notation for chains

## Key mappings
- "how does X work" → resolve_method or resolve_model
- "where to override" → find_override_point  
- "add functionality to" → find_override_point + suggest_pattern
- "impact of changing" → impact_analysis
- "deprecated / upgrade" → find_deprecated_usage + api_version_diff
- "show me code for" → find_examples
- "does Odoo have" → check_module_exists
```

---

## Verify Setup

In Cursor chat, type:
```
Using odoo-semantic, what is the inheritance chain of sale.order in Odoo 17.0?
```

**Expected:** Structured tree output with module names from the index.
**If Cursor answers from training data:** Check that the MCP server is configured in Cursor settings under `mcp.json`.

### Add MCP server to Cursor

In `~/.cursor/mcp.json` (or project `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "odoo-semantic": {
      "type": "http",
      "url": "https://odoo-semantic.viindoo.com:9999/mcp",
      "headers": {
        "X-API-Key": "<YOUR_API_KEY>"
      }
    }
  }
}
```
