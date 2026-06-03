# CLAUDE.md — Odoo Semantic MCP

## Mandatory context

@README.md

The above file is REQUIRED reading.

Tổng quan dự án, onboard user, system requirements, trạng thái milestone

## Dev Commands

```bash
make install           # Tạo venv tại ~/.venv/odoo-semantic-mcp + cài deps
make test              # Unit tests (không cần Docker)
make test-integration  # Integration tests (cần Docker + testcontainers)
make test-all          # Cả hai
make lint              # ruff check src/ tests/
make neo4j-up          # Start Neo4j thủ công (thay cho testcontainers)
```

Venv nằm tại `~/.venv/odoo-semantic-mcp` — không bao giờ tạo `.venv/` trong repo.

## Hai Nguyên Tắc Cốt Lõi

**Boil the Lake:** Làm đúng từ đầu rẻ hơn làm lại. Schema phải version-aware và cross-repo ngay từ đầu — migration sau khi có data tốn gấp 10 lần.

**Ship Wow Product:** Output MCP tool phải có cấu trúc cây rõ ràng, AI client đọc được ngay không cần parse thêm.

## Agent Rules — Bắt Buộc

**Read trước khi Edit/Write:** Phải dùng Read tool đọc file trong session hiện tại trước khi dùng Edit hoặc Write. Không dựa vào memory session trước — file có thể đã thay đổi.

**Search trước khi tạo mới:** Trước khi thêm function/class/constant/section mới → grep codebase confirm chưa có implementation tương tự. Duplicate implementation = source of truth conflict.

**Confirm trước khi xóa:** Xóa file, function, hoặc test nằm ngoài scope task được giao → confirm với user trước. Không "cleanup" ngoài phạm vi.

**Edit > Write:** Dùng Edit để sửa file có sẵn. Chỉ dùng Write khi tạo file mới hoàn toàn — Write overwrite toàn bộ không có warning.

## Pipeline — Không Cross-Import Ngang Hàng

```
scanner → registry → resolver → parser → (writer_neo4j | embedder → writer_pgvector) → server
```

`scanner` không import `parser`. `registry` không import `writer`. Mỗi file một trách nhiệm.

## Neo4j — C1 Schema (Critical)

Mỗi module tạo node Model riêng, không gộp theo tên model. Composite MERGE key bắt buộc cho Module/Model/Field/Method. `Model.is_definition` flag bậc 1 ranking heuristic, fallback `field_count DESC`. INHERITS edge `order` property preserves Pattern D mixin injection order.

