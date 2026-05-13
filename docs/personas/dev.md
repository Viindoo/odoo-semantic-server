# Odoo Semantic — Developer Guide

> **Get started:** Install the [Odoo Semantic plugin](../../dist/odoo-semantic-plugin/README.md) or run `/odoo-semantic:connect` in Claude Code. For other AI tools, see [client setup](../client-setup.md).

The full 14-tool arsenal, optimized for development workflows. From understanding inheritance to safely extending core methods, this guide covers the daily patterns.

---

## All Tools Available to Developers

| Tool | Use case |
|------|----------|
| `resolve_model` | Full inheritance chain + fields + methods for any model |
| `resolve_field` | Field type, compute/related details, extension chain |
| `resolve_method` | Override chain, super() call graph |
| `resolve_view` | XPath inheritance chain for any XML view |
| `find_examples` | Semantic code search across indexed repos |
| `impact_analysis` | Risk assessment before changing a field or method |
| `lookup_core_api` | Verify an API symbol exists and is not deprecated |
| `api_version_diff` | Identify breaking changes between Odoo versions |
| `find_deprecated_usage` | Audit your module for deprecated API usage |
| `lint_check` | Check module against Odoo coding standards |
| `suggest_pattern` | Find the canonical implementation pattern |
| `check_module_exists` | Verify module availability + CE/EE flag |
| `find_override_point` | Locate the safest method to override |
| `cli_help` | Look up `odoo-bin` flags and options |

---

## Standard Development Workflow

### 1. Understand before touching

Before adding logic to a model:

```
resolve_model("sale.order", "17.0")
```

Get the full inheritance chain, field count, method list, and which modules have already extended this model. Know what you're stepping into before writing a single line.

### 2. Find the right extension point

Before writing an `@api.onchange`, `_compute_*`, or `super()` call:

```
find_override_point("sale.order", "action_confirm", "17.0")
```

Returns `super_safety` score and which modules are already overriding this method. If `super_ratio` is low, your override is at higher risk of being called out-of-order.

### 3. Get the pattern right

Before implementing a new pattern (computed cross-model field, wizard, report):

```
suggest_pattern("computed field that aggregates from child records with currency conversion")
```

Returns curated `PatternExample` nodes with code snippets, gotchas, and anti-pattern warnings from the indexed codebase.

### 4. Verify the API

Before calling any `@api.*` decorator, `name_get`, `_name_search`, or ORM method:

```
lookup_core_api("name_get", "17.0")
```

If the result shows `status: deprecated` or `removed_in: 17.0` — find the replacement before building on it.

### 5. Check your work

After writing the module:

```
lint_check("my_module", "17.0")
find_deprecated_usage("17.0")
```

---

## Sample Developer Questions

Copy these prompts into your AI tool:

1. **Model exploration:**
   > "Using odoo-semantic, resolve model account.move in Odoo 17.0. Show me the full inheritance chain and list fields added by each module."

2. **Safe extension:**
   > "Using odoo-semantic, find_override_point for account.move action_post in Odoo 17.0. Is it safe to override? What is the super_ratio?"

3. **Pattern lookup:**
   > "Using odoo-semantic, suggest_pattern for implementing an onchange that updates a computed monetary field across multiple models in Odoo 17."

4. **Pre-upgrade audit:**
   > "Using odoo-semantic, find_deprecated_usage for Odoo 17.0 in our codebase. List all HIGH risk items with file locations."

5. **View override:**
   > "Using odoo-semantic, resolve_view for sale.view_order_form in Odoo 17.0. Show the full XPath chain so I know exactly where to inject my override."

---

## Plugin Skills (Claude Code)

If you use **Claude Code** with the Odoo Semantic plugin:

| Skill | What it does |
|-------|-------------|
| `/odoo-override-finder` | Given a model + method, returns safe override point + existing overrides + suggest_pattern |
| `/odoo-deprecation-audit` | Full deprecated API scan with replacement suggestions |
| `/odoo-version-diff` | Side-by-side API diff between two Odoo versions for a given symbol |

---

## Tips

- Always pass the `odoo_version` parameter — results differ significantly between versions.
- `find_override_point` returns `anti_patterns` — read them before writing.
- If `resolve_model` shows more than 10 modules extending a model, consider whether your extension logic could conflict with others.
- `suggest_pattern` queries are semantic, not keyword — describe what you want to achieve, not what method to use.
