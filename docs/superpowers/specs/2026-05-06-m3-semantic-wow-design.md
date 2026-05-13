# M3 "Semantic Wow" — Design Spec

> **Status:** ✓ DONE — M3 shipped 2026-05-06

> **Ngày:** 2026-05-06 (updated 2026-05-07)
> **Trạng thái:** Approved (v3 — post 3-round Opus debate) — ready for implementation plan
> **Outcome:** `find_examples("tính thuế theo quốc gia đối tác")` trả về code thật từ
> codebase Viindoo, dùng được ngay, hoạt động với query bất kỳ ngôn ngữ nào.

**Changelog:**

| Version | Thay đổi |
|---------|---------|
| v1 | Initial spec |
| v2 | Schema UNIQUE+HNSW, dim 1024→512, rerank fix, JS→tree-sitter, sliding-window XML |
| v3 | Model bge-m3→**Qwen3-Embedding-4B Q5_K_M** (MRL 2560→1024), reject two-tier + Voyage-code-3, query instructions thành named constants, VRAM budget, benchmark 30→100 queries stratified, remove query-time translation |

---

## Scope & Scope Shift

### Scope M3

- `src/indexer/embedder.py` — pluggable `EmbedderClient`, default Qwen3-Embedding-4B via llama.cpp/Ollama
- `src/embedding/instructions.py` — named constants cho query-time instructions (mới)
- `src/indexer/parser_js.py` — era-aware JS chunk extraction via tree-sitter (kéo từ M4)
- `src/indexer/writer_pgvector.py` — chunk + store embeddings, delete-before-insert per module
- `src/db/migrate.py` — `embeddings` table + `vector` extension + HNSW index
- `src/mcp/server.py` — tool `find_examples` (hybrid ANN + Neo4j rerank)
- `tests/` — unit + integration + `@pytest.mark.ollama` recall tests (100 queries stratified)
- `TASKS.md` — cập nhật scope shift
- `pyproject.toml` — thêm `pgvector`, `tree-sitter-javascript`
- `odoo-semantic.conf.example` — thêm `[embedder]` section
- `docs/deploy.md` — thêm hướng dẫn pull Qwen3-Embedding-4B Q5_K_M GGUF thủ công

### Scope Shift vs TASKS.md gốc

`parser_js.py` kéo từ M4 vào M3 — M4 chỉ add Neo4j write path (JSPatch, OWLComp nodes).

### Không thuộc scope M3

- Neo4j nodes cho JS (JSPatch, OWLComp) — M4
- `TARGETS_MODEL` edge — M4
- Code-to-code similarity / Tier 2 embedding — **không có tool nào cần**, re-evaluate tại M5 nếu có use case cụ thể
- Web UI, API key middleware — M5
- Incremental re-index — M6

---

## Section 1 — Corpus & Chunking

| Loại | Đơn vị chunk | Metadata prefix | Chunking |
|------|-------------|-----------------|---------|
| Python method | Docstring (nếu có) + method body | `[module] model.name.method_name(ver)` | Nguyên block |
| Python field | Field definition | `[module] model.name: field_name` | Nguyên dòng |
| XML view | Mỗi `<record model="ir.ui.view">` | `[module] xmlid (type, inherit_from)` | Sliding-window nếu > 512 tokens |
| QWeb template | Mỗi `<template>` block | `[module] xmlid` | Sliding-window nếu > 512 tokens |
| JS Era 1 (8–11) | `Widget.extend({...})` block | `[module] WidgetName.extend (era1, ver)` | tree-sitter |
| JS Era 2 (12–15) | `odoo.define('name', ...)` block | `[module] define:name (era2, ver)` | tree-sitter |
| JS Era 3 / OWL (16+) | Class definition hoặc `patch(...)` | `[module] ClassName / patch:target (era3, ver)` | tree-sitter |

**Docstring enrichment (index-time, không phải query-time):**
Nếu method có docstring → prepend vào đầu chunk content trước code body. Giúp
Qwen3-Embedding có NL anchor để align query ngôn ngữ bất kỳ → code. AST reads/writes
extraction bị hoãn sang M3.1 — sẽ measure impact trước khi build.

**JS parser — tree-sitter (không phải regex):**
Regex vỡ với template literals, multi-line `patch()`, ASI. `py-tree-sitter` +
`tree-sitter-javascript` handle đúng cả 3 era. Era detection per-file — `@odoo-module`
là signal Era 3 nhưng optional từ Odoo 17; fallback: detect ES6 `import { }` pattern.

**Sliding-window XML/QWeb:**
View > 512 tokens → chia thành chunks với 64-token overlap, share `entity_name` (xmlid),
phân biệt bằng `chunk_idx`. Tránh mất nửa cuối view phức tạp.

**Documents không có instruction prefix khi index.** Per Qwen3-Embedding spec: chỉ
queries mới có instruction, không phải documents. Thêm instruction vào documents sẽ
hurt embedding quality.

---

## Section 2 — Embedder & Schema

### Embedding Model

**Qwen3-Embedding-4B** (Alibaba, 2025, Q5_K_M quantization, MRL 2560→1024 dim).

| Metric | bge-m3 (rejected) | **Qwen3-Embedding-4B** |
|--------|-------------------|----------------------|
| MTEB Multilingual | 59.56 | **69.45** (+9.89) |
| MTEB Code (CoIR) | ~35–40 | **80.06** (+40) |
| VRAM (Q5_K_M) | ~2–4 GB | **~4.0–5.0 GB** |
| MRL support | ✓ | ✓ (2560→1024 = 98% quality) |
| Ollama | ✓ | ✓ (Q5_K_M via GGUF) |
| Vietnamese | Trung bình | Tốt (100+ languages, SEA-BED tested) |

Lý do reject two-tier + Voyage-code-3:
- **Không có tool nào cần code-to-code similarity** trong M3/M4 scope — YAGNI.
- **Instructions không tạo ra namespace geometry riêng** — chỉ bias cùng một embedding
  space. Hai "namespace" = 2× storage + 2× index cho vectors cùng manifold.
- **Voyage-code-3 là API-only** — không self-hostable, vi phạm offline-first.

### VRAM Budget (8GB GPU)

| Component | VRAM |
|-----------|------|
| Qwen3-Embedding-4B Q5_K_M weights | ~2.9 GB |
| Activations (batch 16, seq 2048) | ~1.5 GB |
| Framework overhead | ~0.5 GB |
| **Total peak** | **~5.0 GB** |
| Headroom cho re-index burst | ~3.0 GB |

Neo4j JVM heap (4 GB) là RAM, không phải VRAM — không conflict.

### Ollama Operational Note

`ollama pull qwen3-embedding:4b` ship **Q4_K_M (2.5 GB) by default** — không phải
Q5_K_M. Để dùng Q5_K_M:
```bash
# Pull GGUF từ Mungert/Qwen3-Embedding-4B-GGUF trên HuggingFace
# Tạo Modelfile, đăng ký với Ollama
ollama create qwen3-embedding-q5km -f Modelfile
```
Chi tiết trong `docs/deploy.md`. Alternative: chạy llama.cpp server trực tiếp.

### Config Keys

| Key | Default | Ghi chú |
|-----|---------|---------|
| `EMBEDDER_URL` | `http://localhost:11434` | Ollama hoặc llama.cpp server |
| `EMBEDDER_MODEL` | `qwen3-embedding-q5km` | Registered Ollama model name |
| `EMBEDDER_DIM` | `1024` | MRL-truncated từ 2560; opt-up tối đa 2560 |

### License Risk

Qwen3-Embedding được fine-tune một phần trên **MS MARCO** (non-commercial license).
GitHub issue [#166](https://github.com/QwenLM/Qwen3-Embedding/issues/166) đã raise
— Alibaba chưa trả lời. Model release là Apache 2.0.

- **Internal tooling (Viindoo dev team):** Risk thấp, proceed.
- **Expose ra ngoài như SaaS:** Cần legal review trước khi ship.

Fallback nếu license bị block: `bge-m3` (MIT, no issues) — trade quality để safety.

### PostgreSQL Schema

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
    chunk_idx    INTEGER NOT NULL DEFAULT 0,  -- sliding-window index
    content      TEXT NOT NULL,      -- docstring prefix (nếu có) + metadata + code body
    vec          vector(1024),       -- Qwen3-4B MRL-truncated 2560→1024, L2-normalized
    indexed_at   TIMESTAMP DEFAULT NOW()
);

