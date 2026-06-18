# CLAUDE.md - Odoo Semantic MCP

## Mandatory context

@README.md

REQUIRED reading: project overview, system requirements, milestone status.

## Dev commands

```bash
make install           # venv tại ~/.venv/odoo-semantic-mcp + cài deps
make test              # Unit tests (không cần Docker)
make test-integration  # Integration tests (cần Docker + testcontainers)
make test-all          # Cả hai
make lint              # ruff check src/ tests/
make neo4j-up          # Start Neo4j thủ công (thay cho testcontainers)
```

Venv ở `~/.venv/odoo-semantic-mcp` - không bao giờ tạo `.venv/` trong repo.

## Core principles

- **Boil the Lake:** làm đúng từ đầu rẻ hơn làm lại. Schema phải version-aware + cross-repo ngay từ đầu - migration sau khi có data đắt gấp 10.
- **Ship Wow Product:** output MCP tool phải là cây có cấu trúc, AI client đọc được ngay (ADR-0023 tree grammar, English-only).

## Agent rules (bắt buộc)

- **Read trước Edit/Write** - đọc file trong session hiện tại trước khi sửa; đừng tin trí nhớ session trước.
- **Search trước khi tạo mới** - grep confirm chưa có implementation tương tự; duplicate = source-of-truth conflict.
- **Confirm trước khi xóa** thứ ngoài scope task được giao; không "cleanup" ngoài phạm vi.
- **Edit > Write** - chỉ Write khi tạo file mới hoàn toàn (Write overwrite không warning).

## Architecture invariants

