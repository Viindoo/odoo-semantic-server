# M7 Close-All + Go-Live Plan

> **Status:** ✓ DONE — M7 shipped 2026-05-11

> **Session tag:** `20260511-close-all` (orchestrator mints actual `osm-wt-<timestamp>-<rand>` at Phase 3a)
> **Plan author:** Opus 4.7 orchestrator (this conversation)
> **Spawning model strategy:** Haiku cho mechanical/plumbing, Sonnet cho design judgment
> **Status:** Draft v1 — awaiting user approval

---

## 1. Context

M1–M6 đã ship (xem `TASKS.md`). Sản phẩm chạy production tại `https://odoo-semantic.viindoo.com/`. Push này có 2 mục tiêu đồng thời:

1. **Close M7 "Lifecycle Wow"** — track ecosystem evolution: module rename GC, embedding cost observability, qualified-name resolver, cross-repo dep tracking, migration tool adoption.
2. **Go-live officially** — fix critical production-readiness gaps audited 2026-05-11: `setup_indexes()` race trên fresh deploy với `--profile-workers >1`, Neo4j backup/restore docs broken, nginx Option C stale comment, FERNET rotation step trỏ sai file path, missing pre-launch E2E checklist + disaster-recovery runbook, missing MCP HTTP-transport smoke tests che cover M1/M2/M2.5/M4 manual E2E items.

Outcome push này:
- Tất cả 14 MCP tools auto-verified qua MCP HTTP smoke test (close M1/M2/M2.5/M4 manual E2E).
- Mọi production ops procedure (backup/restore/FERNET rotation/DR) doc'd + tested.
- M7 thesis "Lifecycle awareness" hoàn thành.
- Web UI có session-based auth (defense-in-depth, không chỉ tin SSH tunnel).
- M3 recall benchmark có mock-test trong CI thường + nightly Ollama-gated.

Quyết định scope user xác nhận:
- **Include force:** qualified-name resolver, migration tool (yoyo), cross-repo dep tracking.
- **Web UI auth:** session-based vào FastAPI (Sonnet-L).
- **Recall:** mock CI + nightly Ollama-gated.
- **E2E:** smoke tests AUTO + manual pre-launch checklist (cả hai).
- **Originally deferred to M8:** rerank coeffs tuning, risk thresholds recalibration, USES_CORE_SYMBOL V0→V1 expansion, default_clone_dir URL query handling — **all closed in M7 final-closeout 2026-05-11**. Only `viindoo_equivalent_qname` auto-populate remains deferred (indefinite — see TASKS.md note + investigation 2026-05-10).

---

## 2. Approach

13 WIs, dispatch theo Pattern 3 (mixed parallel + 1 linear stack 3-deep) per CLAUDE.md orchestration workflow. Pipeline.py chain (3 WIs share file) làm linear; còn lại 10 WIs parallel off master. Mỗi WI: 1 worktree dưới `$WAVE_DIR=/tmp/osm-wt-<session-tag>/`, 1 branch `feat/m7-<wi-id>-<topic>`, 1 commit.

Mỗi WI self-test trong scope rồi `make test` đầy đủ trước commit. Integration phase: cherry-pick theo topological order vào `feat/m7-close-all` branch off master, resolve conflicts manual với Edit, push, PR, monitor CI, Opus review post-CI-green theo CLAUDE.md Phase 8, fix findings cùng PR ("Boil the lake").

Pre-launch verify: tao tự chạy manual checklist trên staging/production sau merge, signoff bằng commit `docs: pre-launch signoff <date>`.

---

## 3. Worktree Topology

### 3.1. Layout (commit graph)

```
master ┬── C1 (Haiku-XS) ── C4 (Sonnet-L) ── W14 (Sonnet-L)    [pipeline.py linear stack 3-deep]
       │
       ├── C2 (Haiku-XS)         [src/web_ui/templates/repos.html JS poll cap]
       ├── C3 (Haiku-S)          [src/mcp/server.py _NULL_HINT + test_output_snapshots.py]
       ├── C5 (Sonnet-M)         [embedder.py + writer_pgvector.py + health.py + dashboard.py]
       │
       ├── T1 (Haiku-M)          [NEW tests/test_smoke_e2e_mcp_http.py]
       ├── T2 (Sonnet-M)         [NEW tests/test_smoke_register_index_query_flow.py]
       ├── T4 (Haiku-XS)         [tests/test_patterns_schema.py expand needles]
       ├── T5 (Haiku-XS)         [tests/test_smoke_product_wow.py remove dead guards]
       │
       ├── W13 (Sonnet-L)        [parser_python.py qualified-name resolver]
       ├── W15 (Sonnet-M)        [src/db/migrate.py → yoyo-migrations adoption]
       ├── W16 (Sonnet-L)        [src/web_ui/ session-based auth middleware + login page]
       ├── R1 (Sonnet-M)         [tests/test_find_examples_recall_mock.py + nightly-smoke.yml]
       └── D1 (Sonnet-L)         [docs/deploy.md overhaul + 2 new docs files]
```

