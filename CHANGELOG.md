# Changelog

All notable changes to Odoo Semantic MCP are documented here.

## [Unreleased] — 2026-05-21 — Housekeeping: SPDX headers + script fix + ADR-0031

### Changed
- [SPLIT] Housekeeping: added SPDX-License-Identifier: AGPL-3.0-or-later headers to all 200 `tests/**/*.py` and 6 `scripts/` files (`.py` + `.sh`). Fixed `add-spdx-headers.sh` `prepend_py()` to insert SPDX as line 2 when shebang is present (preserves shebang executability). Extended script to cover `tests/`, `scripts/*.py`, and `scripts/*.sh` targets. Added ADR-0031 (python-dotenv auto-load at CLI entry points) to `CLAUDE.md` ADR list.

## [Unreleased] — 2026-05-20 — Security: purge internal deployment data

### Security
- [SECURITY] Purged private Viindoo deployment topology (private repo names, seed roster, version presets) from the public repository. Master-data seed roster removed; profiles and repos are now created by admins via the web UI or JSON API. History rewrite applied.

## [Unreleased] — 2026-05-20 — Open-core repo split + AGPL license metadata

### Changed
- [SPLIT] Moved MIT plugin + client docs to Viindoo/odoo-mcp-client. Server repo retains AGPL-3.0 backend + Astro web UI. Added SPDX-License-Identifier: AGPL-3.0-or-later headers across all 88 `src/**/*.py` files and 42 `site/src/**` files (.ts/.tsx/.astro). Added license field to `pyproject.toml` and `site/package.json`. Added copyright + applicability notice atop `LICENSE`. Added `NOTICE` (Viindoo trademark statement + common_passwords attribution) and `data/common_passwords.txt.LICENSE`.

## [Unreleased] — 2026-05-18 — Post-0.4.1 hardening + go-live deploy + M9 Coverage Fill + M9 RBAC follow-up

6 PRs merged after v0.4.1. Production deployed at PR #119 / commit `3f081b9` (admin-invite signup model active). PR #120 (M9 Coverage Fill) + PR #121 (docs signoff) merged but not yet deployed to prod. Two post-deploy hotfixes shipped 2026-05-18 — PR #124 (`init_pool` ordering in seed_patterns CLI) and PR #125 (CLIFlag null command_name MERGE bug surfaced when running `index-core` against M9 curated spec_data). PR #<TBD> (M9 RBAC follow-up) in progress.

### Migration 0004 self-contained SQL rescue (PR #117)

#### Added
- `migrations/0004_add_missing_version_profiles.sql` seeds all 12 root CE profiles (`odoo_8` through `odoo_19`) with `ON CONFLICT (name) DO NOTHING`. SQL is self-contained for DBA-only rescue paths (no Python required).
- `src/db/seed_master_data.py` remains source of truth for the CE root profiles and still handles 2-pass FK inserts for hierarchical profiles.

#### Tests
- Profile-touching tests migrated to distinct test names (`test_root_99`, `test_mid_99`, `test_leaf_99` at version 99.0) or switched to a seeder-only fixture profile for conflict-test scenarios.
- Seed count assertion in `test_master_data_seed.py` bumped 5 → 12.

### Security headers — CSP + Permissions-Policy (PR #118)

#### Added — closes M9 CSP gap (memory: m9_csp_permissions_policy_gap.md)
- FastAPI `_SecurityHeadersMiddleware` injects `Content-Security-Policy: default-src 'none'` + `Permissions-Policy` on every JSON-API response (ADR-0015 — JSON-only, never serves HTML).
- Astro SSR `_addSecurityHeaders()` emits per-path tighter CSP on every SSR response (`/admin/*`, `/signup`, `/verify-email`, `/reset-password`). `script-src 'self' 'unsafe-inline'` because Astro inlines small page scripts.
- Edge nginx/Caddy emits permissive superset CSP that covers prerendered static pages (`/`, `/pricing`, `/bootstrap`, `/benchmarks`).
- 8 regression tests in `TestSecurityHeadersFastAPI` replace nginx-placeholder `TestNginxHeadersDocumented`.

