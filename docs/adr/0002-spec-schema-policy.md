# ADR-0002: Spec Schema Policy — CoreSymbol, LintRule, CLI Nodes

**Date:** 2026-05-08  
**Status:** Accepted

## Context

Milestone 4.5 ("Spec Wow") thêm 3 loại tri thức Odoo upstream vào graph:

1. **CoreSymbol** — API entity của Odoo core (function/class/decorator/exception/field type/ORM method) với lifecycle theo phiên bản
2. **LintRule** — pylint custom Odoo + ESLint OWL + ruff rule per phiên bản
3. **CLICommand + CLIFlag** — `odoo-bin` subcommand và flag với status thay đổi giữa các phiên bản

Mỗi loại đều có **3 đặc tính cross-version**:
- Symbol/rule/flag thêm mới ở 1 version cụ thể (vd `@api.deprecated` thêm v19)
- Bị deprecated/removed ở version sau (vd `--longpolling-port` removed v18)
- Thay thế bởi entity khác (vd `group_operator` → `aggregator` v18)

Cần policy rõ ràng cho composite key, version-range representation (per-version node hay version-range single node), lifecycle edge structure, và scope của edge `USES_CORE_SYMBOL` từ user code → CoreSymbol. Đồng thời M4.5 phải tuân thủ ADR-0001 — không ALTER PostgreSQL until M6.

## Decision

1. **Composite key per-version, không gộp version range.**
   - `CoreSymbol`: `(qualified_name, odoo_version)` — vd `("odoo.tools.safe_eval", "19.0")`
   - `LintRule`: `(rule_id, odoo_version)` — vd `("E8501", "17.0")`
   - `CLICommand`: `(name, odoo_version)` — vd `("server", "17.0")`
   - `CLIFlag`: `(flag_name, command_name, odoo_version)` — vd `("--longpolling-port", "server", "17.0")`

   Lý do: `status`, `signature`, `message` thay đổi per version → cần query riêng lẻ. Storage linear với version count (~15 version v8-v22 → acceptable).

2. **Lifecycle qua property trên CoreSymbol node — không dùng lifecycle edge.**
   - `cs.added_in: "<version>"` — version đầu tiên symbol xuất hiện (chỉ set khi diff engine phát hiện symbol này không có ở version trước).
   - `cs.removed_in: "<version>"` — version symbol biến mất (set trên node version cũ nhất, value = version mới không còn symbol này).
   - `cs.deprecated_in: "<version>"` — version status đổi sang `deprecated` (set trên node version mới, value = version đó).
   - `(:CoreSymbol)-[:REPLACED_BY]->(:CoreSymbol)` — **duy nhất edge thực sự cross-symbol**. Vd `group_operator@v17 → aggregator@v18`. Tạo ONLY khi `old.replacement_qname` trỏ tới symbol tồn tại trong version mới.

   **Lý do chọn property thay vì edge**: 3 loại lifecycle còn lại (`ADDED_IN`, `REMOVED_IN`, `DEPRECATED_IN`) không cần target node riêng — value là string version, không liên kết structural đến node khác. Property SET `added_in/removed_in/deprecated_in` trực tiếp lên node tương ứng đơn giản hơn, idempotent hơn, query nhanh hơn (không cần traverse edge). Chỉ `REPLACED_BY` cần edge vì nó biểu diễn relationship giữa 2 symbol khác nhau.

   Cypher query lifecycle: `WHERE cs.added_in IS NOT NULL`, `WHERE cs.deprecated_in IS NOT NULL` — không cần traverse. Numeric version compare cho ORDER BY vẫn dùng `toFloat(cs.odoo_version) DESC` per gotcha `CLAUDE.md`.

3. **`USES_CORE_SYMBOL` edge V0: chỉ bind khi `status ∈ {deprecated, removed}`.**
   - Edge: `(:Method|:Field)-[:USES_CORE_SYMBOL]->(:CoreSymbol)` — bind từ user code reference → Odoo core API
   - V0 scope hẹp giảm noise — full bind (mọi API call) defer M6 sau khi có data validate set.
   - Nếu CoreSymbol target không tồn tại → silent skip (không tạo placeholder, tránh ghost node giống pattern `:INHERITS {unresolved}` đã có).
   - **Update M7 final-closeout 2026-05-11:** V0 set (5 entries) expanded to V1 (14 entries, capped to bound false-positive surface). V1 covers 3 categories: removed (no in-place replacement), signature-changed (kwarg breaking), renamed-option / moved-module. False-positive suppression via `_collect_module_local_defs` scope-resolver (M7 W13) covers V1 entries identically. See `src/indexer/parser_python.py:_DEPRECATED_API_SYMBOLS` for current curated list.