### 3.2. Worktree dir layout

```
$WAVE_DIR=/tmp/osm-wt-<session-tag>/
├── trunk/                                    ← master ref, never modified
├── m7-c1-setup-indexes-race/                 ← off master
├── m7-c4-gc-flag/                            ← off m7-c1 (commit on stack)
├── m7-w14-cross-repo-dep/                    ← off m7-c4 (commit on stack)
├── m7-c2-clone-poll-cap/                     ← off master
├── m7-c3-null-hint-snapshot/                 ← off master
├── m7-c5-embedding-observability/            ← off master
├── m7-t1-mcp-http-smoke/                     ← off master
├── m7-t2-register-index-query/               ← off master
├── m7-t4-ee-ref-test-expand/                 ← off master
├── m7-t5-ssh-dead-code-cleanup/              ← off master
├── m7-w13-qualified-name-resolver/           ← off master
├── m7-w15-migration-tool/                    ← off master
├── m7-w16-webui-session-auth/                ← off master
├── m7-r1-recall-mock-nightly/                ← off master
├── m7-d1-docs-overhaul/                      ← off master
└── m7-integration/                           ← integration consolidator off master
```

### 3.3. Invariants

- **Main repo isolation:** không `git checkout`/`commit`/`rebase`/`cherry-pick` ở `/home/tuan/git/odoo-semantic-mcp`. Lệnh duy nhất chạy với main-repo cwd là initial `git worktree add` để mint trunk.
- **Session-scoped paths:** `$WAVE_DIR` mint tại Phase 3a với tag duy nhất, KHÔNG hardcode.
- **Branch names:** `feat/m7-<wi-id>-<topic>` (e.g. `feat/m7-c1-setup-indexes-race`). Conflict detect tại push.

---

## 4. Per-WI Specs

Mỗi WI dưới đây có format: **Goal** · **Files** · **Test (business intent)** · **Dispatch hard rules**.

### C1 — setup_indexes() race fix (Haiku-XS, 1 file)

- **Worktree:** `m7-c1-setup-indexes-race`, branch `feat/m7-c1-setup-indexes-race`, off master.
- **Goal:** Fix `EquivalentSchemaRuleAlreadyExists` race khi `index_all(profile_workers > 1)` chạy trên fresh Neo4j. Workaround đã có trong `tests/test_indexer_profile_workers.py:389-401` (pre-call `setup_indexes` trước ThreadPoolExecutor) — promote vào `index_all()` production code.
- **Files:** `src/indexer/pipeline.py` (single edit ở đầu `index_all()`).
- **Test (business intent):** "Fresh Neo4j + `index-repo --all --profile-workers 2` trên 2 profiles → cả 2 index thành công, query model trả data 2 versions, không exception." → expand `test_indexer_profile_workers.py` thêm test xóa workaround pre-call (assert setup_indexes giờ idempotent từ `index_all`).
- **Dispatch:** Haiku. Hard rules CLAUDE.md §Phase 4 + commit format `[FIX] indexer: race-safe setup_indexes in index_all (M7 C1)`.

### C4 — `--gc` flag cho module rename (Sonnet-L, 3 files)

- **Worktree:** `m7-c4-gc-flag`, branch `feat/m7-c4-gc-flag`, **off m7-c1** (linear stack — share pipeline.py).
- **Goal:** Implement ADR-0007 §D5 follow-up. Sau full scan của repo, compare `Module` nodes (Neo4j) vs scanner output, DETACH DELETE stale nodes. Risk gate: chỉ chạy nếu scanner found ≥1 module (tránh wipe khi scan fail).
- **Files:**
  - `src/indexer/writer_neo4j.py` — new method `gc_stale_modules(repo_url, version, live_paths: set[str]) → int`
  - `src/indexer/pipeline.py` — `_index_repo` accept `gc: bool` param, call gc post-scan w/ risk gate
  - `src/indexer/__main__.py` — `--gc` CLI flag wired to `index-repo`
  - `docs/adr/0007-incremental-indexer.md` — update §D5 stance to "explicit GC available via --gc"
- **Test (business intent):**
  - "Index repo có module `addons/stock`; rename → `addons/inventory` trong fixture; run `--gc`; query Neo4j cho `Module {name:'stock'}` returns 0 rows AND `Module {name:'inventory'}` exists."
  - "Scanner fails (returns 0 modules) → `--gc` KHÔNG delete bất kỳ Module node nào (risk gate)."
  - new file `tests/test_indexer_gc.py` (3 tests: rename cleanup, risk gate, no-op on no-rename).
- **Dispatch:** Sonnet (multi-file design judgment). Commit `[ADD] indexer: --gc flag for module rename cleanup (M7 C4)`.

