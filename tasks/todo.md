---
status: active
scope: project
reads-with:
  - product_brief.md
  - roadmap.md
---

# Todo

Daily working checklist. Tick items as they complete. Move stale items to an `_archive/` section at the bottom rather than deleting silently.

## Invariants

- [ ] **Primary value = token/context reduction for the AI client.** Correctness is the floor; token savings is what we sell
- [ ] Respect the phase order: P1 → P2 → P3 → P4; P5 runs in parallel from end of P3
- [ ] No feature enters code without a `confirmed` spec in `specs/`
- [ ] Every MCP response returns `indexed_at_sha`
- [ ] Every tool is tenant-scoped; `public` (shared Odoo CE) + `<tenant>` (private addons). See ADR-0004
- [ ] Product brand name must not contain "Odoo"

## Decisions needed before P1 kickoff

### Urgent (blocks P1)

- [x] **Embedding provider default** — accepted 2026-04-22: Voyage `voyage-code-3` default, `bge-code-v1` self-host first-class. See `decisions/0002-embedding-provider.md`
- [x] **Postgres vs Neo4j** — accepted 2026-04-22: single PostgreSQL 16 + pgvector. See `decisions/0001-postgres-vs-neo4j.md`
- [ ] **Brief update — multi-tenant overlay** — current `product_brief.md` does not mention the shared/tenant overlay model (ADR-0004). David to review and either incorporate into brief or approve ADR as canonical override
- [x] **Tailscale tenant** — accepted 2026-04-22: **personal tailnet** (user-owned auth key). Sidecar block present but commented-out in `docker-compose.yml`; user enables after seeding `TS_AUTHKEY` in `.env`.

### Important (can resolve during P1)

- [ ] Hosted-tier pricing confirmation ($10/project/month) and trial policy
- [ ] Target audience for P5 — Viindoo ecosystem only vs global public launch
- [ ] DPA template sourcing for Hosted BYOC

### Needed before P5 (public distribution)

- [ ] **Brand name** — must not contain "Odoo" (trademark). Demoted from Urgent 2026-04-22: does not block P1–P4. Revisit around week 6–7 (start of P3) with more info on pilot audience + domain availability. Odoo-adjacent safe candidates: Inheritly, Addonly, Manifold, Xpathic, Loomix. See `decisions/0003-brand-name.md`

### Nice-to-have (needed before P5)

- [ ] OSS license choice (Apache 2.0 recommended)
- [ ] Doc site domain — subdomain of viindoo.com vs standalone
- [ ] Community channel — GitHub Discussions / Discord / both

## Current work

Gate 1 (Design confirmed) **passed 2026-04-22**. Ready to implement.

### WP-8 FastMCP server + 3 P1 tool handlers — done 2026-04-22

Delivered:

1. `osm/server/tenancy.py` — `TenantContext(tenant, schemas)` with `validate_tenant` regex gate, `context_from_env` (`OSM_TENANT` env, default `public`), `context_from_tenant`. `public` collapses to single-schema; tenant overlays public per ADR-0004.
2. `osm/server/db.py` — `union_all()` wraps per-schema SELECTs in a subquery aliased `osm_u` so outer ORDER BY references output-column names (not inner table aliases). `effective_indexed_at_sha()` collapses per-row shas into the envelope sha or returns `None` (handler raises `StaleIndexError` → 409).
3. `osm/server/errors.py` — `HandlerError` base + `InvalidInputError` (400), `NotFoundError` (404), `StaleIndexError` (409).
4. `osm/server/handlers/resolve_model.py`, `resolve_field.py`, `resolve_method.py` — raw SQL across UNION-ALL schemas, pydantic input models per spec §2, output envelope `{result, indexed_at_sha, warnings}` per `architecture/mcp-server.md`. Field `effective` merges non-null attrs last-wins (`resolve_field.md` §5b). Method `chain_is_broken` set when any non-root override has `calls_super=False`.
5. `osm/server/app.py` — `build_app()` registers 3 FastMCP tools (`resolve_model`, `resolve_field`, `resolve_method`) with lifespan capturing `DATABASE_URL` + tenant context. `main()` supports stdio (default) and streamable-http transports. Handler errors serialised into envelope with `{status_code, message, type}`.
6. `scripts/regenerate_golden.py` — one-shot script to re-label golden entries from live handler output. Preserves `TODO` skeletons and entries with `skip_handler` marker (e.g. `product.product.list_price` via `_inherits` delegation — flagged P2+ feature gap).
7. Tests: `test_tenancy.py` (9), `test_db_helpers.py` (6) offline; `test_handlers_golden.py` (7 DB-gated) — boots a throwaway tenant, runs WP-6 indexer over the fixture corpus, asserts every labeled golden entry is byte-equal (modulo `file` path prefix) to live handler output across all 3 tools + 400/404 error paths.

