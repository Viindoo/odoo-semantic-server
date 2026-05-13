# ADR-0004: Defined-in Ranking Heuristic cho resolve_* Tools

**Date:** 2026-05-10  
**Status:** Accepted — M5.5+

## Context

Các MCP tool `resolve_model`, `resolve_field`, `resolve_method` cần xác định module "gốc" (primary / base) trong số nhiều module cùng chạm vào một model name. Schema đã tạo đúng một Model node riêng per `(name, module, odoo_version)` tuple per ADR-0001 / CLAUDE.md C1 schema — vấn đề nằm ở Cypher query chọn node nào là *base*.

**Query gốc (bug):**

```cypher
ORDER BY COUNT { ()-[:INHERITS]->(m) } ASC
```

Hai lỗi đồng thời:

1. **`ASC` inverted** — extension module có ít inbound INHERITS edge hơn base, nên `ASC` trả extension lên đầu thay vì base.
2. **Không deterministic khi tie** — khi nhiều node cùng `inbound=1` (vd 60+ extension nodes của `sale.order` v17), Cypher chọn thứ tự tùy ý tùy storage layout.

**Hai bug instance cụ thể được ghi nhận:**

- **v8 `sale.order`**: một leaf extension module không có outgoing INHERITS edge tới node cùng tên (parser miss — orphan Model node). Với `ASC`, node này có `inbound=0` → xếp đầu → được report là "Defined in" thay vì module base.
- **v17 `sale.order`**: 60+ Model node, nhiều node tie ở `inbound=1`. Cypher arbitrary-order chỉ cho kết quả đúng tình cờ (storage order).

**Bối cảnh module edition:** Property `Module.edition` (`community/enterprise/viindoo/oca/custom`) được set từ M4.6 (ADR-0003). Không có property này, không thể rank community base trước custom extension một cách portable.

## Survey — 5 Declaration Pattern

Khảo sát corpus Odoo v8–v17 cho thấy 5 pattern khai báo class:

| ID | `_name` | `_inherit` | Ý nghĩa |
|----|---------|------------|---------|
| A  | `"x.y"` | không có   | Base definition thuần — `had_explicit_name=True`, `is_definition=True` |
| B  | không có| `"x.y"` (hoặc `["x.y"]`) | Extension thuần — `had_explicit_name=False`, `is_definition=False` |
| C  | `"x.y"` | `"x.y"` (same) | Redeclare trong module khác — `had_explicit_name=True`, nhưng `name IN inherit_list` → `is_definition=False` |
| D  | `"x.y"` | `["x.y", "mixin.a"]` | Kế thừa mixin — `is_definition=False` (name nằm trong inherit_list) |
| E  | `"x.y"` | không có; dùng `_inherits = {"z.z": "z_id"}` | Delegation — `is_definition=True` (không liên quan outgoing INHERITS same-name) |

**Invariant runtime từ corpus:** Một Model node của extension module **luôn có ít nhất một outgoing INHERITS edge đến node cùng tên**. Base definition **không có** outgoing INHERITS đến node cùng tên. Pattern C và D đều tạo outgoing INHERITS same-name → đúng với invariant.

## Decision

### 5-tier deterministic ranking

```cypher
ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
         edition_rank ASC, mod_name ASC
```

Smoke test trên data thật cho thấy heuristic dựa thuần `is_ext` (EXISTS outgoing same-name INHERITS) **không reliable**: writer's self-inherit branch tạo edge ngược trên một số Model node (vd `[odoo_8.0] sale` có outgoing INHERITS đến `sale.order` ở module khác do thứ tự xử lý), khiến base thực sự bị classify là extension. Do đó Decision dùng signal độc lập với chất lượng INHERITS edge.

**Tier 1 — `is_def_rank`** (0 = base post-reindex, 1 = không/chưa biết):

```cypher
CASE WHEN coalesce(m.is_definition, false) THEN 0 ELSE 1 END AS is_def_rank
```

`m.is_definition` được parser+writer set tường minh khi `had_explicit_name=True AND name NOT IN inherit_list` — authoritative signal sau reindex.

**Tier 2 — `field_count DESC`** (số Field node module này khai báo cho model):

```cypher
COUNT {
    (:Field {model: $name, module: m.module, odoo_version: $v})
} AS field_count
```