### W14 — Cross-repo dependency change tracking (Sonnet-L, 3 files)

- **Worktree:** `m7-w14-cross-repo-dep`, branch `feat/m7-w14-cross-repo-dep`, **off m7-c4** (linear stack).
- **Goal:** ADR-0007 §Out-of-scope follow-up. Sau `_index_repo` cho repo B, detect Neo4j Modules có `DEPENDS_ON` edges đến changed modules trong B từ repos KHÁC; reset `repos.head_sha = NULL` cho những repos đó (force re-index next run).
- **Files:**
  - `src/indexer/pipeline.py` — post-write hook trong `_index_repo` collects `changed_module_names`, calls cross-repo helper
  - `src/db/repo_registry.py` — new `reset_head_sha(repo_ids: list[int])` bulk helper
  - new file `src/indexer/cross_repo.py` — `find_dependent_repos(driver, version, module_names) → list[repo_id]` Cypher query
  - `docs/adr/0007-incremental-indexer.md` — note Out-of-scope item closed
- **Test (business intent):**
  - "Index repo A (has `base`) + repo B (depends on `base`); update file trong A's `base` module; run indexer A; assert B's `repos.head_sha = NULL` so next run reindexes B."
  - "Repo A change KHÔNG ảnh hưởng repo C nếu C không DEPENDS_ON A's modules → C.head_sha unchanged."
  - new file `tests/test_cross_repo_dep_propagation.py` (3 tests).
- **Dispatch:** Sonnet. Commit `[ADD] indexer: cross-repo dep change propagation (M7 W14)`.

### C2 — Clone-status poll cap (Haiku-XS, 1 file)

- **Worktree:** `m7-c2-clone-poll-cap`, off master.
- **Goal:** `pollCloneCells()` trong `repos.html` poll mãi mãi nếu repo stuck pending. Add max-tick (72 = 6 min @ 5s tick) → stop polling + show "Polling timed out, check server logs".
- **Files:** `src/web_ui/templates/repos.html` (only JS section, lines ~209-226).
- **Test (business intent):** browser test simulate clone stuck pending → sau 6 phút, cell display timeout message, network tab show no further requests. Extend `tests/test_web_ui_browser.py` thêm test (hoặc unit test JS logic nếu Playwright phức tạp).
- **Dispatch:** Haiku. Commit `[FIX] web_ui: cap clone-status polling at 6min (M7 C2)`.

### C3 — `_NULL_HINT` repr fix + cross-version snapshot (Haiku-S, 2 files)

- **Worktree:** `m7-c3-null-hint-snapshot`, off master.
- **Goal:** Bundle 2 fixes: (a) `_NULL_HINT` `!r` format gây nested quotes trong `_diff_method_across_versions` output — chuyển sang plain f-string; (b) `test_api_version_diff_output_contract` quá yếu (chỉ `startswith`) — thêm cross-version content guard.
- **Files:**
  - `src/mcp/server.py` `_diff_method_across_versions` (~line 1882-1890)
  - `tests/test_output_snapshots.py` thay test cũ + thêm `test_api_version_diff_cross_version_contract`
- **Test (business intent):** "Khi from_version có signature stored và to_version chưa stored → output chứa human-readable hint message KHÔNG có nested quote characters. Khi cả 2 versions có signature khác nhau → output show diff với both signatures." Bao gồm Neo4j fixture seeding 2 versions cùng method, signature khác nhau.
- **Dispatch:** Haiku. Commit `[FIX] mcp: NULL_HINT repr + cross-version diff snapshot (M7 C3)`.

### C5 — Embedding observability (Sonnet-M, 4 files)

- **Worktree:** `m7-c5-embedding-observability`, off master.
- **Goal:** Surface embedding cost metrics. `Qwen3Embedder.call_count` thread-safe counter, log line cuối indexer run, `/health` endpoint trả `embeddings_total: int` (Postgres `SELECT COUNT(*) FROM embeddings`).
- **Files:**
  - `src/indexer/embedder.py` — `Qwen3Embedder` add `call_count` attribute (threading.Lock for thread-safety), `FakeEmbedder` same shape
  - `src/indexer/writer_pgvector.py` `write_module_embeddings` return embed call count
  - `src/indexer/pipeline.py` `_index_repo` aggregate + log final embed metrics
  - `src/mcp/health.py` add `embeddings_total` field via `COUNT(*)` query
  - `src/web_ui/routes/dashboard.py` surface count in admin dashboard (optional UI widget)
  - `docs/adr/` — NEW `0010-embedding-observability.md` (decisions: call_count thread-safety, COUNT(*) cost, no Prometheus this push)
