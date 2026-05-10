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

## Pipeline — Không Cross-Import Ngang Hàng

```
scanner → registry → resolver → parser → (writer_neo4j | embedder → writer_pgvector) → server
```

`scanner` không import `parser`. `registry` không import `writer`. Mỗi file một trách nhiệm.

## Neo4j — C1 Schema (Critical)

**Mỗi module tạo node Model riêng**, không gộp theo tên model:

```cypher
// ĐÚNG — 2 nodes, nối bằng INHERITS
(:Model {name: 'sale.order', module: 'sale',      odoo_version: '17.0'})
(:Model {name: 'sale.order', module: 'viin_sale', odoo_version: '17.0'})

// SAI — gộp vào 1 node sẽ tạo self-loop khi extension MERGE
(:Model {name: 'sale.order', odoo_version: '17.0'})
```

**Composite key cho MERGE:**
- Module: `(name, odoo_version)`
- Model: `(name, module, odoo_version)`
- Field/Method: `(name, model, module, odoo_version)`

MERGE chỉ dùng key, SET properties riêng — không bao giờ đưa mutable props vào MERGE key.

**Model.is_definition flag** — set bởi parser/writer khi:
- `_name` được declare explicit trong class body (`had_explicit_name=True`), AND
- `name NOT IN inherit_list` (loại trừ redeclare extensions Pattern C/D).

Dùng làm tier 1 ranking "Defined in" trong `resolve_*` (post-reindex authoritative).
Pre-reindex fallback chính là `field_count DESC` (số Field declared cho model trong
mỗi module — base luôn nhiều nhất). Xem `docs/adr/0004`.

**INHERITS edge `order` property** — `r.order` = list-index trong `_inherit`,
preserving Pattern D mixin injection order cho future MRO reconstruction.
Resolver dùng `coalesce(r.order, 0)` cho data pre-reindex.

## Neo4j 5.x Gotchas

```cypher
-- Sắp xếp version (numeric, không phải lexicographic):
ORDER BY toFloat(v) DESC               -- ĐÚNG cho Cypher
ORDER BY v DESC                        -- SAI ("9.0" > "17.0")

-- Sắp xếp chính xác hơn (split major.minor):
ORDER BY toInteger(split(v,'.')[0]) DESC, toInteger(split(v,'.')[1]) DESC
                                        -- ĐÚNG nhất, robust với "8.0", "17.0", "20.0"

-- Đếm pattern expression:
ORDER BY COUNT { ()-[:INHERITS]->(m) } -- ĐÚNG (Neo4j 5.x)
ORDER BY size(()-[:INHERITS]->(m))     -- SAI (Neo4j 4.x, CypherSyntaxError)
```

Dùng `.single()` chỉ khi chắc chắn có đúng 1 row. Dùng `.data()` cho 0-N rows.
`single()` trả `None` nếu không có row → dùng để phát hiện unresolved edge.

**ORDER BY phải có deterministic tiebreak** khi nhiều row có thể tie:

```cypher
-- ĐÚNG — tiebreak bằng column ổn định:
ORDER BY rank_key DESC, mod.name ASC

-- SAI — Cypher không guarantee order khi tie, gây bug ngầm:
ORDER BY rank_key DESC
```

Đặc biệt áp dụng cho ranking heuristic trong `resolve_*` (xem `docs/adr/0004`).

**Python-side version compare** (cùng nguyên tắc):

```python
# ĐÚNG — numeric tuple compare:
sorted(versions, key=lambda v: tuple(int(p) for p in v.split('.')), reverse=True)

# SAI — string compare ("9.0" > "17.0" → False vì lexicographic):
sorted(versions, reverse=True)
```

## v8/v9 Enablement (M4.5 Phase 0)

Project hỗ trợ Odoo v8 → v20+. Hai pattern bắt buộc:

**1. ManifestFinder Protocol pluggable** (per [ADR-0002](docs/adr/0002-spec-schema-policy.md)):

