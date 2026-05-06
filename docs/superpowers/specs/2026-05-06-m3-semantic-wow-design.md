# M3 "Semantic Wow" — Design Spec

> **Ngày:** 2026-05-06
> **Trạng thái:** Approved (v2 — post Opus debate) — ready for implementation plan
> **Outcome:** `find_examples("tính thuế theo quốc gia đối tác")` trả về code thật từ
> codebase Viindoo, dùng được ngay, hoạt động với query bất kỳ ngôn ngữ nào.

**Changelog v2 (post Opus debate):**
- Schema: thêm `UNIQUE` + `chunk_idx` + `indexed_at`, switch IVFFlat → **HNSW**
- Writer: delete-before-insert per module (transactional) — ngăn corpus nhân đôi
- Dim: đổi default **1024 → 512** (Matryoshka bge-m3, ~98% quality, RAM/storage tiết kiệm)
- Rerank: fix formula chống demote Viindoo modules — `context_module` dominant (+0.20), centrality coefficient ↓ 0.02
- JS parser: **tree-sitter** thay regex — handle template literals, multi-line, optional `@odoo-module`
- XML/QWeb: sliding-window chunking (64-token overlap) thay truncate cho view > 512 tokens

---

## Scope & Scope Shift

### Scope M3 (spec này)

- `src/indexer/embedder.py` — pluggable EmbedderClient, default bge-m3 via Ollama
- `src/indexer/parser_js.py` — era-aware JS chunk extraction via tree-sitter (kéo từ M4)
- `src/indexer/writer_pgvector.py` — chunk + store embeddings vào pgvector
- `src/db/migrate.py` — thêm `embeddings` table + `vector` extension
- `src/mcp/server.py` — tool `find_examples` (hybrid ANN + Neo4j rerank)
- `tests/` — unit + integration + `@pytest.mark.ollama` recall tests
- `TASKS.md` — cập nhật scope shift
- `pyproject.toml` — thêm `pgvector`, `tree-sitter-javascript`
- `odoo-semantic.conf.example` — thêm `[embedder]` section

### Scope Shift vs TASKS.md gốc

`parser_js.py` được kéo từ M4 vào M3 vì pipeline diagram
(`thiet-ke-kien-truc.md §5`) chỉ rõ JS Parser feeds cả Neo4j Writer (M4) lẫn
Embedder (M3). Dùng tree-sitter ngay từ M3 — M4 chỉ add Neo4j write path
(JSPatch, OWLComp nodes) trên top parser đã có. Không duplicate logic.

### Không thuộc scope M3

- Neo4j nodes cho JS (JSPatch, OWLComp) — vẫn ở M4
- `TARGETS_MODEL` edge (View → Model) — vẫn ở M4
- Web UI, API key middleware — M5
- Incremental re-index (M6) — nhưng schema M3 đã chuẩn bị sẵn (UNIQUE + chunk_idx)

---

## Section 1 — Corpus & Chunking

Gì được embed vào pgvector:

| Loại | Đơn vị chunk | Metadata prefix | Chunking |
|------|-------------|-----------------|---------|
| Python method | Method body | `[module] model.name.method_name(ver)` | Nguyên block |
| Python field | Field definition | `[module] model.name: field_name` | Nguyên dòng |
| XML view | Mỗi `<record model="ir.ui.view">` | `[module] xmlid (type, inherit_from)` | Sliding-window nếu > 512 tokens |
| QWeb template | Mỗi `<template>` block | `[module] xmlid` | Sliding-window nếu > 512 tokens |
| JS Era 1 (8–11) | `Widget.extend({...})` block | `[module] WidgetName.extend (era1, ver)` | tree-sitter |
| JS Era 2 (12–15) | `odoo.define('name', ...)` block | `[module] define:name (era2, ver)` | tree-sitter |
| JS Era 3 / OWL (16+) | Class definition hoặc `patch(...)` | `[module] ClassName / patch:target (era3, ver)` | tree-sitter |

**Tại sao embed cả field?** Query dạng "partner_country_id là related hay compute?" sẽ
hit field chunk tốt hơn method chunk.

**Metadata prefix:** `bge-m3` encode prefix cùng body → embedding mang ngữ cảnh
module/model/version. Nếu benchmark cho thấy prefix hurt recall (< 3pp improvement
trên held-out set), drop prefix — filter column đã làm job này chính xác hơn.

**Chunk size:** ≤ 512 tokens. Method/field thường nhỏ hơn — dùng nguyên.

**Sliding-window cho XML/QWeb:** block > 512 tokens → chia thành chunks với 64-token
overlap, tất cả share cùng `entity_name` (xmlid), phân biệt bằng `chunk_idx`.
Tránh mất nửa cuối của view phức tạp (account.view_move_form, v.v.).

**JS parser — tree-sitter (không phải regex):** Regex vỡ với template literals, ASI,
multi-line `patch()`. `py-tree-sitter` + `tree-sitter-javascript` parse chính xác cả
3 era. Era detection per-file (không chỉ per-version) vì Era 2 files tồn tại trong
Odoo 16 codebase chưa port. `@odoo-module` marker là signal Era 3 nhưng optional từ
Odoo 17 — fallback: detect `import { ... } from` ES6 pattern.

---

## Section 2 — Embedder & Schema

### Embedding Model

Default: **`bge-m3`** (BAAI, **512 dim** Matryoshka-truncated, 100+ languages,
offline-first via Ollama).

Tại sao 512 dim thay vì 1024: `bge-m3` hỗ trợ Matryoshka — truncate 1024 → 512 dim
giữ ~98% retrieval quality. Storage: 512 × 4B × 150k chunks = 300 MB vectors (vs
600 MB). Quan trọng trên server 8 GB RAM. Expose `EMBEDDER_DIM` trong config để admin
opt-up lên 1024 nếu cần.

Tại sao `bge-m3` thay vì `nomic-embed-text`: `nomic-embed-text-v1.5` MIRACL Vietnamese
~30 vs `bge-m3` ~60 — khoảng cách đủ lớn để ảnh hưởng recall thực tế.

**Indexing time estimate (4 vCPU / Ollama CPU):** bge-m3 Ollama CPU ~50–100 chunks/sec.
Full Viindoo stack ~150k chunks → **25–50 phút** first-index. Document rõ trong
`odoo-semantic.conf.example` và README. M6 incremental sẽ giảm về giây cho re-index.

### Config Keys

| Key | Default | Ghi chú |
|-----|---------|---------|
| `EMBEDDER_URL` | `http://localhost:11434` | Ollama local hoặc OpenAI-compat remote |
| `EMBEDDER_MODEL` | `bge-m3` | Swappable |
| `EMBEDDER_DIM` | `512` | Matryoshka bge-m3; đổi lên 1024 nếu cần accuracy |

### PostgreSQL Schema (thêm vào `migrate.py`)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS embeddings (
    id           BIGSERIAL PRIMARY KEY,
    chunk_type   TEXT NOT NULL,
    -- 'method' | 'field' | 'view' | 'qweb' | 'js_era1' | 'js_era2' | 'js_era3'
    module       TEXT NOT NULL,
    odoo_version TEXT NOT NULL,
    entity_name  TEXT NOT NULL,      -- method/field/xmlid/widget name
    model_name   TEXT,               -- null cho JS/XML chunks
    file_path    TEXT NOT NULL,
    chunk_idx    INTEGER NOT NULL DEFAULT 0,  -- sliding-window index (0 = toàn bộ)
    content      TEXT NOT NULL,      -- metadata prefix + code body
    vec          vector(512),        -- dimension = EMBEDDER_DIM (default 512)
    indexed_at   TIMESTAMP DEFAULT NOW()
);

-- Idempotent re-index: upsert theo unique key
CREATE UNIQUE INDEX IF NOT EXISTS ux_embeddings_chunk
    ON embeddings (module, odoo_version, chunk_type, entity_name, file_path, chunk_idx);

