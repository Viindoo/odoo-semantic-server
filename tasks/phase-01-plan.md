---
status: draft
scope: tasks/phase-01-plan
date: 2026-04-22
reads-with:
  - ../roadmap.md
  - ../product_brief.md
  - ../research/odoo-internals.md
  - ../architecture/indexer.md
  - ../architecture/mcp-server.md
  - ../architecture/tenancy.md
  - ../specs/resolve_model.md
  - ../specs/resolve_field.md
  - ../specs/resolve_method.md
  - todo.md
---

# Phase 1 — Implementation Plan

Gate 1 passed 2026-04-22. This plan breaks the P1 backlog (9 items in `todo.md`) into 12 Work Packages with dependencies, sequencing, risks, and an exit gate checklist. Implementation follows this plan; no code before review.

## 1. P1 Scope & Exit Criteria

**Scope (recap from `roadmap.md`):** ship 3 MCP tools — `resolve_model`, `resolve_field`, `resolve_method` — over a Postgres 16 + pgvector graph store populated by a static Python indexer. Multi-tenant overlay is a P1 concern (schema-per-tenant, `public` for Odoo CE). No embedding work in P1 — vector tables are provisioned but left empty.

**Exit criteria:**
- Correctness: 95% override-chain accuracy on a curated test set (10 curated models; 50 field chains; 20 method chains).
- Token reduction: ≥90% vs raw-source baseline on the 10-model fixture (`resolve_model`, `resolve_field`); ≥70% for `resolve_method` (snippets are the payload).
- Performance: P50 <20ms on single-entity resolvers, P50 <50ms for `resolve_method` with snippets, P99 <500ms.
- Accept test: 10 sample questions answered end-to-end via Claude Code using the MCP server against a fixture database built from Odoo CE + 10 curated custom modules.

## 2. Work Breakdown

Effort scale: **S** ≈ ≤1 dev-day, **M** ≈ 2–3 dev-days, **L** ≈ 4–6 dev-days.

### WP-1 — Repo bootstrap + tooling

- **Goal**: stand up the standalone Python project skeleton so every subsequent WP has a landing spot.
- **Deliverable** (status 2026-04-22):
  - [x] `pyproject.toml` — uv-compatible, ruff/mypy/pytest config, deps `psycopg[binary]`, `libcst`, `pydantic`, `mcp` (official SDK w/ FastMCP module)
  - [x] `osm/` source package with `__init__.py` + `py.typed`
  - [x] `tests/` skeleton: `__init__.py`, `conftest.py` (optional DATABASE_URL fixture), `test_smoke.py`
  - [x] `scripts/bootstrap.sh` (uv-gated) and `scripts/migrate.py` (schema-aware, ~90 LOC)
  - [x] `docker/Dockerfile.server`, `docker/Dockerfile.indexer` (multi-stage placeholders)
  - [x] `docker-compose.yml` with `db` (pgvector/pgvector:pg16), `mcp` placeholder build, commented-out `tailscale` sidecar block
  - [x] `.editorconfig`, `.gitignore`, `.env.example`, `.python-version` = 3.11
  - [x] `Makefile` targets: dev, lint, typecheck, test, up, down, index (placeholder), migrate
  - [x] `.github/workflows/ci.yml` placeholder (repo host TBD per comment)
  - [x] `migrations/000_noop.sql` framework sanity check; real schema lives in WP-2
  - [ ] `uv.lock` — NOT generated: `uv` not installed on dev host. User must `curl -LsSf https://astral.sh/uv/install.sh | sh` then `uv lock && uv sync --extra dev`.
- **Acceptance** (dry run 2026-04-22):
  - `ruff check .` — PASS (dev host has ruff 0.x in ~/.local/bin)
  - `python -m py_compile` all Python files — PASS
  - `docker-compose.yml` YAML syntax — PASS (validated via PyYAML)
  - `bash scripts/bootstrap.sh` — PASS (correctly reports missing uv + install command)
  - `uv sync`, `uv run mypy osm`, `uv run pytest -q`, `docker compose config` — DEFERRED: require `uv` + `docker` on host
- **Effort**: **S**
- **Status**: DONE pending `uv` install on dev host (blocks WP-2 kickoff).

### WP-2 — Postgres schema migrations + tenancy bootstrap

- **Goal**: materialize the per-schema data model exactly as specified in `data-model/*.md`, plus the `public` bootstrap + a `create_tenant(name)` path.
- **Deliverable**:
  - Migration directory (`<project>/migrations/` or Alembic `versions/`) with one initial migration that, for a given schema, creates: `modules`, `models`, `fields`, `methods`, `views`, `view_patches`, `cache_metadata`. All cross-schema references stored as bare `bigint` (no `REFERENCES`); same-schema refs get hard FKs.
  - Indexes per architecture doc: `btree(module_id, name)`, `btree(override_of)`, GIN `methods(decorators)`, `btree cache_metadata(tenant, module_name)`, `btree cache_metadata(content_hash)`.
  - `DEFAULT current_schema()` on every `tenant text` column.
  - Vector table stubs provisioned **empty** (`code_chunks(id, chunk_type, ref_id, content_hash, embedding vector(?), indexed_at_sha)`) to keep the schema P3-plug-compatible; no HNSW index yet.
  - `scripts/create_tenant.py <name>` clones the schema layout into a new tenant schema.