4. **Static spec data v8-v16: empty placeholder JSON.**
   - File: `src/indexer/spec_data/lint_rules_<version>.json`, `cli_flags_<version>.json`
   - V8-v16: `{"_curate_status": "pending", "_generated_at": "2026-05-08", "rules": [], "flags": []}` — placeholder rỗng. Curate manual defer M6 hoặc community contribution.
   - V17-v19: code-extract from Odoo source (parser_lint_rules + parser_cli read source thực tế).

5. **ADR-0001 compliance: 0 ALTER PostgreSQL.**
   - Toàn bộ schema mới = Neo4j node label + edge type. Không touch `embeddings`, `profiles`, `repos` tables.
   - `writer_neo4j.setup_indexes()` thêm 4 `CREATE INDEX IF NOT EXISTS` cho 4 node label mới — idempotent, an toàn re-run.

6. **Version detection cho CoreSymbol/LintRule/CLI: dùng `odoo_version` field từ profile registry hiện có.**
   - `parser_odoo_core` / `parser_lint_rules` / `parser_cli` accept `odoo_version` argument từ caller (pipeline.py).
   - Static fallback: nếu version <= "16.0" → đọc từ `spec_data/*_<version>.json` thay vì parse code (vì code Python 2 v8-v9 không AST-parse được).

> **Lưu ý quan trọng:** Per-version node là intentional duplicate. `CoreSymbol("safe_eval", "17.0")` và `CoreSymbol("safe_eval", "18.0")` là 2 node riêng dù tên giống nhau. Query "lookup symbol latest version" phải dùng `MATCH (cs:CoreSymbol {qualified_name: $qn}) ORDER BY toFloat(cs.odoo_version) DESC LIMIT 1` — không phải single-node lookup.

## Consequences

**Positive:**
- Query per-version chính xác, không bị nhầm version (vd `lookup_core_api("name_get", "18.0")` trả `status: removed`, `lookup_core_api("name_get", "17.0")` trả `status: deprecated`).
- Diff giữa 2 version dễ via separate node lookup + diff_engine.
- Schema agnostic v8-v19+ — không hardcode version range.
- ADR-0001 compliant — không cần migration tool sớm hơn dự định.

**Negative:**
- Storage linear với version count: ~5000-8000 CoreSymbol node × N version = 75k-120k node với 15 version. Acceptable cho Neo4j; index lookup vẫn O(log n).
- Diff engine phải chạy cross-version sau mỗi indexer run. Cost ~1-2s/version pair, chạy 1 lần khi index thay vì query time.

**Risk:**
- **Symbol rename giữa version (vd class relocated `odoo/tools/safe_eval.py` → `odoo/safe_eval.py`)** → `qualified_name` thay đổi → `REPLACED_BY` edge sinh sai (hoặc miss). Mitigation: diff_engine có fallback fuzzy match by `short_name` (last segment của qualified_name) khi exact match fail. Vẫn risk false positive — log warning + manual review trước khi MERGE edge.
- **`USES_CORE_SYMBOL` sparse data ban đầu** → tool `find_deprecated_usage` luôn empty cho đến khi M4.5 land + index Odoo core. Mitigation: tool docstring + output header note rõ requirement "Run indexer with `--index-core <path>` first".
- **Static placeholder v8-v16 → tool trả empty hoặc misleading** "no rules found for v8". Mitigation: tool output hiển thị `data_source: "static/<date>" curate_status: pending` để user biết đó là gap data, không phải fact.

## Alternatives Considered

1. **Single CoreSymbol node với property `version_range: "17.0-18.0"`** — reject. Khó query boundary case (symbol có ở 17 và 19 nhưng skip 18 → range string không expressible). Thay đổi `status` mid-range cần update property in-place, không tracking history. Cypher filter version range phức tạp.

2. **PostgreSQL table `core_symbols` (cùng level với `embeddings`)** — reject. Vi phạm ADR-0001 (cần ALTER hoặc CREATE TABLE add-only — `CREATE IF NOT EXISTS` chấp nhận nhưng add column sau breaks). Cross-store join (Neo4j user code ↔ PostgreSQL spec) phức tạp. Neo4j consistent với existing pattern cho graph entity (Module/Model/Field/Method/View đều ở Neo4j).

3. **CoreSymbol node version-less + property `versions: ["17.0", "18.0"]` array** — reject. Property update không atomic (concurrent indexer run race). Filter `WHERE "18.0" IN cs.versions` chậm hơn composite key index. Diff engine phải reconstruct timeline từ array history → fragile.

4. **In-memory data (Python dict) thay vì Neo4j node** — reject. Không cross-reference được với user code Method/Field nodes. Tool query phải reload module → restart cost. Không sharable giữa indexer và MCP server (2 process).

## References

- ADR-0001: Schema Evolution Policy (PostgreSQL)
- M4.5 plan: `docs/superpowers/plans/2026-05-08-milestone-4-5-spec-wow.md`
- Survey notes: 3 turn discovery của ~80 changes v17→v18→v19 trong CoreSymbol/LintRule/CLI
- `CLAUDE.md` Neo4j 5.x gotcha: numeric version compare `toFloat(v) DESC`
