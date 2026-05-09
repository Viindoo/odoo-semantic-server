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
- [ ] E2E test: kết nối VS Code + Claude Code, verify kết quả *(auto tests đầy đủ — chỉ còn manual verify với Claude Code thật; xem `make test-all` cho count hiện tại)*
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
- [ ] E2E test: kết nối VS Code + Claude Code, verify `resolve_view` kết quả

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
- [ ] E2E manual: clone Odoo 17 → register → index → Claude Code call 4 tools

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
- [ ] **E2E manual**: Ollama chạy với qwen3-embedding-q5km → index Viindoo 17.0 → Claude Code call `find_examples`
- [ ] **Recall benchmark**: `pytest tests/test_find_examples_recall.py -m ollama` → VN≥0.75, EN≥0.80

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
- [x] `install.sh` + `systemd/` templates: non-Docker installation path

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

**Section F — Job Tracking (P2 — chưa implement):**
- [ ] `src/db/migrate.py`: table `indexer_jobs` (id, profile_name, status, started_at, finished_at, error_msg, pid, created_at) + indexes
- [ ] `src/db/job_registry.py`: CRUD — `create_job()`, `update_job()`, `get_last_job()`, `list_running_jobs()`
- [ ] `src/indexer/__main__.py`: thêm `--job-id INT` arg → update job status start/success/error
- [ ] `src/web_ui/routes/repos.py`: `index_repo()` tạo job record + truyền `--job-id` vào subprocess
- [ ] `GET /repos/jobs/{job_id}/status` route: JSON `{status, pid, error_msg}`
- [ ] `src/web_ui/templates/repos.html`: status badge + JS polling 5s nếu running/queued
- [ ] `tests/test_job_registry.py`: unit tests CRUD

> **Lý do tách M5.5:** items polish không block M5 ship; deferred items cần auth layer M5 trước. Pattern theo M2.5 precedent (milestone phụ giữa product milestones).

## Milestone 6 — "Scale Wow" (Ongoing)
**Intent:** Hỗ trợ toàn bộ ecosystem Viindoo, multi-version, incremental updates.  
**Outcome:** Re-index chỉ mất vài giây. Index đồng thời 16.0 + 17.0 + 18.0.

- [ ] **Auto-clone qua SSH khi user add repo (moved from M5):** detect SSH URL trong Web UI → auto-clone via Ed25519 private key + `GIT_SSH_COMMAND` + `tempfile.mkstemp(mode=0o600)` → set `local_path` automatically; companion: host fingerprint management UI (`StrictHostKeyChecking=accept-new` policy)
- [ ] `src/indexer/incremental.py`: git commit hash tracking, skip unchanged modules
- [ ] Multi-version: index song song nhiều versions
- [ ] `src/indexer/version_presets.py`: preset "viindoo-17.0", "viindoo-18.0"
- [ ] OpenUpgrade support: migration path awareness across versions
- [ ] **Pattern catalogue maintenance (M4.6 defer):**
    - [ ] Auto-reseed `seed_patterns.py` integrate vào indexer run thay vì one-shot CLI manual (per M4.6 plan §Defer M6)
    - [ ] Seed expansion từ ~50 → ~200 patterns + community contribution path (PR template + `src/data/patterns.json` review checklist)
    - [ ] `find_override_point` cross-version diff — surface pattern thay đổi giữa v17 vs v18 (vd `_compute_*` rename, decorator switch)