```python
class ModernManifestFinder:  # rglob '__manifest__.py' (v10+)
class LegacyManifestFinder:  # rglob '__openerp__.py' (v8-9)

def get_manifest_finder(odoo_version: str) -> ManifestFinder:
    major = int(odoo_version.split('.')[0])
    return LegacyManifestFinder() if major <= 9 else ModernManifestFinder()
```

Odoo v8/v9 dùng `__openerp__.py` thay `__manifest__.py`. Pluggable finder dispatch theo `odoo_version` — landed M4.5 WI1.1.

**2. Era-aware parser_python.py** (giống `parser_js.py` era pattern):

- Era1 (v8-9, Python 2 syntax): `_parse_era1_text()` text-regex extract `_name`, `_inherit`, `_columns` dict. Skip method body. Graceful fallback khi `ast.parse` raise `SyntaxError` (Python 2 syntax `print x`, `except E, e:`, etc.).
- Era2 (v10+): AST như hiện tại.

`FIELD_TYPES_LEGACY` set bao gồm `function`, `related`, `dummy`, `sparse` cho Era1 — Odoo v8-v10 declare field qua `_columns = {...}` dict thay vì class-level attribute.

**3. `_latest_version()` numeric compare** (per ADR-0002):

KHÔNG hardcode "17.0" fallback. Trả `None` khi DB rỗng → caller hiển thị error rõ "No data indexed. Run indexer first."

## Version-aware paths cho `index-core`

`parser_odoo_core.py` dùng `_resolve_core_paths(odoo_root, logical_path, version)` để map allow-list paths:

- **v8/v9**: prefix `openerp/` thay `odoo/` (Odoo namespace rename ở v10).
- **v19+**: `odoo/{fields,models,api}.py` đã thành package directories — fallback sang `odoo/orm/{fields*,models,decorators,environments}.py`.

Khi Odoo release major mới, kiểm tra CoreSymbol count diff vs version trước. Drop > 20% trong bất kỳ kind nào nghi ngờ file path refactor → update `_resolve_core_paths` + add regression test. Xem `docs/adr/0005`.

## AST Parsing Gotcha

```python
# ĐÚNG — chỉ lấy top-level statements
for stmt in tree.body:
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Dict):
        return ast.literal_eval(stmt.value)

# SAI — ast.walk dive vào nested dict, trả về sub-dict không phải manifest
for node in ast.walk(tree):
    if isinstance(node, ast.Dict): ...
```

`ast.walk` chỉ dùng khi cần đi vào bên trong function body. `tree.body` cho manifest parsing.

`_inherit` có thể là string hoặc list → luôn normalize về list. Nếu thiếu `_name` nhưng có `_inherit` → `name = inherit[0]` (Odoo convention).

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

## Orchestrated Multi-Subagent Workflow

Khi 1 milestone/wave có ≥4 work-items (WIs) có dependencies, dùng pattern này thay vì làm tuần tự. Proven qua M5.5 closeout · M6 Wave 1 (8 WIs, depth-2 stack) · M6 Wave 2 (8 WIs, 3 chains, depth-5 deepest).

### Phase 1 — Investigation (parallel Sonnet)

Trước plan, spawn 1-4 Sonnet subagent song song (1 message với multiple `Agent` tool calls), mỗi cluster 1 agent. Prompt self-contained: file paths cụ thể + gap questions + output format `<600 words` + `DO NOT write code`. Findings inform plan.

### Phase 2 — Plan mode + AskUserQuestion

Trong plan mode: read findings, draft topology + WI list. Use `AskUserQuestion` cho scope decisions (in-scope / defer rules / worktree path) **trước** `ExitPlanMode`. Plan file structure: Context · Approach · Topology · Shared Context · per-WI specs · Integration Phase · Risk & Rollback · Decision Points.

### Phase 3 — Worktree topology

#### 3a. Two invariants