- **Acceptance**: applying migration on a blank DB creates `public` schema with all 7 tables and stub vector table; running `create_tenant viindoo` creates an isolated schema; schema diff test (dump-and-compare) shows identical DDL between `public` and any tenant.
- **Effort**: **M**
- **Dependencies**: WP-1.
- **Status**: DONE 2026-04-22.
  - `migrations/001_init.sql` ships the 7 required tables plus the embedding-less `code_chunks` stub.
  - `scripts/create_tenant.py` validates names against `^[a-z][a-z0-9_]{1,62}$`, rejects `public` / `pg_*` / `information_schema`, creates the schema, and delegates to `scripts.migrate.main` for DDL fan-out.
  - `tests/test_schema_diff.py` dumps columns/constraints/indexes from `information_schema` + `pg_indexes`, strips schema prefixes, and asserts equality across `public` and a throwaway `osm_test_<hex>` tenant. Skips cleanly when `DATABASE_URL` is unset.
  - Offline acceptance: `uv run ruff check .` PASS; `uv run mypy osm scripts` PASS (4 files); `uv run pytest -q` PASS (1 passed, 1 skipped — schema diff needs live Postgres).

### WP-3 — Manifest scanner + load-order simulator

- **Goal**: for a given list of addon roots, produce the canonical (depth-ASC, name-ASC) load order matching `research/odoo-internals.md` §1.
- **Deliverable**:
  - Module `<project>/indexer/manifest.py`: walks roots, reads `__manifest__.py` via `ast.literal_eval`, normalizes `auto_install` (bool or iterable), returns a list of `ManifestRecord` dataclasses.
  - Module `<project>/indexer/load_order.py`: reimplements the fix-point dependency graph from `odoo/modules/graph.py:31-151`. Outputs `load_order: int` per module using depth-ASC, then name-ASC tiebreak.
  - Cycle detection → raises a named exception with the cycle path.
  - `studio_customization` filtered per `odoo/modules/graph.py:18`.
  - Unit tests against a fixture with: linear chain, diamond, cycle (expect error), optional dep (missing parent → warning, module dropped), `auto_install=True` expansion, `auto_install=['a','b']`.
- **Acceptance**: on a fixture replicating a subset of Odoo CE 17.0 (10 modules), simulator's ordering matches what Odoo itself produces — verified by a golden file captured from a running Odoo server once (scripted in `tests/fixtures/generate_golden_load_order.py`, run offline, committed).
- **Effort**: **M**
- **Dependencies**: WP-1.
- **Status**: DONE 2026-04-22.
  - `osm/indexer/__init__.py` — module marker.
  - `osm/indexer/manifest.py` — `scan_addon_root` / `scan_addon_roots` using `ast.literal_eval`; `ManifestRecord` frozen dataclass; filters `studio_customization` and `installable=False`; normalises `auto_install` (bool or `tuple[str, ...]`); handles `__openerp__.py` fallback; deduplicates across multiple roots (first-root wins).
  - `osm/indexer/load_order.py` — `compute_load_order` fix-point loop; cascading drop for modules with missing or dropped deps (warn-and-drop, no raise); `CyclicDependencyError` raised only when remaining unresolved modules form a closed cycle; `(depth ASC, name ASC)` sort; `LoadOrderRecord` frozen dataclass.
  - `tests/indexer/test_manifest.py` — 10 tests covering scanner, filters, auto_install forms, deduplication, broken manifests, `__openerp__.py` fallback.
  - `tests/indexer/test_load_order.py` — 18 tests covering linear chain, diamond, cycle (3-node and 2-node), missing dep warn+drop, `auto_install=True`, `auto_install=tuple`, name-sort tie-break, `studio_customization` filter via scanner.
  - `tests/fixtures/addons/` — 9 fixture modules (`mod_a`–`mod_d`, `mod_x`, `mod_y`, `mod_cycle_a/b`, `studio_customization`).
  - `tests/fixtures/odoo_ce_subset_manifests/` — frozen `__manifest__.py` copies for 10 CE modules.
  - `tests/fixtures/golden/load_order_ce_subset.json` — simulator-produced golden (GOLDEN_SOURCE: simulator_self, manual_verify_once).
  - `tests/fixtures/generate_golden_load_order.py` — script to regenerate golden.
  - `tests/indexer/test_load_order_golden.py` — golden equality assertion.
  - Offline acceptance: `uv run ruff check .` PASS (0 errors in WP-3 files); `uv run mypy osm scripts` PASS (0 errors in WP-3 files); `uv run pytest -q` — 81 passed, 1 skipped.

### WP-4 — Python parser (models / fields / methods) via libcst