- **Pipeline một chiều:** `scanner → registry → resolver → parser → (writer_neo4j | embedder → writer_pgvector) → server`. Không cross-import ngang hàng (scanner không import parser; registry không import writer). Enforce bởi `tests/test_pipeline_import_discipline.py`.
- **Neo4j C1 schema:** mỗi module một node Model riêng (không gộp theo tên); composite MERGE key cho Module/Model/Field/Method. `Model.is_definition` = ranking heuristic bậc 1, fallback `field_count DESC`. Same-name INHERITS = K×D edges extender→definition (writer W1 cần `tip.is_definition=true`), **không** K² mesh. → [`huong-dan-stack.md §Schema C1`](docs/huong-dan-stack.md#schema-c1), ADR-0013, ADR-0048.
- **Tool surface:** 31 MCP tools + 9 resources. `tests/test_tool_count_sync.py` enforce; bump phải sync `pyproject.toml` + `site/src/lib/constants.ts` (SITE_VERSION/TOOL_COUNT/RESOURCE_COUNT).
- **`is_admin` DB-sourced** qua `is_admin_session(request)` (`src/web_ui/auth.py`) - KHÔNG `request.session.get("is_admin")` (key đó không được set; đọc trả `False` âm thầm, ẩn data của admin). ADR-0011/0026.
- **Multi-tenant fail-closed:** read-side luôn qua choke-point filter + RLS trên embeddings; KHÔNG có `tenant_id` trong Neo4j MERGE key. ADR-0034.

## Neo4j 5.x gotchas

- `ORDER BY toFloat(v) DESC` cho version sort (NOT lexicographic); ORDER BY luôn có deterministic tiebreak (vd `..., mod.name ASC`).
- `COUNT { ()-[:INHERITS]->(m) }` (5.x), không phải `size(...)` (4.x). `.single()` chỉ khi chắc 1 row; `.data()` cho 0-N.
- **ORM read KHÔNG dùng VLP `*1..N`** (nổ path enumeration trên same-name mesh, bug #273) - dùng per-hop name-dedup flat `OPTIONAL MATCH` + `WITH collect(DISTINCT ...)`, không `CALL { WITH }` (5.26 deprecation). ADR-0048.
- Timeout/concurrency env: `NEO4J_QUERY_TIMEOUT_SECONDS` (30; bound từng query qua `neo4j.Query(timeout=)`), server `db.transaction.timeout`=600s; `ORM_QUERY_MAX_CONCURRENCY`/`NONORM_READ_MAX_CONCURRENCY` (8, pool riêng) + `*_SLOT_ACQUIRE_TIMEOUT` (5s) - thread-held `threading.BoundedSemaphore` (cancel-safe), KHÔNG `asyncio.Semaphore`; `SESSION_IDLE_TIMEOUT` (3600s, bỏ qua khi `--transport stdio`).
- **Full Cypher patterns:** [`huong-dan-stack.md §Cypher gotchas`](docs/huong-dan-stack.md#cypher-gotchas).

## Parsing & versions (v8 → v19)

- **ManifestFinder** (`registry.get_manifest_finder`): Legacy `__openerp__.py` (v8-9) / Dual (v10) / Modern `__manifest__.py` (v11+). ADR-0002.
- **Era-aware Python parser:** era1 text-regex (`parser_python_era1._parse_era1_text` + `FIELD_TYPES_LEGACY` cho `_columns`) cho v8-9; era2 AST (v10+). Version dispatch qua `VersionRegistry` (ADR-0032, dùng ở `parser_odoo_core`/`_cli`/`_xml`).
- **index-core paths:** `parser_odoo_core._resolve_core_paths()` - `openerp/` (v8-9), fallback `odoo/orm/` (v19+). Drop >20% CoreSymbol vs version trước = nghi path refactor → fix + regression test. ADR-0005.
- **AST:** dùng `tree.body` (NOT `ast.walk`) cho manifest; `_inherit` normalize về list; thiếu `_name` + có `_inherit` → `name = inherit[0]`. → [`huong-dan-stack.md §AST`](docs/huong-dan-stack.md#ast-parsing).
- **`_latest_version()`** numeric compare - KHÔNG hardcode `"17.0"`; DB rỗng → `None`.

## Testing

```python
pytestmark = pytest.mark.neo4j      # mọi test cần Neo4j
TEST_VERSION = "99.0"               # data test, tránh đụng data thật
# fixture clean_neo4j tự dọn version 99.0 trước/sau mỗi test - luôn dùng
```

- Unit tests không cần Docker; integration dùng testcontainers tự spin-up.
- **FastMCP v3 (#324):** decorator default = function-mode → `@mcp.tool` trả HÀM GỐC (callable). Ưu tiên test import `_resolve_model`/`_resolve_field`/`_resolve_method` (underscore impl, ổn định). Gọi tool body: `server.X(...)` (KHÔNG `.fn` — `.fn` chỉ có trên `FunctionTool`). Lấy `FunctionTool` (cho `.output_schema`/`.parameters`/`.description`): `await mcp.get_tool("X")`. Đếm surface: `await mcp.list_tools()` / `list_resource_templates()`. v3 gỡ `_tool_manager`/`_resource_manager`/`_deprecated_settings` (đọc `json_response`/`stateless_http`/`debug` từ `fastmcp.settings`).
- **Không suppress warnings** upstream (testcontainers / authlib) - fix root cause, không `filterwarnings`/`ignore`. Xem CONTRIBUTING.md.

## Indexer ops (chi tiết ở ADR)

- **Incremental:** so `git rev-parse HEAD` vs `repos.head_sha` (bằng→skip, force-push→full, else diff qua `incremental.compute_changed_module_paths()`); `--full` monthly dọn stale nodes. ADR-0007.
- **Auto-reseed:** `_SeedMeta` sentinel lưu sha256 của `patterns.json` (skip re-embed khi unchanged; `--force` bypass). ADR-0007.
- **Cross-profile parallel:** `--profile-workers N --max-workers M`, per-profile Postgres advisory lock. ADR-0006.
- **SSH auto-clone:** `POST /profiles/{id}/clone-all` + `GET /repos/{id}/clone-status`; key qua `GIT_SSH_COMMAND` (NOT `-i`), `known_hosts` pre-pinned + `StrictHostKeyChecking=yes` (no TOFU), full clone, per-repo advisory lock, fetch+reset-hard refresh. ADR-0008 + ADR-0035.

## Image versions (SSOT)

`NEO4J_IMAGE`/`PG_IMAGE` trong `.env.example` là nguồn sự thật. Bump phải sửa **cả** `.env.example` VÀ `.github/workflows/nightly-smoke.yml` (CI hardcode). `tests/test_env_versions_sync.py` enforce. Harness policy: ADR-0006.

## Related docs

| File | Đọc khi nào |
|------|-------------|
| `TASKS.md` | Trước task mới - milestone nào đang active |
| `docs/thiet-ke-kien-truc.md` | Schema Neo4j, pipeline, MCP tool spec |
| `docs/huong-dan-stack.md` | Sâu: Neo4j patterns, AST gotchas, FastMCP tips |
| `docs/adr/` | Architecture Decision Records - đọc ADR liên quan trước khi đụng schema/policy/auth |
| `docs/adr/INDEX.md` | Lookup nhanh 50 ADR (1 dòng/ADR + amendments) |
| `docs/deploy.md`, `docs/deploy/go-live-checklist.md` | Deploy + go-live ops |
| `CONTRIBUTING.md` | Setup dev, tests, commit + release workflow |