Smoke test 9 model thật (sale.order/res.partner/product.*/stock.picking/mail.thread/account.move qua v8 và v17): module base **luôn** có nhiều field nhất cho model đó. Vd `sale.order @ 17.0`: `[odoo_17.0] sale` có 59 fields, `sale_stock` có 12, customer extensions 1–10. Robust 100% trên empirical sample. Đây là pre-reindex signal duy nhất đạt 100% accuracy.

**Tier 3 — `dependents DESC`** (số module phụ thuộc vào module chứa Model node):

```cypher
COUNT { ()-[:DEPENDS_ON]->(mod) } AS dependents
```

Tiebreak khi nhiều module có cùng `field_count` (hiếm với Pattern A bases). Module base thường có `dependents` cao do toàn bộ ecosystem phụ thuộc nó. Lưu ý: signal này **một mình** không đủ — `account` có 122 dependents trong v17 nhưng KHÔNG phải base của `res.partner` (`base` mới là). Dùng kết hợp với `field_count` mới đúng.

**Tier 4 — `edition_rank ASC`** (community = base, custom = leaf):

```cypher
CASE mod.edition
     WHEN 'community'  THEN 0
     WHEN 'enterprise' THEN 1
     WHEN 'viindoo'    THEN 2
     WHEN 'oca'        THEN 3
     ELSE 4 END AS edition_rank
```

Rank thấp = base hơn. Portable qua mọi deployment. Dựa trên `Module.edition` property (ADR-0003, M4.6).

**Tier 5 — `mod_name ASC`** (tiebreak alphabetical):

Loại bỏ hoàn toàn Cypher arbitrary-order khi 4 tier trên tie.

**Áp dụng đồng nhất cho 3 tool:** `_resolve_model`, `_resolve_field`, `_resolve_method` trong `src/mcp/server.py` — tất cả dùng cùng 5-tier pattern với `m_node` (resolved Model proxy) thay `m` cho field/method query.

### Tại sao KHÔNG dùng EXISTS subquery + inbound DESC

Heuristic ban đầu của plan dùng `EXISTS { (m)-[:INHERITS]->(:Model {name: $name}) }` để detect extension, và `inbound DESC` làm secondary signal. Smoke test phơi bày 2 vấn đề:

1. **Writer self-inherit edge bug**: Khi extension declare `_inherit = "sale.order"`, writer chạy branch đặc biệt (`writer_neo4j.py:54-61`) tìm "tip" Model cùng tên không có incoming INHERITS, rồi tạo edge `(ext)-[:INHERITS]->(tip)`. Tùy thứ tự index, "tip" có thể là base thực sự (đúng) HOẶC là một extension đã indexed trước (sai). Result: graph có edge `(odoo_8.0/sale)-[:INHERITS]->(website_sale_delivery/sale.order)` — base node lại có outgoing INHERITS cùng tên.

2. **Parser miss orphan**: Một số customer Era1 module declare `_inherit = "sale.order"` nhưng parser không emit edge (lý do khác nhau — có thể manifest issue, có thể parser regex miss). Resulting orphan có `EXISTS = false`, được rank như base → wrong.

Field count signal không phụ thuộc vào INHERITS edge quality, do đó immune to cả 2 vấn đề này.

### Schema additions (implementation trong WI-3)

**`parser_python.py`:**
- `had_explicit_name: bool` — set `True` khi AST parser tìm thấy `_name = "..."` literal trong class body (Era2: line 253–264; Era1: line 538).
- `ModelInfo` dataclass carry field này (lines 356, 617).

**`writer_neo4j.py`:**
- `Model.is_definition` property = `had_explicit_name AND name NOT IN inherit_list` — computed và SET trong MERGE (lines 47–52).
- `INHERITS` edge `r.order` property = list-index trong `_inherit` list — SET khi tạo edge (lines 69–93). Dùng cho MRO reconstruction tương lai; không ảnh hưởng ranking hiện tại.

## Consequences

**Positive:**
- Deterministic hoàn toàn — không có Cypher arbitrary-order cho bất kỳ input nào.
- **Pre-reindex correctness validated**: smoke test trên 9 real model (v8 + v17) đạt 100% accuracy trước khi reindex nhờ `field_count` primary signal.
- Sau reindex: `is_definition` property accelerate ranking (tier 1 short-circuit, không cần count subquery cho 99% query).
- Portable: không hardcode repo name, path prefix, hay module name. Field count + DEPENDS_ON count + edition rank đều derive từ existing schema.
- `r.order` property (WI-3 schema add) mở đường MRO reconstruction tương lai.
- Robust against writer self-inherit edge bug + parser-miss orphan: field count không phụ thuộc INHERITS edge quality.