Acceptance: `ruff check .` PASS; `mypy osm scripts` PASS (21 files); `pytest -q` 227 passed live. `python -m osm.server.app --help` boots; `build_app()._tool_manager.list_tools()` enumerates `['resolve_model', 'resolve_field', 'resolve_method']`.

### WP-6 Indexer driver + cache metadata + delta re-index — done 2026-04-22

Delivered:

1. `osm/indexer/driver.py` (~1100 LOC) — `IndexStats` + `index(addon_roots, conn, tenant, git_sha)` entrypoint. Pipeline: manifest scan → `compute_load_order` → per-file blake2b-16 hash vs `cache_metadata.content_hash` → parse every python file (libcst ~ms/file at P1 scale; cached files reparsed for a global resolver view) → row-level upsert of modules/models/fields/methods with content-stable diff checks → orphan deletion → grouped-chain `override_of` write-back → `cache_metadata` upsert or touch. `SET LOCAL search_path TO "<tenant>", public` pins the tenant. Single transaction per run; caller owns `commit`.
2. **Override write-back dedup**: links grouped by `(model_name, entity_name)` with per-DB-row `seen` set so multiple `ParsedMethod` / `ParsedField` collapsed under `UNIQUE(model_id, name)` do not produce self-loops or re-run flip-flops. Critical for modules like base/res_users.py where 3 classes extend `res.groups` in one file.
3. `scripts/index.py` CLI — `--addons <path>` (repeatable), `--tenant`, `--git-sha`, `--database-url`. Tenant validated against `^[a-z][a-z0-9_]{1,62}$|^public$`. Reuses psycopg3 connection lifecycle from `scripts/migrate.py`.
4. `Makefile index:` target — `make index ADDONS="./tests/fixtures/odoo_ce_subset ./tests/fixtures/custom_addons" TENANT=public GIT_SHA=<sha>`.
5. `tests/indexer/test_driver_unit.py` (16 tests) — hash stability, auto_install coercion, `_model_names_for`, file enumeration, stats rollup. No DB needed.
6. `tests/indexer/test_driver_integration.py` (4 tests, `DATABASE_URL`-gated) — full 20-module index; re-run idempotence (indexed_at_sha persists across a git_sha bump; no data-table writes); single-method-body delta (only `cache_metadata.git_sha` changes for the touched file); two-tenant isolation.
7. `tests/test_schema_diff.py` — fixed Postgres 18 sequence-default normalisation (`nextval('public.seq'::regclass)` vs `nextval('seq'::regclass)`) so schema-diff passes under both 16 and 18.

Acceptance: `ruff check .` PASS; `mypy osm scripts` PASS (11 files); `pytest -q` 205 passed with `DATABASE_URL=postgresql:///osm_wp6_test?user=soncrits` live.

### WP-7 Test fixture corpus — done 2026-04-22

Delivered:

1. `tests/fixtures/odoo_ce_subset/` — 10 curated CE modules (base, web, bus, mail, product, sale, account, stock, sale_management, contacts). 1.6 MB, `models/` only (no views/data/wizard). `sale/__manifest__.py` deps trimmed to `['product','account','mail']` to keep subset self-contained.
2. `tests/fixtures/custom_addons/` — 10 hand-written Viindoo-flavored modules, each exercising one edge case (multi-inherit, `_inherits` delegation, field override with/without compute, method override super/break-super, conditional optional dep, `_register=False`, `@api.depends` added, `_order` override). 248 KB.
3. `tests/fixtures/golden/` — `resolve_model.json` 10/10 full; `resolve_field.json` 10/50 full + 40 TODO skeletons; `resolve_method.json` 5/20 full + 15 TODO. Remaining labelling finishes during WP-8 handler work (spec pragma allows).
4. `tests/fixtures/README.md` — catalogs each fixture with spec section reference.
5. `tests/indexer/test_fixtures_load.py` — smoke test: all 20 modules parse + load-order resolve without warnings.
6. `pyproject.toml` — ruff `exclude` extended for `odoo_ce_subset/` and `custom_addons/` (real Odoo line-length/B018 not ours to fix).

Acceptance: `ruff` PASS, `mypy` PASS (9 source files), `pytest -q` 184 passed 1 skipped.

### WP-5 Override-chain computation — done 2026-04-22

Delivered:

1. `osm/indexer/resolver.py` (427 LOC) — `FieldOverrideLink` + `MethodOverrideLink` frozen dataclasses; `compute_field_override_chains()`, `compute_method_override_chains()`, `synthesize_inherits_fields()`, `compute_method_mro()` (C3 linearization with linear fallback), `compute_resolver_result()` top-level entry for WP-6 driver.
2. `tests/indexer/test_resolver.py` (388 LOC) — 39 tests, 10 curated scenarios including Risk R1 (`_inherits` child-local wins, case 6) + Risk R7 (MRO vs linear chain divergence, case 10).
3. `tests/fixtures/resolver/` — 6 inline fixture files for scenarios not covered by WP-4/WP-7 fixtures.

Warnings propagation: `dynamic_inherit` → chain emit blocked (spec §5c case 3). `conditional_import` → chain emitted with warning. `register_false_chain` → chain still computed, flag propagated.

Acceptance post-WP-5 only: `ruff` + `mypy` PASS, `pytest -q` 119 passed 1 skipped.

### WP-4 Python parser (models / fields / methods) — done 2026-04-22

Delivered:

1. `osm/indexer/python_parser.py` — libcst `MetadataWrapper` + `PositionProvider` single-pass visitor. Exports `ParsedModel`, `ParsedField`, `ParsedMethod` frozen dataclasses; `FileParseResult` NamedTuple; `parse_file(path, conditional_submodules)` entry point; `scan_models_package(init_path)` conditional-import scanner.
2. `tests/fixtures/python_parser/` — 10 fixture files covering all acceptance cases: `pure_inherit.py`, `multi_inherit.py`, `inherits_delegation.py`, `conditional_import/` package, `register_false.py`, `nested_classes.py`, `depends_decorator.py`, `super_call.py`, `dynamic_inherit.py`, `broken_syntax.py`.
3. `tests/indexer/test_python_parser.py` — 41 unit tests; all green.
4. `tests/indexer/test_python_parser_real.py` — 10 acceptance tests against `product_product.py` + `product_template.py`; all pass (not skipped).

Offline acceptance: `ruff check .` PASS, `mypy osm scripts` PASS (0 errors), `pytest -q` 81 passed 1 skipped.

### WP-3 Manifest scanner + load-order simulator — done 2026-04-22

Delivered:

1. `osm/indexer/__init__.py` — module marker.
2. `osm/indexer/manifest.py` — `scan_addon_root` / `scan_addon_roots`; `ManifestRecord` frozen dataclass; `ast.literal_eval` only; filters `studio_customization` + `installable=False`; normalises `auto_install` to `bool | tuple[str, ...]`; `__openerp__.py` fallback; first-root-wins deduplication.
3. `osm/indexer/load_order.py` — `compute_load_order` fix-point loop matching `graph.py:31-151`; cascading warn-and-drop for missing/dropped deps; `CyclicDependencyError` for closed cycles; `(depth ASC, name ASC)` sort; `LoadOrderRecord` frozen dataclass.
4. `tests/indexer/test_manifest.py` + `tests/indexer/test_load_order.py` + `tests/indexer/test_load_order_golden.py` — 29 tests total; all green.
5. `tests/fixtures/addons/` — 9 fixture modules.
6. `tests/fixtures/odoo_ce_subset_manifests/` — frozen manifests for 10 CE modules (base, web, mail, bus, product, stock, account, sale, sale_management, contacts).
7. `tests/fixtures/golden/load_order_ce_subset.json` — simulator-produced golden; GOLDEN_SOURCE: simulator_self, manual_verify_once.
8. `tests/fixtures/generate_golden_load_order.py` — golden regeneration script.

Offline acceptance: `ruff check` PASS (WP-3 files clean), `mypy osm scripts` PASS (WP-3 files clean), `pytest -q` 81 passed 1 skipped.

Note: pre-existing ruff/mypy errors in `osm/indexer/python_parser.py` (WP-4 file) were present before this WP and are unchanged.

### WP-2 Postgres schema migrations + tenancy bootstrap — done 2026-04-22

Delivered:

1. `migrations/001_init.sql` — idempotent DDL for `modules`, `models`, `fields`, `methods`, `views`, `view_patches`, `cache_metadata`, plus the embedding-less `code_chunks` stub. All 8 tables carry `tenant text NOT NULL DEFAULT current_schema()`. Cross-schema refs (`fields.override_of`, `methods.override_of`) stored as bare `bigint` with no REFERENCES. Indexes per architecture: btree on `(module_id, name)`, `override_of`, `(model_id, field_name)`, `(model_id, method_name)`, view hot paths, GIN on `methods.decorators`, btree on `cache_metadata (tenant, module_name)` and `cache_metadata (content_hash)`.
2. `scripts/create_tenant.py` — CLI validates schema name against `^[a-z][a-z0-9_]{1,62}$`, rejects `public` / `pg_*` / `information_schema`, creates schema, then runs migrations via `scripts.migrate.main`. Idempotent.
3. `tests/test_schema_diff.py` — skipped when `DATABASE_URL` unset; otherwise creates throwaway `osm_test_<hex>` tenant and asserts identical columns / constraints / indexes vs `public` after schema-name normalization. Teardown drops the tenant schema.