**Main repo isolation** — KHÔNG `git checkout`/`commit`/`rebase`/`cherry-pick` ở main repo. User có thể đang trên branch khác, working tree dirty; workflow tuyệt đối không đụng. Lệnh duy nhất chạy với main-repo cwd là initial `git worktree add` để mint trunk; sau đó mọi git op chạy qua trunk hoặc WI worktree.

**Session-scoped paths** — KHÔNG hardcode worktree dir cố định. Multi-session sẽ collision. Mint session tag (timestamp + random) → `$WAVE_DIR=/tmp/<project>-wt-<tag>/` riêng cho phiên hiện tại.

Layout sau setup:

```
$WAVE_DIR/
├── trunk/                       ← branch=master, never modified, used as base ref
├── m<N>-w<X>-<topic>/           ← per-WI worktree (1 commit, 1 branch)
├── m<N>-w<Y>-<topic>/
└── wave-<N>-integration/        ← cherry-pick consolidator
```

Trunk worktree đóng vai canonical "master" reference. Sau khi trunk tồn tại, mọi git op dispatch qua trunk; main repo path không cần được truy cập nữa cho phase implement.

Branch names clean (`feat/m<N>-w<X>-<topic>`). Branch collision detect tại push (`--force-with-lease`).

#### 3b. Topology patterns

Mỗi WI = 1 worktree under `$WAVE_DIR`, 1 branch, 1 commit. Diagrams show **commit graph** — `master` = ref pointed-to by trunk worktree.

**Pattern 1 — All independent (parallel off trunk):**

```
master ┬── WI1
       ├── WI2
       └── WI3
```

WIs độc lập (không share files, không share schema). Dispatch parallel trong 1 message với multiple `Agent` calls.

**Pattern 2 — Linear stack (each depends on prior):**

```
master ── WI1 ── WI2 ── WI3 ── WI4
```

Dùng khi WIs share files OR step depend trên previous output. Wave 2 Chain A = 5-deep linear (schema → field → scanner → writer → module → pipeline). Dispatch tuần tự (next WI sau khi parent landed).

**Pattern 3 — Mixed parallel + linear (Wave 2 actual):**

```
master ┬── WI1 ── WI2 ── WI3 ── WI4 ── WI5    (Chain A, depth-5)
       ├── WI6 ── WI7                          (Chain B, depth-2)
       └── WI8                                  (Chain C, depth-1)
```

Each chain dispatched independently. Inside chain: sequential.

**Pattern 4 — Diamond DAG (multi-parent dependency, "synthetic base"):**

```
master ┬── WI1 ────────────────────┐
       ├── WI2 ──────┐              │
       │             ▼              │
       │       feat/wi4-base ── WI4 ┬── WI5 ──────┐
       │       (= master + cherry-pick WI1+WI2)   │
       │                             │            ▼
       │                             └── WI6 ── feat/wi7-base ── WI7
       │                                          ▲   (= master + cherry-pick WI3+WI5+WI6)
       └── WI3 ───────────────────────────────────┘
```

Khi WI có ≥2 parent dependencies, orchestrator tạo synthetic `feat/<wi>-base` branch off master + cherry-pick TẤT CẢ deps lên đó, RỒI spawn WI worktree off base. Pattern generalize cho mọi DAG via topological sort.

WI4's commit = delta over `wi4-base` only (KHÔNG chứa WI1+WI2 changes — đã ở base). Khi cherry-pick WI4 lên integration đã có WI1+WI2 → no double-apply, no conflict.

Final integration: cherry-pick các WI commits theo **topological order** vào `wave-<N>-integration` worktree off master.

### Phase 4 — Subagent dispatch

**Sonnet** cho design judgment (logic mới, refactor, pipeline integration, multi-file). **Haiku** cho mechanical work (schema column add, single-file plumbing, pure data type plumbing).

Mỗi prompt MUST chứa: worktree path tuyệt đối + branch · stack history (parent commits + what they added, including synthetic base nếu có) · Goal + Files list with concrete edits · Hard rules block · Validation commands · Commit message format · Output format requirements.

Hard rules block (copy-paste vào mỗi prompt):

```
- DO NOT push (orchestrator integrates)
- DO NOT switch branches
- DO NOT touch files outside list
- DO NOT add `from __future__ import annotations` (Python 3.12 enforce, Wave 1 H4)
- Use `str | None` not `Optional[str]` (ruff UP)
- Single commit per WI
- Commit format: `[TYPE] scope: description (M<N> W<X>)` (bracket style, NOT conventional)
```

Spawn parallel agents trong 1 message khi WIs độc lập. Stack chains dispatch tuần tự (next WI sau khi parent landed). Synthetic-base WIs: orchestrator cherry-pick deps + verify clean state TRƯỚC khi dispatch agent.

### Phase 5 — Integration cherry-pick

Tạo `feat/m<N>-wave-<X>` worktree off master. Cherry-pick từng WI commit theo topological order. Conflict resolve manual với Edit tool — thường keep-both cho additive changes, merge logic cho branching.

Cross-impact integration fixes (vd Wave 2 W2-4 ↔ W2-8 mocks): commit mới trên integration branch — KHÔNG quay lại sửa WI worktrees.

### Phase 6 — Local validation BEFORE push

Run `make lint` + `make test` (unit) + `make test-integration` trên integration worktree. Local pgvector skip baseline ~25-30 tests (CI có pgvector → ít skip hơn). Browser skip qua conftest hook nếu chromium missing.

### Phase 7 — PR + CI monitor

Push integration branch → `gh pr create`. Body structure: `## Summary` (1-3 bullets) · per-WI bullets · `## Test plan` checklist.

Monitor `gh pr checks <N>`. Re-poll qua `ScheduleWakeup(270s, "<<autonomous-loop-dynamic>>")` — under prompt-cache TTL (300s).

CI fail: pull failed-job logs, fix as new commit trên integration branch, squash multi-round fixes via `--fixup` + `--autosquash`, force-push với `--force-with-lease`.

### Phase 8 — Plan adherence review (Opus)

Post-CI-green, spawn 1 Opus subagent review PR. 4 passes: plan adherence per WI · test coverage adequacy · latent bug hunt (read implementation) · documentation drift. Output severity-tagged findings (HIGH/MED/LOW) với file:line refs + fix sketches.

User principle **"Boil the lake"**: fix tất cả findings trong cùng PR (brief Sonnet for fixes), không defer Wave sau.

### Phase 9 — Merge + cleanup

`gh pr merge <N> --rebase --delete-branch`. Local error `"master is already used by worktree"` là harmless (trunk có master checked out) — verify server-side success qua `gh pr view --json state,mergedAt,mergeCommit`.

Cleanup: sync trunk via `pull --ff-only origin master` (KHÔNG ở main repo), remove WI + integration worktrees, remove trunk last, delete branches, xóa `$WAVE_DIR`.

### Concurrency hazards (multi-session)

- **Worktree path**: dùng session tag (Phase 3a). KHÔNG hardcode shared path cố định.
- **Branch namespace**: branch name conflict trên remote chỉ phát hiện tại push. 2 sessions cùng làm same Wave # → cần coordinate qua human channel hoặc đặt suffix.
- **`TEST_VERSION='99.0'`**: integration tests dùng "99.0" như carve-out — testcontainers spawn ephemeral containers per session (default behavior) nên KHÔNG share local Postgres/Neo4j giữa sessions là an toàn. Nếu phải dùng shared local DB → carve riêng `99.{session-tag-short}` hoặc serialize sessions.
- **Plan file**: Claude session có own plan file path — không collide.
- **`docker compose up -d` local**: nếu 2 sessions cùng start local stack → port collision (7687, 5432). Pattern: dùng testcontainers (per-session ephemeral) thay vì local docker compose cho phase implement.
- **gh CLI auth**: shared user-level token. Sequential GH operations atomic on remote.