-- Idempotent re-index
CREATE UNIQUE INDEX IF NOT EXISTS ux_embeddings_chunk
    ON embeddings (module, odoo_version, chunk_type, entity_name, file_path, chunk_idx);

-- HNSW — no rebuild on growth, consistent latency, write-once read-many corpus
CREATE INDEX IF NOT EXISTS idx_embeddings_vec
    ON embeddings USING hnsw (vec vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- Pre-filter: version + type trước ANN scan
CREATE INDEX IF NOT EXISTS idx_embeddings_filter
    ON embeddings (odoo_version, chunk_type, module);
```

**Storage (500k chunks, halfvec fp16):**
1024 dim × 2 bytes × 500k = **~1 GB** cho vectors. Tổng table với content + indexes ≈ 3–4 GB.

**Dimension mismatch guard:** table đã tồn tại với dim khác → log error + raise, yêu cầu
drop + re-index thủ công.

### `EmbedderClient` Interface

```python
# src/embedding/instructions.py
INSTRUCT_NL_TO_CODE = (
    "Instruct: Given a developer question in any language, "
    "retrieve the Odoo code chunk that answers it.\nQuery: "
)
# Future (only add when a concrete tool needs it):
# INSTRUCT_CODE_TO_CODE = (
#     "Instruct: Given an Odoo code snippet, retrieve other snippets "
#     "implementing the same pattern.\nQuery: "
# )
```

```python
# src/indexer/embedder.py
class EmbedderClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    # Documents: no prefix. Queries: caller prepends instruction constant.

class Qwen3Embedder:    # production — POST /api/embed, batch, MRL truncate to dim
    ...

class FakeEmbedder:     # CI/tests — random L2-normalized vector, seeded
    ...
```

**Writer pattern — delete-before-insert per module:**
```python
def write_module_embeddings(conn, module: str, version: str, chunks: list[Chunk]):
    with conn.cursor() as cur:
        conn.autocommit = False
        cur.execute(
            "DELETE FROM embeddings WHERE module = %s AND odoo_version = %s",
            (module, version),
        )
        execute_values(cur, INSERT_SQL, [c.as_tuple() for c in chunks])
        conn.commit()
```
Atomic — ngăn duplicate, dọn stale chunks khi code bị xóa.

---

## Section 3 — Hybrid Retrieval & `find_examples`

### Query Flow

```
find_examples(query, odoo_version, limit, context_module, chunk_types)
        │
        ▼  prepend INSTRUCT_NL_TO_CODE + EmbedderClient.embed([query])
  [1024-dim L2-normalized query vector]
        │
        ▼  pgvector HNSW (cosine) — filter odoo_version + chunk_types — top-20
  20 raw chunks (content, score, module, entity_name, chunk_type, file_path)
        │
        ▼  Neo4j rerank
   1. Lọc chunk từ module '__unresolved__'
   2. Centrality: số module DEPENDS_ON chunk's module
   3. score = cosine * (1 + 0.02 * log(dependents + 1))
   4. context_module boost: +0.20 nếu chunk trong dependency chain (dominant)
        │
        ▼  sort DESC, top-N
  Formatted output
```

**Rerank heuristics (v0):** Coefficient 0.02 và boost 0.20 là placeholder — tune tại M6
dựa trên held-out eval set. Label rõ trong code comment để không bị treat như ground truth.

### Tool Signature

```python
find_examples(
    query: str,                            # bất kỳ ngôn ngữ
    odoo_version: str = "latest",
    limit: int = 5,
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

---

## Section 4 — Testing & CI

### Test Files

| File | Mark | Mô tả |
|------|------|-------|
| `test_embedder.py` | unit | FakeEmbedder contract, Qwen3Embedder retry + MRL truncation |
| `test_parser_js.py` | unit | tree-sitter era detection + chunking, edge cases |
| `test_embedding_instructions.py` | unit | constants không rỗng, tiền tố đúng format |
| `test_writer_pgvector.py` | postgres | Store + ANN query, UNIQUE upsert, delete-before-insert |
| `test_mcp_find_examples.py` | postgres + neo4j | Full hybrid flow với FakeEmbedder |
| `test_output_snapshots.py` | postgres + neo4j | Lock find_examples output schema |
| `test_find_examples_recall.py` | ollama | 100 queries stratified, recall@5 gate |

### Recall Benchmark Gate (`@pytest.mark.ollama`)

**100 queries stratified** — required pass trước khi tag M3 release:

| Segment | Count | Examples |
|---------|-------|---------|
| English NL | 40 | "compute tax amount by partner country" |
| Vietnamese NL | 40 | "tính thuế theo quốc gia đối tác" |
| Code-mixed | 20 | "compute amount_total cho sale.order" |

**Pass criteria:**
- VN recall@5 ≥ 0.75
- Gap(EN recall − VN recall) ≤ 0.05 (tức là multilingual thực sự work)
- EN recall@5 ≥ 0.80

Nếu không đạt: add docstring-derived AST enrichment trước khi xem xét query-time
translation. Query-time translation chỉ xem xét nếu enrichment vẫn không đạt, và chỉ
dùng model thật (≥ 7B hoặc API chất lượng cao) — không bao giờ dùng model 0.5B.

### Key Test Cases `test_parser_js.py`

```
Era 1: Widget.extend({ start: function(){...} })        → 1 chunk, era1
Era 2: odoo.define('sale.widget', fn)                   → 1 chunk, era2
Era 3: /** @odoo-module */ class Foo {}                 → 1 chunk, era3
Era 3: patch(Bar, { method() { return `${x}` } })       → 1 chunk (template literal)
Era 3 without @odoo-module (Odoo 17+)                   → detect via ES6 import
Multiple patch() calls                                  → N chunks
Large block > 512 tokens                                → sliding-window, chunk_idx 0,1,2
Empty / non-JS file                                     → 0 chunks, no error
```

### CI Changes (`ci.yml`)

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

```toml
# pyproject.toml — thêm marker
markers = [
    "neo4j: ...",
    "postgres: ...",
    "ollama: integration tests yêu cầu Ollama/llama.cpp đang chạy (skip bằng -m 'not ollama')",
]
```

---

## File Inventory

```
src/
├── embedding/
│   ├── __init__.py
│   └── instructions.py      -- INSTRUCT_NL_TO_CODE (và future constants)
├── indexer/
│   ├── embedder.py          -- EmbedderClient protocol + Qwen3Embedder + FakeEmbedder
│   ├── parser_js.py         -- tree-sitter era-aware JS chunking (kéo từ M4)
│   └── writer_pgvector.py   -- chunk all types → embed → delete-before-insert upsert
├── db/
│   └── migrate.py           -- CREATE EXTENSION vector + embeddings table (HNSW, UNIQUE)
└── mcp/
    └── server.py            -- thêm find_examples tool

tests/
├── test_embedder.py
├── test_embedding_instructions.py    (mới)
├── test_parser_js.py
├── test_writer_pgvector.py
├── test_mcp_find_examples.py
├── test_find_examples_recall.py      (mới, @pytest.mark.ollama)
└── test_output_snapshots.py          (extend existing)

.github/workflows/
└── ci.yml                   -- thêm postgres service container

docs/
└── deploy.md                -- thêm hướng dẫn Qwen3-4B Q5_K_M GGUF setup

TASKS.md                     -- cập nhật scope shift (parser_js.py M4→M3)
pyproject.toml               -- pgvector>=0.3,<0.5; tree-sitter>=0.21; ollama marker
odoo-semantic.conf.example   -- [embedder] section
```

---

## Dependencies Mới

```toml
# pyproject.toml
dependencies = [
    ...
    "pgvector>=0.3,<0.5",
    "tree-sitter>=0.21",
    "tree-sitter-javascript>=0.21",
]
```

```ini
# odoo-semantic.conf.example
[embedder]
url   = http://localhost:11434
model = qwen3-embedding-q5km
dim   = 1024
# dim = 2560  # max quality, cần thêm ~3 GB storage cho 500k chunks
#
# Ollama setup (Q5_K_M không có trong default tag):
#   Xem docs/deploy.md — section "Qwen3-Embedding-4B Q5_K_M setup"
#
# Indexing time estimate (8GB VRAM GPU, full Viindoo v17 ~150k chunks):
#   First index: ~1–2 giờ. Re-index incremental (M6): vài giây.
#
# License note: Apache 2.0 / MS MARCO training data issue pending (#166).
#   Internal tooling: OK. External SaaS: legal review trước khi ship.
```