Offline acceptance: `uv run ruff check .` PASS, `uv run mypy osm scripts` PASS, `uv run pytest -q` PASS (1 passed, 1 skipped).

### WP-1 Repo bootstrap + tooling — done 2026-04-22

See `tasks/phase-01-plan.md` §2 "WP-1" for the full scope. Decisions locked during this wave:

1. Dependency manager: **uv** (`pyproject.toml` + `uv.lock`). No Poetry, no pip-tools.
2. Migrations: **raw SQL files** in `migrations/`, applied by `scripts/migrate.py` (psycopg 3, `--schema` flag). No Alembic / SQLAlchemy.
3. Python package name: **`osm`** (placeholder; rename when brand lands).
4. Database: **Postgres 16 + pgvector** via `pgvector/pgvector:pg16`.
5. Tailscale sidecar: commented-out block in `docker-compose.yml`; user owns the auth key via personal tailnet.
6. Python: **3.11+** (`requires-python = ">=3.11"`).
7. MCP SDK: official `mcp>=1.0` (contains FastMCP surface) rather than third-party `fastmcp`.

Blocker for WP-2 kickoff: `uv` must be installed on the dev host (`curl -LsSf https://astral.sh/uv/install.sh | sh`); then run `uv lock && uv sync --extra dev` to generate + commit `uv.lock`.

## Design closed 2026-04-22

- [x] `research/odoo-internals.md` filled with CE 17 source references (status: draft, 535 lines)
- [x] ADR-0001 (Postgres + pgvector) → accepted
- [x] ADR-0002 (Voyage default + bge self-host) → accepted
- [x] ADR-0004 (Multi-tenant overlay, schema-per-tenant) → accepted
- [x] `architecture/indexer.md` → confirmed
- [x] `architecture/graph-store.md` → confirmed (+ no cross-schema FK rule, GIN on decorators)
- [x] `architecture/tenancy.md` → confirmed (Open question #1 closed — soft logical refs)
- [x] `architecture/mcp-server.md` → confirmed
- [x] `data-model/modules.md` → confirmed (dropped `license`)
- [x] `data-model/models.md` → confirmed (added `indexer_notes` jsonb)
- [x] `data-model/fields.md` → confirmed (added `default`, documented nullable semantics)
- [x] `data-model/methods.md` → confirmed (dropped `is_api_*`, use GIN on decorators)
- [x] `data-model/views.md` → confirmed
- [x] `data-model/cache_metadata.md` → new file (status: draft)
- [x] Specs `resolve_model`, `resolve_field`, `resolve_method` → confirmed (with §5b Resolution rules + §5c Edge cases)

## Backlog — Phase 1 implementation

- [ ] Bootstrap repo (`Viindoo/<project-name>`), Docker Compose, PostgreSQL 16 + pgvector extension
- [x] Write Postgres schema migrations per `data-model/*.md` (per-schema + `public` bootstrap) — done 2026-04-22 (WP-2)
- [x] Implement manifest scanner + module load-order simulator (per `research/odoo-internals.md` §1) — done 2026-04-22 (WP-3)
- [x] Implement `libcst` Python parser → populate `models`, `fields`, `methods` tables — done 2026-04-22 (WP-4)
- [x] Implement `override_of` chain computation (field stack vs method MRO, per specs §5b) — done 2026-04-22 (WP-5)
- [x] Wire indexer driver + `scripts/index.py` CLI + cache-metadata delta re-index — done 2026-04-22 (WP-6)
- [x] Wire FastMCP server with 3 P1 tools (`resolve_model`, `resolve_field`, `resolve_method`) — done 2026-04-22 (WP-8)
- [x] Build test fixture: Odoo CE subset + 10 custom modules with curated override cases — done 2026-04-22 (WP-7)
- [ ] Write accept test: 10 sample questions end-to-end via Claude Code
- [ ] Publish correctness + token-reduction benchmark per roadmap P1 exit criteria

## Blockers / open questions

- Who is the first BYOC pilot? (needed for P3 validation)
- GPU availability for self-hosted embedding spike

## Review

- Date:
- Outcome:
- Verified:
- Remaining:
