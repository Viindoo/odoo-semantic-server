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
class ModernManifestFinder:  # rglob '__manifest__.py' (v10+)
class LegacyManifestFinder:  # rglob '__openerp__.py' (v8-9)

def get_manifest_finder(odoo_version: str) -> ManifestFinder:
    major = int(odoo_version.split('.')[0])
    return LegacyManifestFinder() if major <= 9 else ModernManifestFinder()
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

## Orchestrated Multi-Subagent Workflow

Khi ≥4 WIs có dependencies → 9-phase pattern (investigation → worktree topology → dispatch → integration → CI → merge → review → cleanup).

**Chi tiết đầy đủ:** [`docs/orchestration-workflow.md`](docs/orchestration-workflow.md).

**Skip khi:** 1-3 WI changes / pure docs change / bug hotfix without investigation.

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

`POST /repos/{id}/clone` auto-clone SSH repos: key via `GIT_SSH_COMMAND` env (NOT `-i`), tempfile `mkstemp(0o600)` + `try/finally unlink`, project-local `known_hosts` (`StrictHostKeyChecking=accept-new`), full clone (no `--depth=1` — incremental needs history). `clone_status`: manual/pending/cloned/error + UI poll 5s.

**Policy chi tiết:** [`docs/adr/0008-ssh-auto-clone.md`](docs/adr/0008-ssh-auto-clone.md).

## Tài Liệu Liên Quan

| File | Đọc khi nào |
|------|-------------|
| `TASKS.md` | Trước khi bắt đầu task mới — xem milestone nào đang active |
| `docs/thiet-ke-kien-truc.md` | Cần hiểu schema Neo4j, pipeline, MCP tool spec |
| `docs/huong-dan-stack.md` | Cần hiểu sâu stack: Neo4j patterns, AST gotchas, FastMCP tips |
| `docs/adr/` | Architecture Decision Records — đọc trước khi đụng schema/policy |
| `CONTRIBUTING.md` | Setup dev, chạy tests, workflow commit |

**ADR đã có:** `0001` schema evolution · `0002` spec schema policy (CoreSymbol/LintRule/CLI per-version) · `0003` pattern storage (PatternExample Neo4j + reuse embeddings) · `0004` auth-web-ui-ssh-policy · `0005` core coverage version paths · `0006` environment harness (M6 Wave 1) · `0007` incremental indexer (head_sha tracking, force-push fallback, module rename caveat, auto-reseed sentinel) · `0008` SSH auto-clone (URL detection, key delivery via env, tempfile safety, project-local known_hosts, full clone) · `0009` pattern catalogue community contribution (80+ curated patterns, test-enforced minimum) · `0010` embedding observability (call_count thread-safe, COUNT(*) /health) · `0011` Web UI session auth (bcrypt cost=12, 8h TTL, cookie SameSite=strict) · `0012` persona-skill-architecture (M7.5 — TRIGGER/PREFER/SKIP routing) · `0013` Defined-in ranking heuristic (M5.5 — is_definition flag, field_count fallback, deterministic tiebreak) · `0014` Astro unified UI (M8 — SSR pages + React islands, /admin/* gated by middleware → FastAPI /api/auth/verify) · `0015` FastAPI pure JSON API (M8 — Jinja2 removed, /api/* JSON only, Astro renders all HTML) · `0016` Profile hierarchy + Neo4j Option Y (parent_profile_id FK, ancestor profile array property, cycle-free + version-match validation) · `0017` OAuth via arctic + oslo (state + PKCE, Google/GitHub, account linking on verified email) · `0018` backup bundle contract (tar.gz: postgres.sql + neo4j.dump + fernet.enc + manifest.json) · `0019` restore upload security (OWASP 10-item checklist, tarfile filter='data', pre-restore safety backup) · `0020` FERNET key delivery + atomic rotation (--old-key-env/--new-key-env, fail-fast in prod, transaction rollback) · `0021` admin audit log (@audit_action decorator, audit_cli context manager, 18+ routes) · `0022` MFA TOTP (pyotp, Fernet-encrypted secrets, 10 HMAC backup codes, admin-required policy) · `0023` Tool output completeness (M9 W-OSM Wave 1 — tree grammar contract, English-only language policy, truncation+total disclosure via `_render_capped`, next-step hint mapping for 18 drill-down tools).