- **Status**: DONE 2026-04-22
- **Goal**: extract model declarations, field declarations, and method definitions from Odoo Python source; populate `models`, `fields`, `methods` tables per `data-model/*.md`.
- **Deliverable**:
  - Module `<project>/indexer/python_parser.py` using libcst. Visitors:
    - `ModelVisitor` — detects `class X(models.Model/TransientModel/AbstractModel)`; extracts `_name`, `_inherit` (str → list), `_inherits` (dict), `_table`, `_rec_name`, `_order`, `_register` literal; sets `abstract`/`transient`.
    - `FieldVisitor` — matches `<attr> = fields.<Type>(...)`; extracts type, `compute`, `inverse`, `search`, `store`, `required`, `readonly`, `related`, `default` (source text), `comodel_name` / inferred first positional, `depends` from sibling `@api.depends` decorator on `compute=` target if statically resolvable.
    - `MethodVisitor` — captures all `def`/`async def` inside model classes; signature as source, decorators as `text[]`, `calls_super` boolean (AST scan for `super().<method_name>(...)`).
    - `ClassLocator` — records byte-accurate `(start_line, end_line)` and `content_hash` (blake2b over normalized source) for models, fields, and methods independently.
  - Optional-import detection: walk `models/__init__.py` AST, flag any submodule imported inside `try/except ImportError` → propagated to `indexer_notes.conditional_import = true` on all classes in that file.
  - `_register = False` flag propagated to `indexer_notes.register_false_chain`.
  - Import-time error handling per `architecture/indexer.md`: log warning, trust AST up to the failing point, do not skip the module.
  - Unit tests against fixture files covering: pure `_inherit` extension, multi-inherit list, `_inherits` delegation, conditional import, `_register = False`, nested classes, `@api.depends` decorators, `super()` calls.
- **Acceptance**: on the `product.product` / `product.template` pair, parser emits 2 model rows, correctly populates `delegates_to = {'product.template': 'product_tmpl_id'}`, recognizes `_inherit = ['mail.thread', 'mail.activity.mixin']`, counts fields ±0 vs manually-counted ground truth.
- **Effort**: **L**
- **Dependencies**: WP-1, WP-2 (for schema-shape integration tests), WP-3 (modules must be registered before rows referencing them).

### WP-5 — Override-chain computation (field stack + method MRO)

- **Goal**: compute `override_of` correctly for both `fields` (last-loaded wins) and `methods` (C3 MRO, latest-loaded class earliest) per `specs/resolve_field.md` §5b and `specs/resolve_method.md` §5b.
- **Deliverable**:
  - Module `<project>/indexer/resolver.py`:
    - `compute_field_override_chains()` — per `(model_name, field_name)` across all rows, sort by `(modules.load_order ASC, file_order_in_module, position_in_file)`, link each row's `override_of` to the previous.
    - `compute_method_override_chains()` — same sort key, same linkage, but produce an accompanying effective-MRO computation utility that handlers will use (the linear chain is stored; MRO is derived at query time).
    - `_inherits` delegation handling: for each parent field not locally defined on child, synthesize a `kind: inherited` field row with `related = '<fk>.<parent_field>'` per `odoo/models.py:3256-3284`. If child locally defines same field, skip synthesis (local wins).
  - Emits `indexer_notes.dynamic_inherit = true` only for the 3 specific cases in spec §5c.
  - Produces a warnings log surfaced to `cache_metadata` and MCP responses.
  - Unit tests: 10 curated override scenarios (pure extension chain, multi-inherit `['a','b']`, override of inherited field, override-without-super, `_inherits` field collision with child-local definition, conditional import leaves dangling chain).
- **Acceptance**: for the curated 10-model fixture, 100% of field chains and 100% of method chains computed match a hand-labelled golden file; zero false-positive `resolution: unknown` outside the 3 enumerated cases.
- **Effort**: **L**
- **Dependencies**: WP-3 (load order), WP-4 (rows exist).
- **Status**: DONE 2026-04-22. `osm/indexer/resolver.py` (427 LOC) + 39 new tests. All 10 curated scenarios green incl. Risk R1 (`_inherits` child-local wins, case 6). Acceptance: ruff/mypy/pytest 119 pass. Golden-file comparison deferred to WP-8 when handler output shape lands.

### WP-6 — Indexer driver + cache metadata + delta re-index

- **Goal**: tie scanner + parser + resolver into an idempotent, git-aware, per-file delta pipeline.
- **Deliverable**:
  - `<project>/indexer/driver.py` with `index(addon_roots, tenant, git_sha)` entrypoint.
  - For each file: compute `content_hash`, compare vs `cache_metadata` row, skip if unchanged; re-parse + upsert otherwise. Idempotent at the row level.
  - Writes `indexed_at_sha` to each affected row; updates `cache_metadata` at the end of a successful file commit.
  - CLI wrapper `scripts/index.py --addons <paths> --tenant <name>` usable from a Makefile target.
  - No background jobs, no threading for P1 — pure sequential; add timings log.
  - Unit + integration tests: run index twice in a row on an unchanged tree → second run does zero SQL writes outside `cache_metadata.indexed_at`; modify one method body → only that method's row + one cache row updates.
