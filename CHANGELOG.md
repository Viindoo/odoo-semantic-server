# Changelog

All notable changes to Odoo Semantic MCP are documented here.

## [Unreleased] — 2026-05-17 — M9 Coverage Fill batch

6 WIs landed: CSS/SCSS parser, v8 era1 field gap fix, pattern backfill, lint/CLI curation.

### Added
- CSS/SCSS indexing: new `parser_css.py` + `parser_scss.py` with tree-sitter-css backend (regex fallback). Creates `:Stylesheet` Neo4j nodes (composite key `(file_path, module, odoo_version)`) + `:DEFINED_IN` + `:IMPORTS` edges. Pgvector chunk_types `css`/`scss`. (WI-A1, ADR-0025)
- PatternExample catalogue v9-v15: 30 curated patterns from real Odoo sources (`patterns.json` 83→113). (WI-A3)
- LintRule static curation v8-v19: 12 `spec_data/lint_rules_X.json` populated with ~270 rules + schema. (WI-A4)
- CLIFlag static curation v8-v19: 12 `spec_data/cli_flags_X.json` populated with ~880 flags + schema + cross-version deprecation tracking. (WI-A5)

### Fixed
- v8 era1 `_columns` extraction: string-aware brace scan no longer truncates blocks at `{` inside string literals. `FieldInfo.source_definition` now populated for era1. (WI-A2)

### Notes
- Post-deploy ops B1-B11 (CoreSymbol/LintRule/CLI ingestion runs, OBS-1 reindex, viindoo_internal_19 registration, full reindex for CSS/SCSS embeddings) tracked in plan `streamed-cuddling-phoenix.md`.
- WI-A7 (deferred items absorption into TASKS.md M10/M10.5/M11 + ADR follow-up sections) pending Opus dispatch.

---

## [0.4.1] — 2026-05-16 — M9 follow-up: Web UI parity for repo & profile management

5 WIs merged via PR #116.

### Added (M9 follow-up: Web UI parity)

- `PATCH /api/repos/repos/{id}` — edit URL/branch/ssh_key_id/local_path qua Web UI; preserves `head_sha` (incremental indexer compatible). ADR-0024.
- `PATCH /api/repos/profiles/{id}` — edit name/version/description; rejects `name`/`version` change on indexed profiles (HTTP 409 `ProfileIndexedError`); enforces ancestor + descendant version-match invariant (HTTP 422). ADR-0024.
- Admin UI: Edit Repo form, Edit Profile form, profile hierarchy tree view (toggle flat/tree, localStorage persist).
- RepoTable surfaces `clone_error_msg`, `error_msg`, `last_indexed_at` columns.
- Index + Index-All buttons: `--full` checkbox (expose ADR-0007 cleanup flag).
- Audit log captures before/after snapshots for PATCH mutations (ADR-0021 extension).

### Fixed

- TOCTOU race in `update_repo` UNIQUE check — catch `psycopg2.errors.UniqueViolation` → HTTP 409 instead of 500.
- ProfileTree.astro testid clash with flat list (namespaced `profile-tree-*`).
- ProfileTree.astro client-side DOM build → SSR template (Astro convention parity).

### Tests

- +9 backend tests for PATCH endpoints (empty body, single field, indexed guard, ancestor/descendant version match, concurrent UniqueViolation).
- +5 browser tests for tree view toggle and localStorage persistence.

---

## [0.4.0] — 2026-05-15 — M9 "Auth Wow" + M8 cleanup + comprehensive security hardening

19 worktrees merged via 9-phase orchestration. PR #100.

### Added — Auth Wow features

- **OAuth (Google + GitHub)** via `arctic` + `oslo` in Astro SSR. State + PKCE CSRF protection. Account linking on verified email. ADR-0017.
- **Public signup** (`/signup`) with email verification (256-bit token, 24h TTL, single-use), hCaptcha, 3/hour resend rate-limit, HTML-escaped email templates.
- **MFA TOTP** enrollment via `pyotp` with Fernet-encrypted secrets + 10 HMAC-hashed backup codes. Admin user enforced after 7-day grace. ADR-0022.
- **Multi-user admin** (`/admin/users`) — `is_admin` gating, deactivate (revokes sessions), reactivate, reset-password-link (1h TTL token).
- **Tenant API keys** — `user_id` FK scoping; users see only their own keys, admin sees all. `expires_at` filter.
- **Backup CLI bundle** (`.tar.gz`: postgres.sql + neo4j.dump + fernet.enc passphrase-encrypted + manifest.json) + Web UI trigger with SSE log stream. ADR-0018.
- **Restore upload** (`/api/operations/restore`) with full OWASP 10-item checklist: size, content-type, extension, `tarfile.extractall(filter='data')`, disk space, SHA-256 audit, maintenance mode 503, pre-restore safety backup, admin + fresh-MFA (5 min). ADR-0019.
- **Admin audit log** (`admin_audit_log` table) + `@audit_action` decorator + `audit_cli` context manager. 18+ routes covered. ADR-0021.

### Added — Security hardening (30+ findings closed)

