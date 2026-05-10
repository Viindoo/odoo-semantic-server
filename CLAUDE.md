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

Dùng cho ranking "Defined in" trong `resolve_*`. Cypher có fallback EXISTS check
khi property absent (data cũ chưa reindex). Xem `docs/adr/0004`.

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

## Upstream Warnings — Không Dùng suppress

Hai warnings từ testcontainers (`@wait_container_is_ready`) và một từ authlib (via fastmcp) là upstream issues. **Không dùng `filterwarnings`/`suppress`/`ignore`** — fix root cause hoặc chờ upstream fix. Đã documented trong `CONTRIBUTING.md`.

## Image Versions — Nguồn Sự Thật

`NEO4J_IMAGE` trong `.env.example` là nguồn sự thật cho local dev (testcontainers đọc biến này). Khi bump version: sửa `.env.example`.

**CI exception:** GitHub Actions service containers được khởi động *trước* bất kỳ step nào — không thể đọc `.env.example` tại parse time. Do đó `ci.yml` phải hardcode image version. Khi bump Neo4j: cập nhật **cả hai** `.env.example` VÀ `ci.yml:services.neo4j.image`.

## Tài Liệu Liên Quan

| File | Đọc khi nào |
|------|-------------|
| `README.md` | Tổng quan dự án, onboard user, system requirements, trạng thái milestone |
| `TASKS.md` | Trước khi bắt đầu task mới — xem milestone nào đang active |
| `docs/thiet-ke-kien-truc.md` | Cần hiểu schema Neo4j, pipeline, MCP tool spec |
| `docs/huong-dan-stack.md` | Cần hiểu sâu stack: Neo4j patterns, AST gotchas, FastMCP tips |
| `docs/adr/` | Architecture Decision Records — đọc trước khi đụng schema/policy |
| `CONTRIBUTING.md` | Setup dev, chạy tests, workflow commit |

**ADR đã có:** `0001` schema evolution (PostgreSQL no ALTER until M6) · `0002` spec schema policy (CoreSymbol/LintRule/CLI per-version, M4.5) · `0003` pattern storage (PatternExample Neo4j + reuse embeddings, M4.6) · `0004` Defined-in ranking heuristic (M5.5) · `0005` core coverage version paths (M5.5).
