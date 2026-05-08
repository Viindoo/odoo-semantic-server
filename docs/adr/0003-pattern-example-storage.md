# ADR-0003: PatternExample Storage — Neo4j Node + Reuse Embeddings Table

**Date:** 2026-05-08  
**Status:** Draft

## Context

Milestone 4.6 ("Pattern Wow") cần lưu **~50 curated PatternExample** (computed field cross-model, write override với super(), XPath avoid replace, OWL patch v17, …) với 2 yêu cầu chính:

1. **Semantic search** qua intent string (vd `"how to compute field across related model"`) — cần embedding vector + ANN index. Stack hiện có pgvector + HNSW (M3 `embeddings` table).
2. **Metadata phong phú** — `intent_keywords[]`, `gotchas[]` (ranked anti-patterns), `file_ref`, `language`, `odoo_version_min`, link tới `CoreSymbol` (M4.5 dep) khi pattern dùng API cụ thể. Stack hiện có Neo4j cho graph metadata.

ADR-0001 cấm ALTER PostgreSQL until M6 → không thể `CREATE TABLE pattern_examples` mới (kỹ thuật `CREATE IF NOT EXISTS` chấp nhận tạo bảng, nhưng pattern thiết kế dài hạn đòi schema-versioned migration tool ở M6 — thêm bảng giờ tăng tech debt).

Đồng thời M4.6 còn cần enrichment `Module.edition` (`community/enterprise/viindoo/oca/custom`) + `Module.viindoo_equivalent_qname` (cho EE confusion list) và `Method.convention_kind/super_safety/return_required` — đều là property additions trên node Neo4j hiện có.

## Decision

1. **PatternExample = Neo4j node với metadata; embedding vector reuse `embeddings` table với `chunk_type='pattern_example'`.**

   Neo4j node:
   ```
   (:PatternExample {
     pattern_id: 'computed-field-cross-model',  -- unique key
     intent_keywords: ['computed', 'depends', 'cross-model', 'partner_id'],
     file_ref: 'addons/sale/models/sale_order.py:245',
     snippet_text: '@api.depends(...)\\ndef _compute_X(self):\\n    ...',
     gotchas: ['Missing Many2one root in @api.depends path', '...'],
     odoo_version_min: '17.0',
     language: 'python'
   })
   ```

   PostgreSQL `embeddings` row:
   ```
   chunk_type = 'pattern_example'
   module = '__patterns__'           -- sentinel
   odoo_version = '17.0'             -- match pattern.odoo_version_min
   entity_name = 'python__computed-field-cross-model'  -- encode language + pattern_id
   file_path = 'addons/sale/models/sale_order.py:245'
   chunk_idx = 0                     -- pattern là single-chunk
   text = snippet_text + "\n---\n" + gotchas joined
   vec  = <embedding qwen3-1024d>
   ```

   Existing UNIQUE constraint `(chunk_type, module, odoo_version, entity_name, file_path, chunk_idx)` đã accept `chunk_type='pattern_example'` — không cần ALTER.

2. **Module enrichment qua Neo4j SET property — không ALTER.**
   - `Module.edition`: `'community' | 'enterprise' | 'viindoo' | 'oca' | 'custom'` — SET trong MERGE statement
   - `Module.viindoo_equivalent_qname`: nullable string, hardcoded từ `EE_CONFUSION` dict cho 16 EE-only module
   - Detection logic (parser_python.py mới):
     - Folder pattern `viin_*/to_*` hoặc path chứa `tvtmaaddons/erponline-enterprise` → `'viindoo'`
     - Manifest `license = 'OCA-...'` → `'oca'`
     - Manifest `license ∈ {'LGPL-3', 'GPL-3'}` + path chứa `/odoo/addons/` → `'community'`
     - Fallback → `'custom'`

3. **Method enrichment qua Neo4j SET property — không ALTER.**
   - `Method.convention_kind`: `'compute' | 'inverse' | 'search' | 'default' | 'builder' | 'prepare' | 'check' | 'action' | 'crud' | 'private' | 'public'`
   - `Method.super_safety`: `'always' | 'usually' | 'never'`
   - `Method.return_required`: bool
   - Detection: regex map theo method name (xem M4.6 plan WI2 cho map đầy đủ).

4. **Language filter cho `suggest_pattern` qua entity_name slug encoding.**
   - Format `entity_name = "<language>__<pattern_id>"` — vd `python__computed-field-cross-model`, `xml__xpath-avoid-replace`, `js__owl-patch-v17`
   - pgvector filter: `WHERE entity_name LIKE 'python__%'` (B-tree index trên entity_name đã có)
   - Tránh ALTER `embeddings` add column `language`.

5. **`USES_CORE_SYMBOL` edge từ PatternExample (M4.5 dep).**
   - `(:PatternExample)-[:USES_CORE_SYMBOL]->(:CoreSymbol)` — bind pattern dùng API cụ thể (vd pattern `computed-field-cross-model` USES `@api.depends`)
   - Nếu CoreSymbol target không tồn tại (M4.5 chưa land hoặc chưa index Odoo core) → silent skip, backfill sau khi M4.5 ship + re-run `seed_patterns.py`.

