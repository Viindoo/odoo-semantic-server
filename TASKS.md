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

> ADR: [`docs/adr/0003-pattern-example-storage.md`](docs/adr/0003-pattern-example-storage.md)  
> Depends on: M4.5 (CoreSymbol node cho USES_CORE_SYMBOL edge — graceful skip nếu chưa ship)

## Milestone 5 — "Product Wow"
**Intent:** Đóng gói thành sản phẩm bất kỳ ai deploy được trong dưới 10 phút.
**Outcome:** `docker compose up -d` + Web UI add repos + index. Admin tạo API key → user add vào Claude Code config → MCP tools respond. Production-ready: `GET /health` + Postgres advisory lock ngăn indexer chạy chồng.

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
- [x] flash warning banner (amber style) — was src/web_ui/templates/repos.html, file removed in M8 W1 (logic moves to Astro in W3)
- [x] `tests/test_web_ui_repos.py`: 2 unit tests dedup (blocked + ok path)

**Section F — Job Tracking (P2 — Complete):**
- [x] `src/db/migrate.py`: table `indexer_jobs` (id, profile_name, status, started_at, finished_at, error_msg, pid, created_at) + indexes
- [x] `src/db/job_registry.py`: CRUD — `create_job()`, `update_job()`, `get_last_job()`, `list_running_jobs()`, `get_job()`
- [x] `src/indexer/__main__.py`: thêm `--job-id INT` arg → update job status start/success/error
- [x] `src/web_ui/routes/repos.py`: `index_repo()` tạo job record + truyền `--job-id` vào subprocess
- [x] route GET /repos/jobs/{job_id}/status: JSON `{id, profile_name, status, pid, started_at, finished_at, error_msg, created_at}` — landed cùng `src/web_ui/routes/repos.py`
- [x] status badge + JS polling 5s nếu running/queued — was src/web_ui/templates/repos.html, file removed in M8 W1 (logic moves to Astro W3, tested by W7 browser)
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

5 P1 issues queued for M8 production fix-ups; 7 P2 issues queued for polish. Verification reports archived internally.
- `tests/eval/auto_route_125.yaml` — 125-query golden set (regression baseline)

**Intent:** Make AI clients (Claude Code, Claude.ai, Gemini, ChatGPT) **proactively auto-pick** `odoo-semantic` tools across five personas (CEO, developer, consultant, marketer, sales). Currently descriptions only say WHAT tools do — non-technical users phrasing questions in business language never reach the right tool. Two-track fix: rewrite 14 tool docstrings with `TRIGGER / PREFER / SKIP` clauses (Track 1), and ship a Claude Code plugin bundling MCP config + 11 persona skills + 2 router sub-agents (Track 2). Cross-vendor adapters for Gemini Gems / OpenAI Custom GPT / Cursor sit alongside the plugin.

**Outcome:** Hit-rate ≥ 80% on auto-route across 5 personas × 25 sample queries, measured on Claude Code + Gemini + ChatGPT with variance ≤ 15%. Distributed via Viindoo self-host marketplace; `/odoo-semantic:connect` slash command handles API-key prompt + `~/.claude.json` write + validation.

**Track 1 — Tool docstring TRIGGER blocks:**
- [x] T1.1–T1.14: 14 MCP tool docstrings rewritten with TRIGGER/PREFER/SKIP in `src/mcp/server.py`
- [x] T1.15: `tests/test_mcp_tool_descriptions.py` — 28 parametrized assertions pass (14 TRIGGER/PREFER/SKIP + 14 ≤1500 chars)
- [x] T1.16: `tests/test_smoke_e2e_mcp_http.py` extended — 11 stub classes for uncovered tools

**Track 2 — Claude Code plugin package:**
- [x] T2.1: plugin scaffold (moved to [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client))
- [x] T2.2–T2.12: 11 persona SKILL.md files (CEO ×2, Dev ×3, Consultant ×2, Marketer ×2, Sales ×2)
- [x] T2.13: `agents/odoo-router.md` — Haiku model, classify-only
- [x] T2.14: `agents/odoo-upgrade-planner.md` — Sonnet model, multi-step orchestration
- [x] T2.15: `commands/connect.md` — `/odoo-semantic:connect` interactive install
- [x] T2.16: `tests/test_skill_disambiguation.py` — 31/31 pass, 100% routing accuracy

**Track 3 — Cross-vendor adapters + persona docs:**
- [x] T3.1–T3.4: Gemini, OpenAI, Cursor adapters + 5 persona guides (moved to [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client))
- [x] T3.5: `README.md` — Persona Guides section added

**Track 4 — Release & verification:**
- [x] T4.1: `docs/adr/0012-persona-skill-architecture.md`
- [x] T4.2: `docs/deploy/pre-launch-checklist.md` — 11 skill sign-off rows added
- [x] T4.3: Internal pilot — measure auto-route hit-rate ≥80% (post-deploy) *(2026-05-14 — Claude Code static-dispatch proxy: overall 96% (120/125), CEO 100% · Dev 100% · Consultant 92% · Marketer 92% · Sales 96%; tất cả 5 personas ≥80%. Method: 125-query golden set tại `tests/eval/auto_route_125.yaml`; static prediction từ SKILL.md TRIGGER phrases. Full live LLM measurement defer M8. Report archived internally.)*
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