- **Test (business intent):**
  - "Index 1 module với 5 chunks → embedder.call_count >= 1, embeddings_total via `/health` >= 5"
  - "Index lại same module (skip path) → call_count delta == 0 (no re-embed)"
  - new file `tests/test_embedding_observability.py` (3 tests)
- **Dispatch:** Sonnet. Commit `[ADD] obs: embedding call_count + /health embeddings_total (M7 C5)`.

### T1 — MCP HTTP smoke test (Haiku-M, 1 new file)

- **Worktree:** `m7-t1-mcp-http-smoke`, off master.
- **Goal:** Auto-cover M1 (resolve_model), M2 (resolve_view), M4 (impact_analysis) E2E items. Test gọi MCP HTTP transport thật via `httpx.AsyncClient(app=mcp.streamable_http_app())` với JSON-RPC `tools/call` payload.
- **Files:** new `tests/test_smoke_e2e_mcp_http.py`. Bao gồm 3 tests, mỗi test seed Neo4j data minimal rồi gọi 1 trong 3 tools qua HTTP.
- **Test (business intent):** "Calling `tools/call` MCP method với name `resolve_model`, arguments `{model_name: 'sale.order', odoo_version: '99.0'}` trả về JSON-RPC response chứa user-visible tree format với module name + inheritance chain markers (`├─`/`└─`)."
- **Dispatch:** Haiku. Commit `[ADD] tests: MCP HTTP smoke covering M1/M2/M4 E2E (M7 T1)`.

### T2 — Register-index-query flow smoke (Sonnet-M, 1 new file)

- **Worktree:** `m7-t2-register-index-query`, off master.
- **Goal:** Auto-cover M2.5 E2E. Full admin workflow: `add_profile → add_repo → index_profile → _resolve_model`. Local tmp repo fixture.
- **Files:** new `tests/test_smoke_register_index_query_flow.py`. 1 large test wiring full pipeline.
- **Test (business intent):** "Admin runs add-profile, add-repo (local tmp git repo có 1 minimal Odoo module), index-repo → MCP `_resolve_model` cho module đó trả về module name + `Defined in:` block. Đảm bảo no data leak across profiles."
- **Dispatch:** Sonnet (multi-system wire-up). Commit `[ADD] tests: register-index-query E2E smoke (M7 T2)`.

### T4 — EE-ref test list expand (Haiku-XS, 1 file)

- **Worktree:** `m7-t4-ee-ref-test-expand`, off master.
- **Goal:** `test_patterns_schema.py` forbidden needles 5 → 16 (all `EE_CONFUSION.keys()`) + Viindoo equivalent module names. Close license-boundary blind spot.
- **Files:** `tests/test_patterns_schema.py` (import `EE_CONFUSION` from `src.data.ee_modules`).
- **Test (business intent):** "Pattern snippet `import` từ `helpdesk` hoặc reference `viin_document` fails test, blocked from catalogue."
- **Dispatch:** Haiku. Commit `[FIX] tests: expand EE-ref forbidden list to 16 keys (M7 T4)`.

### T5 — SSH dead-code test guard cleanup (Haiku-XS, 1 file)

- **Worktree:** `m7-t5-ssh-dead-code-cleanup`, off master.
- **Goal:** `test_smoke_product_wow.py:196,213` có `try/except ImportError` skip SSH-keys tests. M5 đã ship SSH key module → guards stale → enable tests.
- **Files:** `tests/test_smoke_product_wow.py`.
- **Test (business intent):** "Generate Ed25519 keypair → encrypt với FERNET → decrypt → match. Real SSH key flow protected end-to-end, không silent-skip."
- **Dispatch:** Haiku. Commit `[FIX] tests: enable SSH key smoke tests post-M5 (M7 T5)`.

### W13 — Qualified-name AST scope resolver (Sonnet-L, 2 files)

- **Worktree:** `m7-w13-qualified-name-resolver`, off master.
- **Goal:** Eliminate false-positive USES_CORE_SYMBOL khi local helper trùng tên Odoo deprecated API (e.g. local `def name_get` không phải override). Track `import` statements + scope map, filter refs by `qualified_name STARTS WITH 'odoo.'`.
- **Files:**
  - `src/indexer/parser_python.py` — new helper `_build_import_scope_map(tree) → dict[short_name, qualified_name]`, refactor `_extract_core_symbol_refs` to consult scope
  - new file `tests/test_parser_python_scope_resolver.py` — 4-6 tests: pure-Odoo call (positive), local-helper-same-name (negative), `from odoo.models import` short alias (positive), `import odoo as o` qualified (positive)
- **Test (business intent):** "Module có `def name_get(self): return 'local'` (utility) → `find_deprecated_usage` KHÔNG list module này. Module có `from odoo.models import name_get; obj.name_get()` → list module này. Module có `super().name_get()` trong Model subclass → list module này (real override)."
- **Dispatch:** Sonnet (AST scope resolver substantial). Commit `[ADD] parser: qualified-name AST scope resolver (M7 W13)`.