#### Notes
- Nonce-based CSP migration tracked as M10 followup.

### Go-live batch — writer profile + MFA sync + backup CLI + /api/health (PR #119)

5 commits squashed: 4 WIs (Pattern 1 orchestration) + 1 followup commit (Opus review HIGH fixes + boil-the-lake findings + sanitization). Verified end-to-end on production 2026-05-17 (deploy + post-deploy ops phase). See PR description + `docs/deploy/pre-launch-checklist.md` followups #12-#15 for known gaps.

#### Fixed — WI-1 indexer writer + parser_js + ADR-0016 D7
- `src/indexer/writer_neo4j.py`: 6 placeholder MERGE sites (Module dep, Model INHERITS, Model DELEGATES_TO, View INHERITS_VIEW, QWebTmpl EXTENDS_TMPL, OWLComp PATCHES) now inherit the referencing module's profile array:
  - `ON CREATE SET <node>.profile = $profiles` on first MERGE.
  - `ON MATCH SET <node>.profile = [x IN coalesce(<node>.profile, []) WHERE NOT x IN $profiles] + $profiles` on subsequent MERGEs — UNION semantics mirroring real-node pattern from commit `4ff56a8` (prevents clobber when profile B references a stub previously created for profile A).
- `src/indexer/writer_neo4j.py`: 3 resolver MATCH sites (INHERITS Model, DELEGATES_TO Model, PATCHES OWLComp) now exclude `__unresolved__` stubs via `WHERE NOT coalesce(<var>.unresolved, false)` — symmetric with existing INHERITS_VIEW + EXTENDS_TMPL pattern. Without this, second referencer would resolve INHERITS to first referencer's stub and skip the union write.
- `src/indexer/parser_js.py`: `_extract_era3_components()` returns early when `int(odoo_version.split('.')[0]) < 14` — OWL framework only exists v14+.
- `docs/adr/0016-profile-hierarchy-and-neo4j-isolation.md`: new section **D7 — Stub node ownership policy** documenting the UNION pattern + 6 writer sites + future-contributor guidance.

#### Fixed — WI-2 webui auth MFA sync
- `src/web_ui/routes/totp.py`: `_enable_totp()` and `_delete_totp()` now also `UPDATE webui_users SET mfa_enabled = TRUE/FALSE WHERE id = %s` in the same transaction as the `totp_secrets` write. Login still gates on `totp_secrets.enabled`; users column is now authoritative for queries.
- `migrations/m9_009_backfill_mfa_enabled.sql`: idempotent symmetric reconciliation — sets TRUE for users with `totp_secrets.enabled=TRUE`, FALSE for any user `mfa_enabled=TRUE` without a matching TOTP row. Followup commit added the FALSE-reset half (boil-the-lake F).

