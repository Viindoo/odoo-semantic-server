# M3 "Semantic Wow" — Design Spec

> **Ngày:** 2026-05-06
> **Trạng thái:** Approved — ready for implementation plan
> **Outcome:** `find_examples("tính thuế theo quốc gia đối tác")` trả về code thật từ
> codebase Viindoo, dùng được ngay, hoạt động với query bất kỳ ngôn ngữ nào.

---

## Scope & Scope Shift

### Scope M3 (spec này)

- `src/indexer/embedder.py` — pluggable EmbedderClient, default bge-m3 via Ollama
- `src/indexer/parser_js.py` — era-aware JS chunk extraction (kéo từ M4)
- `src/indexer/writer_pgvector.py` — chunk + store embeddings vào pgvector
- `src/db/migrate.py` — thêm `embeddings` table + `vector` extension
- `src/mcp/server.py` — tool `find_examples` (hybrid ANN + Neo4j rerank)
- `tests/` — unit + integration tests, CI postgres service
- `TASKS.md` — cập nhật scope shift

### Scope Shift vs TASKS.md gốc

`parser_js.py` được kéo từ M4 vào M3 vì pipeline diagram
(`thiet-ke-kien-truc.md §5`) chỉ rõ JS Parser feeds cả Neo4j Writer (M4) lẫn
Embedder (M3). Làm JS chunking ở M3 tránh duplicate logic. M4 chỉ cần add Neo4j
write path (JSPatch, OWLComp nodes) trên top của parser đã có.

### Không thuộc scope M3

- Neo4j nodes cho JS (JSPatch, OWLComp) — vẫn ở M4
- `TARGETS_MODEL` edge (View → Model) — vẫn ở M4
- Web UI, API key middleware — M5
- Incremental re-index — M6

---

## Section 1 — Corpus & Chunking

Gì được embed vào pgvector:

| Loại | Đơn vị chunk | Metadata prefix | Ghi chú |
|------|-------------|-----------------|---------|
| Python method | Method body | `[module] model.name.method_name(ver)` | |
| Python field | Field definition | `[module] model.name: field_name` | |
| XML view | Mỗi `<record model="ir.ui.view">` | `[module] xmlid (type, inherit_from)` | Truncate ≤ 512 tokens |
| QWeb template | Mỗi `<template>` block | `[module] xmlid` | |
| JS Era 1 (8–11) | `Widget.extend({...})` block | `[module] WidgetName.extend (era1, ver)` | Regex-based |
| JS Era 2 (12–15) | `odoo.define('name', ...)` block | `[module] define:name (era2, ver)` | Regex-based |
| JS Era 3 / OWL (16+) | Class definition hoặc `patch(...)` | `[module] ClassName / patch:target (era3, ver)` | Regex + `@odoo-module` marker |

**Tại sao embed cả field?** Query dạng "partner_country_id là related hay compute?" sẽ
hit field chunk tốt hơn method chunk.

**Tại sao metadata prefix?** `bge-m3` encode metadata prefix cùng với body → embedding
mang ngữ cảnh module/model/version, không chỉ pure semantic của code snippet.

**Chunk size:** ≤ 512 tokens per chunk. Fields thường < 100 tokens — dùng nguyên. XML
views hoặc JS blocks lớn hơn → truncate ở 512 tokens.

**JS era detection:** dựa vào `odoo_version` của repo + pattern trong file:
- Era 1: `Widget.extend(` present
- Era 2: `odoo.define(` present
- Era 3: `/** @odoo-module */` marker ở đầu file

---

## Section 2 — Embedder & Schema

### Embedding Model

Default: **`bge-m3`** (BAAI, 1024 dim, 100+ languages, offline-first via Ollama).

Tại sao `bge-m3` thay vì `nomic-embed-text`: `nomic-embed-text` English-first,
multilingual coverage yếu. `bge-m3` map query tiếng Việt/Nhật/... và code tiếng Anh
về cùng semantic space.

### Config Keys

| Key | Default | Ghi chú |
|-----|---------|---------|
| `EMBEDDER_URL` | `http://localhost:11434` | Ollama local hoặc remote |
| `EMBEDDER_MODEL` | `bge-m3` | Swappable sang nomic-embed-text, OpenAI-compat API |
| `EMBEDDER_DIM` | `1024` | Phải khớp model — dùng khi tạo `vector($DIM)` column |