**Status:** `[x]` DONE — 2026-05-14 (PR #86 merged, 1195 tests pass, 6/6 CI green, ADR-0014/0015/0016 committed).

**P1 production fix-ups (từ M7.5 verification 2026-05-14 — bắt buộc trước public launch):**

> **2026-05-14 hotfix executed** (worktree `worktree-m7.5-hotfix`): 4/5 P1 RESOLVED, 1/5 DEFERRED → M8. Real prod root causes khác runbook ban đầu — runbook đã được sửa. Chi tiết archived internally.

- [x] **M7.5-P1-A:** Fix embedder URL — actual root cause: wrong port `:9999` (closed) trên remote `embed.viindoo.com`, không phải Ollama localhost TLS. Drop port → use 443. Conf line 19 edited + MCP restart. Verified: `curl https://embed.viindoo.com/api/embed` → 401 (auth required = OK). **Runbook §4.2.**
- [x] **M7.5-P1-B:** Run `index-core --source ~/git/odoo_17.0 --version 17.0` — 501 CoreSymbol + 12 CLICommand + 80 CLIFlag + 17 LintRule populated. `name_get` indexed (status=stable per P2 quirk). **Runbook §5 Tier 1.**
- [x] **M7.5-P1-C:** Bundled with P1-B. `--gevent-port` flag indexed for v17. **Runbook §5 Tier 1.**
- [x] **M7.5-P1-D:** `add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;` added to `/etc/nginx/sites-available/odoo-semantic-mcp` server block (corrected filename — runbook had `odoo-semantic`). Nginx reloaded; verified. **Runbook §3.**
- [~] **M7.5-P1-E:** DEFERRED to M8 (Branch B chosen per M8/M9 Astro unified decision — Jinja2 webui replaced by Astro this week). Absorbed into M8 plan §9 acceptance criteria + pre-launch §10 M8 dependency annotation with explicit exit criteria (W3+W4 merged + `odoo-semantic-astro.service` active). **Runbook §6 Branch B.**

**M8/M9 backlog from hotfix discoveries (2026-05-14):**

- [~] **Profile + core index gap v9-v19 (OBS-1):** Profiles for v13/14/15/16/19 need to be created by the admin via the web UI or `python -m src.manager add-profile ...`; prod DBs missing them just need a re-run of `python -m src.db.migrate` for schema, then profile creation. **NOTE:** an earlier draft of this work introduced `migrations/0004_add_missing_version_profiles.sql` as belt-and-suspenders; it was removed because it violated the schema-only yoyo-migration contract (see `src/db/migrate.py` docstring and `src/db/seed_master_data.py` line 8-14) and broke 16 integration tests that assume `run_migrations()` leaves the profiles table empty. v10/11/12 profiles existed before OBS-1 (seeder was always complete). Remaining: run indexer per version + register local repo paths via webui if using `/home/user/git/odoo_<N>.0/` instead of auto-clone paths.
- [ ] **v18 source repo missing (OBS-1 deferred):** `odoo_18.0` not on disk as of 2026-05-15. Register via admin webui SSH auto-clone (ADR-0008) — clones automatically. Once cloned, run `index-repo --profile odoo_18`.
- [ ] **v8 parser limitation:** `index-core --version 8.0` writes 167 CoreSymbol but 0 CLIFlag/LintRule — era1 (openerp-server) CLI structure not handled. Extend `parser_cli.py` for era1.
- [ ] **Admin UI core-index status column [P3 UX]:** Admin `/repos` page only shows MODULE index status (`indexed/error/pending` from Postgres `repos.status`). Add column or badge for CORE index status per version (CoreSymbol count > 0). Prevents user confusion that "v17 indexed" implies core index complete.
- [ ] **Cleanup test artifact:** `MATCH (m:Module {odoo_version: '96.0', name: 'snap_mod', module_name: NULL}) DETACH DELETE m` — one anomalous node from test run leaking into production Neo4j.
- [ ] **Re-register local v9-v16/v19 via webui (decide):** Currently local `~/git/odoo_<V>.0` directories exist but no Postgres profile/repo records. Either retire local clones in favor of webui-registered + auto-cloned repos (consistent topology), or keep local as mirror. **Recommendation:** re-register via webui to retire ad-hoc local layout.

**P2 polish queue (đã ship code-side; production hoặc downstream pending):**
- [x] **M7.5-P2-AR:** 5 persona TRIGGER tuning fixes shipped 2026-05-14 — plugin skill TRIGGER descriptions expanded with failing-query phrases (plugin now at [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client)). Disambiguation regression 31/31 PASS. *Note: full live LLM re-measurement defer M8.*
- [x] **M7.5-P2-LINT:** Added pylint-odoo rule **W8201** (translation-format-interpolation, "String formatting used in UserError/ValidationError — use lazy %s args or named placeholders") to `src/indexer/spec_data/lint_rules_{16.0,17.0,18.0}.json`. 11 new tests in `tests/test_parser_lint_rules.py`. Admin cần re-run `index-core` để load vào production catalogue.
- [x] **M7.5-P2-DOCS:** Added "Ollama Setup (cho recall benchmark)" section in `CONTRIBUTING.md` (line 221) — qwen3-embedding-q5km pull + verify steps + cross-link to `docs/deploy/embedder-setup.md`.



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
Plans archived internally.

**Decisions locked:**
- 2026-05-11: Astro + React Flow + baked JSON snapshot (`scripts/dump_graph_snippet.py`).
- 2026-05-12: Astro `output: 'hybrid'` — unified cho cả landing (static) VÀ admin (SSR). FastAPI → pure JSON API (Jinja2 xóa). Tailwind CSS. `site/` dir (thay `landing/`).

**Streams + sub-PRs (all shipped via PR #86 — feat/m8-wave-integration):**
- [x] **Stream A — FastAPI pure JSON API**: `src/web_ui/` pure JSON routes, `/api/auth/{login,logout,verify}`. ADR-0015.
- [x] **Stream B — Astro hybrid full**:
  - [x] `site/` dir, `output: 'server'` (Astro 5.x), Tailwind, pnpm, tsconfig.
  - [x] `scripts/dump_graph_snippet.py` + baked JSON (`site/public/graph-snapshot.json`).
  - [x] 7 admin pages Astro SSR + AdminLayout + Astro middleware auth.
  - [x] Landing + React Flow GraphAnimation island + cinematic frames.
  - [x] Pricing placeholder + docs pages. ADR-0014.
- [x] **Stream C — nginx integration**: `/` + `/admin/*` → Astro :4321; `/api/` → FastAPI :8003.
- [x] **Stream D — systemd + CI**: `odoo-semantic-astro.service`, CI `setup-node` + `pnpm build` + `pnpm run check`.
- [x] **Stream X — Web UI ↔ CLI parity** (done): 9 WIs đã merge — delete profile/repo, index options, reset-embed, index-all, index-core, seed-patterns, apply-preset.

**Acceptance criteria:** `[x]` ALL MET — `GET /` 200 Lighthouse ≥80/95/95 ✓; `GET /admin/login` 200 (Astro SSR) ✓; `POST /api/auth/login` 200 JSON + set-cookie ✓; unauthenticated `GET /admin/` → redirect `/admin/login` ✓; Jinja2 không còn trong `pyproject.toml` ✓; nginx -t pass ✓; `make lint + test` + `pnpm run check` green ✓; ADR-0014 + ADR-0015 + ADR-0016 committed ✓; 1195 tests pass, 6/6 CI green ✓.

**SaaS roadmap:**
- M9 "Auth Wow" — OAuth Google/GitHub, public signup, tenant API keys (zero migration debt).
- M10 "Billing Wow" — Stripe, plan tiers.
- M11 "Dashboard Wow" — `/dashboard` reuse React Flow từ M8 hero.
- M12 "Multi-tenant Wow" — Neo4j namespacing.

**Khi nào start:** M7 đã shipped (PR #46, 2026-05-11). Operator fix-ups done (PR #45, #48). Có thể start ngay. Stream A + Stream B scaffold có thể parallel (độc lập).

---

## Milestone 9 — "Auth Wow" + M8 Cleanup

**Status:** `[x]` DONE — 2026-05-15 (PR #100 merged, 19 worktrees, v0.4.0). M8 cleanup streams + Auth Wow + 30+ security findings closed.

**Theme:** "Sweep the floors before opening the doors." — close M8 technical debt, harden CI infra, then add Auth features on clean foundation.

**Intent (Auth Wow):** Public signup, OAuth, multi-user admin, self-serve account operations. Zero migration debt — Jinja2 đã xóa hết trong M8, M9 chỉ làm feature mới thuần túy trên Astro SSR + FastAPI JSON API.

**OAuth libraries:** `arctic` + `oslo` trong Astro SSR middleware. FastAPI `/api/auth/oauth-token` cho token exchange.

### Stream A — CI Infra Hardening (deadline-driven)

- [x] Update `actions/setup-node@v4` → v5 + `pnpm/action-setup@v4` → v5 supporting Node.js 24. **Deadline 2026-06-02** (GitHub forced upgrade — browser CI jobs will break). P0. **Note:** PR #98 already bumped Node 20 → 22 and pnpm `version: 9 → 10` (Astro 6 / overrides requirement); this task only bumps the action major refs (`@v4 → @v5`).
- [x] Replace `python -m jsonschema` with `check-jsonschema` pip package (ci.yml:35). Eliminates DeprecationWarning + future-proofs against jsonschema CLI interface changes.
- [x] Add `actionlint` as a CI gate to catch workflow drift before it reaches runner.

### Stream B — Test Debt + Coverage Gaps from M8

- [x] Port `tests/test_clone_poll_timeout.py` to Astro: verify `MAX_TICKS=72` equivalent in `site/src/pages/admin/repos.astro` + add browser test. Currently 4 tests skipped.
- [x] Decide on 9 legacy stub files in `tests/test_web_ui_*_browser.py` (M8 W7 breadcrumbs): delete for clarity or keep as git-discoverable history. Document decision. *(Deleted 8 MIGRATED tombstone files in M9 T0.)*
- [x] Add `LoopbackOnlyMiddleware` explicit test: non-loopback client → 403 (`src/web_ui/app.py:42-49`). Coverage gap from Phase 8 review.
- [x] Add `_json_safe` regression test fixture pattern for new JSONResponse routes — prevent recurrence of datetime 500 bug.
- [x] Fix 2 known M9 warnings (CONTRIBUTING.md Known Upstream Warnings §4-5):
  - `neo4j._sync.driver:547 DeprecationWarning` — close session explicitly in `test_git_utils` + `test_indexer_main` fixture teardown.
  - `httpx._client per-request cookies` — refactor `test_web_ui_auth.py` helper to use `httpx.Client(cookies=...)`.
- [x] Update `_bypass_webui_auth_for_legacy_tests` autouse fixture comment in `conftest.py:63` — description "tests pre-date session auth" no longer accurate since M8 browser tests also use it.

### Stream C — REST Semantics + Error Handling Polish

- [x] `POST /api/repos/profiles/{id}/clone-all`: returns 200 "no pending repos" for nonexistent `profile_id` — distinguish missing-resource (404) from empty-collection (200).
- [x] `PATCH /api/repos/profiles/{id}/parent`: returns 400 for nonexistent `profile_id` — should be 404. Refactor `_validate_parent` to raise typed exception mapped to correct HTTP status in route.
- [x] Decide policy for `/docs` + `/redoc` middleware exempt entries when `docs_url=None, redoc_url=None` are intentional disables. Either re-enable for dev env or trim exempt set.

### Stream D — Astro UI Completeness (expose M8 backend features in UI)

PR #87 + #88 added backend features absorbed into M8 branch. The Astro pages don't yet expose UI for them:

- [x] Parent dropdown in `site/src/pages/admin/repos.astro` calling `PATCH /api/repos/profiles/{id}/parent`. Allow setting profile hierarchy from UI.
- [x] "Clone all pending" button per profile in `repos.astro` calling `POST /api/repos/profiles/{id}/clone-all`. Plus polling spinner for in-progress job count.
- [x] M8.1 deferred: JS toggle for URL pattern in `RepoTable.astro` SSH key dropdown — currently SSR-conditional only (`tests/browser/admin/test_repos.py:130`).
- [x] `JobStatus.astro` component is duplicated/dead — confirm and either wire into `operations.astro` or delete (Phase 8 R1#7 finding).

### Stream E — Production Operational (M7.5 + M8 carry-over)

- [x] Cleanup `99.0` test artifact nodes in Neo4j (operational, run on prod: `MATCH (m:Module {odoo_version: '99.0'}) DETACH DELETE m`).
- [x] Index core symbols for Odoo v9-v19 on production server (operational, re-run `index-core` per version after M8 deploy).
- [x] **M7.5-P2-SEED:** Seed production `suggest_pattern` catalogue (operational — run `python -m src.indexer.seed_patterns` on prod server). *(2026-05-18 — Ran on prod with PR #124 hotfix during B10 ops phase; pattern embeddings v9-v15 written.)*

### Stream F — Long-tail Features (defer or kill)

- [x] MFA TOTP for Web UI session auth (ADR-0011 extension — security hardening before public launch). ADR-0022.
- [x] **W-OSM Wave 1 — Tool output completeness (2026-05-16):** 14 → 21 MCP tools. Added 7 new tools (`describe_module`, `list_fields`, `list_methods`, `list_views`, `list_owl_components`, `list_qweb_templates`, `list_js_patches`) for module architecture overview + entity enumeration + UI-layer inventory. Retrofit grammar consistency across all 14 existing tools (tree connectors, sublist indent, truncation via `_render_capped`, `Next:` footer on 18 drill-down tools). ADR-0023 codifies tree grammar contract + English-only output policy + next-step hint mapping. Plan: internal plan (archived).

### Stream G — Process Discipline Learnings from M8

Two bug patterns surfaced twice during M8 — encode as automated lint to prevent recurrence:

- [x] Add ruff/custom lint rule: warn on `JSONResponse(<dict>)` without `_json_safe` wrap when dict may contain `datetime` objects. *(Advisory shell script `lint_json_response.sh` — 127 legacy violations tracked in backlog cleanup PR.)*
- [x] Add ruff/custom lint rule: warn on client-side `fetch()` with mutating method (`POST`/`PUT`/`DELETE`) without `Content-Type: application/json` header — prevents Astro 5.x `checkOrigin` rejection. *(Advisory shell script `lint_fetch_content_type.sh` — 0 violations.)*
- [x] Document both patterns in `CONTRIBUTING.md` Common Pitfalls section (after lint rules built).

### Auth Wow Items (original M9 scope)

- [x] OAuth integration (Google/GitHub) — `arctic` + `oslo` trong Astro SSR; callback URL `/admin/auth/callback`. ADR-0017.
- [x] Public signup flow + email verification — `/signup` Astro page + FastAPI `/api/auth/register` + SMTP config. (256-bit token, 24h TTL, hCaptcha, 3/hour resend rate-limit.)
- [x] Tenant API key issuance per user (sau signup, user tự tạo từ dashboard). `user_id` FK + `expires_at` filter.
- [x] Self-serve webui user management (list/deactivate/reset users qua Web UI — hiện CLI-only `manager create-webui-user`). `/admin/users` with `is_admin` gating.
- [x] Backup/Restore Web UI (React component trong `operations.astro`, gọi `/api/operations/backup|restore` — security review trước khi expose). ADR-0018, ADR-0019.
- [x] FERNET key rotation Web UI (chỉ expose sau khi có 2FA + audit log). CLI `--old-key-env/--new-key-env` atomic rotation. ADR-0020.
- [x] CLI delete-profile / delete-repo subcommands trong `src/manager/__main__.py` (automation parity). Also: `delete-webui-user`, `list-webui-users`, `create-webui-user --admin`.
- [x] DB migrate trigger UI (read-only display current migration version). `/admin/operations` migrations section (yoyo `_yoyo_migrations` table).

*(Plan file archived internally.)*

### Stream H — Web UI Parity for Repo & Profile Management (M9 follow-up, PR #116)

**Status:** `[x]` DONE — 2026-05-16 (PR #116, v0.4.1). 5 WIs + review fixes merged.

**ADR:** ADR-0024 — PATCH mutation policy (preserve `head_sha` + reject mutations on indexed profiles).

- [x] **WI-A — Surface clone/index errors + last_indexed_at:** RepoTable exposes `clone_error_msg`, `error_msg`, `last_indexed_at` columns.
- [x] **WI-B — --full checkbox on Index + Index-All buttons:** Expose ADR-0007 `--full` reindex flag in Web UI.
- [x] **WI-C — Edit Repo form:** `PATCH /api/repos/repos/{id}` endpoint + Web UI form (URL/branch/ssh_key_id/local_path). Preserves `head_sha`.
- [x] **WI-D — Edit Profile form:** `PATCH /api/repos/profiles/{id}` endpoint + Web UI form. Rejects `name`/`version` change on indexed profiles (409); enforces ancestor/descendant version-match (422).
- [x] **WI-E — Profile hierarchy tree view:** Toggle flat/tree in admin UI, localStorage persist. ProfileTree.astro SSR template (Astro convention parity). Namespaced `profile-tree-*` testids.

**Review fixes (post-PR-review):**

- [x] Ancestor version-match + indexed guard 409 (critical).
- [x] UniqueViolation catch → HTTP 409 (TOCTOU race safety).
- [x] Audit log before/after snapshots for PATCH mutations (ADR-0021 extension).
- [x] ProfileTree testid namespace fix.
- [x] +9 backend tests + +5 browser tests.

### Stream I — M9 Hardening / Go-Live (PRs #117, #118, #119, #121 — 2026-05-17)

**Status:** `[x]` DONE — 4 PRs merged 2026-05-17. PR #117/#118/#119 deployed to production same day; PR #121 docs-only.

**Plan:** internal plan (archived) (orchestrated multi-subagent, 4 WIs + 1 followup commit consolidating 3 HIGH Opus findings + 6 boil-the-lake fixes + sanitization).

- [x] **PR #117 — Migration 0004 self-contained:** SQL rescue path for 12 root CE profiles (`odoo_8`-`odoo_19`). Idempotent `ON CONFLICT DO NOTHING`. Python seeder remains source of truth for Viindoo addon profiles. Seed count test bumped 5 → 12.
- [x] **PR #118 — Security headers:** FastAPI `_SecurityHeadersMiddleware` (default-src 'none' for JSON API) + Astro SSR `_addSecurityHeaders()` (per-path CSP for `/admin/*`, `/signup`, `/verify-email`, `/reset-password`) + edge nginx permissive superset for prerendered pages. 8 regression tests. Closes M9 CSP gap (memory: `m9_csp_permissions_policy_gap.md`). Nonce-based CSP migration tracked as M10 followup.
- [x] **PR #119 — Go-live batch (4 WIs + boil-the-lake followup):**
  - WI-1 indexer writer + parser_js + ADR-0016 D7: 6 placeholder MERGE sites inherit referrer profile via ON CREATE / ON MATCH union semantics (mirrors real-node pattern from commit `4ff56a8`); 3 resolver MATCH sites exclude `__unresolved__` stubs via `WHERE NOT coalesce(unresolved, false)`; parser_js `_extract_era3_components` early-returns for Odoo < v14; ADR-0016 §D7 stub ownership policy added.
  - WI-2 webui auth MFA sync: `_enable_totp`/`_delete_totp` also `UPDATE webui_users.mfa_enabled` in same transaction; migration `m9_009_backfill_mfa_enabled.sql` symmetric reconciliation (TRUE + FALSE halves).
  - WI-3 backup CLI + systemd + runbook: `_resolve_postgres_tool` + `_resolve_neo4j_tool` docker-exec fallback helpers (`-e PGPASSWORD` forwarding for postgres); stdout redirect for pg_dump (host pipe instead of container `-f` write); systemd `odoo-semantic-backup.service` + `.timer` (`/bin/sh -c '$(date +%Y%m%d-%H%M%S)'` ExecStart for strftime expansion); extended logrotate stanza; bilingual `backup-runbook.md`.
  - WI-4 `/api/health` auth-exempt endpoint: route in `app.py`, `_EXEMPT_EXACT` set in `middleware.py`, new `src/_version.py` via `importlib.metadata.version("odoo-semantic-mcp")`.
  - +11 new tests across 4 new test files.
- [x] **PR #121 — Pre-launch checklist signoff (docs only):** Update `docs/deploy/pre-launch-checklist.md` flipping §4.1/§5.1/§8.6/§10.5 items to `[x]` post-deploy. Section 11 sign-off table filled (9 of 11 sections). Known followups #12-#15 appended (OWLComp v14 anachronism, Neo4j online backup, logrotate stanza perms, §6 tools 15-21 prod smoke).

**Production deploy ops phase (2026-05-17 — post-PR-#119 merge, before PR #120 deploy):**

- [x] `git pull origin master` + `pip install -e ".[all]"` + `pnpm install --frozen-lockfile && pnpm build` (Astro).
- [x] `python -m src.db.migrate` — applies `m9_009_backfill_mfa_enabled.sql`.
- [x] `sudo systemctl restart odoo-semantic-mcp odoo-semantic-webui odoo-semantic-astro` — 3 services healthy + stable PIDs.
- [x] Smoke verification: `curl -sI https://odoo-semantic.viindoo.com/` shows all 6 security headers (HSTS + CSP + Permissions-Policy + X-Frame DENY + nosniff + Referrer); `/api/health` returns 200 application/json.
- [x] Backup systemd timer installed + enabled (`OnCalendar=*-*-* 03:00:00`, `Persistent=true`). Manual run produced 2.55 GB postgres bundle (Neo4j component skipped — followup #19).
- [x] Logrotate config installed (stanza 2 OK; stanza 1 pre-existing followup #20).
- [x] Crash sim: `sudo systemctl kill -s SIGKILL odoo-semantic-webui` → auto-restart in 5s (new PID, Active: active).
- [x] Postgres data hygiene: deleted 1 stale `indexer_jobs` queued row (11 days old, no worker) + 4 inactive never-used API keys.
- [x] Neo4j Cypher cleanup ×2: deleted 2,670 `__unresolved__` stubs v9-v19 + 3,316 v8 orphan children (from prior stale-cleanup) → NULL profile count 5,988 → 2 → 0 (post-reindex).
- [x] Reindex `--all --full --no-embed` verify run on prod (~16 min main + ~3 min targeted rerun for 3 profiles that hit advisory-lock race with an orphan indexer). Writer fix verified on live data: 0 NULL profile nodes regenerated; `__unresolved__ AND profile IS NULL` count = 0.
- [x] Ghost uvicorn `--port 8093` (manual dev instance, PID 1018515) killed.

**Acceptance gate met for go-live:** 9 of 11 pre-launch sections `[x]`, 2 partial (§5 non-prod restore optional, §9 cron optional). Admin-invite signup model active. See [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) for full signoff table.

**Known followups (non-blocking, tracked in `docs/deploy/pre-launch-checklist.md` "Known follow-ups" §12-#15):**

- [ ] **#12 OWLComp pre-v14 anachronism (239 stubs):** Post-reindex shows 239 `__unresolved__` OWLComp at v8-v13 created by JSPatch era3 detection. Read-side `list_owl_components` MCP tool already has era guard (skip v<14) so user-facing output is correct — impact is only raw-graph pollution. Fix: symmetric v14 guard in `_extract_era3_patches` (parser_js) OR belt-and-suspenders at writer PATCHES placeholder site. Plus Cypher cleanup of current 239 anachronisms. Defer to M10.
- [ ] **#13 Neo4j online backup:** `neo4j-admin database dump` requires offline DB; fails on running container. Bundle currently postgres-only (manifest.json + postgres.sql). Replace with Cypher-driver-based export (`CALL apoc.export.cypher.all`) or upgrade to Enterprise for `neo4j-admin database backup`. Update `src/cli.py` + ADR-0018 bundle contract. Defer to M10.
- [ ] **#14 Logrotate /var/log stanza 1 perms:** Pre-existing `/etc/logrotate.d/odoo-semantic` stanza 1 (`/var/log/odoo-semantic-reindex.log`) fails because `/var/log/` is world-writable. Fix: add `su root syslog` directive OR change log location. NOT introduced by WI-3 (stanza 2). Operational fix.
- [ ] **#15 §6 tools 15-21 prod smoke:** 7 M9 W-OSM Wave 1 tools (`describe_module`, `list_fields`, `list_methods`, `list_views`, `list_owl_components`, `list_qweb_templates`, `list_js_patches`) need end-to-end smoke against prod MCP endpoint via Claude Code or another MCP client. Deferred to next session per go-live decision. All 7 are code-complete + unit-tested.

### Stream J — M9 RBAC + Key-Ownership Bug Fix (M9 follow-up, PR #<TBD>)

**Status:** `[x]` DONE — 2026-05-18 (6 WIs orchestrated, code + docs).

**ADR:** ADR-0026 — RBAC + key ownership (fixes session bug, closes deactivate authz hole, adds admin promote/demote, `/account` self-service surface).

**Root cause of regression:** `src/web_ui/routes/api_keys.py:57` reads `request.session.get("is_admin", False)`, but login never wrote that field. All 5 legacy keys had `user_id IS NULL` → filter `WHERE user_id = <admin_uid>` returned 0 rows.

- [x] **WI-1 — Backend session bug fix + ownership-guarded deactivate:** `is_admin_session()` helper (DB-sourced, clarifies ADR-0011). Ownership check on `PATCH /api/api-keys/{id}/deactivate`.
- [x] **WI-2 — Backend RBAC methods + routes:** `set_user_admin()`, `set_user_active()` with last-admin protection. `PATCH /api/admin/users/{id}/admin` endpoint. `PATCH /api/admin/api-keys/{id}/owner` for NULL-key assignment.
- [x] **WI-3 — Frontend middleware + layout:** Astro middleware redirects non-admin users from `/admin/*` to `/account/api-keys`. New `AccountLayout` component.
- [x] **WI-4 — Admin UI enhancements:** Owner column + "Assign owner" banner for legacy NULL keys. Admin promote/demote toggle on `/admin/users` with last-admin protection.
- [x] **WI-5 — Account self-service surface:** `/account/index` dashboard + `/account/api-keys` (list/create/deactivate own keys). Non-admin UX.
- [x] **WI-6 — Documentation:** ADR-0026 + TASKS.md + CHANGELOG.md + CLAUDE.md note on `is_admin` source-of-truth rule.

---

## Milestone 9 Coverage Fill — 2026-05-17

**Batch name:** `coverage-fill-batch` (6 WIs)

- [x] **WI-A1** CSS/SCSS parser + `:Stylesheet` node + `:IMPORTS` edge + ADR-0025 (commit 6db163e)
  - New `src/indexer/parser_css.py` (430 LoC) + `parser_scss.py` (441 LoC) — tree-sitter-css backend + regex fallback
  - `:Stylesheet` node with composite MERGE key `(file_path, module, odoo_version)`, properties: `language ∈ {css, scss}`, `selector_count`, `import_count`, `variable_count`, `mixin_count`
  - `:DEFINED_IN` + `:IMPORTS` relationships; pgvector chunk_types `css`/`scss` for semantic search
  - 18 tests pass (8 CSS + 10 SCSS)

- [x] **WI-A2** v8 era1 `_columns` balanced-paren extraction fix (commit 1d0e8dd)
  - Fixed string-aware brace scan — no longer truncates at `{` inside string literals
  - Closes the v8 era1 field extraction gap (previously truncated when help text contained `{`)
  - `FieldInfo.source_definition` now populated for era1 fields
  - 9 new tests

- [x] **WI-A3** PatternExample v9-v15 backfill (commit d6a2406)
  - 30 patterns appended (83 → 113 total)
  - Per-version: v9=4, v10=5, v11=5, v12=5, v13=4, v14=3, v15=4 patterns
  - All snippets from real `~/git/odoo_{9..15}.0/` sources; stable-API focus
  - 2 tests pass + schema validation OK

- [x] **WI-A4** LintRule static curation v8-v19 (commit a1b0298)
  - 12 `spec_data/lint_rules_X.0.json` populated (v8 to v19), `_curate_status: "complete"`
  - Per-version: v8=20, v9=21, v10=24, v11=23, v12=22, v13=22, v14=23, v15=21, v16=21, v17=24, v18=24, v19=26 rules
  - New `lint_rule.schema.json`; 49 tests pass

- [x] **WI-A5** CLIFlag static curation v8-v19 (commit 0abd715)
  - 12 `spec_data/cli_flags_X.0.json` populated (v8 to v19), `_curate_status: "complete"`
  - Per-version: v8=72, v9=66, v10=67, v11=71, v12=70, v13=72, v14=73, v15=76, v16=78, v17=80, v18=85, v19=72 flags
  - Cross-version deprecation tracking (--xmlrpc-interface → --http-interface, etc.)
  - New `cli_flag.schema.json`; 144 parametrized tests pass

- [x] **WI-A6** Docs hygiene transcribe (this commit)
  - Mechanical update: README + TASKS + CHANGELOG + architecture docs reflect A1-A5 implementation facts
  - No schema changes, no new ADRs (ADR-0025 landed with A1)

- [x] **WI-A7** Deferred items absorption (commit landed with PR #120 squash)
  - Cross-document reasoning placed 10 deferred items into M10/M10.5/M11 sections below + ADR-0025 Future Work + ADR-0023/ADR-0010 follow-up notes

**Post-deploy ops (B1–B11) tracked separately in the Group B post-deploy ops table below.**

### Post-deploy hotfixes (2026-05-18)

Two prod CLI bugs surfaced when Group B operations ran against the deployed code; both shipped as one-line fixes within the same B-phase session:

- [x] **PR #124** `[FIX] indexer: init_pool before job_store in seed_patterns CLI` — `seed_patterns --force` crashed because `get_pool()` was called before `init_pool()`. Cherry-picked from a Haiku agent's accidental master commit (workflow lesson saved to memory `feedback_brief_worktree_for_subagents`).
- [x] **PR #125** `[FIX] indexer: coalesce CLIFlag command_name null → "server"` — all 9 `index-core` jobs aborted with `Cannot merge ... null property value for 'command_name'` because curated `cli_flags_*.json` global flags (e.g. `--config`, `--init`) declared `command_name: null`, which Neo4j 5.x rejects in MERGE identity keys. `_load_static_cli_flags` now coalesces None → "server" matching the live parser default. Regression test covers explicit null, explicit "server", and missing key.

### Group B post-deploy ops outcomes (2026-05-18)

| Item | Outcome | Notes |
|---|---|---|
| B1 v18 odoo source auto-clone | ✅ Pre-done | `/home/.../clones/odoo_18/odoo` cloned + indexed before this session; verified via DB query (repo id 34, clone_status=cloned). |
| B2 `index-core` v9-v16 + v19 | ✅ Done | After PR #125 fix; per-version CoreSymbol 179-534, LintRule 21-63, CLIFlag 66-78. |
| B3 odoo_18 full reindex (OBS-1) | 🟡 Running | Long-running setsid job; baseline 897 modules, target parity with v17 ~1368. |
| B4 internal profile 18.0 branch | ⏸ **Deferred (OBS-2)** | See below — upstream has no `18.0` branch for the required internal repo. |
| B5 internal profile 19.0 + repos | ⏸ **Deferred (OBS-3)** | See below — none of the required internal repos has a `19.0` branch upstream. |
| B6 internal profile 17.0 diagnosis | ✅ False positive | Earlier audit query error; profile has 49/49 modules indexed correctly. |
| B7 v13 theme modules | ✅ False positive | v13 has no theme_* modules by design. |
| B8 full reindex all profiles for CSS/SCSS | ⏸ Scheduled overnight | 6-9h wall clock; deferred to off-peak per operator. |
| B9 v8 reindex (era1 fix verify) | 🟡 Running | Long-running setsid job; expect Neo4j Field count to converge with pgvector field embedding count for v8. |
| B10 `seed_patterns --force` | ✅ Done | Required PR #124 hotfix to unblock; pattern embeddings for v9-v15 written. |
| B11 `index-core` v8 + v17 + v18 | ✅ Done | Bundled with B2 after PR #125; v8 has 0 CLICommand (era1 limitation, see line 652). |

### Out of scope — deferred due to upstream Viindoo branch gaps

- [ ] **OBS-2 internal profile 18.0 branch coverage gap**
  - Source: B-phase ops 2026-05-18 — attempted to register an internal repo at `18.0` branch; git rejected with `fatal: Remote branch 18.0 not found in upstream origin`.
  - Acceptance: when upstream cuts `18.0` branch on the required internal repo, re-run the B4 registration step (insert repo row via `repo_store().add_repo(...)` then `python -m src.cloner --repo-id N` then `index-repo --profile <internal_profile_18> --full`).
  - Dependency: upstream branch cut (external).

- [ ] **OBS-3 internal profile 19.0 — not yet creatable**
  - Source: B-phase ops 2026-05-18 — all required internal repos only have `17.0`/`18.0`/`master` branches; none has `19.0`.
  - Acceptance: when at least one required internal repo has `19.0` branch cut, create the profile via `python -m src.manager add-profile <internal_profile_19> --version 19.0`, then register repos by DB insert + cloner.
  - Dependency: upstream branch cuts (external).

---

## Milestone 10 — "Billing Wow" + Tool Surface + Polish

**Status:** `[~]` M10A + M10.5 P1+P2 shipped; M10C partially shipped PR #159 2026-05-21 (WI-1..5 + review-followup); M10B/M10C remaining pending. Prod reindex v8→v19 (comodel_name + mth.depends + migration m9_010) remains an OPS follow-up — admin run cuoi tuan.

**Intent:** Three independent substreams launched after M9 ship. M10A delivers low-risk MCP tool surface expansion. M10B delivers Stripe billing core (largest scope). M10C absorbs polish + observability + carry-over fixes from M7.5/M8/M9.

**Outcome:** Public users can self-subscribe to a paid plan; admin can see MRR dashboard. CSS/SCSS index addressable via dedicated MCP tools. Indexer pipeline emits Prometheus-scrape histogram for embed latency. NAMEGET deprecation status correct on shipped tools. Stub-only modules covered by `reembed-stubs` CLI path.

> **Restructure history:** M10 substream split landed 2026-05-18 (see PR commit history).

### M10A — Tool Surface Expansion (low-risk, ship first)

- [x] **MCP tool surface for Stylesheet** — HIGH (2026-05-21, PR #156)
  - Source: WI-A7 absorption (M9 Coverage Fill deferred items).
  - Scope: 2 new MCP tools — `resolve_stylesheet(module, odoo_version)` returns stylesheet chain + variable list; `find_style_override(selector_or_variable, odoo_version)` traces which module last re-declares a CSS custom property / overrides a selector.
  - Acceptance: tools registered in `src/mcp/server.py`; output follows ADR-0023 tree-grammar contract; routing matrix in [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/reference/mcp-tool-routing.md) lists both tools with TRIGGER phrases EN+VI; per-tool integration test against fixture profile.
  - Dependency: WI-A1 (`:Stylesheet` node landed via ADR-0025) + B8 reindex must populate stylesheet nodes for production profiles before tool ships.
  - Cross-ref: ADR-0025 §Future Work item 1 forward-refs back to this entry.
  - **Follow-up (cross-repo):** routing matrix EN+VI for `resolve_stylesheet`/`find_style_override` needs update at [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/reference/mcp-tool-routing.md).

- [x] **M10 Quick Wins from `osm_vs_odoo-ls.md`** — HIGH (2026-05-21, PR #156)
  - 4 sub-tasks (each independently shippable):
    - [x] **Magic fields auto-injection** — `resolve_model`/`list_fields`/`resolve_field` include `id`, `display_name`, `create_uid`, `create_date`, `write_uid`, `write_date` as synthetic Field rows when the model is a `models.Model` subclass. Source-of-truth: hard-coded list in `src/constants.py::MAGIC_FIELDS`; not written to Neo4j (synthetic at query time only). (2026-05-21, PR #156)
    - [x] **`from_module` param** for `resolve_model` + `resolve_field` — restrict inheritance chain / field declarations to those originating from a specific module. (2026-05-21, PR #156)
    - [x] **`noqa` support in `lint_check`** — `# noqa: <rule_id>` inline comment suppresses the matching rule for that line. (2026-05-21, PR #156)
    - [x] **CLI batch audit** — `python -m src.indexer audit-repo --profile <name> --output audit.json` emits a JSON file with per-module coverage stats. *(2026-05-21, PR #159 WI-3)*
  - Acceptance: each sub-task has its own unit test + snapshot test; output schemas documented in the [routing matrix](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/reference/mcp-tool-routing.md) for the 3 MCP-facing changes.

- [x] **Superset filter parity** — HIGH (2026-05-21, PR #157)
  - Source: drift surfaced during the odoo-mcp-client v0.7 migration — the discriminator supersets (ADR-0028) forwarded pagination (`start_index`/`limit`) and `from_module` (v0.7.0) but silently dropped the per-method *filter* params their impl functions accept. Confirmed an incremental oversight, not intentional (ADR-0028 silent on exclusion; impl funcs `_list_*` already support all four).
  - Scope: `model_inspect` forwards `kind` (field ttype — method='fields') + `view_type` (method='views'); `module_inspect` forwards `view_type` (method='views') + `bound_model` (method='owl') + `era` + `target` (method='js') to `_list_fields` / `_list_views` / `_list_views_by_module` / `_list_owl_components` / `_list_js_patches`. No new tools (still 20) — optional kwargs only.
  - Acceptance: 5 forwarding unit tests in `tests/test_mcp_inspect_router.py`; `model_inspect` docstring trimmed under the 1500-char tool-description budget (ADR-0023); ADR-0028 Timeline records full filter parity. Bumps v0.7.1.
  - Cross-ref (cross-repo): routing matrix + Cursor/Gemini/OpenAI snippets document all 5 filters at [Viindoo/odoo-mcp-client#10](https://github.com/Viindoo/odoo-mcp-client/pull/10).

- [ ] **§6 tools 15-21 prod smoke** — verify 7 M9 W-OSM Wave 1 tools (`describe_module`, `list_fields`, `list_methods`, `list_views`, `list_owl_components`, `list_qweb_templates`, `list_js_patches`) end-to-end against prod MCP endpoint via Claude Code or another MCP client. All 7 are code-complete + unit-tested. Cross-ref: pre-launch-checklist.md Known follow-ups #15.

### M10B — Billing Wow Core

- [ ] **Stripe SDK integration** — add `stripe>=10` to `pyproject.toml`; service module `src/billing/stripe_client.py` with retries + idempotency keys.
- [ ] **Postgres migrations (3 new):** `subscriptions` (user_id, plan_id, status, current_period_start/end, cancel_at_period_end, stripe_subscription_id); `billing_accounts` (user_id, stripe_customer_id, payment_method_id); `plan_usage_log` (user_id, tool_name, called_at, success).
- [ ] **FastAPI `/api/billing/*` routes** — POST `/subscribe/{plan_id}`, GET `/subscription`, PATCH `/cancel`, POST `/stripe/webhook` (signature-validated).
- [ ] **Astro `/account/billing` page** + Stripe.js React island for payment method management.
- [ ] **Pricing page wire-up** — replace `/pricing/` waitlist teaser with live tier cards calling `/api/billing/subscribe/{plan_id}`.
- [ ] **ADR-0027 — Billing domain model** — record plan tiers ↔ feature gates, multi-currency stance, tax handling.
- [ ] **MCP usage gate** — `src/mcp/middleware.py` checks `user.subscription.plan.tool_allowlist` before servicing requests; emits `plan_usage_log` row.

### M10C — Polish + Observability

- [x] **M7.5-P2-NAMEGET — Parser body-level deprecation detection** — Odoo 17 uses runtime `warnings.warn(..., DeprecationWarning)` instead of `@api.deprecated` decorator for `name_get`. Extended `src/indexer/parser_odoo_core.py` to AST-walk method bodies and detect `warnings.warn(..., DeprecationWarning)` calls (tighten warn-match pattern in review-followup). After re-index, `lookup_core_api("name_get", "17.0")` returns `status='deprecated'`. *(2026-05-21, PR #159 WI-2 + review-followup)*

- [x] **Pgvector re-embed for cleaned-up stubs** — MED *(2026-05-21, PR #159 WI-3)*
  - Scope: `python -m src.indexer reembed-stubs --profile <name>` enumerates modules where `field_count > 0` but `embeddings_count == 0` via `LEFT JOIN embeddings`, re-runs `make_chunks` + `write_module_embeddings`; idempotent; log line summarizes count + total embed calls per ADR-0010 D2.
  - Dependency: WI-A2 (era1 field-gap fix) deployed in production.
  - OPS follow-up: admin runs `reembed-stubs` on prod profiles after applying migration m9_010.

- [ ] **Pgvector observability — Prometheus `embedder_batch_duration_seconds` histogram** — MED
  - Source: WI-A7 absorption (M9 Coverage Fill deferred items); extends ADR-0010 §D7 follow-up.
  - Scope: histogram metric exposed at `/metrics` Prometheus endpoint, recording one observation per `embed()` batch call. Bucket boundaries: 0.1, 0.25, 0.5, 1.0, 1.5, 2.5, 5.0, 10.0, 30.0.
  - Acceptance: `GET /metrics` returns valid Prometheus text-format payload including `embedder_batch_duration_seconds_bucket` + `_count` + `_sum` series; metric tagged by `embedder_type` label; thread-safe under `--max-workers > 1`.
  - Cross-ref: ADR-0010 §D7 forward-refs back to this entry.

- [ ] **Nonce-based CSP** — MED (PR #118 follow-up)
  - Source: WI-A7 absorption (M9 Coverage Fill deferred items); also tracked in MEMORY `m9_csp_permissions_policy_gap`.
  - Scope: replace `'unsafe-inline'` script/style sources with per-request nonces when Astro v5.1+ exposes nonce API. nginx generates nonce via `$request_id`; Astro middleware reads `X-CSP-Nonce` request header and emits `<script nonce="...">` for SSR-rendered scripts.
  - Acceptance: production CSP header includes `script-src 'nonce-<random>' 'self'` (no `'unsafe-inline'`); browser console clean of CSP violations; CI test verifies nonce uniqueness across 10 sequential requests.
  - Status: BLOCKED — awaits Astro v5.1+ nonce API exposure.

- [x] **Admin UI core-index status column** — UX gap resolved. *(2026-05-21, PR #159 WI-5)* Admin `/admin/repos` page now shows a "Core Index" column with per-version CoreSymbol count badge via `GET /api/repos/{id}/core-symbol-counts`. `site/src/components/RepoTable.astro` updated + new FastAPI endpoint in `src/web_ui/routes/repos.py`. Review-followup: N+1 hoist + close guard + version sort `toFloat`.

- [x] **W-UM legacy columns drop migration** — *(2026-05-21, PR #159 WI-4)* `actor_id`, `target_id`, `detail_text` columns in `admin_audit_log` table dropped via `migrations/m9_010_drop_audit_legacy_columns.sql`. Dual-write removed from `src/db/auth_registry.py::AuthRegistry.log_audit()` (now canonical-only INSERT). All consumers confirmed using canonical columns (`actor`, `action`, `target`, `success`; `detail` JSONB via `src.db.audit.write_audit_log`). Review-followup: migration renamed from `0006` to `m9_010` for yoyo ordering consistency.

- [ ] **v8 era1 CLI parser runtime extraction** — LOW priority. `parser_cli.py` writes 0 CLIFlag runtime for v8 (era1 `openerp-server` CLI structure). Static curation `spec_data/cli_flags_8.0.json` already covers (72 flags). Only matters when indexing a live v8 source tree (rare). Extend `parser_cli.py` with `openerp/` path branch for v8/v9.

- [ ] **T3.4b VN translation persona docs** — Translate 5 EN-canonical persona guides to Vietnamese. Guides now live in [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client) at `docs/personas/<role>.md`; add `docs/personas/<role>.vi.md` companion files there. Carried from M7.5 design decision.

- [x] **OWLComp pre-v14 anachronism guard + cleanup** — *(2026-05-21, PR #159 WI-1)* Parser guard shipped: `_extract_era3_patches` in `parser_js.py` now returns early when `major < 14` (symmetric with the existing `_extract_era3_components` guard from PR #119 WI-1). Prevents NEW anachronistic stubs from being written on future reindex. **Cypher cleanup of existing 239 `__unresolved__` OWLComp stubs (v8-v13) + 1 `snap_mod` node (odoo_version='96.0') is OPS work in the full-reindex runbook below — not yet run on prod.** Cross-ref: pre-launch-checklist.md Known follow-ups #12.

---

## Milestone 10.5 — "ORM Intelligence Wow"

**Status:** `[x]` DONE — Phase 1 data layer shipped PR #156 2026-05-21; Phase 2 (4 MCP tools) shipped 2026-05-21 (v0.8.0, branch `feat/m10-5-phase2-orm-tools`). Tool surface 20 → 24. Prod reindex (comodel_name + new `mth.depends`) remains an ops follow-up.

**Intent:** New MCP tool family for ORM-level validation — domains, depends graphs, relation chains. Sits between drill-down tools (M1–M5) and architectural impact (M4 `impact_analysis`).
**Outcome:** AI client validates an ORM domain (`[('partner_id.country_id', '=', 'VN')]`) against the actual model graph before suggesting it to the user — no more hallucinated fields in domain expressions.

> **ADR cross-ref:** ADR-0023 §Follow-up forward-refs back to this milestone.

### Phase 1 — Data layer pre-work (BLOCKER for Phase 2)

`:Field.comodel_name` property does not exist anywhere in the pipeline today (verified via subagent survey 2026-05-18). Must land 4 changes before any ORM tool ships:

- [x] **`FieldInfo.comodel_name` field** — extend dataclass in `src/indexer/models.py:28-35` with `comodel_name: str | None = None`. (2026-05-21, PR #156)
- [x] **Parser extraction** — `src/indexer/parser_python.py`: for `fields.Many2one`/`One2many`/`Many2many` calls, extract first positional arg (the comodel string) and populate `FieldInfo.comodel_name`. Handle both era1 (text-regex `_columns` dict) and era2 (AST). (2026-05-21, PR #156)
- [x] **Writer persist** — `src/indexer/writer_neo4j.py:182`: add `SET f.comodel_name = $comodel_name` clause when writing Field nodes. (2026-05-21, PR #156)
- [ ] **Production reindex** — after migration deploys, run `python -m src.indexer index-repo --all --full` to populate `f.comodel_name` for existing Field nodes (otherwise queries return null). **Note:** ops follow-up — also backfills the new `mth.depends` (Phase 2). Run `index-repo --all --full` on prod.

### Phase 1b — Data layer for validate_depends (Phase 2 prerequisite, shipped v0.8.0)

- [x] **`MethodInfo.depends` field** — `src/indexer/models.py`: `depends: list[str] = field(default_factory=list)`. (v0.8.0)
- [x] **Parser extraction** — `src/indexer/parser_python.py`: era2 decorator loop captures `@api.depends('a.b', ...)` string args (lambda/callable skipped via `_extract_string` returning None); era1 has no decorator depends → empty. (v0.8.0)
- [x] **Writer persist** — `src/indexer/writer_neo4j.py`: `SET mth.depends = $depends`. (v0.8.0)

### Phase 2 — 4 MCP tools (depends on Phase 1 complete) — shipped v0.8.0

> Implemented in new module `src/mcp/orm.py` (primitive `_traverse_field_chain` + 4 impls); 4 thin `@mcp.tool` wrappers in `src/mcp/server.py`. 19 integration tests (`tests/test_orm_validation.py`) + 6 pure operator tests (`tests/test_domain_operators.py`) + parser/writer unit+integration tests, all green.

- [x] **`resolve_orm_chain(model, dotted_path, odoo_version)`** — primitive reused by other 3. Walks dotted path → terminal field type + intermediate comodels; `BROKEN` line at first broken hop (reason ∈ missing/not_relational/dangling_comodel). Handles magic fields + INHERITS/DELEGATES_TO inherited fields. (v0.8.0)

- [x] **`validate_domain(model, domain, odoo_version)`** — `ast.literal_eval` parse; per-term field-path validation via primitive; **version-aware operator set** (`valid_domain_operators` — `any`/`not any` v17+, `parent_of` v9+); logical `&`/`|`/`!` skipped. (v0.8.0)

- [x] **`validate_depends(model, method, odoo_version)`** — reads `Method.depends` from Neo4j, validates each path; flags depends-on-`id` (Odoo `NotImplementedError`); era1 (empty depends) → clear note; `difflib` "did you mean X?" for typos. (v0.8.0)

- [x] **`validate_relation(model, field, target_model, odoo_version)`** — asserts `model.field` is relational → `target_model` (or subtype via INHERITS); reports actual comodel on mismatch + field-typo suggestion. (v0.8.0)

Acceptance: each tool follows ADR-0023 tree-grammar contract (§1 header + §4 Next-step hint mapping); routing matrix in [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/reference/mcp-tool-routing.md) lists all 4 tools with TRIGGER phrases EN+VI; integration tests against `viindoo_17` fixture profile; snapshot tests for tree-text output.

Dependency note: v8/v9 era1 may have partial comodel_name coverage (best-effort text-regex parse).

ADR impact: extends ADR-0023 tool-output completeness contract (no new ADR; section "Follow-up (M10/M10.5)" added).

---

## Milestone 11 — "Architectural Wow" + Curation Depth

**Status:** `[ ]` Not started.

**Intent:** Architectural refactors (parser hook registry, RelaxNG XML validation) + curation depth (lint rules 10-30 → 50+/version, pattern catalogue 35 → 100+). M11 is the milestone where OSM transitions from "covers Odoo" to "rivals/exceeds Odoo LS for static analysis".
**Outcome:** Parser pipeline replaces hard-coded era branches with a `(min_version, max_version, fn)` registry — adding v20 support becomes a single registry append. RelaxNG schema validation catches malformed XML in view inheritance chains. Pattern catalogue absorbs community contributions per ADR-0009.

- [x] **Pattern catalogue expansion 35 → 100+** — MED (community track) *(2026-05-18 — Target met: 113 patterns in src/data/patterns.json via WI-A3 backfill commit d6a2406.)*
  - Source: WI-A7 absorption (M9 Coverage Fill deferred items); extends ADR-0009 community contribution policy.
  - Scope: per ADR-0009, accept community PRs to `patterns.json` with curator review. Target: ≥100 patterns total (current ~83 + 30 from WI-A3 backfill = 113 once batch lands; aim ≥100 retained after curation prune).
  - Acceptance: pattern count test `test_patterns_minimum_count.py::test_minimum_100_patterns` passes; new patterns include CSS/SCSS entries per ADR-0025 Future Work item 2 (Bootstrap variable overrides, OWL scoped CSS, mixin reuse).
  - Dependency: ADR-0009 community contribution flow already documented; needs marketing push to attract PRs.

- [ ] **Static spec_data deepening — lint rules 50+/version** — MED
  - Source: WI-A7 absorption (M9 Coverage Fill deferred items).
  - Scope: extend `spec_data/lint_rules_X.0.json` from current baseline (per WI-A4: v8=20, v9=21, ..., v19=26) to ≥50 rules per major version. Prioritize rules with high frequency in real Odoo deprecation warnings; add ESLint SCSS rules (e.g. `scss/no-duplicate-dollar-variables`) per ADR-0025 Future Work item 4.
  - Acceptance: `test_lint_rules_minimum_count.py::test_minimum_50_per_version` passes for v10+ (v8/v9 may stay at curation baseline due to scarce source data); new rules categorized via existing `category` field (`deprecated_api`, `security`, `performance`, `style`).
  - Dependency: WI-A4 baseline must be deployed and production-validated (i.e. real usage logs reveal high-value gaps).

- [ ] **Parser hooks registry refactor** — HIGH (architectural)
  - Source: `peaceful-orbiting-dongarra.md` deferred items list (WI-A7 absorption).
  - Scope: replace hard-coded era branches in `parser_python.py`, `parser_js.py`, `parser_odoo_core.py` with a `(min_version, max_version, fn)` registry. Adding v20 support becomes a single registry append (`PARSER_REGISTRY.append((20, None, parse_v20_style))`) instead of a new `if major >= 20:` branch in 3 files.
  - Acceptance: regression test — all existing era1/era2/era3 fixtures pass via the new registry without behavioral change; new entry registered for v8/v9 era1 (`parse_legacy_columns`); registry sorts by `min_version` and short-circuits on first match (no fall-through bug).
  - **ADR impact: invalidates parts of ADR-0005 (era-aware path resolution).** New ADR required — `ADR-0026 parser hooks registry` — documenting (a) registry data structure, (b) the parser_python/parser_js/parser_odoo_core unification, (c) version overlap policy when two registry entries match (highest `min_version` wins). ADR-0005 to be retained as historical record; superseded section added on land.
  - Dependency: must land BEFORE any v20 work to avoid duplicating the era-branch debt.

- [ ] **RelaxNG XML schema validation port from Odoo LS** — MED
  - Source: `peaceful-orbiting-dongarra.md` deferred items list (WI-A7 absorption).
  - Scope: port Odoo Language Server's RelaxNG schema files for v15+ view XML; integrate into `parser_xml.py` post-parse step that validates each `<record>`/`<template>` against the schema; surfaces errors as new `:LintViolation` rows tied to the View node.
  - Acceptance: `validate_xml.py` script runs against `viindoo_17` profile producing a violations report; `lint_check(language='xml')` MCP tool returns RelaxNG errors alongside existing Python lint output.
  - Dependency: requires `lxml` (already in `pyproject.toml`) and Odoo LS schema files (vendored under `src/indexer/schemas/odoo_xml/v15+.rng`). Licence-check Odoo LS LGPL terms before vendoring.

---

## Milestone 12 — "v0.6 Shim Removal + M11 Hardening"

**Status:** `[x]` DONE — 2026-05-21 (PR #155, v0.6.0). Stream A (shim removal) + Stream B (WI-B1/B2 hardening + WI-B3 ADR-0029 amendment) shipped in one PR. Orchestrated as a multi-subagent worktree wave (S1 src / S2 tests / S3 docs → diamond integration). 18 tools, CI green. See "Review-followup" note below for in-PR fixes from the `/code-review:code-review` pass.

> Tracked as v0.6 follow-up after v0.5 ships in PR #133 (commit `9ae3732`, `feat/m10-5-m11-tool-ux-architecture`). Two streams: Stream A executes the deprecation timeline promised by [ADR-0028](docs/adr/0028-discriminator-consolidation.md) (one-major-release removal of the 10 legacy shims); Stream B absorbs the three functional/hardening observations surfaced by the M11 security review of PR #133 (zero HIGH/MEDIUM security findings — these are UX / correctness gaps, not vulnerabilities).

**Intent:** Cut the legacy 28-tool surface down to the 18-tool target codified in ADR-0028 + close three M11 review observations against `set_active_*` session state and `resources/read` middleware coverage.
**Outcome:** Fresh `tools/list` reports 18 tools (3 supersets + 4 session + 7 inspection + 4 ORM-validation when M10.5 lands); `resources/read` honours `set_active_version`; bogus version/profile pins return a clear error tree instead of silent fallback; the authz model around `set_active_profile` is documented unambiguously.

**Plans liên quan:**
- ADR cross-refs: [`docs/adr/0028-discriminator-consolidation.md`](docs/adr/0028-discriminator-consolidation.md) §Deprecation timeline + Timeline table; [`docs/adr/0029-implicit-session-context.md`](docs/adr/0029-implicit-session-context.md) §Profile-as-convenience-not-authz amendment (Stream B WI-3); [`docs/adr/0030-mcp-resources-uri-scheme.md`](docs/adr/0030-mcp-resources-uri-scheme.md) (resource handler contract).
- Source PR: #133 (v0.5.0 — 28 MCP tools + 7 Resources + per-API-key session state).

### Stream A — v0.6 Shim Removal (mechanical, ship first)

ADR-0028 §Timeline promises "one major release between deprecation banner and removal". v0.5.x shipped the `DEPRECATED:` banners; v0.6 retires the wrappers. Persona skills in [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client) already migrated to supersets in PR #133 commit `4ba1432` (verified: 0 legacy refs, 15/15 use supersets + `set_active_version`) — no skill changes required.

- [x] **WI-A1 — Delete the 10 `@mcp.tool()` shim wrappers** in `src/mcp/server.py`: *(commit `18b3b66`; also removed orphaned `_deprecation_banner`/`_looks_like_ref`/`_REF_PATTERN`/`_STALE_REF_RECOVERY`/`_format_stale_ref_error` + 4 unused imports)*
  - `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view`
  - `list_fields`, `list_methods`, `list_views`
  - `list_owl_components`, `list_qweb_templates`, `list_js_patches`
  - Keep the `_resolve_*` / `_list_*` implementation functions intact — they are called by the supersets (`model_inspect` / `module_inspect` / `entity_lookup`) via `src/mcp/inspect.py` routers.
- [x] **WI-A2 — Delete `tests/test_mcp_deprecation_shims.py`** (shim-banner equivalence tests no longer applicable). *(commit `d5fcdd2`)*
- [x] **WI-A3 — Update tool count from 28 → 18 across docs + UI:** *(in-repo items done, commit `ea083a9`; client-repo items below stay open — tracked in Viindoo/odoo-mcp-client)*
  - [x] `README.md` (every "28 tools" / "28 MCP tools" reference → 18)
  - [ ] [MCP tool routing matrix](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/reference/mcp-tool-routing.md) (28-tool matrix → 18-tool) — update in Viindoo/odoo-mcp-client repo
  - [ ] [docs/personas/dev.md](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/personas/dev.md) ("28-tool arsenal" phrasing → 18-tool) — update in Viindoo/odoo-mcp-client repo
  - [x] [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) (`server.py + 28 tools` → `+ 18 tools`)
  - [ ] [client setup guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/setup.md) — remove "Tool Surface Change — v0.4 → v0.5" section (legacy no longer callable); keep "Session Context" + "MCP Resources" sections (still applicable). Update in Viindoo/odoo-mcp-client repo.
  - [ ] Cursor, Gemini, OpenAI adapter files in Viindoo/odoo-mcp-client — drop DEPRECATED legacy tool blocks.
  - [x] `site/src/pages/index.astro` — TOOLS array: remove legacy entries; "28" → "18".
  - [x] `site/src/components/Hero.astro` — tag pill + stats "28" → "18". *(also `InstallSnippets.astro`)*
- [x] **WI-A4 — Version bump:** `pyproject.toml` `0.5.0 → 0.6.0`; CHANGELOG.md `[0.6.0]` entry documenting shim removal + Stream B fixes + pagination passthrough. *(commit `ea083a9` + docs-sync)*
- [x] **WI-A5 — Smoke gate before tag:** full integration sweep (CI `integration-tests` green) + `tools/list` == 18 verified + `grep -r 'def resolve_model\|def list_fields' src/mcp/` returns 0 hits outside `inspect.py` / `_resolve_*` impl. CI all green on PR #155.

### Stream B — M11 Hardening (security-review follow-ups, PR #133)

Three observations surfaced during the M11 security review of PR #133. All three are NOT security vulnerabilities (data is global Odoo codebase intelligence with no tenant-private content) — they are functional / UX gaps that should not be carried into v0.6 unfixed.

- [x] **WI-B1 — `resources/read` honours `set_active_version`** — MED *(commit `18b3b66`: added `on_read_resource` hook to `UsageLogMiddleware`, mirrors `on_call_tool` thread-local set/clear; covered by `tests/test_mcp_resources_session.py`)*
  - Symptom: `UsageLogMiddleware` (`src/mcp/tool_log_middleware.py:49-92`) hooks `on_call_tool` only, not `resources/read`. After `set_active_version('17.0')`, calling `odoo://auto/model/sale.order` bypasses the sticky session — `_get_api_key_id()` returns `"default"`, `int("default")` fails in `_fetch_from_db`, fallback to `_latest_version()`.
  - Fix direction: extend `UsageLogMiddleware` to also hook `on_read_resource` and set the same thread-local. Alternative: make `_resolved_version_for` accept an explicit `api_key_id` arg threaded by the FastMCP resource framework.
  - Acceptance: integration test verifies `set_active_version('17.0')` then `resources/read odoo://auto/model/sale.order` returns the v17.0 rendering (NOT `_latest_version()` output); covers both `odoo://auto/*` and any future per-version resource URIs.
  - Cross-ref: [ADR-0029](docs/adr/0029-implicit-session-context.md) §Session resolution; [ADR-0030](docs/adr/0030-mcp-resources-uri-scheme.md) §URI grammar.

- [x] **WI-B2 — Validate `set_active_version` / `set_active_profile` inputs** — LOW *(commit `18b3b66`: validation placed at the tool boundary in `src/mcp/server.py` — NOT the `session.py` db helper as the symptom suggested — because version validation needs Neo4j + the tool can render the error tree; `set_active_version('999.0')` / `set_active_profile('does_not_exist')` now return an error tree naming valid options; covered by `tests/test_session_validation.py`)*
  - Symptom: `src/mcp/session.py` `set_active_version_db` / `set_active_profile_db` accept any string. Pinning to `'99.0'` (non-indexed) or `'nonexistent_profile'` silently falls back or returns empty trees — poor UX, no error surface.
  - Fix direction: in `set_active_version_db`, run a sanity Cypher (`MATCH (m:Module {odoo_version: $v}) RETURN m LIMIT 1`) — empty result raises `ValueError("version not indexed: <v>")` propagated up the tool surface. Same pattern for `set_active_profile_db` against the `profiles` table.
  - Acceptance: `set_active_version('999.0')` returns an error tree naming the available versions; `set_active_profile('does_not_exist')` returns an error tree naming valid profiles. Existing happy-path tests stay green.
  - Note: NOT a security finding — values are parameter-bound everywhere; no injection possible.

- [x] **WI-B3 — Profile-as-convenience-not-authz documentation amendment** — LOW (decision-only) *(path (a) chosen: ADR-0029 amended with "Profile is convenience, not authz" section, commit `ea083a9`. Client-setup-guide note tracked in Viindoo/odoo-mcp-client.)*
  - Symptom: per `src/db/migrate.py:139-147` (`api_keys` schema), any authenticated key can query any profile via the `profile_name` parameter. Resource handlers in `src/mcp/resources.py` deliberately omit `profile_name` from underlying `_resolve_*` calls. v0.5's introduction of `set_active_profile` could mislead users into expecting authz.
  - Decision required, pick one of:
    - **(a) Status quo + documentation (recommended, lowest-effort):** Amend [ADR-0029](docs/adr/0029-implicit-session-context.md) + [client setup guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/setup.md) with explicit note that `set_active_profile` is convenience for default-arg-injection, NOT an access control mechanism. Profile boundary remains data segmentation only.
    - **(b) True profile authz (higher-effort):** Add `allowed_profile_ids JSONB` column to `api_keys`; filter all Cypher queries in resources + tools by this list. Open a separate RFC / ADR before implementing — needs customer demand signal first (i.e. customer-A's index hidden from customer-B).
  - Acceptance: written decision committed — either ADR-0029 amendment (path a) OR new RFC ticket linking to a draft ADR-0031 (path b).
  - Note: by design today, NOT a security finding per se; flagged because v0.5 surface could mislead.

### Review-followup (PR #155 `/code-review:code-review` pass — fixed in-PR to prevent docs/code drift)

The mechanical shim removal initially left dangling references; a multi-agent code review caught them and all were fixed within the same PR (no follow-up debt):

- **Dangling next-step/pager hints + docstrings** — ~49 inline `Next:`/pager f-strings + ~14 `TRIGGER/PREFER/SKIP` docstring clauses inside the *surviving* impls (`_resolve_model`, `_resolve_view`, `_list_fields`, `_list_methods`, `_list_views*`, `_list_owl_components`, `_list_qweb_templates`, `_describe_module`, `find_override_point`, `impact_analysis`) + 2 `inspect.py` fields/methods stubs were redirected to the supersets (`model_inspect`/`module_inspect`/`entity_lookup`). *(commit `8db30f4`)*
- **Pagination parity** — `model_inspect`/`module_inspect` now accept + forward `start_index` / `limit` to the underlying `_list_*` impls, so paginated field/method/view/owl/qweb/js listing survives the flat-tool removal (the pager continuation hint now names a tool that actually paginates). *(commit `2ad9b84`)*
- **Docstring budget** — `find_override_point` docstring trimmed back under the 1500-char limit after the reword. *(commit `cb18f2b`)*
- **Tests** — 4 integration test files migrated off the removed tools (`test_drilldown_refs`, `test_dual_channel_b3_smoke`, `test_dual_channel_envelope`, `test_smoke_e2e_mcp_http`); 8 truncation/era-guard assertions in `test_mcp_server.py` updated to the superset hint names; `test_set_active_profile_returns_confirmation` mock updated for the WI-B2 profiles check.

Wave commits: `18b3b66` (S1 src), `ea083a9` (S3 docs), `d5fcdd2` (S2 tests), `5205600` (hints self-loop), `8db30f4` + `2ad9b84` + `cb18f2b` (review-followup).

### Sequencing note

Stream A can ship first as a clean release (mechanical, low-risk). Stream B WI-B1 and WI-B2 may bundle into the same v0.6 PR if bandwidth allows; WI-B3 is decision-first (path-a amendment is trivial; path-b spawns its own milestone). v0.6 author chooses the bundle based on review capacity.

---

## Pre-launch Signoff

Admin ký tên trước khi mở public / phân phát API key. Xem [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) để biết 10 mục + 18 MCP tool sign-off table.

| Mục | Admin | Ngày | Ghi chú |
|-----|-------|------|---------|
| Infrastructure & TLS | | | |
| Auth & Rate Limiting | | | |
| Port Isolation | | | |
| Logrotate | | | |
| Backup & Recovery | | | |
| MCP Tool Sign-Off (18 tools) | | | |
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
| ↓ | Implementation plans (archived internally) | Per-milestone implementation plans |
| → | [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) | Pre-launch signoff — 10 mục + 18 MCP tool verify |
| → | [`docs/deploy/disaster-recovery.md`](docs/deploy/disaster-recovery.md) | DR runbook — backup frequency, restore order, RTO |