#### Added — WI-3 backup CLI + systemd + runbook
- `src/cli.py` `_get_pg_dsn()`: refactored to use `config.from_env_or_ini("PG_DSN", "database", "pg_dsn")` helper (consistent with rest of codebase).
- `src/cli.py` `_resolve_postgres_tool(tool)`: new helper returns `[tool]` if `shutil.which` finds it locally, else `["docker", "exec", "-i", "-e", "PGPASSWORD", container, tool]` (PGPASSWORD forwarded via `-e VAR` syntax — host env propagates into container). Container name from `POSTGRES_CONTAINER` env, default `odoo-semantic-mcp-postgres-1`.
- `src/cli.py` `_resolve_neo4j_tool(tool)`: parallel helper for Neo4j tools (`neo4j-admin database dump`). Container env `NEO4J_CONTAINER`, default `odoo-semantic-mcp-neo4j-1`. No PGPASSWORD bleed.
- `src/cli.py` `_cmd_backup` pg_dump: stdout redirect (`stdout=open(pg_out, "wb")`) instead of `-f <host_path>` so docker-exec'd pg_dump pipes output back to host. psql restore paths already use stdin redirect (no change needed).
- `docs/deploy/odoo-semantic-backup.service` + `.timer` + extended `logrotate.d/odoo-semantic` + bilingual `backup-runbook.md`. Systemd unit uses canonical placeholders (`User=odoo-semantic` + `/opt/odoo-semantic-mcp`) per public-repo convention; `ExecStart` wraps in `/bin/sh -c '... $(date +%Y%m%d-%H%M%S) ...'` so timestamp expands per run (systemd `%` specifiers don't include strftime).
- 4 new docker-fallback tests in `test_backup_cli_docker_fallback.py` + 4 new Neo4j docker-fallback tests in `test_neo4j_cli_docker_fallback.py` + 5 existing CLI tests patched to mock `shutil.which` (environment-sensitive baseline).
- `migrations/m9_007_totp_secrets.sql` stale comment ("no mfa_enabled needed in webui_users") replaced with reference to WI-2 m9_009 sync.

#### Added — WI-4 /api/health auth-exempt endpoint
- `src/web_ui/app.py` `GET /api/health` returns `{"status": "ok", "version": "<__version__>"}` HTTP 200.
- `src/web_ui/middleware.py` `_EXEMPT_EXACT` set includes `/api/health` so unauthenticated requests bypass `AuthRequiredMiddleware`. Loopback-only + security header middlewares still apply.
- `src/_version.py`: new single-source version reader via `importlib.metadata.version("odoo-semantic-mcp")` with `PackageNotFoundError` fallback (no hardcoded duplication of `pyproject.toml`).
- 1 new TestClient test asserting unauthenticated 200 + `status` + `version` keys.

#### Fixed — Followup commit consolidates Opus review HIGH findings + 6 boil-the-lake fixes
- Docker-exec pg_dump no longer writes `-f <host_path>` inside container (loses output). Now uses stdout redirect.
- PGPASSWORD forwarded into container via `docker exec -e PGPASSWORD` (host env override didn't reach pg_dump inside).
- systemd `osm-%%Y%%m%%d-%%H%%M%%S.tar.gz` placeholder fixed: ExecStart wraps `/bin/sh -c '… $(date +%Y%m%d-%H%M%S) …'` (systemd specifiers don't expand strftime; nightly runs now produce distinct files).
- psql call sites switched from `text=True` to bytes mode for consistency with pg_dump fix; stderr decoded with `errors='replace'` for human-readable errors.
- `tests/test_writer_neo4j_stub_profile.py`: module-level `pytestmark = pytest.mark.neo4j` per CLAUDE.md convention; pure-unit OWL era guard test moved to `tests/test_parser_js.py`.
- `_version.py` deduplication (importlib.metadata).
- m9_009 migration symmetric backfill (also resets FALSE for users without active TOTP).
- Neo4j docker-exec fallback (parallel to Postgres helper).
- `src/web_ui/middleware.py` module docstring updated with `/api/health` in exempt-paths list.

#### Tests
- 11 new tests across 4 new files (writer stub profile, MFA sync, backup CLI docker, /api/health) + Neo4j docker fallback tests (post-followup).

#### Sanitization
- Initial commit history had host-specific paths (`/home/<user>/...`) and prod state in PR body; force-pushed to clean 1-commit branch using canonical `/opt/odoo-semantic-mcp` + `User=odoo-semantic` placeholders matching existing `docs/deploy/odoo-semantic-mcp.service`. Memory: `feedback_public_repo_sanitize.md`.

### M9 Coverage Fill batch (PR #120)

7 WIs landed: CSS/SCSS parser, v8 era1 field gap fix, pattern backfill, lint/CLI curation, deferred items absorption.

#### Added
- CSS/SCSS indexing: new `parser_css.py` + `parser_scss.py` with tree-sitter-css backend (regex fallback). Creates `:Stylesheet` Neo4j nodes (composite key `(file_path, module, odoo_version)`) + `:DEFINED_IN` + `:IMPORTS` edges. Pgvector chunk_types `css`/`scss`. (WI-A1, ADR-0025)
- PatternExample catalogue v9-v15: 30 curated patterns from real Odoo sources (`patterns.json` 83→113). (WI-A3)
- LintRule static curation v8-v19: 12 `spec_data/lint_rules_X.json` populated with ~270 rules + schema. (WI-A4)
- CLIFlag static curation v8-v19: 12 `spec_data/cli_flags_X.json` populated with ~880 flags + schema + cross-version deprecation tracking. (WI-A5)

#### Fixed
- v8 era1 `_columns` extraction: string-aware brace scan no longer truncates blocks at `{` inside string literals. `FieldInfo.source_definition` now populated for era1. (WI-A2)

#### Notes
- Post-deploy ops B1-B11 (CoreSymbol/LintRule/CLI ingestion runs, OBS-1 reindex, additional profile registration, full reindex for CSS/SCSS embeddings) tracked in the post-deploy ops plan.
- WI-A7 (deferred items absorption into TASKS.md M10/M10.5/M11 + ADR follow-up sections) pending Opus dispatch.

### Pre-launch checklist signoff (PR #121, docs only)

#### Changed
- `docs/deploy/pre-launch-checklist.md` items §4.1, §5.1, §8.6, §10.5 `/api/health` flipped to `[x]` post PR #119 deploy. §4.2, §5.2 marked `[~]` partial with followup references. §11 sign-off table filled (9 of 11 sections `[x]`).
- Known followups appended: #12 OWLComp v14 anachronism (239 stubs from JSPatch era3 in pre-v14 modules — read-side era guard already protects user output), #13 Neo4j online backup (Cypher export OR Enterprise backup cmd), #14 logrotate `/var/log` perms (pre-existing stanza), #15 §6 tools 15-21 prod smoke (deferred next session).

### Post-deploy hotfixes (2026-05-18)

#### PR #124 — `[FIX] indexer: init_pool before job_store in seed_patterns CLI`
- `src/indexer/seed_patterns.py` now calls `init_pool(dsn, ...)` before resolving `_get_job_store()`. Previous ordering raised `PostgreSQL pool is not initialized` when invoking `python -m src.indexer.seed_patterns --force`, blocking the B10 PatternExample reseed step of the M9 Coverage Fill post-deploy ops sequence.

#### PR #125 — `[FIX] indexer: coalesce CLIFlag command_name null → "server"`
- `src/indexer/parser_cli.py::_load_static_cli_flags` coerces `command_name` `None` → `"server"`, matching the live parser default for `odoo-bin server` flags.
- M9 Coverage Fill curated `cli_flags_*.json` files (12 versions × ~70-88 flags each) declared `command_name: null` for global flags like `--config`, `--init`, `--update`. Neo4j 5.x rejects null property values in MERGE identity keys (`Cannot merge ... null property value for 'command_name'`), aborting every `index-core` invocation before any CLIFlag node was written.
- Regression test covers explicit null, explicit "server", and missing key.

### Documentation

- Closed 4 de-facto-done backlog items in TASKS.md: M11 pattern catalogue target met (113 patterns), lint_json_response.sh advisory clean (0 violations), Reseed Patterns Web UI button verified wired end-to-end, M7.5-P2-SEED production seeding completed in B10 ops phase.
- Deduplicated 9 redundant TASKS.md backlog entries (NAMEGET, v8 era1 CLI, VN translation, pricing, nonce CSP) — each item now lives in exactly one canonical milestone location.
- Split Milestone 10 into M10A (Tool Surface Expansion) + M10B (Billing Wow Core) + M10C (Polish + Observability) for clearer scope.

### Production state at time of [Unreleased] cut

- Production HEAD: PR #119 / commit `3f081b9` deployed 2026-05-17 (PR #120 + #121 not yet deployed to prod).
- Neo4j: 0 NULL profile nodes (down from 5,988 pre-cleanup); 0 pre-v14 OWLComp anachronisms among NULL-profile set; 239 `__unresolved__` v8-v13 OWLComp stubs remain (have profile set; tracked as followup #12).
- Backup automation: systemd nightly timer scheduled 03:00:00; first manual run produced 2.55 GB postgres bundle (Neo4j component skipped — followup #13).
- Webui crash sim: passed (SIGKILL → 5s auto-restart).
- Embeddings: 528,577 across all profiles (unchanged from pre-deploy; `--no-embed` verify pass did not touch pgvector).

### M9 RBAC + Key-Ownership Bug Fix (PR #<TBD>)

6 WIs orchestrated (5 code, 1 docs). Root cause: `request.session.get("is_admin")` returned False because login never wrote that field; all 5 legacy API keys had `user_id IS NULL` → admin saw empty list. Additionally closes a security hole (unauthenticated users could not deactivate keys, but any authenticated user could deactivate any key by ID without ownership check) and completes M9 §3.4 admin user management.

#### Fixed
- **API key list filter restored for admins** — new `is_admin_session(request)` helper in `src/web_ui/auth.py` DB-sources `is_admin` per request instead of reading absent session field. Clarifies ADR-0011 rule 6 and prevents regression.
- **API key deactivate endpoint now enforces ownership** — `PATCH /api/api-keys/{id}/deactivate` checks that requesting user owns the key OR is an admin (HTTP 403 if neither). Closes M9 security gap.

#### Added
- **Admin promote/demote** — `PATCH /api/admin/users/{id}/admin` endpoint + UI toggle on `/admin/users` with last-admin protection (refuse demote if it leaves 0 active admins). New `set_user_admin()` AuthStore method.
- **Key→owner attribution** — `owner_username` field on `GET /api/api-keys`; Owner column + "Assign owner" banner on `/admin/api-keys` for legacy NULL-owner keys. New `PATCH /api/admin/api-keys/{id}/owner` endpoint for admin assignment. Self-service UI deactivate on `/account/api-keys`.
- **`/account/api-keys` self-service surface for non-admin users** (slim `AccountLayout`). Non-admins hitting `/admin/*` now redirect to `/account/api-keys` (via Astro middleware). New `/account/index` dashboard (read-only, shows "Profile access: VIEW" status).

#### Architecture
- `is_admin_session(request: Request) -> bool` replaces all `request.session.get("is_admin")` calls. DB-sourced, cached 5 min per existing auth cache.
- Web UI surface split: `/admin/*` for admins (full sidebar); `/account/*` for non-admins (slim sidebar).
- Last-admin protection on demote/deactivate via `set_user_admin()` and `set_user_active()` SQL logic.
- NULL-owner system keys assignable by admins interactively (modal + PATCH).

#### Tests
- 28 new backend + frontend tests (WI-1 through WI-5).

#### Fixed — post-Opus-review follow-ups (committed after PR #127 initial review)
- **browser-tests-admin admin seed**: `set_user_password(TEST_ADMIN_USERNAME, ..., is_admin=True)` — the test admin was seeded with `is_admin=False` (default), causing WI3 middleware to redirect the "admin" browser to `/account/api-keys` and all 70+ admin browser tests to time out (25-min wall clock in CI).
- **ADR-0026 doc drift**: last-admin protection status corrected 409→422 (matches `admin_users.py:285`); `/account/index` described as thin redirect not a dashboard (matches `account/index.astro`); audit action names corrected to `user.set_admin` + `api_key.assign_owner` (matches `@audit_action` decorators).
- **`is_admin_session` fail-closed**: `uid=None` now returns `False` instead of `True`. Malformed session cookie or SessionMiddleware crash no longer grants implicit admin privilege.
- **`set_user_admin` / `set_user_active` concurrent demote serialisation**: added `SELECT ... FOR UPDATE` on the target row before the admin-count check, preventing TOCTOU race where two concurrent demotes could both pass the guard and leave 0 admins.
- **`assign_key_owner_route` audit detail**: old_user_id → new_user_id transition now captured in `request.state.audit_detail` before the PATCH call, giving forensic before/after in the audit log.

#### Docs
- ADR-0026 — RBAC + key ownership (5 design decisions, 2 consequences sections, alternatives considered).
- TASKS.md Stream J (6 WIs + completion note).
- CLAUDE.md new section "Auth — is_admin Source of Truth" (1 paragraph clarifying the DB-sourced rule).
- CHANGELOG.md (this section).

---

## [0.5.0] — 2026-05-19 — Tool UX + Architecture (M10.5 + M11)

6 waves + 8 patterns landed in a single worktree via the `feat/m10-5-m11-tool-ux-architecture` branch (33 commits over Waves A–F + F-FINAL). Plan: internal plan (archived). Research: 12 MCP design patterns evaluated, 8 adopted (archived internally). 3 new ADRs (0028/0029/0030) + ADR-0023 amended.

### Wave A — Quick Wins (M10.5)

- **Tool annotations** (WI-A1): `READONLY_TOOL_KWARGS = {"read_only_hint": True, "idempotent_hint": True}` applied to all 21 existing `@mcp.tool()` decorators. Signals to MCP hosts that no write side-effects occur. ADR-0023 §2 docstring language policy re-affirmed.
- **Next-step hints SSOT** (WI-A2): centralized into `src/mcp/hints.py` — single dict maps tool name → hint string. All 18 drill-down tools import from there; 4 CI assertions added.
- **Grammar consistency tests** (WI-A3): `tests/test_grammar_consistency.py` — 4 tests (language-policy regex, no-self-loop, truncation-disclosure, next-step-present).
- **Self-mythology docstrings** (WI-A4): `lookup_core_api` and `find_deprecated_usage` TRIGGER/PREFER/SKIP blocks updated with accurate self-description.

### Wave B — Output Envelope (M10.5)

- **Shared TreeBuilder** (WI-B1): `src/mcp/tree_builder.py` — `TreeBuilder` class with `add_branch`, `add_sublist`, `add_next` methods. `_resolve_model` and `_list_fields` migrated as PoC.
- **Pydantic DTOs** (WI-B2): `src/mcp/dto.py` — 6 `*Ref` + 7 `*Output` Pydantic models. `ModelRef`, `FieldRef`, `MethodRef`, `ViewRef`, `ModuleRef`, `PatternRef`; `ModelOutput`, `FieldOutput`, etc.
- **Dual-channel ToolResult** (WI-B3): 7 priority tools (`resolve_model`, `resolve_field`, `resolve_method`, `resolve_view`, `describe_module`, `list_fields`, `list_methods`) return `{"content": tree_text, "structuredContent": dto.model_dump()}`. AI clients that support `structuredContent` get machine-parseable data; others fall back to tree text.
- **Dual-channel tests** (WI-B4): `tests/test_dual_channel_envelope.py` — 8 tests asserting both channels non-empty + DTO schema round-trips.

### Wave C — Drill-down Cohesion (M10.5)

- **Opaque ref IDs** (WI-C1/C2/C3): `src/mcp/refs.py` — per-call ref minter with API-key tenancy + 5min TTL. 6 `_list_*` tools emit `[ref=fN]` row tokens; 4 `_resolve_*` tools accept `target=<ref>` OR canonical `model+field+version` — backward compatible. Pagination: `start_index: int = 0` added to all 6 list tools.
- **Ref drilldown tests** (WI-C4): `tests/test_drilldown_refs.py` — 8 tests (ref lifecycle, cross-tenant isolation, ref→resolve round-trip).

### Wave D — Discriminator Consolidation (M11)

- **3 superset tools** (WI-D1): `model_inspect(target, odoo_version, kind)`, `module_inspect(target, odoo_version, kind)`, `entity_lookup(target, odoo_version)` implemented in `src/mcp/inspect.py`. Discriminator field in `structuredContent` signals which sub-tool was invoked.
- **10 deprecation shims** (WI-D4): `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view` + 6 `list_*` tools wrapped with `DeprecationWarning` footer + ADR-0028 migration hint. `@deprecated` decorator in `src/mcp/server.py` adds `[DEPRECATED: v0.5 → v0.6]` prefix to tool description.
- **Tests** (WI-D5): `tests/test_mcp_inspect_router.py` (12 tests) + `tests/test_mcp_deprecation_shims.py` (8 tests).
- **ADR-0028** (`docs/adr/0028-discriminator-consolidation.md`): discriminator field contract, deprecation timeline (v0.5 shim → v0.6 removal), migration guide for callers.

### Wave E — Implicit Context (M11)

- **Session state migration** (WI-E1): `migrations/0005_api_key_session_state.sql` — `api_key_session_state` table with `api_key_id PK`, `active_version`, `active_profile`, `updated_at`.
- **Session module** (WI-E2): `src/mcp/session.py` — `read_session()`, `write_session()`, `normalize_version_arg()`, `resolve_version_v2()`. 60s in-process cache per `api_key_id`. 6 sentinel strings collapse to per-key active version.
- **4 session tools + resolver patches** (WI-E3): `set_active_version`, `set_active_profile`, `list_available_versions`, `list_available_profiles` registered in `server.py`. All 21 existing tool wrappers patched to call `resolve_version_v2` so sentinels work transparently.
- **Session tests** (WI-E4): `tests/test_mcp_session_state.py` — 11 tests (read/write round-trip, sentinel collapse, 60s cache, 24h TTL, concurrent tenant isolation).
- **ADR-0029** (`docs/adr/0029-implicit-session-context.md`): 6 sentinels, 3-tier resolution (explicit → session → latest-indexed), TTL policy, concurrent-tenant isolation guarantee.

### Wave F — MCP Resources (M11)

- **7 resource handlers** (WI-F1): `src/mcp/resources.py` — `register_resources(mcp_instance)` wires `@mcp.resource` for 7 `odoo://` URI templates. LRU cache 1000/300s. Cache key formed from **resolved** version (not raw sentinel) — prevents tenant leakage when two API keys with different active versions read `odoo://auto/model/X`.
- **Top-100 popular models** (WI-F2): `src/mcp/resources_index.py` — `odoo://index/popular_models` resource returns top-100 models by field+method count across all indexed versions; cached 1h.
- **Server wiring + docstring hints** (WI-F3): `register_resources(mcp)` called at startup; 7 `_render_*` functions referenced in their respective tool docstrings as "→ available as `odoo://{version}/kind/...`".
- **Tests** (WI-F4): `tests/test_mcp_resources.py` (6 tests), `tests/test_mcp_resource_cache.py` (5 tests), `tests/test_mcp_resources_auth.py` (4 tests including tenant-leakage regression).
- **ADR-0030** (`docs/adr/0030-mcp-resources-uri-scheme.md`): URI scheme rationale, 7 kinds, MIME-native content negotiation, cache architecture, sentinel handling.

### F-FINAL gate followups

- **Pre-launch checklist** (AC-6): §6 updated to 28 tools, §6.5 added (7 MCP Resources sign-off table).
- **ADR-0023 pagination amendment** (AC-7): `start_index` parameter contract, continuation hint grammar (plain text, not `<error>` tag), `[ref=fN]` row token alignment.
- **README + CHANGELOG** (AC-8): MCP section updated to 28 tools + 7 Resources table; this entry.
- **Tenant leakage fix** (latent bug): All 7 resource handlers now resolve version sentinel before forming cache key; regression test `test_two_keys_different_active_versions_get_their_own_bodies` added to `tests/test_mcp_resources_auth.py`.

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
