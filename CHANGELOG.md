# Changelog

All notable changes to Odoo Semantic MCP are documented here.

## [Unreleased]

### Added
- Auto-seed 26 master data profiles via `python -m src.db.migrate`: Odoo CE v8–v19, Standard Viindoo v8–v19 (delta: `tvtmaaddons`, `erponline-enterprise` v10+, `branding` v13+), and Viindoo Internal v17/v18 (delta: `saas-infrastructure`, `saas-infrastructure-common`, `themes` v17-only, `odoo-api`). Total 48 repos seeded with `clone_status='manual'`. Delta-only ownership — PostgreSQL `UNIQUE (url, branch)` forces each (url, branch) into one profile, so each tier owns only repos absent from the lower tier; admins index multiple profiles to get the full bundle. URL convention: `git@github.com:Viindoo/<repo>.git`.
- New CLI: `python -m src.manager seed-master-data` for idempotent re-seed; supports `--profiles-only` and `--reset` (destructive) flags.
- Upgrade runbook: `docs/deploy/master-data-upgrade.md` for production deployments — covers backup, verify, edge cases, and rollback.

## [0.2.0] — 2026-05-12

### M7.5 "Persona Wow"

**Track 1 — TRIGGER/PREFER/SKIP docstrings**
- Rewrote all 14 MCP tool docstrings with structured routing blocks (`TRIGGER when:`, `PREFER over:`, `SKIP when:`) so AI clients auto-pick the right tool from natural-language utterances (EN + VN)
- Added `tests/test_mcp_tool_descriptions.py` — enforces all 14 tools have TRIGGER/PREFER/SKIP and descriptions ≤ 1500 chars
- Extended `tests/test_smoke_e2e_mcp_http.py` with stub coverage for 11 previously uncovered tools

**Track 2 — Claude Code plugin package**
- New `dist/odoo-semantic-plugin/` — installable Claude Code plugin with:
  - 11 persona SKILL.md files: CEO (risk-overview, customization-inventory), Developer (override-finder, deprecation-audit, version-diff), Consultant (feature-check, gap-analysis), Marketer (feature-highlights, addon-diff), Sales (capability-proof, objection-handler)
  - 2 sub-agent files: `odoo-router.md` (Haiku classifier) + `odoo-upgrade-planner.md` (Sonnet orchestrator)
  - `/odoo-semantic:connect` slash command for interactive API-key setup
  - `.mcp.json` template with `${ODOO_SEMANTIC_API_KEY}` env interpolation
- New `dist/marketplaces/viindoo/marketplace.json` for self-host distribution
- Added `tests/test_skill_disambiguation.py` — 31/31 parametrized routing accuracy tests (100%)

**Track 3 — Cross-vendor adapters + persona docs**
- New `dist/gemini-gem-instructions.md` — Gemini Gem system instructions with full tool routing for all 14 tools + 5 persona modes
- New `dist/openai-gpt-instructions.md` — Custom GPT instructions with routing rules + OpenAPI Action schema
- New `dist/cursor-rules.md` — Cursor `.cursorrules` with file-type-based auto-triggers for Odoo files
- New `docs/personas/{ceo,dev,consultant,marketer,sales}.md` — 5 EN persona onboarding guides with sample prompts and tool workflows
- Updated `README.md` — added Persona Guides section with cross-vendor adapter links

**Track 4 — Architecture & checklist**
- New `docs/adr/0012-persona-skill-architecture.md` — ADR for TRIGGER protocol + persona skill approach + rejected alternatives
- Extended `docs/deploy/pre-launch-checklist.md` — 11 persona skill sign-off rows in §6

## [0.1.0] — 2026-05-11

- M1–M7 Complete: resolve_model, resolve_field, resolve_method, resolve_view, find_examples, impact_analysis, lookup_core_api, api_version_diff, find_deprecated_usage, lint_check, cli_help, suggest_pattern, check_module_exists, find_override_point
- API key auth + Web UI admin (M5)
- SSH auto-clone, incremental indexer, cross-profile parallel indexing (M6)
- Qualified-name AST scope resolver, yoyo-migrations, Web UI session auth, nightly recall benchmark, go-live docs (M7)