### W15 — Migration tool adoption (yoyo-migrations) (Sonnet-M, ~5 files)

- **Worktree:** `m7-w15-migration-tool`, off master.
- **Goal:** Adopt yoyo-migrations để future-proof non-additive schema changes. Refactor `src/db/migrate.py` SCHEMA_SQL block thành initial migration file.
- **Files:**
  - `pyproject.toml` — add `yoyo-migrations` dependency
  - `src/db/migrate.py` — replace SCHEMA_SQL inline + ALTER block với yoyo runner (`from yoyo import read_migrations, get_backend`)
  - new dir `migrations/` — `0001_initial.sql` (current schema baseline), `0002_indexer_jobs.sql` etc.
  - `docs/adr/0001-schema-evolution-policy.md` — revision noting yoyo adopted
  - update `Makefile` `migrate` target
- **Test (business intent):** "Re-run `python -m src.db.migrate` trên DB có pending migration → apply chỉ migration mới, mark applied, existing data unchanged. Re-run lại → 0 migrations applied (idempotent)."
- **Dispatch:** Sonnet (multi-file, schema-evolution-critical). Commit `[ADD] db: adopt yoyo-migrations (M7 W15)`.

### W16 — Web UI session-based auth (Sonnet-L, ~6 files)

- **Worktree:** `m7-w16-webui-session-auth`, off master.
- **Goal:** Add login form + session middleware vào FastAPI Web UI (port 8003) thay vì chỉ tin SSH tunnel/IP allowlist. Defense-in-depth.
- **Files:**
  - `src/web_ui/auth.py` — new module: session cookie middleware + password hash (bcrypt) + login/logout handlers
  - `src/web_ui/routes/login.py` — new file: GET/POST `/login`, GET `/logout`
  - `src/web_ui/templates/login.html` — new
  - `src/web_ui/middleware.py` — new (or extend if exists): redirect unauth `/` → `/login` with `next` param
  - `src/web_ui/__init__.py` — mount middleware + routes
  - `src/db/migrate.py` — new `webui_users` table (`username PRIMARY KEY, password_hash, created_at`)
  - `src/manager/__main__.py` — new `create-webui-user <username>` CLI (prompts password, bcrypt hash, store)
  - `src/web_ui/templates/base.html` (hoặc tương đương) — navbar logout link
  - `docs/adr/` — NEW `0011-webui-session-auth.md` (decisions: bcrypt cost, session TTL 8h, cookie SameSite=strict + httponly + secure)
  - `docs/deploy.md` — section "Web UI auth setup" + first-time create-webui-user step
- **Test (business intent):**
  - "Unauth GET `/repos` → 302 redirect `/login?next=/repos`"
  - "POST `/login` correct credentials → session cookie set, GET `/repos` 200 OK"
  - "POST `/login` wrong password → 401 + flash error, no cookie"
  - "GET `/logout` → cookie cleared, subsequent GET `/repos` → 302"
  - "Health endpoint `/health` exempt (no auth)" — but `/health` là MCP server port 8002, không phải Web UI port 8003 → confirm scope: Web UI có endpoint health riêng cần bypass? Check.
  - new file `tests/test_web_ui_auth.py` (6-8 tests) + extend `tests/test_web_ui_browser.py` (login flow via Playwright)
- **Dispatch:** Sonnet (auth design judgment, multi-file). Commit `[ADD] web_ui: session-based auth middleware (M7 W16)`.

### R1 — Recall mock CI test + nightly Ollama-gated job (Sonnet-M, ~3 files)

- **Worktree:** `m7-r1-recall-mock-nightly`, off master.
- **Goal:** M3 recall benchmark từ manual-only → wire nightly CI. Plus mock-recall trong CI thường để cover ranking logic.
- **Files:**
  - new `tests/test_find_examples_recall_mock.py` — FakeEmbedder + pre-computed embeddings (deterministic cosine distances) + same query dataset → assert ranking ordering correct (top-3 must contain expected results)
  - `.github/workflows/nightly-smoke.yml` — new job `recall-benchmark` with `OLLAMA_URL` secret check, skip cleanly nếu unset, run `pytest tests/test_find_examples_recall.py -m ollama`
  - `tests/test_find_examples_recall.py` — verify existing thresholds (VN≥0.75, EN≥0.80) still set, no changes needed
  - `docs/deploy.md` — section "Recall benchmark setup" (Ollama endpoint + threshold rationale)
- **Test (business intent):** "Given deterministic query 'compute tax based on partner country' + curated docs với known semantic ordering → top-3 results must contain expected docs in expected order. CI thường cover this without Ollama; nightly với Ollama thật assert recall@5 ≥ thresholds."
- **Dispatch:** Sonnet. Commit `[ADD] tests: recall mock CI + nightly Ollama-gated (M7 R1)`.

### D1 — Docs overhaul cho go-live (Sonnet-L, ~10 files)

- **Worktree:** `m7-d1-docs-overhaul`, off master.
- **Goal:** Single Sonnet WI consolidate ALL docs/deploy.md updates + 2 new files. Tránh conflict đa-author trên cùng deploy.md.
- **Files:**
  - `docs/deploy.md` §2.4 — fix Neo4j backup commands (`docker compose exec neo4j mkdir -p /data/backups`, dump command, `docker cp` to extract; restore commands)
  - `docs/deploy.md` §3.5 — unify systemd service file path (canonical: `docs/deploy/*.service`; remove conflict với `systemd/*.template`)
  - `docs/deploy.md` §13 (FERNET rotation) — fix `.env` → `/etc/odoo-semantic/webui.env` + add `sudo systemctl restart odoo-semantic-webui`
  - `docs/deploy.md` §2.4 (PG backup) — add `mkdir -p ~/backups`
  - `docs/deploy.md` §3 — add port 9999 nginx variant + HSTS + security headers + `listen 9999 ssl`
  - `docs/deploy.md` §7 (security checklist) — add: HSTS verify, Web UI port 8003 NOT reachable from internet (curl verify command), rate_limit_rpm configured, webui.env separately backed up, FERNET in secrets manager, Docker daemon not TCP-exposed, Web UI session-auth enabled (links to W16)
  - `docs/deploy/nginx.conf.example` — remove stale Option C comment "API Key M5 chưa implement"; promote Option C as primary
  - `odoo-semantic.conf.example` — add `[auth]` section with `rate_limit_rpm = 120` + comment
  - `docs/deploy.md` § new — log rotation: ship logrotate config + reference từ §3.6 cron section
  - new file `docs/deploy/logrotate.d/odoo-semantic` — logrotate rules for `/var/log/odoo-semantic-reindex.log`
  - new file `docs/deploy/pre-launch-checklist.md` — per-tool sign-off table covering 14 MCP tools + auth + rate limit + health + install page; bilingual headers
  - new file `docs/deploy/disaster-recovery.md` — backup frequency recommendations, restore order (PostgreSQL first; Neo4j optional/re-index from source), step-by-step commands, validation queries (`MATCH (m:Module) RETURN count(m)`, `SELECT COUNT(*) FROM embeddings`), estimated RTO (~2h cho 400 modules re-index)
  - `TASKS.md` — close all M7 items + add "Pre-launch signoff" row
  - `README.md` — link new disaster-recovery + pre-launch-checklist docs
- **Test (business intent):** doc-only WI; test = `tests/test_doc_sync.py` (existing) verify TASKS.md M7 closure markers + cross-link integrity. Manual reading by user as approval gate.
- **Dispatch:** Sonnet (multi-file consolidation). Commit `[DOC] go-live: backup/restore + checklist + DR runbook + security (M7 D1)`.

---

## 5. Integration Phase

Worktree `m7-integration` off master. Cherry-pick theo topological order:

```
1.  C1 (setup_indexes race)        ← pipeline.py chain root
2.  C4 (--gc flag)                  ← needs C1
3.  W14 (cross-repo dep)            ← needs C4
4.  C2 (clone-poll cap)             ← parallel
5.  C3 (NULL_HINT + snapshot)       ← parallel
6.  C5 (embedding observability)    ← parallel
7.  T4 (EE-ref test expand)         ← parallel
8.  T5 (SSH dead-code cleanup)      ← parallel
9.  W13 (qualified-name resolver)   ← parallel
10. W15 (migration tool)            ← parallel
11. T1 (MCP HTTP smoke)             ← parallel; may need W15 schema baseline if migration touches tests fixtures
12. T2 (register-index-query)       ← parallel; sequence after W15 if depends on schema state
13. R1 (recall mock + nightly)      ← parallel
14. W16 (Web UI session auth)       ← parallel; may touch repos.html navbar → coord with C2
15. D1 (docs overhaul)              ← parallel; last cherry-pick để doc reflect everything
```

**Conflict hot-spots predicted:**
- `pipeline.py`: C1/C4/W14 stack — auto-resolved by linear stack order.
- `repos.html`: C2 (JS poll) + W16 (navbar logout link). If W16 touches navbar include only, no conflict. Resolve at integration if both modify same template section.
- `migrate.py`: W15 (yoyo refactor) + W16 (add webui_users table). W16 must add table via yoyo migration file (not inline SQL). Sequence W15 before W16 during cherry-pick + W16 prompt explicitly says "use yoyo migration file pattern".
- `__main__.py` (manager): W16 adds `create-webui-user` subcommand. No conflict expected.
- `docs/deploy.md`: D1 consolidates all docs → D1 cherry-pick last so other WIs' inline doc changes (ADR-0007 D5 update, ADR-0010 new, ADR-0011 new) layered correctly.