> **Lưu ý storage join:** `suggest_pattern` query 2-step:
> 1. pgvector ANN search trên `embeddings` (chunk_type='pattern_example') → top-N `entity_name` + cosine score
> 2. Neo4j MATCH `(:PatternExample {pattern_id: $pid})` cho mỗi entity_name → fetch metadata
> 
> Cost: 1 round-trip pgvector + 1 round-trip Neo4j (batch query với UNWIND). Acceptable latency ~50-100ms cho top-5.

## Consequences

**Positive:**
- 0 ALTER PostgreSQL — ADR-0001 compliant.
- Reuse infrastructure M3 (embedder Qwen3 + pgvector + HNSW index) — không thêm dep.
- Module/Method enrichment không ảnh hưởng existing query (property SET là additive).
- `find_examples` (M3) và `suggest_pattern` (M4.6) co-exist không xung đột — chunk_type filter tách biệt domain.
- Pattern seed có thể re-run idempotent: `seed_patterns.py` MERGE node + delete-then-insert embedding rows (delete WHERE chunk_type='pattern_example').

**Negative:**
- Cross-store query: `suggest_pattern` cần 2 connection (pgvector + Neo4j). Network 2x vs single store. Mitigated: Neo4j fetch là batch query, không N+1.
- Slug encoding `<language>__<pattern_id>` ràng buộc naming — pattern_id không được chứa `__`. Validate trong seed CLI.
- `embeddings` table dùng cho cả code search (M3 `find_examples`) và pattern search (M4.6 `suggest_pattern`) — query plan optimizer phải lọc theo `chunk_type`. HNSW index per-row, performance không ảnh hưởng nhưng filter step thêm cost ~5-10ms.

**Risk:**
- **Pattern seed obsolete khi Odoo bump version** — `odoo_version_min: "17.0"` nhưng pattern có thể không apply v19+. Mitigation: snippet `file_ref` luôn trỏ Odoo CE thực tế version_min; M6 add field `odoo_version_max` + cron re-validate.
- **Semantic search recall thấp với 50 seed** — embedding chỉ cover snippet+gotchas, không cover follow-up patterns. Mitigation: M6 mở rộng seed (~200), thêm pattern feedback loop (helpful/not). V0 acceptable nếu recall ≥60% trên 5 query thủ công.
- **`Module.edition` detection false positive cho custom addon** — manifest không có license field → fallback `'custom'`. Acceptable; tool output hiển thị edition rõ ràng để user verify.
- **EE_CONFUSION dict stale** — Odoo có thể move module CE↔EE giữa các version (vd `note` đã từng). Mitigation: dict có `_source_date: 2026-05-08` annotation; M6 auto-detect từ manifest path scan upstream Odoo CE repo.

## Alternatives Considered

1. **CREATE TABLE `pattern_examples`** in PostgreSQL — reject. Vi phạm spirit ADR-0001 (chấp nhận `CREATE IF NOT EXISTS` cho table mới, nhưng dài hạn cần migration tool — thêm bảng giờ tăng tech debt M6 phải migrate). Cross-store join Neo4j ↔ PostgreSQL phức tạp hơn việc giữ metadata trong Neo4j.

2. **ALTER `embeddings` ADD COLUMN language** — reject. Vi phạm trực tiếp ADR-0001 ("không ALTER until M6"). Slug encoding workaround không tăng độ phức tạp đáng kể (chỉ cost split string khi query).

3. **PatternExample là Neo4j node, embedding vector lưu trong Neo4j property** (vector index Neo4j 5.13+) — reject. Neo4j vector index latency cao hơn pgvector HNSW (theo benchmark M3 evaluation). Codebase đã invest vào pgvector — duplicate vector store overhead.

4. **PatternExample ngoài database (YAML file + in-memory load)** — reject. Không cross-reference được với CoreSymbol/Module/Method nodes trong Neo4j. MCP server phải hot-reload YAML khi seed update → restart cost.

5. **Module.edition là node label thay vì property** (`(:Module:CommunityModule)`, `(:Module:EnterpriseModule)`) — reject. Multi-label MERGE phức tạp; query filter `WHERE 'EnterpriseModule' IN labels(m)` chậm hơn property index.

## References

- ADR-0001: Schema Evolution Policy (PostgreSQL)
- ADR-0002: Spec Schema Policy (CoreSymbol/LintRule/CLI — M4.5 dep)
- M4.6 plan: `docs/superpowers/plans/2026-05-08-milestone-4-6-pattern-wow.md`
- Survey: 16/16 EE confusion modules confirmed CE-absent (2026-05-08)
- Pattern catalog: 7 base patterns + ~43 variations từ Odoo CE v17 + Viindoo addons survey
