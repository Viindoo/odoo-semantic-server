# Changelog

All notable changes to Odoo Semantic MCP are documented here.

## [Unreleased]

### Fixed

- `[FIX] indexer: replace urllib with httpx for true wall-clock timeout, fix indexer freeze when embed backend slow/silent`

### Security

- **`site/`: bump `astro` 5.x → 6.x and `@astrojs/node` 9.x → 10.x.** Closes 5 dependabot alerts (CVE-2026-42570 / 45028 / 41067 / 41322 / 29772). Major bump required — Astro 5.x and @astrojs/node 9.x are EOL with no CVE backports.
  - `devalue` pinned to `^5.8.1` via `pnpm-workspace.yaml` `overrides` (transitive — astro 6 still pulls 5.8.0 by default).
  - **Deploy upgrade required:** Node.js ≥ 22.12.0 (was 20+), pnpm ≥ 10 (was 9+). `pnpm-workspace.yaml` now uses `allowBuilds:` + `overrides:` fields (pnpm 10+ format).
  - CI bumped: Node 20 → 22, pnpm 9 → 10 in `.github/workflows/ci.yml`.

## [0.3.0] — 2026-05-14 — M8 "Public Wow"

### Breaking Changes

- **Web UI rewritten as Astro SSR (port 4321 default).** FastAPI dropped all Jinja2 templates and now returns JSON only (port 8003).
  - Deployers must add `odoo-semantic-astro.service` (systemd unit provided at `docs/deploy/odoo-semantic-astro.service`) and run `pnpm build` in `site/` before starting.
  - Nginx config: use `docs/deploy/nginx-m8.conf` — routes `/api/*` → 8003, `/admin/*` + `/` → 4321, `/mcp` → 8002.
  - Direct browser requests to `/api/*` now return `Content-Type: application/json` — no HTML pages served from FastAPI.

### Added

- **Astro 5.x SSR server** (`output: 'server'`, Tailwind CSS, pnpm) in `site/`
- **6 admin pages** SSR-rendered by Astro: login, dashboard, repos, api-keys, ssh-keys, operations
- **AdminLayout** Astro component + Astro middleware session auth (`GET /api/auth/verify` → 401 → redirect `/admin/login`)
- **Landing page** with React Flow `GraphAnimation` island + cinematic 5-frame hero reveal; baked graph snapshot (`site/public/graph-snapshot.json` from `scripts/dump_graph_snippet.py`)
- **Public install page** at `/install/` — Astro SSR, API-key onboarding flow
- **Pricing placeholder page** at `/pricing/` — teaser for M9 SaaS tiers
- **68 browser tests** (Playwright) split across `tests/browser/admin/` (auth-gated flows) + `tests/browser/public/` (landing + install page); 2 parallel CI jobs (`browser-admin`, `browser-public`)
- **ADR-0014** Astro unified UI architecture decision
- **ADR-0015** FastAPI pure JSON API policy
- **ADR-0016** Profile hierarchy + Neo4j Option Y isolation (`parent_profile_id` FK, ancestor array, cycle-free validation) — renumbered from draft 0014 to avoid clash with Astro ADR
- **`_json_safe` helper** (`src/web_ui/utils.py`) for safe `datetime` → ISO string conversion in `JSONResponse` — prevents 500 errors on datetime-bearing objects
- **`/api/jobs/{id}/status` endpoint** extracted to dedicated jobs router (`src/web_ui/routers/jobs.py`)
- **CI Node 20** setup via `actions/setup-node@v4` + `pnpm/action-setup@v3`; `pnpm run check` (TypeScript + Astro type-check) added as required CI gate
- **Auto-seed 26 master data profiles** via `python -m src.db.migrate`: Odoo CE v8–v19, Standard Viindoo v8–v19, Viindoo Internal v17/v18 (48 repos total, `clone_status='manual'`)
- **CLI `seed-master-data`**: idempotent re-seed with `--profiles-only` / `--reset` flags
- **Upgrade runbook** `docs/deploy/master-data-upgrade.md`

### Removed

- All Jinja2 templates (`src/web_ui/templates/*.html`)
- `jinja2` dependency from `pyproject.toml`
- Direct HTML rendering from any FastAPI route

### Fixed (during M8)

- **Astro 5.x `checkOrigin` security:** all mutation fetches in Astro pages now send `Content-Type: application/json` (Astro 5 rejects requests without this header for CSRF protection)
- **Session datetime serialization 500** in `/api/dashboard/stats` and SSH key listing — root cause: `datetime` objects not JSON-serializable in `JSONResponse`; fixed with `_json_safe` wrapper
- **Logout endpoint missing** — `POST /api/auth/logout` added; Astro logout page wired correctly

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