### PostgreSQL Schema (thêm vào `migrate.py`)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS embeddings (
    id           BIGSERIAL PRIMARY KEY,
    chunk_type   TEXT NOT NULL,
    -- 'method' | 'field' | 'view' | 'qweb' | 'js_era1' | 'js_era2' | 'js_era3'
    module       TEXT NOT NULL,
    odoo_version TEXT NOT NULL,
    entity_name  TEXT NOT NULL,   -- method/field/xmlid/widget name
    model_name   TEXT,            -- null for JS/XML chunks
    file_path    TEXT NOT NULL,
    content      TEXT NOT NULL,   -- metadata prefix + code body
    vec          vector(1024),    -- dimension = EMBEDDER_DIM
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_embeddings_vec
    ON embeddings USING ivfflat (vec vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_embeddings_version
    ON embeddings (odoo_version, chunk_type);
```

**Dimension mismatch guard:** nếu table `embeddings` đã tồn tại với dimension khác
`EMBEDDER_DIM` → `migrate.py` log warning + raise, yêu cầu drop table + re-index thủ
công. Không auto-drop (blast radius quá lớn).

**IVFFlat vs HNSW:** IVFFlat với `lists=100` là sweet spot cho corpus < 1M chunks.
Cần `VACUUM ANALYZE embeddings` sau bulk insert để ANN accuracy đạt max.

### `EmbedderClient` Interface

```python
class EmbedderClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

class OllamaEmbedder:          # production — gọi Ollama REST API
    ...

class FakeEmbedder:            # CI/tests — trả random vector đúng dim
    ...
```

`OllamaEmbedder` dùng batch call (`/api/embed`) để giảm round-trips. Retry 3 lần với
exponential backoff nếu Ollama chưa warm.

---

## Section 3 — Hybrid Retrieval & `find_examples`

### Query Flow

```
find_examples(query, odoo_version, limit, context_module, chunk_types)
        │
        ▼  EmbedderClient.embed([query])
  [1024-dim query vector]
        │
        ▼  pgvector ANN — cosine, top-20 candidates
  20 raw chunks (content, score, module, entity_name, chunk_type, file_path)
        │
        ▼  Neo4j rerank
   1. Lọc chunk từ module '__unresolved__'
   2. Query centrality: số module DEPENDS_ON mỗi chunk's module
   3. score_final = cosine_score * (1 + 0.1 * log(dependents + 1))
   4. Nếu context_module: boost +0.05 cho chunk trong dependency chain
        │
        ▼  sort by score_final DESC, top-N
  Formatted output
```

### Neo4j Rerank Cypher

```cypher
UNWIND $module_names AS mod
MATCH (m:Module {name: mod, odoo_version: $version})
OPTIONAL MATCH ()-[:DEPENDS_ON]->(m)
RETURN mod, COUNT(*) AS dependents
```

### Tool Signature

```python
find_examples(
    query: str,                       # bất kỳ ngôn ngữ
    odoo_version: str = "latest",     # filter pgvector + Neo4j
    limit: int = 5,                   # top-N sau rerank
    context_module: str | None = None,# boost dependency chain
    chunk_types: list[str] | None = None,  # filter: ["method", "js_era3"]
) -> str
```

### Output Format

```
find_examples: "tính thuế theo quốc gia đối tác" (17.0)
Found 5 results

─────────────────────────────────────────
#1 · score 0.91 · method · [account] account.move._compute_tax_amount
   File: addons/account/models/account_move.py:482
   ┌──────────────────────────────────────────
   │ def _compute_tax_amount(self):
   │     for move in self:
   │         country = move.partner_id.country_id
   │         ...
   └──────────────────────────────────────────

#2 · score 0.87 · js_era3 · [account] patch:TaxGroupWidget
   File: addons/account/static/src/components/tax_group/tax_group.js:34
   ┌──────────────────────────────────────────
   │ patch(TaxGroupWidget, {
   │     _getTaxValues(taxLine) { ... }
   │ });
   └──────────────────────────────────────────
```

**Score hiển thị** để AI client tự judge độ tin cậy. **chunk_type rõ ràng** để AI
biết ngay snippet là Python/JS/XML. **File path** để có thể jump to source.

---

## Section 4 — Testing & CI

### Test Files

| File | Mark | Mô tả |
|------|------|-------|
| `test_embedder.py` | unit | FakeEmbedder interface contract, OllamaEmbedder retry logic |
| `test_parser_js.py` | unit | Era detection + chunk extraction (3 eras, edge cases) |
| `test_writer_pgvector.py` | postgres | Store chunks + ANN query, dimension mismatch guard |
| `test_mcp_find_examples.py` | postgres + neo4j | Full hybrid flow với fake vectors |
| `test_output_snapshots.py` | postgres + neo4j | Extend existing — lock find_examples schema |

### Key Test Cases `test_parser_js.py`

```
Era 1: Widget.extend({ start: function(){...} })  → 1 chunk, metadata era1
Era 2: odoo.define('sale.widget', fn)              → 1 chunk, metadata era2
Era 3: /** @odoo-module */ class Foo + patch(Bar)  → 2 chunks (class + patch)
Large file: multiple patch() calls                 → N chunks, each ≤ 512 tokens
No JS content: empty file                         → 0 chunks (no error)
```

### CI Changes (`ci.yml`)

Thêm postgres service container:

```yaml
services:
  postgres:
    image: pgvector/pgvector:0.8.2-pg16  # đồng bộ .env.example
    env:
      POSTGRES_DB: odoo_semantic_test
      POSTGRES_USER: odoo_semantic
      POSTGRES_PASSWORD: password
    ports: ["5432:5432"]
    options: >-
      --health-cmd pg_isready
      --health-interval 10s
      --health-retries 5
```

Pytest marker `ollama` — skip trong CI, chỉ chạy local khi Ollama available.

---

## File Inventory

```
src/indexer/
├── embedder.py          -- EmbedderClient protocol + OllamaEmbedder + FakeEmbedder
├── parser_js.py         -- era-aware JS chunk extraction (kéo từ M4)
└── writer_pgvector.py   -- chunk all types → embed → bulk insert

src/db/
└── migrate.py           -- thêm CREATE EXTENSION vector + embeddings table

src/mcp/
└── server.py            -- thêm find_examples tool

tests/
├── test_embedder.py
├── test_parser_js.py
├── test_writer_pgvector.py
├── test_mcp_find_examples.py
└── test_output_snapshots.py  (extend existing)

.github/workflows/
└── ci.yml               -- thêm postgres service

TASKS.md                 -- cập nhật scope shift (parser_js.py từ M4 → M3)
pyproject.toml           -- thêm pgvector Python package
odoo-semantic.conf.example -- thêm [embedder] section
```

---

## Dependency mới

```toml
# pyproject.toml
dependencies = [
    ...
    "pgvector>=0.3",   # psycopg2 adapter cho vector type
]
```

```ini
# odoo-semantic.conf.example
[embedder]
url   = http://localhost:11434
model = bge-m3
dim   = 1024
```