-- ANN index: HNSW — no rebuild on corpus growth, better recall vs IVFFlat
CREATE INDEX IF NOT EXISTS idx_embeddings_vec
    ON embeddings USING hnsw (vec vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Pre-filter index: version + type trước khi ANN scan
CREATE INDEX IF NOT EXISTS idx_embeddings_filter
    ON embeddings (odoo_version, chunk_type, module);
```

**Tại sao HNSW thay IVFFlat:** IVFFlat cần rebuild khi corpus 2×; recall phụ thuộc
`lists` value cần tune. HNSW: recall ổn định, không rebuild, phù hợp write-once
read-many corpus. pgvector 0.8.2 hỗ trợ đầy đủ.

**Dimension mismatch guard:** nếu table `embeddings` đã tồn tại với dimension khác
`EMBEDDER_DIM` → `migrate.py` log error + raise, yêu cầu drop + re-index thủ công.
Không auto-drop.

**Writer pattern — delete-before-insert per module:**

```python
def write_module_embeddings(conn, module: str, version: str, chunks: list[Chunk]):
    with conn.cursor() as cur:
        conn.autocommit = False
        # Xóa embeddings cũ của module này trước khi insert mới
        cur.execute(
            "DELETE FROM embeddings WHERE module = %s AND odoo_version = %s",
            (module, version),
        )
        execute_values(
            cur,
            """INSERT INTO embeddings
               (chunk_type, module, odoo_version, entity_name, model_name,
                file_path, chunk_idx, content, vec)
               VALUES %s
               ON CONFLICT (module, odoo_version, chunk_type, entity_name, file_path, chunk_idx)
               DO UPDATE SET vec = EXCLUDED.vec, content = EXCLUDED.content,
                             indexed_at = NOW()""",
            [chunk.as_tuple() for chunk in chunks],
        )
        conn.commit()
```

Pattern này: (1) ngăn duplicate trên re-index, (2) dọn stale chunks khi code bị xóa,
(3) atomic — nếu embed fails giữa chừng, không để corpus ở trạng thái partial.

### `EmbedderClient` Interface

```python
class EmbedderClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

class OllamaEmbedder:    # production — batch call /api/embed, retry 3× backoff
    ...

class FakeEmbedder:      # CI/tests — random vector đúng dim, seed cho reproducibility
    ...
```

---

## Section 3 — Hybrid Retrieval & `find_examples`

### Query Flow

```
find_examples(query, odoo_version, limit, context_module, chunk_types)
        │
        ▼  EmbedderClient.embed([query])
  [512-dim query vector]
        │
        ▼  pgvector ANN (HNSW cosine) — filter odoo_version + chunk_types — top-20
  20 raw chunks (content, score, module, entity_name, chunk_type, file_path)
        │
        ▼  Neo4j rerank
   1. Lọc chunk từ module '__unresolved__'
   2. Query centrality: số module DEPENDS_ON mỗi chunk's module (Neo4j)
   3. score_reranked = cosine_score * (1 + 0.02 * log(dependents + 1))
   4. Nếu context_module: boost +0.20 cho chunk trong dependency chain (dominant)
        │
        ▼  sort by score_reranked DESC, top-N
  Formatted output
```

**Rerank rationale:**
- Centrality coefficient **0.02** (thay vì 0.1) — tránh upstream Odoo core modules
  (account, sale, ~200 dependents) overwhelm Viindoo localization modules (viin_*, to_*)
  vốn ít dependents hơn nhưng là code người dùng thực sự cần.
- `context_module` boost **+0.20** là dominant — khi AI coding session đang edit module X,
  code từ X's dependency chain cần thắng rõ ràng. +0.05 quá yếu, không có ý nghĩa thực tế.
- Cả hai hệ số là v0 heuristic — label rõ trong code comment, tune trong M6 dựa trên
  held-out query set.

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
    query: str,                            # bất kỳ ngôn ngữ
    odoo_version: str = "latest",          # filter pgvector + Neo4j
    limit: int = 5,                        # top-N sau rerank
    context_module: str | None = None,     # dominant boost +0.20
    chunk_types: list[str] | None = None,  # filter: ["method", "js_era3"]
) -> str
```

### Output Format

```
find_examples: "tính thuế theo quốc gia đối tác" (17.0)
Found 5 results

─────────────────────────────────────────
#1 · score 0.91 · method · [viin_account] account.move._compute_vn_tax_amount
   File: addons/viin_account/models/account_move.py:134
   ┌──────────────────────────────────────────
   │ def _compute_vn_tax_amount(self):
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

Score hiển thị để AI client tự judge. chunk_type rõ để AI biết ngay Python/JS/XML.
File path để jump-to-source.

---

## Section 4 — Testing & CI

### Test Files

| File | Mark | Mô tả |
|------|------|-------|
| `test_embedder.py` | unit | FakeEmbedder contract, OllamaEmbedder retry logic |
| `test_parser_js.py` | unit | tree-sitter era detection + chunking, edge cases |
| `test_writer_pgvector.py` | postgres | Store + ANN query, UNIQUE upsert, delete-before-insert |
| `test_mcp_find_examples.py` | postgres + neo4j | Full hybrid flow với FakeEmbedder |
| `test_output_snapshots.py` | postgres + neo4j | Extend existing — lock find_examples schema |
| `test_find_examples_recall.py` | ollama | 10 hand-curated query→chunk pairs, assert recall@5 ≥ 0.80 |

`@pytest.mark.ollama` — skip trong CI, chạy local + nightly pre-release. Bắt buộc
pass trước khi tag M3 release.

### Key Test Cases `test_parser_js.py`

```
Era 1: Widget.extend({ start: function(){...} })       → 1 chunk, era1
Era 2: odoo.define('sale.widget', fn)                  → 1 chunk, era2
Era 3: /** @odoo-module */ class Foo {}                → 1 chunk, era3
Era 3: patch(Bar, { method() { return `${x}` } })      → 1 chunk, era3 (template literal)
Era 3 file: multiple patch() calls                     → N chunks
Era 3 without @odoo-module marker (Odoo 17+)           → detect via ES6 import pattern
Large block > 512 tokens                               → sliding-window, chunk_idx 0,1,2...
Empty / non-JS file                                    → 0 chunks, no error
```

### CI Changes (`ci.yml`)

Thêm postgres service container:

```yaml
services:
  postgres:
    image: pgvector/pgvector:0.8.2-pg16   # đồng bộ .env.example
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

Thêm marker `ollama` vào `pyproject.toml`:
```toml
markers = [
    "neo4j: ...",
    "postgres: ...",
    "ollama: integration tests yêu cầu Ollama đang chạy (skip bằng -m 'not ollama')",
]
```

---

## File Inventory

```
src/indexer/
├── embedder.py          -- EmbedderClient protocol + OllamaEmbedder + FakeEmbedder
├── parser_js.py         -- tree-sitter era-aware JS chunk extraction (kéo từ M4)
└── writer_pgvector.py   -- chunk all types → embed → delete-before-insert bulk upsert

src/db/
└── migrate.py           -- CREATE EXTENSION vector + embeddings table (HNSW, UNIQUE)

src/mcp/
└── server.py            -- thêm find_examples tool

tests/
├── test_embedder.py
├── test_parser_js.py
├── test_writer_pgvector.py
├── test_mcp_find_examples.py
├── test_find_examples_recall.py   (mới — @pytest.mark.ollama)
└── test_output_snapshots.py       (extend existing)

.github/workflows/
└── ci.yml               -- thêm postgres service container

TASKS.md                 -- cập nhật scope shift (parser_js.py từ M4 → M3)
pyproject.toml           -- thêm pgvector, tree-sitter-javascript; thêm ollama marker
odoo-semantic.conf.example -- thêm [embedder] section
```

---

## Dependencies Mới

```toml
# pyproject.toml
dependencies = [
    ...
    "pgvector>=0.3,<0.5",          # psycopg2 adapter cho vector type
    "tree-sitter>=0.21",           # JS parser runtime
    "tree-sitter-javascript>=0.21",# JS grammar
]
```

```ini
# odoo-semantic.conf.example
[embedder]
url   = http://localhost:11434
model = bge-m3
dim   = 512
# dim = 1024   # uncomment nếu cần accuracy tối đa (cần 2× storage + RAM)
# Indexing time estimate (4 vCPU, Ollama CPU, full Viindoo 17 stack ~150k chunks):
# First index: ~25–50 phút. Re-index incremental (M6): vài giây.
```
