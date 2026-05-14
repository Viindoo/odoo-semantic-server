# Bảng Theo Dõi Tiến Độ — Odoo Semantic MCP

> **Quy ước trạng thái:**
> - `[ ]` — chưa bắt đầu
> - `[~]` — đang làm (AI agent hoặc human đang xử lý)
> - `[x]` — hoàn thành, đã commit
> - `[!]` — bị blocked (ghi lý do bên dưới)
>
> **Quy tắc cho AI agent:** Trước khi bắt đầu task, đánh `[~]` và commit. Sau khi xong, đánh `[x]` và commit. Không làm nhiều tasks cùng lúc trên cùng file.

---

## Milestone 1 — "First Wow"
**Intent:** Chứng minh giá trị cốt lõi — AI hiểu inheritance chain cross-repo.  
**Outcome:** `resolve_model("account.move", "17.0")` trả về đúng full chain không hallucinate.

- [x] `docker-compose.yml`: Neo4j + PostgreSQL/pgvector
- [x] `src/indexer/scanner.py`: git branch detection + manifest discovery
- [x] `src/indexer/registry.py`: module registry per version
- [x] `src/indexer/resolver.py`: topological sort + circular dep handling
- [x] `src/indexer/parser_python.py`: `_name`/`_inherit`/`_inherits`/fields/methods
- [x] `src/indexer/writer_neo4j.py`: Module/Model/Field/Method nodes + edges
- [x] `src/mcp/server.py`: `resolve_model` + `resolve_field` + `resolve_method`
- [x] E2E test: kết nối VS Code + Claude Code, verify kết quả *(2026-05-14 — verified via pre-launch §6 sign-off: resolve_model/resolve_field/resolve_method all PASS on production; see `docs/m7.5-batch1-mcp-signoff.md`)*
- [x] `.github/workflows/ci.yml`: lint + unit tests + integration tests (Neo4j service container)

## Milestone 2 — "View Wow"
**Intent:** Mở rộng semantic awareness sang UI layer + thiết lập anti-drift guard.  
**Outcome:** `resolve_view("sale.view_sale_order_form", "17.0")` trả về đúng XPath overrides + view chain.

- [x] `src/indexer/models.py`: thêm XPathInfo, ViewInfo, QWebInfo, ViewParseResult
- [x] `src/indexer/parser_xml.py`: views, inherit_id, xpath targets
- [x] `src/indexer/parser_qweb.py`: template inheritance chain
- [x] `src/indexer/writer_neo4j.py`: View/QWebTmpl nodes + INHERITS_VIEW/EXTENDS_TMPL edges + indexes
- [x] `src/mcp/server.py`: `resolve_view` + view chain reconstruction
- [x] `tests/test_doc_sync.py`: TASKS.md file guard + stale `[~]` marker guard (anti-drift)
- [x] `tests/test_output_snapshots.py`: MCP output schema contract tests (anti-drift)
- [x] E2E test: kết nối VS Code + Claude Code, verify `resolve_view` kết quả *(2026-05-14 — `resolve_view("sale.view_order_form", "17.0")` PASS in §6, 25 view extensions với XPath detail; see `docs/m7.5-batch1-mcp-signoff.md`)*

## Milestone 2.5 — "Foundation Wow"
**Intent:** Hạ tầng đủ để E2E test M1+M2 trên data thật + nền cho M5 per-user scoping.
**Outcome:** `python -m src.indexer --profile viindoo_17` index full Odoo 17 + Viindoo addons; Claude Code gọi 4 MCP tools trên data thật.

- [x] `src/config.py`: INI reader (`configparser`)
- [x] `odoo-semantic.conf.example`: app config template
- [x] `src/db/migrate.py`: schema `profiles` + `repos`
- [x] `src/db/repo_registry.py`: CRUD profiles/repos
- [x] `src/manager/__main__.py`: admin CLI (`add-profile`, `add-repo`, `list`)
- [x] `src/indexer/pipeline.py`: wire `parser_xml` + `parser_qweb` (M2 blocker fix)
- [x] `src/indexer/__main__.py`: `python -m src.indexer --profile / --all`
- [x] `src/mcp/server.py`: read host/port from `odoo-semantic.conf`
- [x] `docker-compose.yml`: bind DB ports `127.0.0.1` (same-server default)
- [x] `Makefile`: extend `install` target — copy configs, hint next steps
- [x] `.gitignore`: thêm `odoo-semantic.conf` (user secret)
- [x] `README.md`: deploy steps thật
- [x] `CONTRIBUTING.md`: cập nhật source tree
- [x] `docs/deploy.md`: production deploy guide — DB / App / Proxy tiers
- [x] E2E manual: clone Odoo 17 → register → index → Claude Code call 4 tools *(2026-05-14 — production server `odoo-semantic.viindoo.com` đã indexed Odoo 17.0; 4 tools (resolve_model, resolve_field, resolve_method, resolve_view) all PASS via Claude Code plugin; see `docs/m7.5-batch1-mcp-signoff.md`)*

## Milestone 3 — "Semantic Wow"
**Intent:** Tìm kiếm code theo ngữ nghĩa.  
**Outcome:** `find_examples("compute tax based on partner country")` trả về code thật, dùng được ngay.