---

## 6. Pre-Launch Verification

Sau merge PR vào master, tao (main session, hoặc human user) chạy:

**Phase A — Auto smoke (CI gate):**
- `make lint`, `make test`, `make test-integration`, `make test-all` đều pass.
- Nightly recall job (mock) green.

**Phase B — Manual production verify per `docs/deploy/pre-launch-checklist.md`:**
1. Fresh deploy trên staging VM theo `docs/deploy.md` §1-5 → measure time-to-first-query < 30 min.
2. Backup → simulated disaster → restore theo `docs/deploy/disaster-recovery.md` → verify all data + indexed state recoverable (RTO ≤ 2h).
3. Per-tool verify: gọi 14 MCP tools via Claude Code prompt trên production data + signoff.
4. Auth verify: `curl https://<domain>/mcp` without `X-API-Key` → 401; with valid key → 200.
5. Rate limit: 121 requests/min → 429.
6. Web UI session: unauth → `/login`; correct creds → dashboard; logout → cookie cleared.
7. Recall benchmark (nightly Ollama): VN≥0.75, EN≥0.80 confirmed.
8. Cert renewal dry-run: `certbot renew --dry-run` clean.
9. Logrotate dry-run: `logrotate -d /etc/logrotate.d/odoo-semantic` no errors.
10. Web UI port 8003 NOT externally reachable: `curl http://<server-public-ip>:8003/` from external IP → connection refused.

**Commit signoff:** `[DOC] M7 pre-launch signoff <YYYY-MM-DD> — all 10 verify items green` on master directly (orchestrator coordinates with user).

---

## 7. Test Discipline (Business Intent, Not Code Internals)

Mỗi WI test theo principle "bảo vệ nghiệp vụ, không bảo vệ implementation":

- **Sai (code-internal):** `assert _build_chain returns list of dict with 'module' key`.
- **Đúng (business-intent):** `assert resolve_model('sale.order', '17.0') output text chứa 'sale' module name + inheritance tree connectors '├─'/'└─'`.

Hard rule cho mỗi subagent prompt: test assertions phải verify USER-OBSERVABLE input → output, không internal data shape. Test fixture phải minimal nhưng đầy đủ cho flow thật.

Anti-drift snapshot tests (test_output_snapshots.py): expand coverage cho 14 MCP tools — C3 close gap api_version_diff (cross-version content guard).

---

## 8. Risk & Rollback

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `--gc` flag delete legitimate Module nodes khi scan fail | Med | High | Risk gate in pipeline (scan ≥1 module) + integration test simulate scan failure |
| `setup_indexes` race fix breaks existing `index_profile` callers | Low | Med | Pre-call in `index_all` only; per-profile path unchanged |
| Cross-repo dep tracking N+1 query trên Neo4j large graphs | Med | Med | Use Cypher set-based query (filter by version + module names list) — single round trip |
| Yoyo migration adoption breaks existing fresh-install flow | Med | High | Run `python -m src.db.migrate` 2 lần trong CI (idempotent check); doc rollback step |
| Web UI session auth lock admin out (forgot password) | Med | Med | CLI bypass: `create-webui-user --reset <username>` always available; doc this in deploy guide |
| Qualified-name resolver false-negative cho legitimate Odoo overrides | Med | Low | Conservative: nếu scope ambiguous → keep V0 behavior (flag); only filter when CONFIRMED non-Odoo |
| Migration tool conflict với existing data | High | High | First migration is baseline (no-op on existing prod DB); yoyo `--apply-from <revision>` baseline command documented |
| Production cert renewal fail post-deploy | Low | High | Pre-launch verify item 8 (certbot dry-run) + monitor alert |

**Rollback strategy:** Mỗi WI là 1 commit cherry-picked → revert specific commits via `git revert <sha>` on integration branch + new PR. Yoyo migration rollback: `yoyo rollback` command preserved.

---

## 9. Decision Points

Awaiting user approval qua ExitPlanMode. Sau approve:

1. Mint `$WAVE_DIR=/tmp/osm-wt-$(date +%Y%m%d-%H%M)-$(openssl rand -hex 3)/`.
2. `git worktree add $WAVE_DIR/trunk master` (run from main repo, ONE-TIME).
3. Per CLAUDE.md Phase 3-9 workflow: create worktrees, dispatch subagents parallel/sequential per topology, integration cherry-pick, push, PR, CI monitor, Opus review, fix findings, merge, cleanup.
4. User runs Phase B manual pre-launch verify.
5. Signoff commit on master.

---

## 10. Critical Files Touched (Reference Index)