- [ ] **EE_CONFUSION auto-detect (M4.6 defer):** thay hardcode `src/data/ee_modules.py` 16-entry dict bằng auto-detect từ manifest `license = 'OEEL-1'` + path scan upstream Odoo CE repo (per M4.6 plan §Risk & Mitigation). Vẫn keep hardcode dict làm fallback cho khi indexer chưa scan upstream.
- [ ] **`viindoo_equivalent_qname` auto-populate (M4.6 defer):** thay hardcode mapping bằng Neo4j graph traversal — query Module nodes có `name LIKE 'viin_%'` HOẶC `'to_%'` + match feature tags vs EE module name (per M4.6 plan §Defer M6).
- [ ] **Per-profile advisory locks (P3):** `src/indexer/pipeline.py` — thay `_LOCK_ID` global constant bằng `_profile_lock_id(profile_name: str) -> int` (hash `f"odoo-semantic-{profile_name}"`). Cập nhật `indexer_is_running()` nhận thêm `profile_name` param. Hai profile khác nhau có thể index song song không block nhau.
- [ ] **ThreadPoolExecutor parallel repo scan (P3):** `src/indexer/pipeline.py` `index_profile()` thêm `max_workers: int = 1` param. Khi `> 1`: wrap `_index_repo()` bằng `ThreadPoolExecutor` — mỗi thread cần PG connection riêng.
- [ ] **PostgreSQL connection pool (P3) — proper fix cho H1:** `src/mcp/server.py` + `src/mcp/middleware.py` — thay singleton `_pg_conn` + `_PG_LOCK` bằng `psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=10)`. Workaround tạm thời (_PG_LOCK bao quanh cursor trong tool handlers) đã ship trong fix/pre-launch-critical.

**Section G — Pre-launch audit deferred (2026-05-09):**
- [ ] **M3 — Feedback API trên MCP server (P2):** `POST /api/feedback` hiện chỉ expose trên Web UI port 8003 (localhost-only). Remote end-user dùng MCP port 8002 không thể submit feedback. Fix: mount `feedback.router` trực tiếp vào ASGI app của MCP server, bỏ qua loopback guard (đã có X-API-Key auth).
- [ ] **M4 — Qwen3Embedder default model name (P3):** `src/indexer/embedder.py:59` default `model="qwen3-embedding:4b"` khác với config default `qwen3-embedding-q5km`. Sửa class default thành `"qwen3-embedding-q5km"` cho nhất quán với README và `odoo-semantic.conf.example`.
- [ ] **M5 — Password lộ trong process list khi pg_dump (P2):** `src/cli.py:38` — `subprocess.run(["pg_dump", dsn, ...])` expose password trong `/proc/<pid>/cmdline`. Fix: parse DSN → set `PGPASSWORD` env var, truyền host/port/user/dbname riêng thay vì DSN string.
- [ ] **L1 — health endpoint dùng private FastMCP attr (P3):** `src/mcp/health.py:34` — `mcp._tool_manager._tools` là private internal API. Nếu FastMCP update, health trả `mcp_tools: -1` thay vì error rõ ràng. Tìm public API thay thế hoặc wrap trong try/except với fallback mô tả rõ hơn.
- [ ] **L3 — No `maxlength` trên Web UI form inputs (P3):** `src/web_ui/templates/api_keys.html` + `repos.html` + `ssh_keys.html` — thêm `maxlength="200"` cho text inputs để tránh cực trị.
- [ ] **L4 — Cache dict không có lock (P3):** `src/mcp/middleware.py` — `_KEY_CACHE` và `_CACHE_TS` được read/write từ multiple threads mà không lock. Thêm `_cache_lock = threading.Lock()` bao quanh toàn bộ cache operations.
- [ ] **L6 — `embeddings: 0` không giải thích lý do (P3):** `src/indexer/__main__.py` — khi `embedder=None` (không config), in thêm dòng "Embeddings skipped — EMBEDDER_URL not configured. Use --no-embed to suppress." vào stdout cùng summary.

---

## Điều Hướng Tài Liệu

| | File | Nội dung |
|---|------|----------|
| ← | [`README.md`](README.md) | Điểm bắt đầu: tổng quan, onboard, hướng dẫn deploy |
| ↓ | [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Thiết kế kiến trúc đầy đủ: schema, pipeline, MCP tools |
| ↓ | [`docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md`](docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md) | Implementation plan chi tiết Milestone 1 — bắt đầu ở đây |