- [x] `pyproject.toml`: thêm pgvector, tree-sitter, tree-sitter-javascript, ollama marker
- [x] `src/indexer/models.py`: thêm `source_code`/`source_definition`/`arch`/`content`/`file_path` + `JSChunk`
- [x] `src/indexer/parser_python.py`: capture source text cho method + field
- [x] `src/indexer/parser_xml.py`: capture arch + file_path cho ViewInfo
- [x] `src/indexer/parser_qweb.py`: capture content + file_path cho QWebInfo
- [x] `src/db/migrate.py`: embeddings table + pgvector extension + HNSW index
- [x] `src/embedding/instructions.py`: `INSTRUCT_NL_TO_CODE` constant (Qwen3 asymmetric)
- [x] `src/indexer/embedder.py`: EmbedderClient Protocol + FakeEmbedder + Qwen3Embedder (MRL 1024-dim)
- [x] `src/indexer/parser_js.py`: era-aware JS parser (Era1 Widget.extend, Era2 odoo.define, Era3 OWL/patch)
- [x] `src/indexer/writer_pgvector.py`: EmbeddingChunk + make_chunks + write_module_embeddings (delete-before-insert)
- [x] `src/mcp/server.py`: `find_examples` MCP tool (hybrid pgvector ANN + Neo4j centrality rerank)
- [x] `tests/`: 100% unit test coverage cho tất cả M3 components
- [x] `docs/deploy.md`: thêm §9 Embedder Setup (Ollama + pgvector bootstrap + license note)
- [x] **E2E manual**: Ollama chạy với qwen3-embedding-q5km → index Viindoo 17.0 → Claude Code call `find_examples` *(2026-05-14 — UNBLOCKED post-PR #84: actual root cause was wrong embedder port `:9999`, not Ollama SSL. After URL fix, client-side smoke via Claude Code MCP plugin PASS: `find_examples("sale order confirm", "17.0")` → 5 results, top score 0.84. 2-of-2 cross-check (Opus + Sonnet shadow) confirmed. Report: `docs/m7.5-mcp-verification.md`.)*
- [ ] **Recall benchmark**: `pytest tests/test_find_examples_recall.py -m ollama` → VN≥0.75, EN≥0.80 *(2026-05-14 — BLOCKED bởi cùng Ollama SSL issue. Local benchmark cần `qwen3-embedding-q5km` model pull + Viindoo 17.0 re-index local; defer đến khi có local Ollama replica hoặc production fix.)*

## Milestone 4 — "Impact Wow"
**Intent:** Full-stack impact analysis từ Python model đến JS component.  
**Outcome:** `impact_analysis("field", "sale.order.amount_total", "17.0")` liệt kê chính xác tất cả thứ bị ảnh hưởng.

- [x] `src/indexer/writer_neo4j.py`: TARGETS_MODEL edge (View → Model) — hoãn từ M2, prerequisite để query view ảnh hưởng khi đổi model/field
- [x] `src/indexer/parser_js.py`: parse_module_graph() — extract JSPatchInfo + OWLCompInfo cho Neo4j
- [x] `src/indexer/writer_neo4j.py`: JSPatch + OWLComponent nodes + PATCHES edges
- [x] `src/mcp/server.py`: `impact_analysis` + risk_level scoring

## Milestone 4.5 — "Spec Wow"
**Intent:** Index Odoo upstream specs (API lifecycle, lint rules, CLI flags) + unblock Odoo v8/v9 codebase support.
**Outcome:** `lookup_core_api("name_get", "18.0")` → `status: removed`; `cli_help("server", "--longpolling-port", "18.0")` → `status: removed, replacement: --gevent-port`; `find_deprecated_usage("19.0")` quét code user upgrade chuẩn bị; clone Odoo 8 → indexer hết silent-skip (era-aware parser).

- [x] WI0: ADR-0002 spec schema policy review + accept
- [x] WI1: Phase 0 v8/v9 enablement (`registry.py` ManifestFinder Protocol, `parser_python.py` era-aware text-regex, `mcp/server.py` `_latest_version()` numeric compare fix)
- [x] WI2: `parser_odoo_core.py` + `diff_engine.py` + CoreSymbol nodes (allow-list 8 file core)
- [x] WI3: `parser_lint_rules.py` + LintRule nodes (code-extract v17-v19, static placeholder v8-v16)
- [x] WI4: `parser_cli.py` + CLICommand/CLIFlag nodes
- [x] WI5: 5 MCP tool (`lookup_core_api`, `api_version_diff`, `find_deprecated_usage`, `lint_check`, `cli_help`)
- [x] WI6: USES_CORE_SYMBOL edge từ user code (extend `parser_python.py` AST visit, V0 scope: deprecated/removed only)
- [x] WI7: Tests + snapshots + integration
- [x] WI8: Docs update (TASKS.md, README.md, kien-truc.md, CLAUDE.md)

> Plan: [`docs/superpowers/plans/2026-05-08-milestone-4-5-spec-wow.md`](docs/superpowers/plans/2026-05-08-milestone-4-5-spec-wow.md)  
> ADR: [`docs/adr/0002-spec-schema-policy.md`](docs/adr/0002-spec-schema-policy.md)

## Milestone 4.6 — "Pattern Wow"
**Intent:** Curated patterns + override convention metadata để AI viết code đúng idiom Odoo + Viindoo, chống hallucinate Odoo Enterprise module trên stack Community/Viindoo.
**Outcome:** `suggest_pattern("computed field cross-model partner_id")` → 3-5 ví dụ thật từ Odoo CE + gotchas ranked; `check_module_exists("knowledge", "17.0")` → `is_ee_confusion: Yes` + warning + Viindoo equivalent (nếu có); `find_override_point("sale.order", "action_confirm", "17.0")` → `super_safety: always`, `super_ratio: 7/7`, anti-patterns list.

- [x] WI0: ADR-0003 pattern storage policy review + accept
- [x] WI1: Module enrichment (`edition` ∈ {community/enterprise/viindoo/oca/custom} + `viindoo_equivalent_qname` + `EE_CONFUSION` dict 16 entry)
- [x] WI2: Method enrichment (`convention_kind` + `super_safety` + `return_required` từ method name regex map)
- [x] WI3: PatternExample Neo4j node + reuse `embeddings` table với `chunk_type='pattern_example'` (per ADR-0003)
- [x] WI4: Pattern seed ~50 entry curation (`src/data/patterns.json`) + `seed_patterns.py` one-shot CLI
- [x] WI5: 3 MCP tool (`suggest_pattern`, `check_module_exists`, `find_override_point`)
- [x] WI6: Tests + snapshots (+ smoke job `tests/test_smoke_pattern_wow.py`: seed CLI E2E + EE warning + super_ratio + USES_CORE_SYMBOL silent-skip — wired into `.github/workflows/ci.yml` smoke-tests job)
- [x] WI7: Docs update (TASKS.md, README.md, kien-truc.md)

> Plan: [`docs/superpowers/plans/2026-05-08-milestone-4-6-pattern-wow.md`](docs/superpowers/plans/2026-05-08-milestone-4-6-pattern-wow.md)  
> ADR: [`docs/adr/0003-pattern-example-storage.md`](docs/adr/0003-pattern-example-storage.md)  
> Depends on: M4.5 (CoreSymbol node cho USES_CORE_SYMBOL edge — graceful skip nếu chưa ship)

## Milestone 5 — "Product Wow"
**Intent:** Đóng gói thành sản phẩm bất kỳ ai deploy được trong dưới 10 phút.
**Outcome:** `docker compose up -d` + Web UI add repos + index. Admin tạo API key → user add vào Claude Code config → MCP tools respond. Production-ready: `GET /health` + Postgres advisory lock ngăn indexer chạy chồng.

> Plan: [`docs/superpowers/plans/2026-05-09-milestone-5-product-wow.md`](docs/superpowers/plans/2026-05-09-milestone-5-product-wow.md) (rev 2 — post-Opus debate)
> ADR: [`docs/adr/0004-auth-web-ui-ssh-policy.md`](docs/adr/0004-auth-web-ui-ssh-policy.md)

**Wave 1 — Foundation (Haiku):**
- [x] `docs/adr/0004-auth-web-ui-ssh-policy.md`: ADR record 10 quyết định kiến trúc M5 (no AUTH_DISABLED, Postgres lock, fail-fast FERNET_KEY, v.v.)
- [x] `src/db/migrate.py`: thêm `api_keys`, `ssh_key_pairs` (+ `key_version` INT), `usage_log` tables
- [x] `src/db/auth_registry.py`: CRUD api_keys, ssh_key_pairs, usage_log
- [x] `src/manager/__main__.py`: thêm `create-api-key <name>` subcommand — CLI bridge trước khi Web UI land
- [x] `src/indexer/pipeline.py`: **Postgres advisory lock** (`pg_try_advisory_lock`) — thay fcntl, cross-container, async-safe, auto-release on crash
- [x] `src/mcp/health.py` + `src/mcp/middleware.py` (stub): tách từ server.py — health endpoint + middleware placeholder cho Wave 2
- [x] `pyproject.toml`: thêm `jinja2`, `python-multipart`, `cryptography>=42`; httpx dev dep
- [x] `docker-compose.yml`: named volumes + `restart: unless-stopped`
- [x] `Dockerfile`: app container (python:3.12-slim + postgresql-client + git)
- [x] `install.sh` + `docs/deploy/` service files: non-Docker installation path

**Wave 2 — Backend Core:**
- [x] `src/auth.py` + `src/mcp/middleware.py`: AuthMiddleware — LRU cache 5 min + `asyncio.to_thread` DB + `asyncio.create_task` log, **không có AUTH_DISABLED bypass** (Sonnet)
- [x] `src/web_ui/` scaffold: FastAPI + Jinja2 port 8003 hard-bind `127.0.0.1`, dashboard route (Sonnet)
- [x] `tests/test_health_endpoint.py`: health endpoint tests — mcp_tools introspected, không hardcode (Haiku)

**Wave 3 — Web UI Pages:**
- [x] Web UI `/repos`: list profiles + repos, create profile, add repo (SSH URL note: manual clone M6), trigger index non-blocking (Sonnet)
- [x] Web UI `/api-keys`: list + create (raw key shown once) + deactivate (Haiku)
- [x] Web UI `/ssh-keys`: generate Ed25519 keypair, FERNET_KEY **fail-fast** (không ephemeral fallback), show public key + deploy key instructions (Sonnet)

**Wave 4 — Tests:**
- [x] `tests/test_auth_integration.py`: auth + DB end-to-end, advisory lock concurrency, cache TTL (Haiku)
- [x] `tests/test_smoke_product_wow.py` + ci.yml update: health schema, auth 401/bypass `/health` (Haiku)

**Wave 5 — Docs:**
- [x] `README.md` + `CONTRIBUTING.md`: M5 onboarding (docker → Web UI → create-api-key → Claude Code) + manual backup note (Haiku)
- [x] `docs/deploy.md` §10–§13: Auth, Web UI, SSH Keys, Manual Backup; `TASKS.md` M5 `[x]` (Haiku)

## Milestone 5.5 — "Polish Wow"
**Intent:** Observability + test discipline + landing zone cho tech-debt phát sinh trong M5.
**Outcome:** Mọi long-running operation có progress feedback; mọi MCP tool có anti-drift snapshot test; deferred M5 items hoàn tất.

> Plan: [`docs/superpowers/plans/2026-05-07-milestone-5-5-polish-wow.md`](docs/superpowers/plans/2026-05-07-milestone-5-5-polish-wow.md)

**Section A — Indexer observability:**
- [x] `src/indexer/__main__.py`: `--verbose` flag enable INFO logging + `tqdm` progress bar (modules processed / total)
- [x] `tests/test_output_snapshots.py`: thêm snapshot test cho `resolve_view` (pattern khớp 5 tool còn lại — anti-drift guard)
- [x] **Test isolation fix (M4.6 carry-over):** `tests/test_mcp_server_config.py` patch module-level `_driver = object()` rò rỉ sang `tests/test_mcp_spec_tools.py`. Fix: switch sang `monkeypatch.setattr`.

**Section B — Deferred từ M5 (moved per Opus debate rev 2):**
- [x] `src/cli.py`: `backup`/`restore` via subprocess (pg_dump + manual Neo4j note; APOC not required) — moved from M5
- [x] **Pattern feedback loop:** `POST /api/feedback` endpoint + `pattern_feedback` table + thumbs up/down trên `suggest_pattern` output — moved from M5 (requires auth layer, ship sau M5)
- [x] Rate limiting per API key (per-minute sliding window) — DoS protection; local deploy M5 risk thấp → defer
- [x] FERNET_KEY rotation script: re-encrypt tất cả `ssh_key_pairs.private_key_encrypted` rows — document manual procedure M5; script ở đây
- [x] Structured JSON logging (`logging.config` hoặc `structlog`) — observability production

**Section C — Landing zone (debt từ M5 + doc gaps):**
- [x] `tests/test_web_ui_browser.py` (24 tests, Playwright): browser-level E2E cho API Keys, SSH Keys, Repos, Dashboard, Navigation — landed cùng CI refactor (docker compose single source of truth)
- [x] `docs/deploy.md` §4.3 + §5.2: cập nhật auth section từ "M2.5 placeholder" → "M5 X-API-Key required" + thêm `/health` bypass auth note + fix verify config snippet thiếu header
- [x] `README.md`: bỏ note stale "bỏ header X-API-Key (M5 sẽ thêm auth)" → note đúng auth mandatory
- [x] `tests/test_mcp_server_config.py` isolation fix: monkeypatch leak `_driver = object()` sang `test_mcp_spec_tools.py` — switch sang `monkeypatch.setattr`

**Section E — Concurrency Hardening (P1):**
- [x] `src/indexer/pipeline.py`: thêm `indexer_is_running(pg_conn) -> bool` public helper
- [x] `src/web_ui/routes/repos.py`: dedup check trước Popen + `flash` query param trong `repos_page()`
- [x] `src/web_ui/templates/repos.html`: flash warning banner (amber style)
- [x] `tests/test_web_ui_repos.py`: 2 unit tests dedup (blocked + ok path)

**Section F — Job Tracking (P2 — Complete):**
- [x] `src/db/migrate.py`: table `indexer_jobs` (id, profile_name, status, started_at, finished_at, error_msg, pid, created_at) + indexes
- [x] `src/db/job_registry.py`: CRUD — `create_job()`, `update_job()`, `get_last_job()`, `list_running_jobs()`, `get_job()`
- [x] `src/indexer/__main__.py`: thêm `--job-id INT` arg → update job status start/success/error
- [x] `src/web_ui/routes/repos.py`: `index_repo()` tạo job record + truyền `--job-id` vào subprocess
- [x] route GET /repos/jobs/{job_id}/status: JSON `{id, profile_name, status, pid, started_at, finished_at, error_msg, created_at}` — landed cùng `src/web_ui/routes/repos.py`
- [x] `src/web_ui/templates/repos.html`: status badge + JS polling 5s nếu running/queued
- [x] `tests/test_job_registry.py`: unit tests CRUD (15 tests)

> **Lý do tách M5.5:** items polish không block M5 ship; deferred items cần auth layer M5 trước. Pattern theo M2.5 precedent (milestone phụ giữa product milestones).

## Milestone 6 — "Scale Wow" (Shipped 2026-05-11)
**Intent:** Hỗ trợ toàn bộ ecosystem Viindoo, multi-version, incremental updates.  
**Outcome:** Re-index chỉ mất vài giây. Index đồng thời 16.0 + 17.0 + 18.0.

**Backlog top-level (deferred — see Wave 3 / Wave 4 / M7 grouping below):**

*Wave 3 (shipped 2026-05-10, ~10 WIs):*
- [x] `src/indexer/version_presets.py`: preset "viindoo-17.0", "viindoo-18.0"
- [x] **Pattern catalogue maintenance (M4.6 defer) — remaining sub-items:**
    - [x] Seed expansion từ ~50 → ~200 patterns + community contribution path (PR template + `src/data/patterns.json` review checklist)
    - [x] `find_override_point` cross-version diff — surface pattern thay đổi giữa v17 vs v18 (vd `_compute_*` rename, decorator switch)
- [x] **EE_CONFUSION auto-detect (M4.6 defer):** thay hardcode `src/data/ee_modules.py` 16-entry dict bằng auto-detect từ manifest `license = 'OEEL-1'` + path scan upstream Odoo CE repo (per M4.6 plan §Risk & Mitigation). Keep hardcode dict làm fallback cho khi indexer chưa scan upstream.

*Wave 4 (shipped 2026-05-10, ~5 WIs):*
- [x] **Auto-clone qua SSH khi user add repo (moved from M5):** detect SSH URL trong Web UI → auto-clone via Ed25519 private key + `GIT_SSH_COMMAND` + `tempfile.mkstemp(mode=0o600)` → set `local_path` automatically; companion: host fingerprint management UI (`StrictHostKeyChecking=accept-new` policy). Khảo sát 2026-05-10 confirm M5 SSH-key infra (FERNET, ssh_key_pairs table, generate Ed25519, list/CRUD) đầy đủ; chỉ thiếu bridge → add-repo flow (URL detection regex + clone helper + form selector + known_hosts UI optional).

*Defer M7 (xem Milestone 7 section below):*
- → 214 `viindoo_equivalent_qname` auto-populate (graph traversal heuristic)

**Shipped highlights (full audit trail trong Section H/I/G dưới):**
- [x] Wave 1 P3: Per-profile advisory locks — 2 profile khác nhau index song song không block nhau (`src/indexer/pipeline.py _profile_lock_id`).
- [x] Wave 1 P3: ThreadPoolExecutor parallel repo scan — `--max-workers` flag (`pipeline.py index_profile`).
- [x] Wave 1 P3: PostgreSQL connection pool — `_pg_pool` SimpleConnectionPool replaces `_pg_conn` singleton + `_PG_LOCK`.
- [x] Wave 2 Chain A (5 WIs): Incremental indexer — `repos.head_sha` + `Module.last_commit_sha` + `incremental.py` git diff helpers + `pipeline._index_repo` skip-unchanged + force-push fallback + `--full` flag. ADR-0007 records 7 design decisions.
- [x] Wave 2 Chain B (2 WIs): Auto-reseed pattern catalogue — `_SeedMeta` Neo4j sha256 sentinel + `seed_patterns.run()` public callable + auto-call at end of `index_profile()` + `--force` bypass.
- [x] Wave 2 Chain C (1 WI): `index_all --profile-workers` ThreadPoolExecutor wraps profile loop — closes M6 thesis "Index đồng thời 16.0 + 17.0 + 18.0".
- [x] Wave 3 Chain A: Pattern catalogue community contribution — ADR-0009 + jsonschema + PR template + 29 new patterns (total ~50→79 entries).
- [x] Wave 3 Chain B: EE_CONFUSION manifest-license auto-detect + indexed-first lookup in `check_module_exists` tool.
- [x] Wave 3 Chain C: `version_presets.py` + `apply-preset` admin CLI — quick-start Odoo 17.0 + 18.0 + 19.0 multi-version profiles.
- [x] Wave 3 Chain D: `find_override_point` cross-version diff — new `Method.signature` Neo4j property enables pattern change detection v17 vs v18.
- [x] Wave 4 Diamond DAG: SSH auto-clone (`src/git_utils.py` + `src/cloner` + `repos.ssh_key_id`/`clone_status`/`clone_error_msg` + Web UI form UX) — user adds SSH URL, system auto-clones to temp dir, sets `local_path` transparently.

**Section H — Environment harness (P2 — Wave 1 shipped 2026-05-10):**

Mục tiêu: runtime + tests + CI + Docker compose cùng đọc 1 nguồn version pinning. Tránh drift kiểu `.env.example` ghi `neo4j:5.26.25` nhưng `nightly-smoke.yml` hardcode khác.

- [x] **Single source of truth cho env versions (Odoo-style — Wave 1):** `.env.example` declare cả `NEO4J_IMAGE` và `PG_IMAGE`. `docker-compose.yml` đọc qua `${PG_IMAGE:-...}` slot. `nightly-smoke.yml` có comment header note nghĩa vụ sync manually (architectural constraint GitHub Actions service containers parse-time). Anti-drift guard `tests/test_env_versions_sync.py` regex parse `.env.example` + assert workflow chứa cùng image strings. CLAUDE.md "Image Versions — Nguồn Sự Thật" mở rộng cover cả PG.
- [x] **Lock min Neo4j version tại runtime (Wave 1):** `src/mcp/server.py _get_driver()` gọi `CALL dbms.components()` lần đầu lifetime, fail-fast nếu major < 5. Skip nếu `os.getenv("CI") == "true"` (CI service container đã pinned). Module-level `_version_checked` flag tránh re-query.
- [x] **Lock min PostgreSQL + pgvector version (Wave 1):** `src/db/migrate.py run_migrations()` đầu function chạy `SELECT current_setting('server_version_num')::int`, fail-fast nếu < `160000` (PG 16). `_ensure_extension()` thêm `SELECT extversion FROM pg_extension WHERE extname='vector'`, fail-fast nếu < 0.8.
- [x] **Python 3.12 compliance enforcement (Wave 1):**
    - `src/cli.py`: `try/except ImportError` quanh `psycopg2.extensions` xoá (psycopg2-binary mandatory).
    - `tests/test_web_ui_repos.py`: `datetime.now()` → `datetime.now(tz=UTC)` tại 2 call sites.
    - `pyproject.toml [tool.ruff.lint] select`: thêm `"UP"` rule. 13 UP violations existing auto-fixed (≤ 20 threshold): `Optional[X]` → `X | None`, `timezone.utc` → `UTC`, unused imports cleaned.
    - `CONTRIBUTING.md`: section "Python 3.12 Code Style" mới — cấm `from __future__ import annotations`, `typing.Dict/List/Optional/Union[]`, `sys.version_info` guards.
- [x] **Type alias PEP 695 cho `conn` parameter (Wave 1):** `src/db/_types.py` mới định nghĩa `type PgConn = psycopg2.extensions.connection` (PEP 695 native 3.12). 22 functions trong `src/db/{job,auth,repo}_registry.py` + `migrate.py` + `src/manager/__main__.py` annotate `conn: PgConn`.

**Section I — Indexer thesis (Wave 2 — shipped 2026-05-10):**

Mục tiêu: thực thi THESIS của M6 — "Re-index chỉ mất vài giây. Index đồng thời 16.0 + 17.0 + 18.0." 8 WIs orchestrated qua 3 chains: Chain A (incremental, 5-deep), Chain B (auto-reseed, 2-deep), Chain C (profile-workers, 1).

- [x] **W2-1 — Schema head_sha + ModuleInfo.commit_sha (Wave 2 Chain A):** `repos.head_sha TEXT` column (nullable, idempotent ALTER per ADR-0001 M6 policy) + `ModuleInfo.commit_sha: str | None = None` field + `repo_registry.get_repo_head_sha` / `update_repo_head_sha` helpers (last_indexed_at bump on update).
- [x] **W2-2 — Scanner per-module commit sha (Wave 2 Chain A):** `scanner.get_module_commit_sha(repo_path, module_relpath)` via `git -C log -1 --format=%H -- <relpath>` (graceful None on empty repo / non-repo / nonexistent path) + `registry.build_registry()` populates ModuleInfo.commit_sha.
- [x] **W2-5 — Writer Module.last_commit_sha (Wave 2 Chain A):** `writer_neo4j._write_parse_result()` Module MERGE SET clause adds `m.last_commit_sha = $commit_sha` (NOT in MERGE key per ADR-0001). None values OK; re-MERGE updates property.
- [x] **W2-3 — incremental.py module + diff logic (Wave 2 Chain A):** `src/indexer/incremental.py` (new) — `get_repo_head` / `is_ancestor` (force-push detection) / `compute_changed_module_paths` (git diff filtered to module dirs with __manifest__.py or __openerp__.py) / `filter_modules_by_changed`. All errors return safe defaults.
- [x] **W2-4 — Pipeline early-exit + filter + --full (Wave 2 Chain A):** `pipeline._index_repo` checks current HEAD vs stored head_sha. Equal → zero-cost skip. Force-push (not is_ancestor) → log warning + full reindex. Otherwise → filter scan results to changed modules only. head_sha advanced ONLY after full success (partial-failure preserves last successful sha). `--full` CLI flag bypasses incremental skip + diff filter (recommend periodic for stale Module node cleanup from rename/move). See ADR-0007.
- [x] **W2-6 — Auto-reseed sentinel hash gating (Wave 2 Chain B):** `seed_patterns.main()` adds sha256 hash gating via `_SeedMeta {key:'patterns'}` Neo4j sentinel. Skip when current_sha == stored_sha (avoid re-embedding 54 patterns). Sentinel only updated AFTER successful seed. `--force` CLI flag bypasses gating.
- [x] **W2-7 — Pipeline wire seed_patterns (Wave 2 Chain B):** `pipeline.index_profile()` end auto-calls seed_patterns logic. Per `--no-embed`: seed embedding skipped when embedder=None. Auto-reseed failure logged but does NOT fail the whole indexer run. Closes M4.6 §Defer M6.
- [x] **W2-8 — index_all --profile-workers parallel (Wave 2 Chain C):** `pipeline.index_all()` adds `profile_workers: int = 1` keyword-only. When > 1: `ThreadPoolExecutor(max_workers=profile_workers)` wraps profile loop. Each worker calls `open_production_pg()` for own pg_conn (psycopg2 thread-safety). Forces `progress=False` per-profile (avoid tqdm collision). First exception re-raised after all futures complete. Per-profile advisory lock (Wave 1 P1) ensures safety. CLI: `index-repo --all --profile-workers N`. README snippet added.

**Section G — Pre-launch audit deferred (audited 2026-05-09 → shipped 2026-05-10):**
- [x] **M3 — Feedback API trên MCP server (P2):** `feedback.router` được mount vào ASGI app của MCP server (port 8002) qua FastAPI sub-app + `app.mount("")`. Remote end-user nay submit được feedback. Auth X-API-Key middleware bao trùm — không cần loopback guard riêng.
- [x] **M4 — Qwen3Embedder default model name (P3):** Default `model="qwen3-embedding-q5km"` đã align với `odoo-semantic.conf.example` + README (đã đúng từ trước; xác nhận trong M5.5 G).
- [x] **M5 — Password lộ trong process list khi pg_dump (P2):** `src/cli.py` parse DSN, set `PGPASSWORD` env var, truyền `--host/--port/--username/--dbname` riêng. Password không còn trong `/proc/<pid>/cmdline`.
- [x] **L1 — health endpoint dùng private FastMCP attr (P3):** `src/mcp/health.py` chuyển sang public `mcp.get_tools()` (FastMCP 2.3+) với fallback try/except → `-1` + log warning.
- [x] **L3 — `maxlength="200"` trên Web UI form inputs (P3):** 8 text-input/textarea trong `api_keys.html` + `repos.html` + `ssh_keys.html`.
- [x] **L4 — Thread-safe key cache (P3):** `src/mcp/middleware.py` thêm module-level `threading.Lock()` bao quanh tất cả `_KEY_CACHE`/`_CACHE_TS` access (4 hàm `_cache_*`).
- [x] **L6 — `embeddings: 0` không giải thích lý do (P3):** `src/indexer/__main__.py` in dòng "Embeddings skipped — EMBEDDER_URL not configured. Use --no-embed to suppress this notice." khi embedder=None.

## Milestone 7 — "Lifecycle Wow"

**Intent:** Track ecosystem evolution — multi-repo dependency change ripples, auto-curation of Viindoo↔EE mapping, observability of embedding costs, hygiene cleanup beyond M6 incremental thesis.
**Outcome:** AI client trả lời được "đổi file Y trong repo A làm vỡ những gì trong repo B", "module EE Z có Viindoo equivalent nào auto-detected"; admin có metrics về embedding cost + auto-cleanup tools.

**D1 — Go-live docs overhaul (shipped 2026-05-11):**
- [x] `docs/deploy.md`: §2.4 Neo4j backup/restore commands (working docker cp pattern) + §4.1 port 443 variant + HSTS + security headers + §3.5 service file path unified (canonical `docs/deploy/`) + §7 security checklist expanded (HSTS, port isolation, rate_limit_rpm, webui.env, FERNET secrets, Docker TCP, session auth) + §13 FERNET rotation fixed (path + `systemctl restart`) + §14 Log Rotation section new
- [x] `docs/deploy/nginx.conf.example`: remove stale "M5 chưa implement" comment; Option C (X-API-Key) promoted primary; port 443 block + HSTS + security headers added
- [x] `odoo-semantic.conf.example`: `[auth]` section với `rate_limit_rpm = 120` + comment
- [x] `docs/deploy/logrotate.d/odoo-semantic`: new logrotate config (weekly, rotate 4, compress)
- [x] `docs/deploy/pre-launch-checklist.md`: new — bilingual, 10 verification sections, 14 MCP tool sign-off table
- [x] `docs/deploy/disaster-recovery.md`: new — bilingual, backup frequency, restore order, step-by-step commands, validation queries, RTO estimate
- [x] `TASKS.md`: M7 D1 closed + Pre-launch signoff row added
- [x] `README.md`: link 2 new docs in Tài Liệu table

**Carry-over từ M6 (defer M7 confirmed):**
- [ ] **`viindoo_equivalent_qname` auto-populate (M4.6 → M6 → M7 → indefinite defer):** Investigation 2026-05-10 (Wave 2 planning) AND M7 final-closeout 2026-05-11 review both confirmed: hardcoded `EE_CONFUSION` dict in `src/data/ee_modules.py` (16 curated 1-to-1 entries) is the correct approach. Graph-traversal substitute is infeasible until two preconditions met: (a) Viindoo addons indexed in shared profile alongside Odoo CE/EE, and (b) manifest feature-tag heuristic available (e.g. `category` + `summary` keyword overlap). NOT scheduled for any near-term milestone — keep curated dict, add new entries manually.

**Review-deferred items (LOW findings from M6 Opus review — fix in M7):**
- [x] **Neo4j `setup_indexes()` race under `profile_workers > 1`:** fresh Neo4j + parallel workers hit `EquivalentSchemaRuleAlreadyExists`. Fix: catch + ignore in writer OR pre-call once in `index_all` entry point. Workaround documented in `tests/test_indexer_profile_workers.py` (test pre-calls `Neo4jWriter().setup_indexes()` before `index_all(profile_workers=2)`). Affects production `--profile-workers >1` first-run. (M7 C1, commit 60ab2a3)
- [x] **Rerank coefficients tuning (`src/mcp/server.py:489`):** needs Vietnamese + English eval dataset to calibrate `dependents_map` weight vs `in_chain_set` boost. V0 heuristic is conservative placeholder — M7 measure recall/precision on held-out queries. (M7 final-AB)
- [x] **`_compute_risk` thresholds recalibration (`src/mcp/server.py:683`):** needs held-out incident dataset to validate `total >= 10` HIGH / `4-9` MEDIUM / `< 4` LOW buckets. Current thresholds are qualitative against Odoo 17 + Viindoo; M7 quantitative validation. (M7 final-AB)
- [x] **USES_CORE_SYMBOL V0→V1 expansion (`src/indexer/parser_python.py:36`):** V0 scope = deprecated/removed only (5 symbols). Expand to cover "signature changed" + "moved module" APIs per ADR-0002 §3. Current false-positive rate acceptable for MVP. (M7 final-D)
- [x] **Qualified-name symbol resolution (`src/indexer/parser_python.py:67-68`):** full import-chain tracking to eliminate short-name collisions. Today qualified_name heuristic (ENDS WITH) catches most cases; M7 implement proper scope resolver. (M7 W13)
- [x] **Clone-status poll cap (`src/web_ui/templates/repos.html` `pollCloneCells`):** stuck-pending repos poll forever (5s tick). Add max-tick stop + "Polling timed out, check server logs" message. UX improvement. (M7 C2)
- [x] **`_NULL_HINT` repr format cleanup (`src/mcp/server.py` `_diff_method_across_versions` output):** internal sentinel bleeding into API output. Format as actual string value or comment. (M7 C3, commit 5e05410)
- [x] **`default_clone_dir` URL query-string handling (`src/git_utils.py`):** strip query/fragment via `urlparse` to avoid invalid SSH URL. Edge case when user manually adds SSH URL with query params. (M7 final-G)
- [x] **W3-2 EE-reference test list expansion (`tests/test_patterns_schema.py`):** current EE_CONFUSION needle list has ~5 entries; expand to all 16 dict keys + `viin_*` prefix patterns. Better coverage. (M7 T4, commit f533c71)
- [x] **Migration tool adoption:** yoyo-migrations adopted — `src/db/migrate.py` now uses yoyo with baseline detection for legacy deploys + advisory lock for concurrent safety. (M7 W15)

**Spawned từ ADR-0007 §"Out of scope" (M6 Wave 2):**
- [x] **Module rename garbage collection (ADR-0007 §D5):** `--gc` flag added — DETACH DELETE Module nodes whose path no longer exists in current scan. Risk-gated (only if scanner found ≥1 module). (M7 C4)
- [x] **Cross-repo dependency change tracking (ADR-0007 §Out of scope):** incremental run on repo A propagates head_sha reset to repos whose modules DEPENDS_ON A's changed modules. Implementation in `cross_repo.py` + `repo_registry.py::reset_head_sha`. (M7 W14)
- [x] **Embedding cost observability (ADR-0007 §Out of scope):** `FakeEmbedder.call_count` + `Qwen3Embedder.call_count` (thread-safe via Lock), surfaced in `/health` embedding_calls field + dashboard. (M7 C5)

> **Lý do định danh "Lifecycle Wow":** items đa dạng nhưng chung chủ đề "track sự thay đổi theo thời gian" — repo rename hygiene (GC), inter-repo dependency drift, ecosystem correlation (Viindoo↔EE auto-curation), production cost observability.
>
> **Khi nào start M7:** sau khi M6 Wave 3 + Wave 4 đóng. Trước khi start, re-evaluate priority ranking — Viindoo addon indexing maturity + embedding cost pain points + cross-repo dependency surface area.

## Milestone 7.5 — "Persona Wow"

**Status:** `[x]` Complete 2026-05-12 — Track 1: 14 TRIGGER/PREFER/SKIP docstrings + 2 test files. Track 2: Claude Code plugin package (11 SKILL.md + 2 agents + setup command + marketplace.json). Track 3+T4: Gemini/OpenAI/Cursor adapters + 5 persona EN guides + ADR-0012 + pre-launch checklist extension.

**Verification close-out:** `[x]` 2026-05-14 — 4-batch parallel verification on production `odoo-semantic.viindoo.com`:
- Batch 1: 9/14 MCP tools PASS (5 blocked by P1 infra: Ollama SSL × 2, CoreSymbol gap × 2, CLI index gap × 1).
- Batch 2: Auto-route pilot 96% hit-rate (120/125), all 5 personas ≥92% — exceed ≥80% target.
- Batch 3: 8/10 observable infra items PASS (2 P1: HSTS missing, /admin 404).
- Batch 4: M3 recall smoke blocked by P1 Ollama SSL (deferred to local replica).

5 P1 issues queued for M8 production fix-ups; 7 P2 issues queued for polish. Reports:
- `docs/m7.5-verification-issues.md` — consolidated P0/P1/P2 log
- `docs/m7.5-batch1-mcp-signoff.md` — 14 tool per-call results
- `docs/m7.5-pilot-results.md` — auto-route hit-rate per persona + failing queries
- `docs/m7.5-batch3-infra.md` — curl verification §1/§2/§7/§10
- `tests/eval/auto_route_125.yaml` — 125-query golden set (regression baseline)

**Intent:** Make AI clients (Claude Code, Claude.ai, Gemini, ChatGPT) **proactively auto-pick** `odoo-semantic` tools across five personas (CEO, developer, consultant, marketer, sales). Currently descriptions only say WHAT tools do — non-technical users phrasing questions in business language never reach the right tool. Two-track fix: rewrite 14 tool docstrings with `TRIGGER / PREFER / SKIP` clauses (Track 1), and ship a Claude Code plugin bundling MCP config + 11 persona skills + 2 router sub-agents (Track 2). Cross-vendor adapters for Gemini Gems / OpenAI Custom GPT / Cursor sit alongside the plugin.

**Outcome:** Hit-rate ≥ 80% on auto-route across 5 personas × 25 sample queries, measured on Claude Code + Gemini + ChatGPT with variance ≤ 15%. Distributed via Viindoo self-host marketplace; `/odoo-semantic:connect` slash command handles API-key prompt + `~/.claude.json` write + validation.

**Plans liên quan:**
- [`docs/superpowers/plans/2026-05-11-milestone-7.5-persona-proactive.md`](docs/superpowers/plans/2026-05-11-milestone-7.5-persona-proactive.md) — Master plan (4 tracks, 40+ WIs, worktree topology, model assignment per WI).

**Track 1 — Tool docstring TRIGGER blocks:**
- [x] T1.1–T1.14: 14 MCP tool docstrings rewritten with TRIGGER/PREFER/SKIP in `src/mcp/server.py`
- [x] T1.15: `tests/test_mcp_tool_descriptions.py` — 28 parametrized assertions pass (14 TRIGGER/PREFER/SKIP + 14 ≤1500 chars)
- [x] T1.16: `tests/test_smoke_e2e_mcp_http.py` extended — 11 stub classes for uncovered tools

**Track 2 — Claude Code plugin package:**
- [x] T2.1: `dist/odoo-semantic-plugin/` scaffold + `.claude-plugin/plugin.json` + `.mcp.json` + marketplace.json
- [x] T2.2–T2.12: 11 persona SKILL.md files (CEO ×2, Dev ×3, Consultant ×2, Marketer ×2, Sales ×2)
- [x] T2.13: `agents/odoo-router.md` — Haiku model, classify-only
- [x] T2.14: `agents/odoo-upgrade-planner.md` — Sonnet model, multi-step orchestration
- [x] T2.15: `commands/connect.md` — `/odoo-semantic:connect` interactive install
- [x] T2.16: `tests/test_skill_disambiguation.py` — 31/31 pass, 100% routing accuracy

**Track 3 — Cross-vendor adapters + persona docs:**
- [x] T3.1: `dist/gemini-gem-instructions.md`
- [x] T3.2: `dist/openai-gpt-instructions.md`
- [x] T3.3: `dist/cursor-rules.md`
- [x] T3.4: `docs/personas/{ceo,dev,consultant,marketer,sales}.md`
- [ ] T3.4b: VN translation via `/translator` — deferred to M8.x post-launch (not M7.5 blocker; EN canonical per M7.5 design decision 2026-05-11)
- [x] T3.5: `README.md` — Persona Guides section added

**Track 4 — Release & verification:**
- [x] T4.1: `docs/adr/0012-persona-skill-architecture.md`
- [x] T4.2: `docs/deploy/pre-launch-checklist.md` — 11 skill sign-off rows added
- [x] T4.3: Internal pilot — measure auto-route hit-rate ≥80% (post-deploy) *(2026-05-14 — Claude Code static-dispatch proxy: overall 96% (120/125), CEO 100% · Dev 100% · Consultant 92% · Marketer 92% · Sales 96%; tất cả 5 personas ≥80%. Method: 125-query golden set tại `tests/eval/auto_route_125.yaml`; static prediction từ SKILL.md TRIGGER phrases. Full live LLM measurement defer M8. Report: `docs/m7.5-pilot-results.md`.)*
- [x] T4.4: v0.2.0 release tag + changelog (post-merge) *(2026-05-14 — tag `v0.2.0` đã tồn tại tại commit `bb8f1ab` (M7.5 close-out, 2026-05-12); CHANGELOG.md có entry v0.2.0 với 4 tracks documented; README.md "Latest release" sync.)*

**Resolved decisions (2026-05-11):**
1. **Marketplace:** Viindoo self-host (`claude plugin marketplace add viindoo/claude-plugins`).
2. **Auth model:** Setup command prompts user (plugin ships `.mcp.json` template WITHOUT key).
3. **Persona docs locale:** EN canonical first, VN via translator skill in follow-up.

**Stop-points / decision gates:**
- After Track 1: measure Claude Code hit-rate with docstrings only. If ≥60% → Track 2 only needs non-tech personas. If <60% → review TRIGGER quality first.
- After T2.16: if disambiguation <80% → redesign overlap; do NOT ship Track 3 until gate passes.

> **Why M7.5 (not M8 sub-stream):** M8 is about opening production to anonymous traffic + landing page + admin from Internet (a deploy/marketing milestone). M7.5 is about client-side adoption mechanics (auto-pick + persona skills + plugin distribution). The two are independent — M8 can ship without M7.5 and vice versa. Interleaved chronologically (both planned 2026-05-11) but not coupled.

## Milestone 8 — "Public Wow"

**Status:** Planning — revised 2026-05-12 (Astro unified decision). Không có code change.

**P1 production fix-ups (từ M7.5 verification 2026-05-14 — bắt buộc trước public launch):**

> **2026-05-14 hotfix executed** (worktree `worktree-m7.5-hotfix`): 4/5 P1 RESOLVED, 1/5 DEFERRED → M8. Real prod root causes khác runbook ban đầu — runbook đã được sửa. Chi tiết: [`docs/m7.5-verification-issues.md`](docs/m7.5-verification-issues.md) Resolution Stamps.

- [x] **M7.5-P1-A:** Fix embedder URL — actual root cause: wrong port `:9999` (closed) trên remote `embed.viindoo.com`, không phải Ollama localhost TLS. Drop port → use 443. Conf line 19 edited + MCP restart. Verified: `curl https://embed.viindoo.com/api/embed` → 401 (auth required = OK). **Runbook §4.2.**
- [x] **M7.5-P1-B:** Run `index-core --source ~/git/odoo_17.0 --version 17.0` — 501 CoreSymbol + 12 CLICommand + 80 CLIFlag + 17 LintRule populated. `name_get` indexed (status=stable per P2 quirk). **Runbook §5 Tier 1.**
- [x] **M7.5-P1-C:** Bundled with P1-B. `--gevent-port` flag indexed for v17. **Runbook §5 Tier 1.**
- [x] **M7.5-P1-D:** `add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;` added to `/etc/nginx/sites-available/odoo-semantic-mcp` server block (corrected filename — runbook had `odoo-semantic`). Nginx reloaded; verified. **Runbook §3.**
- [~] **M7.5-P1-E:** DEFERRED to M8 (Branch B chosen per M8/M9 Astro unified decision — Jinja2 webui replaced by Astro this week). Absorbed into M8 plan §9 acceptance criteria + pre-launch §10 M8 dependency annotation with explicit exit criteria (W3+W4 merged + `odoo-semantic-astro.service` active). **Runbook §6 Branch B.**

**M8/M9 backlog from hotfix discoveries (2026-05-14):**

- [ ] **Profile + core index gap v9-v19:** Only v8 + v17 profiles exist in Postgres. Register profile + Add Repo (SSH URL `git@github.com:Viindoo/odoo.git` branch `<V>.0`) via admin webui (leverages M6 Wave 4 SSH auto-clone, [ADR-0008](docs/adr/0008-ssh-auto-clone.md)) cho v9, v10, v11, v12, v13, v14, v15, v16, v18, v19. Then `index-core` per version. Unblocks full `api_version_diff` coverage.
- [ ] **v18 source repo missing:** Register via admin webui SSH (as above) — clones automatically via auto-clone flow. Don't direct `git clone` (avoid hand-managed filesystem layout).
- [ ] **v8 parser limitation:** `index-core --version 8.0` writes 167 CoreSymbol but 0 CLIFlag/LintRule — era1 (openerp-server) CLI structure not handled. Extend `parser_cli.py` for era1.
- [ ] **Admin UI core-index status column [P3 UX]:** Admin `/repos` page only shows MODULE index status (`indexed/error/pending` from Postgres `repos.status`). Add column or badge for CORE index status per version (CoreSymbol count > 0). Prevents user confusion that "v17 indexed" implies core index complete.
- [ ] **Cleanup test artifact:** `MATCH (m:Module {odoo_version: '96.0', name: 'snap_mod', module_name: NULL}) DETACH DELETE m` — one anomalous node from test run leaking into production Neo4j.
- [ ] **Re-register local v9-v16/v19 via webui (decide):** Currently local `~/git/odoo_<V>.0` directories exist but no Postgres profile/repo records. Either retire local clones in favor of webui-registered + auto-cloned repos (consistent topology), or keep local as mirror. **Recommendation:** re-register via webui to retire ad-hoc local layout.

**P2 polish queue (đã ship code-side; production hoặc downstream pending):**
- [x] **M7.5-P2-AR:** 5 persona TRIGGER tuning fixes shipped 2026-05-14 — `dist/odoo-semantic-plugin/skills/{odoo-feature-check,odoo-gap-analysis,odoo-feature-highlights,odoo-addon-diff,odoo-capability-proof}/SKILL.md` mở rộng description với failing-query phrases. Disambiguation regression 31/31 PASS. *Note: full live LLM re-measurement defer M8.*
- [x] **M7.5-P2-LINT:** Added pylint-odoo rule **W8201** (translation-format-interpolation, "String formatting used in UserError/ValidationError — use lazy %s args or named placeholders") to `src/indexer/spec_data/lint_rules_{16.0,17.0,18.0}.json`. 11 new tests in `tests/test_parser_lint_rules.py`. Admin cần re-run `index-core` để load vào production catalogue.
- [x] **M7.5-P2-DOCS:** Added "Ollama Setup (cho recall benchmark)" section in `CONTRIBUTING.md` (line 221) — qwen3-embedding-q5km pull + verify steps + cross-link to `docs/deploy/embedder-setup.md`.
- [ ] **M7.5-P2-NAMEGET:** Parser limitation — Odoo 17 dùng runtime `DeprecationWarning` cho `name_get` thay vì `@api.deprecated` decorator. Sau re-index, `lookup_core_api("name_get", "17.0")` show `status='stable'` thay vì 'deprecated'. Cần extend `parser_odoo_core.py` để detect runtime warnings trong body. Track for M8 polish.
- [ ] **M7.5-P2-SEED:** Operational gap discovered 2026-05-14 post-PR #84 cross-check — `suggest_pattern` returns `no patterns indexed. Run: python -m src.indexer.seed_patterns` on production. PatternExample nodes + `_SeedMeta` sentinel absent. Admin task: SSH to prod, run `python -m src.indexer.seed_patterns`. After this, ADR-0007 auto-reseed sentinel skips re-embed on subsequent runs. Tracked in `docs/m7.5-mcp-verification.md` and `docs/m7.5-verification-issues.md`.



**Intent:** Mở production host `odoo-semantic.viindoo.com` cho anonymous public traffic với landing site + admin UI đầy đủ trên Astro unified. Jinja2 xóa hoàn toàn. Đặt nền cho M9 OAuth/signup.

**Outcome:**
- `GET /` → Astro static landing + React Flow hero animation (5s auto-reveal).
- `GET /admin/*` → Astro SSR admin UI (thay thế Jinja2 hoàn toàn; session-auth required).
- `GET /api/*` → FastAPI pure JSON API (no templates).
- `/mcp`, `/install`, `/health` không đổi.

**Architecture target:**
```
nginx (port 443, prod)
├── /          → Astro server (port 4321): static landing
├── /admin/*   → Astro server (port 4321): SSR admin UI (auth-gated)
├── /api/*     → FastAPI (port 8003): JSON API only (no Jinja2)
├── /mcp       → FastAPI (port 8002): MCP server (unchanged)
└── /install/, /health  → FastAPI (port 8002): unchanged
```

Session flow: `/admin/*` → Astro middleware → `GET /api/auth/verify` (FastAPI) → 401 → redirect `/admin/login`.

**Plans liên quan:**
- [`docs/superpowers/plans/2026-05-12-milestone-8-astro-unified.md`](docs/superpowers/plans/2026-05-12-milestone-8-astro-unified.md) — Master plan revised (4 streams, ~7-10 working days).
- ~~[`docs/superpowers/plans/2026-05-11-milestone-8-public-wow.md`](docs/superpowers/plans/2026-05-11-milestone-8-public-wow.md)~~ — Superseded (kiến trúc cũ: landing-only Astro, Jinja2 admin còn lại).
- ~~[`docs/superpowers/plans/2026-05-11-webui-admin-prefix.md`](docs/superpowers/plans/2026-05-11-webui-admin-prefix.md)~~ — Superseded (FastAPI root_path refactor không cần nữa; admin prefix thuộc Astro routing).

**Decisions locked:**
- 2026-05-11: Astro + React Flow + baked JSON snapshot (`scripts/dump_graph_snippet.py`).
- 2026-05-12: Astro `output: 'hybrid'` — unified cho cả landing (static) VÀ admin (SSR). FastAPI → pure JSON API (Jinja2 xóa). Tailwind CSS. `site/` dir (thay `landing/`).

**Streams + sub-PRs (planned, not yet opened):**
- [ ] **Stream A — FastAPI pure JSON API** (1 PR, ~3-4h): `feat/m8-fastapi-json-api` — xóa `src/web_ui/templates/` + Jinja2 dep, convert 26 endpoints → `/api/*` JSON, thêm `/api/auth/{login,logout,verify}`. ADR-0013.
- [ ] **Stream B — Astro hybrid full** (4-5 PRs, ~4-5 days):
  - [ ] `feat/m8-astro-scaffold` — `site/` dir, `output: 'hybrid'`, Tailwind, pnpm, tsconfig (3-4h).
  - [ ] `feat/m8-graph-snapshot` — `scripts/dump_graph_snippet.py` + baked JSON (2-3h).
  - [ ] `feat/m8-admin-pages` — 7 admin pages Astro SSR + AdminLayout + Astro middleware auth (2-3 days).
  - [ ] `feat/m8-hero-animation` — landing + React Flow GraphAnimation island + cinematic frames (1-2 days).
  - [ ] `feat/m8-landing-content` — pricing placeholder + docs pages (1 day). ADR-0012.
- [ ] **Stream C — nginx integration** (1 PR, ~1-2h): `feat/m8-nginx-integration` — `/` + `/admin/*` → Astro :4321; `/api/` → FastAPI :8003; mcp/install/health unchanged. Gate: A + B merged.
- [ ] **Stream D — systemd + CI** (1 PR, ~4-6h): `feat/m8-astro-service` — `odoo-semantic-astro.service`, CI `setup-node` + `pnpm build` + `pnpm run check`, browser test URL update.
- [x] **Stream X — Web UI ↔ CLI parity** (done): 9 WIs đã merge — delete profile/repo, index options, reset-embed, index-all, index-core, seed-patterns, apply-preset.

**Acceptance criteria:** `GET /` 200 Lighthouse ≥80/95/95; `GET /admin/login` 200 (Astro SSR); `POST /api/auth/login` 200 JSON + set-cookie; unauthenticated `GET /admin/` → redirect `/admin/login`; Jinja2 không còn trong `pyproject.toml`; nginx -t pass; `make lint + test` + `pnpm run check` green; ADR-0012 + ADR-0013 committed.

**SaaS roadmap:**
- M9 "Auth Wow" — OAuth Google/GitHub, public signup, tenant API keys (zero migration debt).
- M10 "Billing Wow" — Stripe, plan tiers.
- M11 "Dashboard Wow" — `/dashboard` reuse React Flow từ M8 hero.
- M12 "Multi-tenant Wow" — Neo4j namespacing.

**Khi nào start:** M7 đã shipped (PR #46, 2026-05-11). Operator fix-ups done (PR #45, #48). Có thể start ngay. Stream A + Stream B scaffold có thể parallel (độc lập).

---

## Milestone 9 — "Auth Wow"

**Status:** Planning (chưa start). M8 phải merge trước.

**Intent:** Public signup, OAuth, multi-user admin, self-serve account operations. Zero migration debt — Jinja2 đã xóa hết trong M8, M9 chỉ làm feature mới thuần túy trên Astro SSR + FastAPI JSON API.

**OAuth libraries:** `arctic` + `oslo` trong Astro SSR middleware (compatible natively với Astro output: hybrid). FastAPI `/api/auth/oauth-token` cho token exchange.

**Items planned:**
- [ ] OAuth integration (Google/GitHub) — `arctic` + `oslo` trong Astro SSR; callback URL `/admin/auth/callback`
- [ ] Public signup flow + email verification — `/signup` Astro page + FastAPI `/api/auth/register` + SMTP config
- [ ] Tenant API key issuance per user (sau signup, user tự tạo từ dashboard)
- [ ] Self-serve webui user management (list/deactivate/reset users qua Web UI — hiện CLI-only `manager create-webui-user`)
- [ ] Backup/Restore Web UI (React component trong `operations.astro`, gọi `/api/operations/backup|restore` — security review trước khi expose)
- [ ] FERNET key rotation Web UI (chỉ expose sau khi có 2FA + audit log; tránh leak qua HTTP)
- [ ] CLI delete-profile / delete-repo subcommands trong `src/manager/__main__.py` (parity CLI cho automation)
- [ ] DB migrate trigger UI (read-only display current migration version; trigger giữ ở deploy script)

**Plan file:** [`docs/superpowers/plans/2026-05-12-milestone-9-auth-wow.md`](docs/superpowers/plans/2026-05-12-milestone-9-auth-wow.md)

---

## Pre-launch Signoff

Admin ký tên trước khi mở public / phân phát API key. Xem [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) để biết 10 mục + 14 MCP tool sign-off table.

| Mục | Admin | Ngày | Ghi chú |
|-----|-------|------|---------|
| Infrastructure & TLS | | | |
| Auth & Rate Limiting | | | |
| Port Isolation | | | |
| Logrotate | | | |
| Backup & Recovery | | | |
| MCP Tool Sign-Off (14 tools) | | | |
| Install Page | | | |
| Systemd Services | | | |
| Indexer Cron | | | |
| Full sign-off | | | Phân phát key sau khi ký |

---

## Điều Hướng Tài Liệu

| | File | Nội dung |
|---|------|----------|
| ← | [`README.md`](README.md) | Điểm bắt đầu: tổng quan, onboard, hướng dẫn deploy |
| ↓ | [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Thiết kế kiến trúc đầy đủ: schema, pipeline, MCP tools |
| ↓ | [`docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md`](docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md) | Implementation plan chi tiết Milestone 1 — bắt đầu ở đây |
| → | [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) | Pre-launch signoff — 10 mục + 14 MCP tool verify |
| → | [`docs/deploy/disaster-recovery.md`](docs/deploy/disaster-recovery.md) | DR runbook — backup frequency, restore order, RTO |