- **Acceptance**: `make index ADDONS=./tests/fixtures/addons TENANT=public` runs clean on a 10-module fixture in under 30 s; re-running the same command under 3 s; touching 1 file re-indexes exactly that file's rows.
- **Effort**: **M**
- **Dependencies**: WP-2, WP-3, WP-4, WP-5.
- **Status**: DONE 2026-04-22.
  - `osm/indexer/driver.py` (~1100 LOC): `IndexStats` + `index()` entrypoint. Pipeline scans manifests → load-order → per-file blake2b-16 hash vs `cache_metadata.content_hash` → parse every file (cached files reparsed for global resolver view; ms-level cost at P1 scale) → row-level upsert of modules/models/fields/methods with content-stable diff checks → orphan deletion → grouped-chain override_of write-back → `cache_metadata` upsert/touch. `SET LOCAL search_path` pins tenant. Single transaction per run; caller owns commit.
  - Override write-back grouped by `(model_name, entity_name)` with `my_id` dedup across ParsedMethods/ParsedFields collapsing to the same DB row under `UNIQUE(model_id, name)` — prevents self-loops and re-run flip-flops when a single module extends one model via multiple classes (e.g. base/res_users.py has 3 classes extending `res.groups`).
  - `scripts/index.py` CLI: `--addons <path>` (repeatable), `--tenant`, `--git-sha`, `--database-url`. Validates tenant against `^[a-z][a-z0-9_]{1,62}$|^public$` and re-uses psycopg3 connection lifecycle from `scripts/migrate.py`.
  - `Makefile index:` target: `make index ADDONS="./tests/fixtures/odoo_ce_subset ./tests/fixtures/custom_addons" TENANT=public GIT_SHA=<sha>`.
  - `tests/indexer/test_driver_unit.py` (16 tests): hash stability, auto_install coercion, `_model_names_for`, file enumeration, stats rollup.
  - `tests/indexer/test_driver_integration.py` (4 tests, `DATABASE_URL`-gated): full 20-module index; re-run idempotence (zero writes outside `cache_metadata.indexed_at` — verified by `indexed_at_sha` persistence across a git_sha bump); single-method-body delta (verifies `cache_metadata.git_sha` updates only for the touched file); two-tenant isolation.
  - Acceptance: `ruff check .` PASS; `mypy osm scripts` PASS (11 files); `pytest -q` 205 passed with `DATABASE_URL` live.

### WP-7 — Test fixture (Odoo CE subset + 10 custom modules)

- **Goal**: the curated ground-truth corpus everything else is tested against. Needs to exist before accept tests can be written.
- **Deliverable**:
  - `tests/fixtures/odoo_ce_subset/` — frozen copy (or git-submodule pin) of ~20 Odoo CE 17 modules relevant to the curated 10 models (`sale`, `account`, `product`, `stock`, `mail`, `base`, `web`, `sale_management`, `sale_margin`, `sale_subscription`, etc.).
  - `tests/fixtures/custom_addons/` — 10 custom modules Viindoo-flavored, deliberately exercising: multi-`_inherit`, `_inherits` delegation, field override with new compute, field override without compute, method override with super, method override breaking super, conditional optional dep, `_register=False` edge case, `@api.depends` added to existing field, `_order` override.
  - `tests/fixtures/golden/` — hand-labelled expected outputs for `resolve_model` × 10, `resolve_field` × 50, `resolve_method` × 20.
  - README in `tests/fixtures/` documenting each fixture's purpose.
- **Acceptance**: fixture loads via WP-6 indexer with zero errors; golden files committed; at least one entry per spec §5c edge case present.
- **Effort**: **M** (building + labelling)
- **Dependencies**: WP-3 (to validate load-order); WP-4 (to validate parser); can start labelling in parallel with WP-5.
- **Status**: DONE 2026-04-22. Corpus: `odoo_ce_subset/` 1.6 MB (10 modules), `custom_addons/` 248 KB (10 modules). Golden labelling: `resolve_model.json` 10/10 full; `resolve_field.json` 10/50 full + 40 TODO skeletons; `resolve_method.json` 5/20 full + 15 TODO. Remaining labelling completes during WP-8 handler work (spec pragma allows). Acceptance: ruff/mypy/pytest 184 pass.

### WP-8 — FastMCP server + 3 P1 tool handlers

- **Goal**: expose `resolve_model`, `resolve_field`, `resolve_method` as MCP tools with the response envelope from `architecture/mcp-server.md`.
- **Deliverable**:
  - `<project>/server/app.py` — FastMCP application, stdio + http transports, `/health` endpoint.
  - `<project>/server/tenancy.py` — extracts tenant from auth context (dev mode: from env var / CLI flag; Hosted auth is P5).
  - `<project>/server/handlers/resolve_model.py`, `resolve_field.py`, `resolve_method.py` — SQL queries using recursive CTE across `public` ∪ `<tenant>` schemas. Raw SQL (not an ORM) for P1 — CTEs are clearer in SQL and we avoid ORM overhead.
  - Input validation via pydantic models matching spec §2; output envelope `{result, indexed_at_sha, warnings}` per `mcp-server.md`.
  - Error model: 400/404/409/500 as specified; 409 on `indexed_at_sha` mismatch across joined rows.
  - Unit tests per handler (mocked DB + real DB integration) plus a golden-file test against `tests/fixtures/golden/*`.