- **F1**: Login dummy-hash unconditional bcrypt verify (timing oracle fix — closes username enumeration).
- **F2**: Postgres-backed `login_attempts` rate-limit (multi-worker safe, survives restart).
- **F3**: `TRUSTED_PROXY_CIDRS` env allowlist for `X-Forwarded-For` parsing (prevents IP spoofing).
- **F5**: OAuth `state` + PKCE mandatory.
- **F6**: CSP + Permissions-Policy headers in nginx + Caddyfile parity.
- **F7**: Server-side session store (`active_sessions` table) — instant revoke on logout + session ID rotation on login.
- **F8**: API key hash HMAC-SHA256 (was SHA-256 plain) + 30-day SHA-256 fallback for legacy keys (deadline 2026-06-15).
- **F12**: FERNET startup fail-fast in production if key unset.
- **F13**: `--old-key-env` / `--new-key-env` for `rotate-fernet` (eliminates `/proc/<pid>/cmdline` leak). Atomic rotation with transaction rollback. ADR-0020.
- **F15**: `WEBUI_SECURE_COOKIE` opt-out (`!= "0"` instead of `== "1"`).
- **F20**: `conftest._bypass_webui_auth_for_legacy_tests` now excludes both `test_web_ui_auth.py` AND `test_web_ui_browser.py` (was silent auth bypass).

### Added — DB schema

- 8 new yoyo migrations: `m9_001_oauth_columns`, `m9_002_api_keys_user_fk`, `m9_003_admin_audit_log`, `m9_004_login_attempts`, `m9_005_active_sessions`, `m9_006_email_verifications`, `m9_007_totp_secrets`, `m9_008_key_rotation_log`. `9001_m9_user_mgmt.sql` harmonized as canonical schema.

### Added — UI

- `/admin/users` (list + deactivate + reactivate + reset password).
- `/admin/security` (TOTP enrollment + backup codes).
- `/signup`, `/verify-email`, `/reset-password` (public, prerender=false).
- `/admin/operations` extended: Backup section with SSE log, Restore section with file upload + safety backup display, Migrations read-only display (yoyo `_yoyo_migrations` table), FERNET rotation CLI placeholder.
- `/admin/repos` extended: per-profile parent dropdown (handles 404/422 typed errors from W-RC), "Clone all pending" button + JobStatus wiring, RepoTable SSH key dropdown JS toggle by URL pattern (`git@` → show, `https://` → hide).
- Login page: OAuth "Sign in with Google/GitHub" buttons + MFA step section.

### Added — CLI

- `python -m src.manager` new subcommands: `delete-profile <name>`, `delete-repo <id|url>`, `delete-webui-user <username>`, `list-webui-users`. All deletes require `--yes` or interactive `YES` confirm + write audit log.
- `create-webui-user --admin` flag (bootstraps admin user post-M9 schema where `is_admin DEFAULT FALSE`).

### Added — REST polish

- `POST /api/repos/profiles/{id}/clone-all` returns 404 for nonexistent profile (was 200 "no pending repos").
- `PATCH /api/repos/profiles/{id}/parent` distinguishes 404 (not found) vs 422 (cycle / version mismatch) via typed exceptions (`ProfileNotFoundError`, `ProfileCycleError`, `ProfileVersionMismatchError` in `src/db/exceptions.py`).
- `GET /api/admin/migrations` lists applied yoyo migrations (read-only, admin-gated).

### Added — CI / DX

- Bump `actions/setup-node@v4 → v5`, `pnpm/action-setup@v4 → v5`, `actions/checkout@v4 → v5` (pre-empts GitHub forced Node 24 upgrade — deadline 2026-06-02).
- Replace `python -m jsonschema` with `check-jsonschema` CLI (eliminates DeprecationWarning).
- Add `actionlint` job via `rhysd/actionlint@v1`.
- Top-level `permissions: contents: read` on all workflows (anti-pattern fix).
- `.github/dependabot.yml` for weekly GitHub Actions updates.
- 2 advisory lint scripts: `lint_json_response.sh` (catches `JSONResponse(dict)` missing `_json_safe`), `lint_fetch_content_type.sh` (catches `fetch()` POST/PATCH/DELETE missing `Content-Type` header). Wired into `make lint` as `lint-shell-advisory` (warn-only — 127 legacy JSONResponse violations tracked in backlog for dedicated cleanup PR; lint_fetch_content_type 0 violations).
- New ADRs: 0017 (OAuth), 0018 (backup contract), 0019 (restore upload security), 0020 (FERNET key delivery), 0021 (admin audit log), 0022 (MFA TOTP).

### Changed — Test debt

- Deleted 8 MIGRATED tombstone test files (`test_web_ui_*_browser.py` — coverage moved to `tests/browser/admin/test_repos.py` in M8 W7).
- Fixed httpx per-request cookies + Neo4j session close deprecation warnings (2 of 3 fixed; remaining 1 is documented upstream).
- 656 unit tests + 360 postgres integration tests + 68 neo4j tests pass.

### Operational

- Production runbook `docs/deploy/m9-postmerge-ops.md`: 99.0 test artifact cleanup, index-core v9-v19 re-run, seed-patterns, admin bootstrap, audit log verification, daily cleanup cron (login_attempts, email_verifications, active_sessions).

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