**Negative:**
- Reindex bắt buộc để backfill `is_definition` và `r.order`. Trước reindex: count subqueries (3 trên 5 tier) chạy mỗi query — chậm hơn property lookup nhưng vẫn O(neighborhood).
- `field_count` signal có thể bị skewed nếu một extension module declare nhiều field hơn base (rất hiếm trong CE/Enterprise; có thể xảy ra với customer module patch nặng). Empirical sample 9 model không phát hiện case này — nếu xảy ra trong thực tế, `is_definition` post-reindex sẽ correct.
- `edition_rank` phụ thuộc `Module.edition` (M4.6). Module v8 trong DB hiện tại có nhiều node tag `edition=custom` thay vì `community` (data quality issue from initial registration) — không ảnh hưởng kết quả vì `field_count` đứng trước trong tier order.

**Risk:**
- **Parser-miss field nodes**: Nếu parser fail extract một số field từ base module (vd Era1 `_columns.update` chưa fix), `field_count` của base bị undercount → có thể tie với extension. WI-4 + WI-5 đã giảm miss rate. Mitigation: tier 3 (`dependents DESC`) làm secondary signal.
- **Pattern C redeclare** (cùng `_name` và `_inherit`): `is_definition=False` → xếp đúng là extension. Một số Odoo community module dùng pattern này để "re-open" model — behavior đúng theo invariant.

## Alternatives Considered

1. **Hardcode `Module.repo` prefix (vd `odoo_`)** — reject. Fail trên Enterprise (prefix khác), OCA (prefix đa dạng), Viindoo, và mọi customer deployment không có convention tên repo cố định.

2. **Track `_original_module` runtime attribute của Odoo** — reject. `_original_module` là runtime attribute chỉ có sau `registry.init_models()` — không thể extract từ static parse. `had_explicit_name` từ AST/regex là equivalent tĩnh, đủ cho 5 pattern đã survey.

3. **Recursive AST climb để detect mixin chain** — reject. Flat invariant "outgoing INHERITS same-name = extension" đủ cho tất cả 5 pattern. Recursive climb tăng độ phức tạp parser mà không cải thiện accuracy cho ranking use-case.

4. **Chỉ dùng `inbound DESC`, không có `is_ext`** — reject. Parser-miss orphan extension có `inbound=0`, base thực sự trong codebase nhỏ có thể cũng `inbound=0` (nếu chỉ có 1 module). Không distinguish được.

5. **`is_ext` chỉ từ `is_definition` property, bỏ EXISTS fallback** — reject (initial), accepted (final). Plan ban đầu giữ EXISTS subquery làm pre-reindex fallback. Smoke test cho thấy EXISTS không reliable do writer self-inherit edge bug. Final design loại bỏ EXISTS, dùng `field_count` (data-driven, robust) thay thế.

6. **`is_ext` từ `EXISTS outgoing same-name` + `inbound DESC` + `mod_name`** (plan ban đầu) — reject sau smoke test. EXISTS subquery sai trên 5/9 model thật (writer self-inherit edge). `inbound DESC` cũng không reliable vì các Model node thường tie ở `inbound=1` (chain pattern thay vì star pattern).

## References

- ADR-0001: Schema Evolution Policy — `is_definition`, `had_explicit_name`, `r.order` là Neo4j property (không PostgreSQL ALTER); SET trong MERGE, idempotent, ADR-0001 compliant.
- ADR-0003: PatternExample Storage — định nghĩa `Module.edition` property (community/enterprise/viindoo/oca/custom) dùng trong tier 3.
- `CLAUDE.md` "Neo4j 5.x Gotchas" — `COUNT { pattern }` syntax (Neo4j 5.x); tiebreak tất cả ORDER BY để loại Cypher arbitrary-order.
- `src/mcp/server.py` lines 127–150 (`_resolve_model`), 207–227 (`_resolve_field`), 257–277 (`_resolve_method`) — implementation.
- `src/indexer/parser_python.py` lines 253–264, 538, 356, 617 — `had_explicit_name` extraction (Era2 AST + Era1 regex).
- `src/indexer/writer_neo4j.py` lines 47–52, 69–93 — `is_definition` SET và `r.order` SET.
