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

**Carry-over từ M6 (defer M7 confirmed):**
- [ ] **`viindoo_equivalent_qname` auto-populate (M4.6 → M6 → M7):** thay hardcode mapping bằng Neo4j graph traversal — query Module nodes có `name LIKE 'viin_%'` HOẶC `'to_%'` + match feature tags vs EE module name. **Investigation 2026-05-10 (Wave 2 planning) recommend defer M7:** graph traversal cannot replace curated 1-to-1 mapping; hardcode dict 8 entries actually correct + low-maintenance. Reconsider when Viindoo addons indexed in shared profile + feature-tag heuristic available (e.g. manifest `category` + `summary` keyword overlap).

**Review-deferred items (LOW findings from M6 Opus review — fix in M7):**
- [ ] **Neo4j `setup_indexes()` race under `profile_workers > 1`:** fresh Neo4j + parallel workers hit `EquivalentSchemaRuleAlreadyExists`. Fix: catch + ignore in writer OR pre-call once in `index_all` entry point. Workaround documented in `tests/test_indexer_profile_workers.py` (test pre-calls `Neo4jWriter().setup_indexes()` before `index_all(profile_workers=2)`). Affects production `--profile-workers >1` first-run.
- [ ] **Rerank coefficients tuning (`src/mcp/server.py:489`):** needs Vietnamese + English eval dataset to calibrate `dependents_map` weight vs `in_chain_set` boost. V0 heuristic is conservative placeholder — M7 measure recall/precision on held-out queries.
- [ ] **`_compute_risk` thresholds recalibration (`src/mcp/server.py:683`):** needs held-out incident dataset to validate `total >= 10` HIGH / `4-9` MEDIUM / `< 4` LOW buckets. Current thresholds are qualitative against Odoo 17 + Viindoo; M7 quantitative validation.
- [ ] **USES_CORE_SYMBOL V0→V1 expansion (`src/indexer/parser_python.py:36`):** V0 scope = deprecated/removed only (5 symbols). Expand to cover "signature changed" + "moved module" APIs per ADR-0002 §3. Current false-positive rate acceptable for MVP.
- [ ] **Qualified-name symbol resolution (`src/indexer/parser_python.py:67-68`):** full import-chain tracking to eliminate short-name collisions. Today qualified_name heuristic (ENDS WITH) catches most cases; M7 implement proper scope resolver.
- [ ] **Clone-status poll cap (`src/web_ui/templates/repos.html` `pollCloneCells`):** stuck-pending repos poll forever (5s tick). Add max-tick stop + "Polling timed out, check server logs" message. UX improvement.
- [ ] **`_NULL_HINT` repr format cleanup (`src/mcp/server.py` `_diff_method_across_versions` output):** internal sentinel bleeding into API output. Format as actual string value or comment.
- [ ] **`default_clone_dir` URL query-string handling (`src/git_utils.py`):** strip query/fragment via `urlparse` to avoid invalid SSH URL. Edge case when user manually adds SSH URL with query params.
- [ ] **W3-2 EE-reference test list expansion (`tests/test_patterns_schema.py`):** current EE_CONFUSION needle list has ~5 entries; expand to all 16 dict keys + `viin_*` prefix patterns. Better coverage.
- [ ] **Migration tool adoption:** yoyo-migrations or alembic — defer until first non-additive schema change needed (ADR-0001 revision recorded M6 W5).

**Spawned từ ADR-0007 §"Out of scope" (M6 Wave 2):**
- [ ] **Module rename garbage collection (ADR-0007 §D5):** thay vì recommend periodic `--full`, add explicit `--gc` flag chạy "DETACH DELETE Module nodes whose path no longer exists in current scan". Risk-gated (only if scanner found modules cho repo X) để tránh accidental delete khi scan fail. Replaces D5's "stale orphans accepted" stance with explicit cleanup pass.
- [ ] **Cross-repo dependency change tracking (ADR-0007 §Out of scope):** today mỗi repo's diff được tính độc lập. Nếu repo A's module depend on repo B's module B vừa thay đổi, dependency graph rebuild của A là implicit on next full reindex. M7 explicit: detect inter-repo edge changes + propagate re-index trigger (e.g. via Neo4j relationship watching).
- [ ] **Embedding cost observability (ADR-0007 §Out of scope):** today per-module embedding incremental implicit qua `delete_embeddings_for_module`. M7 add explicit metrics — Ollama API call count per indexer run, vector store delta size, surface trong MCP `/health` hoặc admin Web UI dashboard.

> **Lý do định danh "Lifecycle Wow":** items đa dạng nhưng chung chủ đề "track sự thay đổi theo thời gian" — repo rename hygiene (GC), inter-repo dependency drift, ecosystem correlation (Viindoo↔EE auto-curation), production cost observability.
>
> **Khi nào start M7:** sau khi M6 Wave 3 + Wave 4 đóng. Trước khi start, re-evaluate priority ranking — Viindoo addon indexing maturity + embedding cost pain points + cross-repo dependency surface area.

---

## Điều Hướng Tài Liệu

| | File | Nội dung |
|---|------|----------|
| ← | [`README.md`](README.md) | Điểm bắt đầu: tổng quan, onboard, hướng dẫn deploy |
| ↓ | [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Thiết kế kiến trúc đầy đủ: schema, pipeline, MCP tools |
| ↓ | [`docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md`](docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md) | Implementation plan chi tiết Milestone 1 — bắt đầu ở đây |
