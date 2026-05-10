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

### 4-tier deterministic ranking

```cypher
ORDER BY is_ext ASC, inbound DESC, edition_rank ASC, mod_name ASC
```

**Tier 1 — `is_ext`** (0 = base, 1 = extension):

```cypher
CASE WHEN coalesce(m.is_definition, false) THEN 0
     WHEN EXISTS {
         (m)-[:INHERITS]->(:Model {name: $name, odoo_version: $v})
     } THEN 1
     ELSE 0 END AS is_ext
```

- `m.is_definition = true` (post-reindex): node được parser đánh dấu tường minh là base → `is_ext=0`.
- `m.is_definition` null/false + EXISTS outgoing same-name INHERITS: đây là extension → `is_ext=1`.
- `m.is_definition` null/false + không EXISTS: fallback an toàn cho data chưa reindex → `is_ext=0`.

Fallback EXISTS subquery đảm bảo fix hoạt động ngay trên data hiện tại **trước khi reindex**.

**Tier 2 — `inbound DESC`** (số INHERITS edge trỏ vào node):

```cypher
COUNT { ()-[:INHERITS]->(m) } AS inbound
```

Base thực sự có nhiều inbound (mọi extension trỏ vào). Parser-miss orphan extension có `inbound=0` → bị đẩy xuống trong cùng `is_ext` bucket. Xử lý graceful cho case v8 orphan node.

**Tier 3 — `edition_rank ASC`** (community = base, custom = leaf):

```cypher
CASE mod.edition
     WHEN 'community'  THEN 0
     WHEN 'enterprise' THEN 1
     WHEN 'viindoo'    THEN 2
     WHEN 'oca'        THEN 3
     ELSE 4 END AS edition_rank
```

Rank thấp = base hơn. Portable qua mọi deployment — không hardcode repo name hay path prefix. Dựa trên `Module.edition` property (ADR-0003, M4.6).

**Tier 4 — `mod_name ASC`** (tiebreak alphabetical):

```cypher
mod.name AS mod_name
-- ... ORDER BY ... mod_name ASC
```

Loại bỏ hoàn toàn Cypher arbitrary-order khi 3 tier trên tie.

**Áp dụng đồng nhất cho 3 tool:** `_resolve_model` (lines 127–150), `_resolve_field` (lines 207–227), `_resolve_method` (lines 257–277) trong `src/mcp/server.py` — tất cả dùng cùng 4-tier pattern với `m_node` thay `m` cho field/method query.

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
- Fix ngay lập tức trên data hiện tại qua EXISTS fallback — không cần reindex trước khi deploy.
- Sau reindex: `is_definition` property nhanh hơn EXISTS subquery (property lookup O(1) thay vì graph traversal).
- Portable: không hardcode repo name, path prefix, hay module name — hoạt động cho mọi customer deployment.
- `r.order` property mở đường cho MRO reconstruction tương lai mà không cần schema change.

**Negative:**
- Reindex bắt buộc để backfill `is_definition` và `r.order`. Trước reindex: EXISTS fallback đúng nhưng chậm hơn (~2–5ms/node traversal thay vì property lookup).
- `edition_rank` phụ thuộc `Module.edition` (M4.6). Nếu module chưa có edition (custom addon không có manifest license field) → fallback `edition_rank=4` (đúng — custom = leaf). Không gây sai kết quả, chỉ mất tiebreak tier 3.

**Risk:**
- **Parser-miss orphan node** (v8 bug case): `is_ext=0`, `inbound=0` → xếp sau node có `inbound>0` cùng `is_ext=0`. Nếu không có node nào có `inbound>0`, orphan sẽ thắng — vẫn sai. Mitigation: WI-5 fix Era1 `_columns.update()` + `_columns = X._columns.copy()` parser để giảm miss rate. Long-term: M6 validator flag orphan Model node.
- **Pattern C redeclare** (cùng `_name` và `_inherit`): `is_definition=False` → xếp đúng là extension. Một số Odoo community module dùng pattern này để "re-open" model — behavior đúng theo invariant.

## Alternatives Considered

1. **Hardcode `Module.repo` prefix (vd `odoo_`)** — reject. Fail trên Enterprise (prefix khác), OCA (prefix đa dạng), Viindoo, và mọi customer deployment không có convention tên repo cố định.

2. **Track `_original_module` runtime attribute của Odoo** — reject. `_original_module` là runtime attribute chỉ có sau `registry.init_models()` — không thể extract từ static parse. `had_explicit_name` từ AST/regex là equivalent tĩnh, đủ cho 5 pattern đã survey.

3. **Recursive AST climb để detect mixin chain** — reject. Flat invariant "outgoing INHERITS same-name = extension" đủ cho tất cả 5 pattern. Recursive climb tăng độ phức tạp parser mà không cải thiện accuracy cho ranking use-case.

4. **Chỉ dùng `inbound DESC`, không có `is_ext`** — reject. Parser-miss orphan extension có `inbound=0`, base thực sự trong codebase nhỏ có thể cũng `inbound=0` (nếu chỉ có 1 module). Không distinguish được.

5. **`is_ext` chỉ từ `is_definition` property, bỏ EXISTS fallback** — reject. Yêu cầu reindex trước khi deploy. Với codebase lớn (400+ modules), reindex mất 30–60 phút. EXISTS fallback cho phép fix deploy ngay, reindex background.

## References

- ADR-0001: Schema Evolution Policy — `is_definition`, `had_explicit_name`, `r.order` là Neo4j property (không PostgreSQL ALTER); SET trong MERGE, idempotent, ADR-0001 compliant.
- ADR-0003: PatternExample Storage — định nghĩa `Module.edition` property (community/enterprise/viindoo/oca/custom) dùng trong tier 3.
- `CLAUDE.md` "Neo4j 5.x Gotchas" — `COUNT { pattern }` syntax (Neo4j 5.x); tiebreak tất cả ORDER BY để loại Cypher arbitrary-order.
- `src/mcp/server.py` lines 127–150 (`_resolve_model`), 207–227 (`_resolve_field`), 257–277 (`_resolve_method`) — implementation.
- `src/indexer/parser_python.py` lines 253–264, 538, 356, 617 — `had_explicit_name` extraction (Era2 AST + Era1 regex).
- `src/indexer/writer_neo4j.py` lines 47–52, 69–93 — `is_definition` SET và `r.order` SET.
