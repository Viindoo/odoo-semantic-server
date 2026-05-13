# Odoo Semantic Plugin for Claude Code

A Claude Code plugin that brings Odoo codebase intelligence into your AI coding workflow. Adds 15 persona-specific skills, 2 orchestration agents, and a setup command powered by the Odoo Semantic MCP server.

## Quick install (2 steps — both required)

```bash
claude plugin install dist/odoo-semantic-plugin/
```

Then inside Claude Code, run:

```
/odoo-semantic:setup
```

> ⚠️ **Step 2 is mandatory on Claude Code v2.1.x.** Plugin manifests use a
> `userConfig` block to collect the API key + MCP URL, but the CLI currently
> does not prompt for those values at install time
> ([anthropics/claude-code#39455](https://github.com/anthropics/claude-code/issues/39455),
> [#39827](https://github.com/anthropics/claude-code/issues/39827)). Without
> `/odoo-semantic:setup` the plugin loads its skills but the MCP server silently
> fails — `claude mcp list` will not show `odoo-semantic`.

## Available skills

| Skill | Persona | Description |
|-------|---------|-------------|
| `odoo-risk-overview` | CEO | Executive risk overview of customizations before upgrade |
| `odoo-customization-inventory` | CEO | Structured inventory of all custom modules and their business purpose |
| `odoo-override-finder` | Developer | Find the correct override point and pattern for a method |
| `odoo-deprecation-audit` | Developer | Audit deprecated API usage for upgrade readiness |
| `odoo-version-diff` | Developer + Marketer | Categorized diff of API and feature changes between versions |
| `odoo-feature-check` | Consultant | Check if a feature exists in standard CE or EE |
| `odoo-gap-analysis` | Consultant | Gap matrix of client requirements vs. standard Odoo |
| `odoo-feature-highlights` | Marketer | Marketing-friendly feature highlights for a version |
| `odoo-addon-diff` | Marketer | Side-by-side CE vs EE feature comparison |
| `odoo-capability-proof` | Sales | Evidence-based proof that Odoo supports a client requirement |
| `odoo-objection-handler` | Sales | ACA-structured responses to capability objections |
| `odoo-coder` | Developer | Python/XML backend coder with Odoo conventions baked in |
| `odoo-code-reviewer` | Developer | Review Odoo patches for ORM/inheritance/security pitfalls |
| `odoo-js-coder` | Developer | Legacy web client (v8–v14) JavaScript coder |
| `odoo-owl-coder` | Developer | OWL framework (v15+) component coder |

## Available agents

| Agent | Model | Role |
|-------|-------|------|
| `odoo-router` | Haiku | Classify a user query into the correct MCP tool (classify-only, no tool calls) |
| `odoo-upgrade-planner` | Sonnet | Orchestrate a full upgrade plan from source to target version |

## Setup command

```
/odoo-semantic:setup
```

Interactive setup that:
1. Prompts for your MCP server URL and API key
2. Validates key format (`osm_...`)
3. Updates `~/.claude.json` with the MCP server config
4. Runs a connectivity check against `resolve_model`

## Requirements

- **Odoo Semantic MCP server URL** — self-hosted or use `https://odoo-semantic.viindoo.com:9999/mcp`
- **API key** — format `osm_<alphanumeric>`, obtain from your server admin or via `/install/` endpoint
- Claude Code with MCP support (tested on v2.1.140)

## Alternative — skip the plugin, register the MCP server directly

If you only want the 14 MCP tools (`resolve_model`, `impact_analysis`, etc.) and
don't need the persona-specific skills, register the server without installing
the plugin:

```bash
claude mcp add --scope user --transport http odoo-semantic \
  https://odoo-semantic.viindoo.com:9999/mcp \
  --header "X-API-Key: osm_yourkey"
```

This is what `/odoo-semantic:setup` runs under the hood. The plugin adds 15
skills + 2 orchestration agents on top of the raw MCP server.

## For server admins — issuing API keys

Run on the server (not the client):

```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.manager create-api-key <name>
```

The raw key prints once (`osm_…`). Distribute over a secure channel.