| File | Touched by WI |
|------|---------------|
| `src/indexer/pipeline.py` | C1 (race fix), C4 (--gc), W14 (cross-repo dep) — linear stack |
| `src/indexer/writer_neo4j.py` | C4 (gc_stale_modules method) |
| `src/indexer/__main__.py` | C4 (--gc CLI flag) |
| `src/indexer/parser_python.py` | W13 (qualified-name resolver) |
| `src/indexer/embedder.py` | C5 (call_count) |
| `src/indexer/writer_pgvector.py` | C5 (return embed count) |
| `src/mcp/server.py` | C3 (_NULL_HINT format) |
| `src/mcp/health.py` | C5 (embeddings_total) |
| `src/web_ui/templates/repos.html` | C2 (poll cap), W16 (navbar logout — coordinate) |
| `src/web_ui/auth.py` | W16 (NEW) |
| `src/web_ui/routes/login.py` | W16 (NEW) |
| `src/web_ui/middleware.py` | W16 (auth middleware) |
| `src/web_ui/routes/dashboard.py` | C5 (embedding count widget) |
| `src/db/migrate.py` | W15 (yoyo refactor), W16 (webui_users table via yoyo) |
| `src/db/repo_registry.py` | W14 (reset_head_sha bulk) |
| `src/manager/__main__.py` | W16 (create-webui-user) |
| `pyproject.toml` | W15 (yoyo-migrations dep) |
| `docs/deploy.md` | D1 (multiple sections) |
| `docs/deploy/nginx.conf.example` | D1 |
| `docs/deploy/pre-launch-checklist.md` | D1 (NEW) |
| `docs/deploy/disaster-recovery.md` | D1 (NEW) |
| `docs/deploy/logrotate.d/odoo-semantic` | D1 (NEW) |
| `docs/adr/0001-schema-evolution-policy.md` | W15 (revision) |
| `docs/adr/0007-incremental-indexer.md` | C4 + W14 (D5 + out-of-scope updates) |
| `docs/adr/0010-embedding-observability.md` | C5 (NEW) |
| `docs/adr/0011-webui-session-auth.md` | W16 (NEW) |
| `odoo-semantic.conf.example` | D1 (rate_limit_rpm) |
| `Makefile` | W15 (migrate target) |
| `.github/workflows/nightly-smoke.yml` | R1 (recall job) |
| `tests/test_indexer_profile_workers.py` | C1 (remove pre-call workaround) |
| `tests/test_indexer_gc.py` | C4 (NEW) |
| `tests/test_cross_repo_dep_propagation.py` | W14 (NEW) |
| `tests/test_output_snapshots.py` | C3 (api_version_diff cross-version) |
| `tests/test_smoke_e2e_mcp_http.py` | T1 (NEW) |
| `tests/test_smoke_register_index_query_flow.py` | T2 (NEW) |
| `tests/test_patterns_schema.py` | T4 (expand needles) |
| `tests/test_smoke_product_wow.py` | T5 (remove SSH guards) |
| `tests/test_parser_python_scope_resolver.py` | W13 (NEW) |
| `tests/test_embedding_observability.py` | C5 (NEW) |
| `tests/test_web_ui_auth.py` | W16 (NEW) |
| `tests/test_web_ui_browser.py` | W16 (Playwright login flow) |
| `tests/test_find_examples_recall_mock.py` | R1 (NEW) |
| `tests/test_doc_sync.py` | D1 (verify M7 closure markers) |
| `TASKS.md` | D1 (close M7) |
| `README.md` | D1 (link new docs) |
| `migrations/` | W15 (NEW dir) |

---

## 11. Out of Scope (Confirmed Defer M8+)

> **Update 2026-05-11 (M7 final-closeout):** items 1-3 + 5 below were re-scoped into M7 final-closeout sweep after a 3-Sonnet survey re-evaluated the cost of the work vs the deferral. Synthetic eval datasets sufficed for A/B; D was a cap-15 curation; G a small bug fix. Only item 4 (`viindoo_equivalent_qname`) remains deferred indefinitely.

- ~~Rerank coefficients tuning (`src/mcp/server.py:489`)~~ → **closed M7 final-AB**
- ~~`_compute_risk` thresholds recalibration (`src/mcp/server.py:683`)~~ → **closed M7 final-AB** (current 10/4 thresholds validated optimal vs candidate sweep)
- ~~USES_CORE_SYMBOL V0→V1 expansion (`src/indexer/parser_python.py:36`)~~ → **closed M7 final-D** (14 entries final, capped per false-positive risk)
- `viindoo_equivalent_qname` auto-populate via graph traversal — **still deferred (indefinite)**: needs Viindoo profile indexed at scale + manifest feature-tag heuristic
- ~~`default_clone_dir` URL query handling~~ → **closed M7 final-G** (urlparse strip; minor user-facing edge case but cheap to ship)
- Prometheus `/metrics` endpoint — overkill for current scale
- Per-API-key rate limit — global rate_limit_rpm acceptable
- Indexed `indexer_jobs` cleanup cron — manual SQL documented sufficient