**Chi tiết schema, MERGE patterns, ranking heuristic:** [`docs/huong-dan-stack.md §2 Schema C1`](docs/huong-dan-stack.md#schema-c1) và [`docs/adr/0013-defined-in-ranking-heuristic.md`](docs/adr/0013-defined-in-ranking-heuristic.md).

## Neo4j 5.x Gotchas

Các gotchas quan trọng nhất:
- `ORDER BY toFloat(v) DESC` cho version sort (NOT lexicographic).
- `COUNT { ()-[:INHERITS]->(m) }` (Neo4j 5.x), không phải `size(...)` (4.x).
- `.single()` chỉ khi chắc 1 row; `.data()` cho 0-N rows.
- **ORDER BY phải có deterministic tiebreak** (vd `ORDER BY rank_key DESC, mod.name ASC`) — đặc biệt cho ranking heuristic, xem [`docs/adr/0013`](docs/adr/0013-defined-in-ranking-heuristic.md).

**Full Cypher patterns + numeric compare:** [`docs/huong-dan-stack.md §2 Cypher gotchas`](docs/huong-dan-stack.md#cypher-gotchas).

## v8/v9 Enablement (M4.5 Phase 0)

Project hỗ trợ Odoo v8 → v19+. Hai pattern bắt buộc:

**1. ManifestFinder Protocol pluggable** (per [ADR-0002](docs/adr/0002-spec-schema-policy.md)):

```python
class ModernManifestFinder:  # rglob '__manifest__.py' (v11+)
class LegacyManifestFinder:  # rglob '__openerp__.py' (v8-9)
class DualManifestFinder:    # both (v10: 3 l10n modules still ship __openerp__.py)

def get_manifest_finder(odoo_version: str) -> ManifestFinder:
    major = int(odoo_version.split('.')[0])
    if major <= 9:
        return LegacyManifestFinder()
    if major == 10:
        return DualManifestFinder()  # dedupe preferring __manifest__.py
    return ModernManifestFinder()
```

**2. Era-aware parser_python.py**: Era1 (v8-9) dùng text-regex extract (`_parse_era1_text()`) + `FIELD_TYPES_LEGACY` (`function`, `related`, `dummy`, `sparse`) cho `_columns` dict. Era2 (v10+): AST như hiện tại. Chi tiết: [`docs/huong-dan-stack.md §Era parsing`](docs/huong-dan-stack.md#era-parsing).

**3. `_latest_version()` numeric compare** (per [ADR-0002](docs/adr/0002-spec-schema-policy.md)): KHÔNG hardcode "17.0". Trả `None` khi DB rỗng → caller hiển thị error rõ.

## Version-aware paths cho `index-core`

`parser_odoo_core.py` dùng `_resolve_core_paths()`: v8/v9 prefix `openerp/`; v19+ fallback sang `odoo/orm/`. Drop >20% CoreSymbol count vs prior version → nghi ngờ path refactor → update + regression test.

**Chi tiết:** [`docs/adr/0005-core-coverage-version-paths.md`](docs/adr/0005-core-coverage-version-paths.md).

## AST Parsing Gotcha

Dùng `tree.body` (top-level statements) cho manifest parsing — KHÔNG `ast.walk` (dive vào nested dict, trả sub-dict sai). `_inherit` luôn normalize về list; thiếu `_name` + có `_inherit` → `name = inherit[0]`.

**Full AST patterns:** [`docs/huong-dan-stack.md §AST parsing`](docs/huong-dan-stack.md#ast-parsing).

## FastMCP

`@mcp.tool()` wrap function thành `FunctionTool` — **không callable trực tiếp**. Test phải import `_resolve_model`, `_resolve_field`, `_resolve_method` (underscore prefix), không import tên tool.

## Testing

```python
# Mọi test integration cần Neo4j — thêm vào đầu file:
pytestmark = pytest.mark.neo4j

# Tất cả test data dùng version đặc biệt (không conflict với data thật):
TEST_VERSION = "99.0"

# Fixture clean_neo4j tự dọn trước/sau mỗi test — luôn dùng fixture này
```

Unit tests không cần Docker. Integration tests dùng testcontainers tự spin-up — không cần `docker compose up` thủ công.

## Upstream Warnings — Không Dùng suppress

Hai warnings từ testcontainers (`@wait_container_is_ready`) và một từ authlib (via fastmcp) là upstream issues. **Không dùng `filterwarnings`/`suppress`/`ignore`** — fix root cause hoặc chờ upstream fix. Đã documented trong `CONTRIBUTING.md`.

## Image Versions — Nguồn Sự Thật

`NEO4J_IMAGE` và `PG_IMAGE` trong `.env.example` là nguồn sự thật. Khi bump version: sửa **cả hai** `.env.example` VÀ `.github/workflows/nightly-smoke.yml` (CI hardcode vì Actions parse trước bất kỳ step nào). `tests/test_env_versions_sync.py` enforce sync tự động.

**Môi trường harness policy:** [`docs/adr/0006-environment-harness.md`](docs/adr/0006-environment-harness.md).

## Incremental Indexer (M6 Wave 2)

So sánh `git rev-parse HEAD` với stored `repos.head_sha`: bằng nhau → skip; force-push → full reindex; otherwise → diff filter via `incremental.compute_changed_module_paths()`. `head_sha` chỉ update sau full success. Dùng `--full` monthly để cleanup stale Module nodes từ rename/move.

**Chi tiết + caveats:** [`docs/adr/0007-incremental-indexer.md`](docs/adr/0007-incremental-indexer.md).

## Auto-Reseed Pattern Catalogue (M6 Wave 2)

`_SeedMeta` sentinel node lưu sha256 hash của `patterns.json` — skip re-embed khi unchanged. Wired vào `index_profile()` end. `--force` bypass sentinel. Failure log warning, KHÔNG fail indexer run. Xem [`docs/adr/0007`](docs/adr/0007-incremental-indexer.md).

## Cross-Profile Parallel Indexing (M6 Wave 2)

`--profile-workers 3 --max-workers 2` = 3 profiles parallel, mỗi profile 2 repo-workers nội bộ. Per-profile Postgres advisory lock đảm bảo safe; mỗi thread tự open pg_conn riêng. `progress=False` forced khi `profile_workers > 1`. Xem [`docs/adr/0006`](docs/adr/0006-environment-harness.md).

## SSH Auto-Clone (M6 Wave 4)

`POST /repos/{id}/clone` auto-clone SSH repos: key via `GIT_SSH_COMMAND` env (NOT `-i`), tempfile `mkstemp(0o600)` + `try/finally unlink`, project-local `known_hosts` pre-pinned for GitHub/GitLab/Bitbucket + `StrictHostKeyChecking=yes` (no TOFU — ADR-0035 D3 supersedes the old accept-new; self-hosted forges need manual pinning), full clone (no `--depth=1` — incremental needs history). Mutating git ops run under a per-repo Postgres advisory lock; re-clone of an existing checkout refreshes in place (fetch + reset --hard, ADR-0035 D2/D4). `clone_status`: manual/pending/cloned/error + UI poll 5s.

**Policy chi tiết:** [`docs/adr/0008-ssh-auto-clone.md`](docs/adr/0008-ssh-auto-clone.md).

## Auth — `is_admin` Source of Truth

`is_admin` must always be DB-sourced via `is_admin_session(request)` helper in `src/web_ui/auth.py`.
Never read `request.session.get("is_admin")` — the login flow does not write that key (intentional
per ADR-0011, which prescribed DB-sourced admin checks but did not name the helper). Reading an
absent key silently returns `False`, hiding all admin-visible data from legitimate admins. See
ADR-0026 for full context and design decisions.

## Tài Liệu Liên Quan

| File | Đọc khi nào |
|------|-------------|
| `TASKS.md` | Trước khi bắt đầu task mới — xem milestone nào đang active |
| `docs/thiet-ke-kien-truc.md` | Cần hiểu schema Neo4j, pipeline, MCP tool spec |
| `docs/huong-dan-stack.md` | Cần hiểu sâu stack: Neo4j patterns, AST gotchas, FastMCP tips |
| `docs/adr/` | Architecture Decision Records — đọc trước khi đụng schema/policy |
| `CONTRIBUTING.md` | Setup dev, chạy tests, workflow commit |

**ADR đã có:**

- `0001` schema evolution
- `0002` spec schema policy (CoreSymbol/LintRule/CLI per-version)
- `0003` pattern storage (PatternExample Neo4j + reuse embeddings)
- `0004` auth-web-ui-ssh-policy
- `0005` core coverage version paths
- `0006` environment harness (M6 Wave 1)
- `0007` incremental indexer (head_sha tracking, force-push fallback, module rename caveat, auto-reseed sentinel)
- `0008` SSH auto-clone (URL detection, key delivery via env, tempfile safety, project-local known_hosts, full clone)
- `0009` pattern catalogue community contribution (115 curated patterns, test-enforced minimum ≥80)
- `0010` embedding observability (call_count thread-safe; `embeddings_total`/`embeddings_by_chunk_type` now in `/ready` per ADR-0046 amendment; `null` in `/health` until first `/ready` hit)
- `0011` Web UI session auth (bcrypt cost=12, 8h TTL, cookie SameSite=strict)
- `0012` persona-skill-architecture (M7.5 — TRIGGER/PREFER/SKIP routing)
- `0013` Defined-in ranking heuristic (M5.5 — is_definition flag, field_count fallback, deterministic tiebreak)
- `0014` Astro unified UI (M8 — SSR pages + React islands, /admin/* gated by middleware → FastAPI /api/auth/verify)
- `0015` FastAPI pure JSON API (M8 — Jinja2 removed, /api/* JSON only, Astro renders all HTML)
- `0016` Profile hierarchy + Neo4j Option Y (parent_profile_id FK, ancestor profile array property, cycle-free + version-match validation)
- `0017` OAuth via arctic + oslo (state + PKCE, Google/GitHub, account linking on verified email)
- `0018` backup bundle contract (tar.gz: postgres.sql + neo4j.dump + fernet.enc + manifest.json)
- `0019` restore upload security (OWASP 10-item checklist, tarfile filter='data', pre-restore safety backup)
- `0020` FERNET key delivery + atomic rotation (central getter `src/crypto.py`; LoadCredential delivery + FERNET_KEY env fallback; totp_secrets co-rotation in same txn; env-var-name indirection --old-key-env/--new-key-env; fail-fast in prod via SystemExit(1); full rollback on any InvalidToken)
- `0021` admin audit log (@audit_action decorator, audit_cli context manager, 18+ routes)
- `0022` MFA TOTP (pyotp, Fernet-encrypted secrets, 10 HMAC backup codes, admin-required policy)
- `0023` Tool output completeness (M9 W-OSM Wave 1 — tree grammar contract, English-only language policy, truncation+total disclosure via `_render_capped`, next-step hint mapping for 18 drill-down tools)
- `0024` PATCH mutation policy (M9 follow-up — preserve head_sha on repo PATCH, reject name/version change on indexed profiles HTTP 409, ancestor+descendant version-match HTTP 422, TOCTOU UniqueViolation catch)
- `0025` CSS/SCSS stylesheet indexing (M9 Coverage Fill — `:Stylesheet` node, `:IMPORTS` edges, pgvector chunks)
- `0026` RBAC + key ownership (M9 follow-up — is_admin DB-sourced, deactivate authz hole, admin promote/demote, `/account` self-service)
- `0027` system-user deployment layout (production migration: personal → dedicated system user, ProtectHome policy, TMPDIR/tmpfs gotcha, uv venv no-pip, Docker Compose basename)
- `0028` discriminator consolidation (M11 — model_inspect/module_inspect/entity_lookup supersets, 10 flat tool deprecation shims, 1-major-release removal timeline)
- `0029` implicit session context (M11 - sticky odoo_version+profile_name, 24h sliding TTL, 5-sentinel defense, 3-tier resolution order; **#251 amendment: pin keyed per live MCP session `(api_key_id, mcp_session_id)` not api-key-alone [concurrent same-key sessions no longer clobber], stored IN-MEMORY as source of truth [`api_key_session_state` table now vestigial - no read/write, no migration, not dropped], `MCP_SESSION_PIN_MAX` oldest-evict + 24h in-memory idle TTL, `_nosession` fallback for stdio/header-less; profile read path now WIRED - pinned profile injected at top of `_scope`/`_effective_allowed`, narrowing-only + re-validated at read time via the ADR-0034 tenant choke, fail-closed, no new authz column; pins reset on server restart**)
- `0030` MCP Resources URI scheme (M11 — odoo:// URI grammar, 7 kinds + MIME mapping, in-memory LRU 1000 entries/300s TTL, top-100 popular-model discovery, Postgres cache deferred to M12)
- `0031` python-dotenv auto-load at CLI entry points (override=False, idempotent, main()-only to avoid pytest interference)
- `0032` parser version-dispatch registry (M11 — `VersionRegistry(min_major, max_major, handler)` replaces hard-coded era branches in parser_python/js/core/cli; supersedes prefix-selection part of ADR-0005)
- `0033` odoo.tools symbol coverage (curated, version-aware)
- `0034` multi-tenant pooled isolation + deploy-key credentials (M13 — shared-base + per-tenant overlay reusing ADR-0016 `profile[]`, NO tenant_id in Neo4j MERGE keys, mandatory fail-closed choke-point filter + Postgres RLS on embeddings, spec data stays global, per-tenant deploy-key; supersedes ADR-0016 D6 optional-filter + ADR-0029 profile-not-authz)
- `0035` git access model (M13 — subprocess git CLI kept over GitPython/dulwich/pygit2; per-repo advisory lock for mutating ops, known_hosts pinning replaces accept-new, fetch+reset-hard refresh, evaluate partial clone; supersedes ADR-0008 accept-new posture + revisits full-clone)
- `0036` license policy engine (M13 — config-driven SOFT block: `license_policy` map → serve/ingest_flagged/skip per license class; default OEEL-1=skip [Viindoo's own Odoo SA obligation], copyleft+OPL-1+unknown=serve under submitter ToS; visible `license_notice` to AI+human, never silent; written-permission = config flip, no code change; complements ADR-0034 read-side isolation)
- `0037` path portability (M13 — store file paths repo-relative (`addons/sale/...`) not server-absolute; `repos.local_path` is the only absolute anchor; relativize at writer boundary via transient `ModuleInfo.repo_root`, CoreSymbol/CLICommand relativize against source root in their parser; `_portable_path()` read-side safety-net at 8 render sites; css/scss/less chunks backfill `repo`/`repo_id`; `resources.py` stylesheet reconstructs absolute dynamically via `repo_id→local_path` → **server migration = local_path re-point, no reindex**; Stylesheet/LintViolation MERGE-key relative-keyed → post-reindex cleanup `ops/cleanup_absolute_path_nodes.cypher`)
- `0038` tenant RBAC web-UI write-side (W1 UI plan — `tenant_members` M:N join, `resolve_tenant_scope_web` helper, explicit `tenant_id` in request body (Option A stateless), admin-bypass absolute, W0 gates preserved, GUC-delimiter CHECK on `profiles.name`, `password_hash` nullable fold #176, D8 delete-tenant blocked when resources remain; precondition for W2 customer self-service portal)
- `0039` commercialization platform (M10B — control plane / data plane; `plans` table + `api_keys.plan_id` FK + `usage_counter`; plan-aware MCP middleware with RPM + monthly quota gating; Merchant-of-Record Polar.sh for international self-serve; extract-gradually posture; P0 schema shipped PR #200; P1-P3 Entitlement Activation API + Polar adapter + multi-IdP deferred; P1 billing single migration m13_014; **PR #223 reuses m13_015/m13_016 file numbers for new migrations: `plans.pricing_model` + `plans.min_seats` — deploy must run both**; **PR #224 reuses m13_017 for CRD withdrawal consent — deploy order m13_014→m13_015→m13_016→m13_017**)
- `0040` conftest Priority-2 fallback guard (TD-2 — testcontainers Priority-1 → direct-bolt Priority-2 fallback was auth-failing × 8 against a live Neo4j and tripping `auth_max_failed_attempts`; guard skips Priority-2 unless explicitly opted in, protecting prod instances on dev machines)
- `0041` unlimited plan + per-key quota/rpm overrides (M10B P0-ext — `'unlimited'` plan slug is the SSOT for unlimited access [D5]; `api_keys.rate_limit_override`/`quota_override` columns via m13_009; override 0 = zero-allowed NOT unlimited; admin web-UI for the 4 blocked use cases: grant-unlimited / upgrade-plan / per-key override / downgrade)
- `0042` Admin Settings module (M10B P1.5 — runtime config UI without redeploy; `app_settings`+`app_settings_history` [m13_010], `ee_modules` [m13_011], `patterns` [m13_012]; 3-tier `get_setting()` resolver L1 LRU 60s → L2 Postgres → L3 catalogue default; tenant `quota.*` override; hot-reload ≤60s TTL-poll; audit+rollback per ADR-0021; MFA fresh-gate per ADR-0022; web-only, tool count stays 24; **PR #223 adds Support category: `support.helpdesk_url` [28th catalogue entry]; PR #225 adds Analytics category: `analytics.ga_measurement_id` [29th catalogue entry] + extends `GET /api/site-config` to 5 fields**)
- `0043` MFA step-up freshness (fix: `mfa_verified_at` was never written → permanent 403 on all fresh-MFA gates; write contract: `totp_login` + `POST /api/auth/totp/step-up` both write session key + DB column; `get_mfa_freshness()` via `auth.mfa_freshness_seconds` app_setting [16th Tier-1 setting]; `StepUpMfaModal` frontend sentinel-detect + retry; supersedes implied step-up in ADR-0019/0022; tool count stays 24; **PR #223 adds 17th non-billing Tier-1 entry: `support.helpdesk_url`; PR #225 adds 18th non-billing Tier-1 entry: `analytics.ga_measurement_id` — `settings_registry.py` is the SSOT for current count**)
- `0044` token-bounded embedding (fix #226 — char-based chunking does not bound tokens; `estimate_tokens`/`split_by_token_budget` helpers in `embedder.py` shared with chunking layer; token cap in `_sliding` + all `make_*_chunks` helpers; MCP query cap `_cap_query_text`; truncation choke-point `_truncate_to_ctx` in `_BaseHttpEmbedder`; Bug B length-guard in `_embed_one`; resilient skip-log `_embed_chunks_resilient`; observability ADR-0010 contract unchanged; env: EMBEDDER_NUM_CTX/TOKEN_BUDGET/CHARS_PER_TOKEN)
- `0045` embedding provider abstraction (EmbedderClient structural Protocol with model/dim/num_ctx/chars_per_token attrs + embed/embed_async; `_BaseHttpEmbedder` shared machinery; `OpenAICompatEmbedder` for OpenAI/Voyage/TEI/vLLM/LiteLLM /v1/embeddings; `make_embedder()` factory via EMBEDDER_BACKEND env [default `ollama`]; `embedding_model`+`embedding_dim` columns in m13_018 — stamp every vector row + backfill pre-existing rows; fail-fast `EmbedderDimMismatch` guard in `embedding_guard.py`; **WARNING: switching embedding dimension requires full reindex**; tool count stays 24; migration m13_018 required)
- `0046` MCP embed concurrency + anti-hang (fix #227 production wedge ~11h: FastMCP calls `sync def` on event loop — one blocking embed froze all requests; fix: async hot path `embed_async` via `asyncio.to_thread`, 30s query timeout separate from 1200s batch timeout, `asyncio.Semaphore(EMBEDDER_MAX_CONCURRENCY)` cap + `EmbedOverloaded` fast-reject in 5s, uvicorn `limit_concurrency=EMBEDDER_MAX_CONCURRENCY*16` backpressure; `/health` = pure liveness no DB I/O [reads module-global cache]; `/ready` = new HTTP readiness probe cached 60s [NOT an MCP tool, tool count stays 24]; no hold-and-wait embed↔PG — embed completes before PG checkout)