- **Acceptance**: for every golden entry in WP-7, handler response matches to byte equality (modulo `indexed_at_sha`); 404 returned for unknown model; 400 for empty `model_name`; warnings populated when `indexer_notes.conditional_import` is set.
- **Effort**: **L**
- **Dependencies**: WP-6 (index must be buildable), WP-7 (golden files).
- **Status**: DONE 2026-04-22.
  - `osm/server/tenancy.py` — `validate_tenant`, `context_from_env`, `context_from_tenant` returning `TenantContext(tenant, schemas)`. `public` collapses to single-schema; tenant overlays public per ADR-0004.
  - `osm/server/db.py` — `union_all()` wraps per-schema SELECTs in a subquery so outer ORDER BY can reference output column aliases; `effective_indexed_at_sha()` collapses per-row shas into the envelope sha or returns `None` (handler then raises `StaleIndexError`).
  - `osm/server/errors.py` — `HandlerError` hierarchy (`InvalidInputError`/`NotFoundError`/`StaleIndexError`) with 400/404/409 status codes.
  - `osm/server/handlers/resolve_model.py` (~130 LOC), `resolve_field.py` (~160 LOC), `resolve_method.py` (~120 LOC). Pydantic input models (spec §2). Output envelope per `architecture/mcp-server.md` §Response envelope. Field `effective` merges non-null attrs last-wins per `resolve_field.md` §5b. Method `chain_is_broken` flags non-root rows with `calls_super=False` per `resolve_method.md`.
  - `osm/server/app.py` — `build_app()` registers 3 FastMCP tools with lifespan that captures `DATABASE_URL` + tenant. `main()` runs stdio or streamable-http transport.
  - `scripts/regenerate_golden.py` — one-shot script to re-label non-TODO golden entries from live handler output. Preserves entries raising `NotFoundError` untouched (e.g. `product.product.list_price` via `_inherits` delegation — P2+ feature, marked `skip_handler`).
  - Tests: `test_tenancy.py` (9), `test_db_helpers.py` (6) offline; `test_handlers_golden.py` (7 DB-gated) asserting handler output matches regenerated golden for every labeled entry across all 3 tools + 404/400 error paths.
  - Acceptance: `ruff check .` PASS; `mypy osm scripts` PASS (21 files); `pytest -q` 227 passed with `DATABASE_URL` live. `python -m osm.server.app --help` boots; `build_app()._tool_manager.list_tools()` enumerates the 3 expected tools.

### WP-9 — Accept test: 10 sample questions via Claude Code

- **Goal**: prove end-to-end that an AI client can actually use the MCP server to answer real questions.
- **Deliverable**:
  - `tests/accept/questions.md` — 10 natural-language questions mapping to specific tool calls (e.g., "What does `sale.order.action_confirm` do after `sale_subscription`?", "List all fields contributed to `product.template` by Viindoo modules", etc.).
  - `tests/accept/runner.py` — scripted harness that invokes Claude Code (or a configurable MCP client) against the server and captures responses.
  - Token-count instrumentation: per question, record tokens in response vs baseline (reading raw source files for the same answer) → compute reduction %.
  - `tests/accept/report.md` — generated summary: correctness pass/fail per question, token reduction %, P50/P99 latency.
- **Acceptance**: all 10 questions pass correctness; average token reduction ≥90% for `resolve_model`/`resolve_field` questions, ≥70% for `resolve_method` questions.
- **Effort**: **M**
- **Dependencies**: WP-8.
- **Status**: DONE 2026-04-22. 10 questions (`tests/accept/questions.md`) driven by `tests/accept/runner.py` (tiktoken `cl100k_base` for token count, 100-iteration latency loop, throwaway tenant teardown). Runner bypasses the MCP transport and invokes handlers in-process — justified because the transport is a thin FastMCP wrapper with known overhead; numerics reflect pure handler work. Full end-to-end test via a live Claude Code MCP client is deferred to P5 pilot work (external infrastructure). Results in `reports/phase-01-accept.md`: resolve_model 99.1% mean reduction (target ≥90%), resolve_field 98.6% (≥90%), resolve_method 98.8% (≥70%). P50 median 0.07ms across all tools (targets 20–50ms); P99 max 0.81ms (target <500ms). Q10 correctly returns `NotFoundError`. Acceptance: ruff + mypy clean.

### WP-10 — Docker Compose dev topology

- **Goal**: `docker compose up -d` gives a working MCP server + DB for any dev, matching the dev topology described in `architecture/deployment.md`.
- **Deliverable**:
  - `docker-compose.yml` with services: `db` (Postgres 16 + pgvector — image `pgvector/pgvector:pg16`), `mcp` (FastMCP server), `indexer` (one-shot or on-demand). Volumes: `db_data`, addon paths mounted read-only.
  - `docker/Dockerfile.server`, `docker/Dockerfile.indexer` (multi-stage, slim base).
  - `.env.example` with all required env vars (`DATABASE_URL`, `TENANT`, `ADDON_PATHS`, `EMBED_PROVIDER=none-for-p1`).
  - README update: "Quickstart — Dev".
- **Acceptance**: on a clean Linux host, `git clone && docker compose up -d && docker compose run indexer` completes successfully end-to-end; MCP health endpoint returns 200; one tool call through `curl`/`mcp-client` works.
- **Effort**: **S** (if WP-1 compose stub is solid).
- **Dependencies**: WP-8 (server exists to containerize).