### Hard-won lessons (chronological)

- **CI service container env explicit:** workflow YAML phải set DB passwords explicit — `.env.example` không readable parse-time bởi GitHub Actions.
- **PG DSN IPv4:** `127.0.0.1` không `localhost` (resolves to `::1` trên ubuntu-latest, port mapping IPv4-only → conn refused).
- **Playwright CI:** `playwright install --with-deps chromium`. Conftest hook skip cleanly nếu chromium missing locally (Wave 1 fix).
- **Test data leak:** một số fixture (vd `clean_pg`) không wipe pgvector tables — earlier tests có thể leak rows. Use pre/post count **delta** thay vì absolute `count == 0` (Wave 2 PR #39 round-3 lesson).
- **Linear stack > parallel branches off master** khi WIs share files. Wave 1 P3-off-P1, Wave 2 Chain A 5-deep linear.
- **Synthetic base for multi-parent**: WI depends on ≥2 prior WIs → `feat/<wi>-base` branch with cherry-picks before WI dispatch. Each WI commit remains delta-only.
- **`-rs` pytest flag** chỉ enable khi CI fail với silent skips — exposes root cause với cost noisy logs.

### When to skip this pattern

- 1-3 WI changes: fix trực tiếp trên 1 feature branch.
- Pure docs change: single commit + PR.
- Bug hotfix without investigation: same.

## Upstream Warnings — Không Dùng suppress

Hai warnings từ testcontainers (`@wait_container_is_ready`) và một từ authlib (via fastmcp) là upstream issues. **Không dùng `filterwarnings`/`suppress`/`ignore`** — fix root cause hoặc chờ upstream fix. Đã documented trong `CONTRIBUTING.md`.

## Image Versions — Nguồn Sự Thật

`NEO4J_IMAGE` và `PG_IMAGE` trong `.env.example` là nguồn sự thật cho local dev (testcontainers đọc các biến này). `docker-compose.yml` đọc cả hai qua `${NEO4J_IMAGE:-...}` và `${PG_IMAGE:-...}`. Khi bump version: sửa `.env.example`.

**CI exception:** GitHub Actions service containers được khởi động *trước* bất kỳ step nào — không thể đọc `.env.example` tại parse time. Do đó `.github/workflows/nightly-smoke.yml` phải hardcode image version. Khi bump `NEO4J_IMAGE` hoặc `PG_IMAGE`: cập nhật **cả hai** `.env.example` VÀ `.github/workflows/nightly-smoke.yml` (tất cả jobs có service container).

`tests/test_env_versions_sync.py` kiểm tra tự động: parse `.env.example` → assert `nightly-smoke.yml` chứa đúng image strings. Test fail = `.env.example` và workflow bị lệch.

## Incremental Indexer (M6 Wave 2)

`pipeline._index_repo` so sánh `git rev-parse HEAD` với stored `repos.head_sha`:
- **Bằng nhau** → log "Repo unchanged — skipping" + return ngay (zero-cost).
- **Force-push detected** (`is_ancestor` fail) → log warning + full reindex.
- **Otherwise** → `git diff --name-only old..new` → filter scan results to changed module dirs only via `incremental.compute_changed_module_paths()`.
- **head_sha chỉ update sau full success** — partial failure mid-write giữ nguyên head_sha cũ → next run retry.

**Module node** thêm property `last_commit_sha` (NOT trong MERGE key — mutable SET props per ADR-0001). Cho phép query "module này last touched commit nào" trong `resolve_model` etc.

`--full` flag (`python -m src.indexer index-repo --full ...`) bypass skip + diff filter — dùng định kỳ (recommend monthly) để clean up stale Module nodes từ rename/move.

Module rename caveat: rename dir → cả old và new path xuất hiện trong diff, cả hai re-index. Stale Neo4j Module node cho old path còn lại; dùng `--full` để cleanup. Future Wave: explicit cleanup pass.

## Auto-Reseed Pattern Catalogue (M6 Wave 2)

`seed_patterns.main()` dùng `_SeedMeta {key:'patterns'}` Neo4j sentinel node lưu sha256 hash của `patterns.json`. Khi current_sha == stored_sha → skip (avoid re-embedding 54 patterns). Wired vào `index_profile()` end → mỗi `index-repo` / `index-all` run auto-reseed (cheap khi unchanged).

`--force` flag bypass sentinel: `python -m src.indexer seed-patterns --force`. Auto-reseed failure log warning nhưng KHÔNG fail indexer run.

## Cross-Profile Parallel Indexing (M6 Wave 2)

```bash
python -m src.indexer index-repo --all --profile-workers 3 --max-workers 2
```

3 profiles parallel (Wave 2 W2-8), each profile sử dụng 2 repo-workers nội bộ (Wave 1 P3). Per-profile Postgres advisory lock (Wave 1 P1) đảm bảo safe. Each profile-worker thread **phải tự open pg_conn** (psycopg2 thread-safety). `progress=False` forced khi `profile_workers > 1` (avoid tqdm bar collision).

## SSH Auto-Clone (M6 Wave 4)

Web UI `POST /repos/{id}/clone` auto-clone SSH repos using FERNET-decrypted key (M5 ADR-0004). Policy details in `docs/adr/0008`.

**URL detection:** regex `^git@|^ssh://` for SSH; HTTPS manual (out of scope M6).

**Key delivery:** `GIT_SSH_COMMAND` env var, NOT `-i` flag — prevents leak in `/proc/<pid>/cmdline`.

**Tempfile invariant:** `mkstemp(mode=0o600, prefix='osm-ssh-')` + `try/finally os.unlink()` — cleanup on success/failure/exception. SIGKILL leak accepted (system tmpdir cleanup on reboot).

**Known_hosts:** project-local `~/.local/share/odoo-semantic-mcp/known_hosts` (NEVER system `~/.ssh/`), multi-tenant safe. Policy: `StrictHostKeyChecking=accept-new` auto-persists fingerprints.

**Full clone** (no `--depth=1`) — ADR-0007 incremental indexer needs full git history. Trade-off: 3–10 min per large repo; handled via background subprocess.

**Lifecycle:** `clone_status` column (manual/pending/cloned/error) + UI poll every 5s. Cloner subprocess updates DB on complete. Schema delta: `repos.ssh_key_id` FK + `repos.clone_status` enum.

## Tài Liệu Liên Quan

| File | Đọc khi nào |
|------|-------------|
| `README.md` | Tổng quan dự án, onboard user, system requirements, trạng thái milestone |
| `TASKS.md` | Trước khi bắt đầu task mới — xem milestone nào đang active |
| `docs/thiet-ke-kien-truc.md` | Cần hiểu schema Neo4j, pipeline, MCP tool spec |
| `docs/huong-dan-stack.md` | Cần hiểu sâu stack: Neo4j patterns, AST gotchas, FastMCP tips |
| `docs/adr/` | Architecture Decision Records — đọc trước khi đụng schema/policy |
| `CONTRIBUTING.md` | Setup dev, chạy tests, workflow commit |

**ADR đã có:** `0001` schema evolution (PostgreSQL no ALTER until M6) · `0002` spec schema policy (CoreSymbol/LintRule/CLI per-version, M4.5) · `0003` pattern storage (PatternExample Neo4j + reuse embeddings, M4.6) · `0004` Defined-in ranking heuristic (M5.5) · `0005` core coverage version paths (M5.5) · `0006` environment harness (M6 Wave 1) · `0007` incremental indexer (M6 Wave 2 — head_sha tracking, force-push fallback, module rename caveat, auto-reseed sentinel) · `0008` SSH auto-clone (M6 Wave 4 — URL detection, key delivery via env, tempfile safety, project-local known_hosts, full clone for incremental support, background lifecycle).
