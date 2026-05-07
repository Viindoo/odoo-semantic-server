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

## Neo4j 5.x Gotchas

```cypher
-- Sắp xếp version (numeric, không phải lexicographic):
ORDER BY toFloat(v) DESC               -- ĐÚNG
ORDER BY v DESC                        -- SAI ("9.0" > "17.0")

-- Đếm pattern expression:
ORDER BY COUNT { ()-[:INHERITS]->(m) } -- ĐÚNG (Neo4j 5.x)
ORDER BY size(()-[:INHERITS]->(m))     -- SAI (Neo4j 4.x, CypherSyntaxError)
```

Dùng `.single()` chỉ khi chắc chắn có đúng 1 row. Dùng `.data()` cho 0-N rows.
`single()` trả `None` nếu không có row → dùng để phát hiện unresolved edge.

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
| `CONTRIBUTING.md` | Setup dev, chạy tests, workflow commit |