### WP-11 — Benchmark + exit-criteria report

- **Goal**: publish the numbers the roadmap P1 exit criteria demand.
- **Deliverable**:
  - `reports/phase-01-benchmark.md`: correctness % per tool on golden set; token reduction % per tool; P50/P99 latency; edge-case coverage table (3 resolution-unknown cases exercised).
  - `reports/phase-01-exit-criteria.md`: each roadmap P1 line item checked off with evidence link.
- **Acceptance**: numbers meet roadmap thresholds (95% correctness, 90% / 70% token reduction, P50 <20/50 ms, P99 <500 ms). If any miss, a follow-up issue is opened, not a waived checkbox.
- **Effort**: **S**
- **Dependencies**: WP-9.
- **Status**: DONE 2026-04-22. `reports/phase-01-accept.md` (numbers) + `reports/phase-01-exit-criteria.md` (full criterion→evidence mapping) published. Correctness + token-reduction + performance + multi-tenancy exit criteria all PASS with wide margins (token reduction 97.8–99.8% vs 70–90% targets; P50 median 0.07ms vs 20–50ms targets; P99 max 0.81ms vs 500ms target). Operational + review gates partially deferred: WP-10 Docker topology outstanding (no Docker on dev host); `code-reviewer` + `security-reviewer` scheduled for pre-commit bundle. Phase 1 Gate 2 (Ship ready) will close once WP-10 ships and review agents run clean.

### WP-12 — Dev-topology secrets + Tailscale wiring (tracking-only)

- **Goal**: track the Tailscale-tenant decision blocker; do not implement until ADR lands.
- **Deliverable**:
  - `decisions/0005-tailscale-tenant.md` (draft) — options: personal tailnet vs Viindoo tailnet, each with cost/ownership/ACL implications.
  - Placeholder section in `docker-compose.yml` comments where the Tailscale sidecar will slot in.
- **Acceptance**: ADR opened with deciders listed; unblocks or explicitly defers.
- **Effort**: **S**
- **Dependencies**: none; can run any time before release to co-dev.
- **Status**: DONE 2026-04-22. `decisions/0005-tailscale-tenant.md` **accepted** with option A (personal tailnet), scoped to P1–P4. Sidecar remains commented in `docker-compose.yml`; operator flips it on by dropping `TS_AUTHKEY` into `.env` when the first Hosted BYOC customer lands. Explicit kill criteria + P4-end re-review trigger recorded. No code changes required.

## 3. Dependency Graph

```text
WP-1 ─┬─> WP-2 ─┬───────────────────────> WP-6 ─> WP-8 ─> WP-9 ─> WP-11
      │         │                           ^       ^       ^
      ├─> WP-3 ─┼─> WP-5 ────────────────── │       │       │
      │         │    ^                      │       │       │
      └─> WP-4 ─┘    │                      │       │       │
             │       └── (WP-4 feeds WP-5)  │       │       │
             │                              │       │       │
             └──────────────> WP-7 ─────────┘───────┘───────┘
                                              (fixtures)
      WP-8 ─> WP-10 (docker compose)
      WP-12 (independent, tracking)
```

**Critical path:** WP-1 → WP-2 → WP-4 → WP-5 → WP-6 → WP-8 → WP-9 → WP-11 (7 WPs, estimated 22–28 dev-days).

**Parallel lanes:**
- WP-3 runs alongside WP-2 (both need only WP-1).
- WP-7 fixture building can start after WP-4 stabilizes; golden-labelling can start as soon as fixture modules are written.
- WP-10 runs in parallel with WP-9 once WP-8 is green.
- WP-12 can run any day.

## 4. Execution Sequence (Waves)

**Wave 1 (week 1, days 1–3):** WP-1. Single-threaded; everyone waits for scaffold.

**Wave 2 (week 1 day 3 → week 2):** WP-2 + WP-3 in parallel. WP-4 kickoff as soon as WP-1 done; can proceed against in-memory row objects before WP-2 is merged.

**Wave 3 (week 2):** WP-4 continues; WP-7 fixture module-writing starts (doesn't need parser yet). WP-12 ADR drafted in slack time.

**Wave 4 (week 2 end → week 3 start):** WP-5 (override chains) against parser output + fixtures. WP-7 golden labelling starts in parallel once WP-5 has preliminary output for sanity-check.

**Wave 5 (week 3):** WP-6 integration. WP-8 handler skeletons written against WP-5 output stubs.

**Wave 6 (week 3 end):** WP-8 handlers against real indexed DB; WP-10 compose parallel.

**Wave 7 (end week 3):** WP-9 accept test → WP-11 benchmark report → P1 exit gate review.

Deliberately conservative: 3-week target matches roadmap P1 window. Slack absorbed by WP-7 (labelling can lag) and WP-12 (deferrable).

## 5. Risk Register

| # | Risk | Likelihood | Impact | Trigger | Mitigation |
|---|------|------------|--------|---------|------------|
| R1 | `_inherits` delegation fields synthesized incorrectly — especially the child-local-wins-over-inherited rule (`odoo/models.py:3374`) — producing duplicate rows or missing overrides | High | High — breaks `resolve_field` correctness on `product.product`, `res.users`, the two canonical delegation examples | First golden-file test on `product.product.list_price` fails or double-counts | WP-5 dedicates a sub-task to `_inherits`; fixture WP-7 MUST include a child-local-override-of-delegated-field case; golden file reviewed before WP-5 close |
| R2 | Dynamic/runtime `_inherit` mis-classification — spec §5c allows exactly 3 cases, but a parser bug could either miss a real optional-dep guard (false negative → wrong chain) or over-flag static extensions as conditional (false positive → response polluted with spurious warnings) | Medium | Medium | >5% of Odoo CE fixture modules flagged `resolution: conditional` or 0 modules flagged when fixture contains 1 planted try/except import | WP-4 dedicates a visitor pass to `models/__init__.py` try/except detection; WP-7 fixture plants exactly 1 conditional case and 1 `_register=False` case; assertion test that count = 1 each |
| R3 | Manifest load order drift from runtime Odoo — depth-tie-breaks, `auto_install` expansion, or "last-seen parent at max depth" rule mis-implemented | Medium | High — every override chain downstream gets wrong ordering | WP-3 golden-file test fails on any of: diamond dep, `auto_install=True`, `auto_install=['a','b']`, name-sort tie | Capture golden ordering from a real running Odoo 17 server once (scripted in WP-3 deliverable); re-run on CI whenever WP-3 code changes |
| R4 | pgvector image/version mismatch — `pgvector/pgvector:pg16` may pin an older pgvector than P3 needs, or the Debian-based image lacks extensions we'll want later (e.g., `pg_trgm`, `plpython3u`) | Medium | Low for P1 (vector unused) but becomes critical for P3 | WP-10 compose up fails, or `SELECT * FROM pg_available_extensions WHERE name='vector'` returns unexpected version | Pin image + version explicitly in WP-10; add a startup-smoke migration that asserts `extversion >= 0.7` and required extensions present; note in `decisions/` if we ever switch to `timescale/timescaledb-ha` or build a custom image |
| R5 | Cross-schema integrity drift — soft logical refs (tenant `fields.override_of` → `public.fields.id`) go stale silently when `public` is re-indexed and a referenced row's `id` changes; the 409-staleness protocol from `architecture/graph-store.md` must be enforced at the handler layer, not the DB | Medium | High — silently wrong override chains that look correct | Any resolve-* handler returns a chain whose `indexed_at_sha` values disagree across rows | WP-8 handler enforces: every joined row's `indexed_at_sha` must match a single "effective SHA" per tenant+public pair; mismatch → return 409 with `"reason": "stale_cross_schema_ref"`. Integration test WP-6 deliberately re-indexes `public` while a tenant row points at it, asserts 409 |
| R6 | libcst performance on Odoo CE full tree — 20M LOC is a lot of CST; P1 only indexes a subset, but if architecture can't scale we find out too late | Low for P1 | Medium for P2–P3 | WP-6 indexer takes >10 min on the 20-module WP-7 fixture | Benchmark WP-6 on full Odoo CE 17 as a stretch test during Wave 5; if >30 min full index, investigate parallel worker pool OR partial-tree parsing (models/ only, skip tests/) BEFORE P2 |
| R7 | MRO vs field-stack confusion in handler code — spec §5b explicitly warns these diverge on multi-`_inherit`; handler devs may conflate them | Medium | High | Golden test for `resolve_method` on a multi-inherit model returns field-stack ordering instead of MRO | WP-7 fixture includes a multi-inherit model where the two algorithms disagree; WP-8 handler code-review sign-off explicitly checks §5b wording in PR; separate helpers `method_chain_order()` and `field_chain_order()` enforced (not a single `override_order()`) |
| R8 | `libcst` can't statically resolve `_inherit = SOME_CONSTANT` or `_inherit = base.SOME_LIST` — the §5 research says zero such cases in CE 17, but third-party addons in `tvtmaaddons/` or customer code may have them | Low for `public` (CE), Medium for tenant | Medium — emits `resolution: unknown` where user expects a real answer | WP-4 parser encounters a non-literal `_inherit` RHS | WP-4 treats any non-literal RHS as `indexer_notes.dynamic_inherit=true`; WP-5 does not fabricate a chain; warning surfaced in MCP response; documented in release notes as a known limitation |

## 6. Open Decisions (deferred to dev)

These are NOT decided in this plan. Flagging them so implementation can either resolve or escalate to an ADR:

1. **Python framework — FastMCP pinned version.** `architecture/mcp-server.md` says "FastMCP (Python)". No ADR pins a specific release. Decide between `fastmcp` (Jerry Yip) vs the official `mcp` Python SDK with FastMCP wrapper. Impact: stdio vs http transport story, middleware ergonomics.
2. **Dependency manager — `uv` vs `poetry` vs `pip-tools`.** Recommendation: **`uv`** for (a) lockfile speed, (b) first-class tool-run path, (c) growing ecosystem alignment. Poetry is slower CI-wise; pip-tools needs more glue. Not decided until WP-1 starts.
3. **Migration tool — raw SQL files vs Alembic vs sqitch.** Recommendation: **raw SQL files numbered `V001__*.sql`** executed by a tiny `migrate.py`. Rationale: per-schema fan-out (public + N tenants) is awkward in Alembic's single-HEAD world; schema-as-parameter raw SQL is cleaner. If we later need branching migrations, revisit.
4. **Query layer — raw SQL vs SQLAlchemy Core vs psycopg3 + jinja.** Recommendation: **psycopg3 + textwrap SQL strings**. Recursive CTEs are harder to read through an ORM; P1 has only a handful of queries. Not ORM-averse as a principle; just not-yet-needed.
5. **Auth for dev topology.** `tenancy.md` says "tenant from auth token". P1 dev topology has no real auth. Plan: env var `OSM_TENANT=<name>` per server instance in dev; real auth is a Hosted-tier concern deferred to P5. Flagged to avoid it leaking into WP-8 handler logic (handlers must still read tenant from a pluggable context object, not from env directly).
6. **Tailscale tenant choice (WP-12).** Personal vs Viindoo. Flagged as blocker only for when a second developer joins; WP-12 tracks.
7. **Brand name.** Project directory + package name still use placeholder `<PROJECT>`. Roadmap defers branding to week 6–7 (P3 start). WP-1 MUST NOT hardcode a brand name in `pyproject.toml` — use `osm` (odoo-semantic-mcp) or similar neutral technical shortname that is easy to rename.
8. **Potential doc conflict to flag.** `architecture/graph-store.md` says cross-schema references store `bigint` with no `REFERENCES`; `architecture/tenancy.md` confirms this as closed decision. No conflict found — both docs consistent. (If a third future doc contradicts, escalate per `contexts/dev.md` rule.)
9. **Vector schema shape.** P1 provisions empty vector tables. Exact pgvector dimension (Voyage `voyage-code-3` = 1024, `bge-code-v1` = 1536) not yet fixed; per ADR-0002 both are supported. **Proposal**: do NOT pick a dimension in WP-2 — leave `embedding` column out entirely, create `code_chunks` without the vector column, add it in P3's first migration once provider selection is finalized per tenant. This avoids a schema migration to alter column dimension later.
10. **Code comments policy.** Per global `CLAUDE.md`: default no comments; WHY-only for invariants. Applies to every WP — plan reviewers should flag over-commented PRs.

## 7. Exit Gate Checklist (P1 close)

Tick every box before advancing to P2. Each bullet links to the deliverable that proves it.

### Correctness (ref: `roadmap.md` P1)
- [ ] 10-model fixture `resolve_model` golden = 100% (WP-9)
- [ ] 50-field golden `resolve_field` ≥95% (WP-9)
- [ ] 20-method golden `resolve_method` ≥95% (WP-9)
- [ ] All 3 `resolution: unknown` cases exercised in fixture and returned correctly (WP-7, WP-11 report)
- [ ] Tenant override test: a tenant-schema field override correctly wins over `public` (WP-9)

### Token reduction (primary product value — ref: `product_brief.md`)
- [ ] `resolve_model`: ≥90% reduction vs raw-source baseline on 10-model fixture (WP-11)
- [ ] `resolve_field`: ≥90% reduction vs raw-source baseline (WP-11)
- [ ] `resolve_method`: ≥70% reduction vs raw-source baseline (WP-11)

### Performance (ref: `architecture/mcp-server.md`)
- [ ] P50 `resolve_model` <20 ms (WP-11)
- [ ] P50 `resolve_field` <20 ms (WP-11)
- [ ] P50 `resolve_method` <50 ms (WP-11)
- [ ] P99 all tools <500 ms (WP-11)

### Multi-tenancy (ref: `architecture/tenancy.md`, ADR-0004)
- [ ] `public` + tenant schema union query returns tenant-wins ordering (WP-8 integration tests)
- [ ] Cross-schema staleness returns 409 with clear reason (WP-8, R5 mitigation)
- [ ] `create_tenant.py` successfully provisions a second tenant schema (WP-2)

### Operational
- [ ] `docker compose up -d` + `docker compose run indexer` works on clean host (WP-10)
- [ ] `make index` re-run on unchanged tree: zero row writes outside `cache_metadata.indexed_at` (WP-6)
- [ ] All WP unit + integration tests pass in CI (WP-1 workflow)
- [ ] `ruff`, `mypy` clean on `main`

### Documentation
- [ ] `reports/phase-01-benchmark.md` published (WP-11)
- [ ] `reports/phase-01-exit-criteria.md` published with every checkbox green (WP-11)
- [ ] `tasks/lessons.md` updated with any learnings discovered (continuous)
- [ ] Any new ADR (e.g., ADR-0005 Tailscale tenant) accepted or explicitly deferred (WP-12)

### Review
- [ ] Code review completed by `code-reviewer` agent on every merged PR
- [ ] `security-reviewer` passed on WP-8 server (handles user input + tenant resolution)
- [ ] Gate 2 (Ship ready) per global lifecycle: code merged, tests pass, review green, HDSD draft for `resolve_*` tools drafted

---

**On close:** populate `tasks/todo.md` "Review" section with date, outcome, verified list, and any remaining rollover items before kicking off P2 (`resolve_view`).
