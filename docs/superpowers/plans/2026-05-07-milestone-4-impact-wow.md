# Milestone 4 — "Impact Wow" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Nguyên tắc bắt buộc khi implement:**
> - **Boil the Lake:** Không bỏ qua era nào (Era 1 Widget.extend → Era 3 OWL/patch). Edge case: patch không có target string, OWL component không có template, view có model trỏ đến model chưa index. Làm đúng 100% từ đầu.
> - **Ship Wow Product:** `impact_analysis` output phải đẹp dạng cây — show được "đổi field này → 8 view + 3 method override + 2 JS patch + risk: HIGH" trong 1 màn hình, AI/human đọc xong biết ngay phải test gì.
> - **No schema ALTER (per ADR-0001):** M4 chỉ thêm node label + edge type mới vào Neo4j; KHÔNG ALTER PostgreSQL. M6 mới adopt migration tool.

**Goal:** `impact_analysis("field", "sale.order.amount_total", "17.0")` trả về danh sách chính xác mọi view target model `sale.order`, mọi method có super-call vào field đó (heuristic), mọi JS patch đụng tới component bound vào model — kèm risk_level (low/medium/high).

**Architecture:** 3 lớp bổ sung lên foundation M1–M3:

1. **TARGETS_MODEL edge** (defer từ M2) — `(:View)-[:TARGETS_MODEL]->(:Model)` — cho phép truy ngược từ Model → all Views. Prerequisite của `impact_analysis` query.
2. **JS graph extraction** — `parser_js.py` hiện chỉ produce `JSChunk` cho pgvector; M4 thêm `parse_module_graph` extract `JSPatchInfo` + `OWLCompInfo` cho Neo4j.
3. **`impact_analysis` MCP tool** — single Cypher query (UNION cho 3 entity_type) + scoring function trả risk_level.

**Tech Stack:** Re-use M1–M3 stack. Không thêm dep mới. tree-sitter-javascript đã có (M3). neo4j Python driver, fastmcp, pytest, ruff.

---

## Cấu Trúc File

```
src/indexer/
├── models.py             -- MODIFY: thêm JSPatchInfo, OWLCompInfo, JSGraphResult
├── parser_js.py          -- MODIFY: thêm parse_module_graph() (giữ parse_module() cho chunks)
└── writer_neo4j.py       -- MODIFY: TARGETS_MODEL + JSPatch/OWLComp nodes + edges + indexes

src/indexer/
└── pipeline.py           -- MODIFY: wire parser_js.parse_module_graph() vào _index_repo

src/mcp/
└── server.py             -- MODIFY: thêm _impact_analysis() + @mcp.tool() impact_analysis

tests/
├── test_models.py            -- MODIFY: tests cho JSPatchInfo, OWLCompInfo, JSGraphResult
├── test_parser_js.py         -- MODIFY: tests cho parse_module_graph (3 era × 2 patterns)
├── test_writer_neo4j.py      -- MODIFY: TARGETS_MODEL + JSPatch/OWLComp writer tests
├── test_indexer_pipeline.py  -- MODIFY: pipeline wire-up assertion (graph entity count)
├── test_mcp_server.py        -- MODIFY: impact_analysis happy path + 3 entity_type tests
├── test_mcp_impact_analysis.py -- CREATE: dedicated impact_analysis edge-case tests
└── test_output_snapshots.py  -- MODIFY: thêm contract test cho impact_analysis output

TASKS.md              -- MODIFY: M4 từ [ ] → [~] khi start, → [x] khi xong
README.md             -- MODIFY: cập nhật trạng thái M4 + bullet list 6 MCP tools
docs/thiet-ke-kien-truc.md -- MODIFY: bỏ "(planned)" khỏi schema TARGETS_MODEL/JSPatch/OWLComp
```

---

## Task 1: Models — JSPatchInfo, OWLCompInfo, JSGraphResult

**Files:**
- Modify: `src/indexer/models.py`
- Modify: `tests/test_models.py`

- [ ] **Bước 1: Failing tests** — thêm test cho 3 dataclass mới (defaults, equality).

- [ ] **Bước 2: Code** — thêm vào `models.py`:

```python
@dataclass
class JSPatchInfo:
    """A JS patch on an OWL component or legacy widget."""
    target: str            # patched component/widget name
    patch_name: str        # patch identifier (or file stem)
    module: str
    odoo_version: str
    era: str               # 'extend' (era1) | 'include' (era2) | 'patch' (era3)
    file_path: str

@dataclass
class OWLCompInfo:
    """An OWL component class declaration."""
    name: str
    module: str
    odoo_version: str
    template: str | None = None    # `static template = "..."` if found
    extends: str | None = None     # superclass name if extends Component
    bound_model: str | None = None # heuristic from props/services usage
    file_path: str = ""

@dataclass
class JSGraphResult:
    module: ModuleInfo
    patches: list[JSPatchInfo] = field(default_factory=list)
    components: list[OWLCompInfo] = field(default_factory=list)
```

**Boil-the-lake check:** `bound_model` left as Optional — M4 best-effort, M6 cải thiện với JS-side static analysis.

---

## Task 2: parser_js.parse_module_graph()

**Files:**
- Modify: `src/indexer/parser_js.py`
- Modify: `tests/test_parser_js.py`

- [ ] **Bước 1: Failing tests** — fixtures cho 5 patterns:
  1. Era 1: `var Foo = Widget.extend({ ... })` → JSPatchInfo(era='extend', target='Widget', patch_name='Foo')
  2. Era 2: `odoo.define('module.Foo', function (require) { ... })` (no `.include`) → no patch (just module)
  3. Era 2: `Foo.include({ ... })` → JSPatchInfo(era='include', target='Foo')
  4. Era 3: `patch(MyComponent.prototype, { ... })` → JSPatchInfo(era='patch', target='MyComponent')
  5. Era 3: `class FormView extends Component { static template = "x.y"; }` → OWLCompInfo(name='FormView', extends='Component', template='x.y')

- [ ] **Bước 2: Code** — function dạng `parse_module_graph(module_info) -> JSGraphResult`. Re-walk AST đã parse cho `parse_module` (DRY: extract helper `_parse_tree(filepath)` → `(tree, source)` cache để 2 hàm dùng chung).

- [ ] **Bước 3: Edge cases**:
  - Minified file >200KB → skip (giống logic hiện tại).
  - `lib/` `tests/` dir → skip.
  - File OSError → trả empty.
  - `patch()` không có string arg → patch_name = file stem.
  - Class extends nhưng tên superclass khác `Component` (vd `LegacyComponent`) → vẫn ghi, để query layer filter.

---

## Task 3: writer_neo4j — TARGETS_MODEL edge

**Files:**
- Modify: `src/indexer/writer_neo4j.py`
- Modify: `tests/test_writer_neo4j.py`

- [ ] **Bước 1: Failing test** — write một ParseResult có Model `sale.order` + ViewParseResult có ViewInfo(model='sale.order') → assert `(:View)-[:TARGETS_MODEL]->(:Model)` tồn tại.

- [ ] **Bước 2: Code** — trong `_write_view_result` (hoặc tương đương), sau khi tạo View node:

```cypher
MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
MATCH (m:Model {name: $model_name, odoo_version: $ver})
MERGE (v)-[:TARGETS_MODEL]->(m)
```

Multi-Model nodes (C1 schema): query trỏ tới *tất cả* Model node có cùng name (mỗi module 1 node) — TARGETS_MODEL fan-out là intentional, vì view có thể hiển thị field từ extension module.

- [ ] **Bước 3: Index** — thêm `CREATE INDEX IF NOT EXISTS FOR (v:View) ON (v.xmlid, v.odoo_version)` (đã có) + đảm bảo Model composite index covers query.

- [ ] **Bước 4: Update `_resolve_view`** (M2) — không bắt buộc, nhưng nice-to-have: surface "Targets model: X (N module nodes)" để user thấy edge đã connect.

---

## Task 4: writer_neo4j — JSPatch + OWLComp nodes + edges

**Files:**
- Modify: `src/indexer/writer_neo4j.py`
- Modify: `tests/test_writer_neo4j.py`

- [ ] **Bước 1: Failing tests** — separate tests cho:
  - JSPatch node MERGE key `(target, patch_name, module, odoo_version)`
  - OWLComp node MERGE key `(name, module, odoo_version)`
  - PATCHES edge: `(:JSPatch)-[:PATCHES]->(:OWLComp)` khi target match component name có sẵn → unresolved placeholder nếu không match (giống pattern INHERITS unresolved).
  - EXTENDS edge: `(:OWLComp)-[:EXTENDS]->(:OWLComp)` khi extends match.
  - DEFINED_IN edge: cả JSPatch + OWLComp đều phải có DEFINED_IN → Module.

- [ ] **Bước 2: Code** — thêm `_write_js_graph_result(tx, result: JSGraphResult)` theo pattern `_write_parse_result`. Public API: `Neo4jWriter.write_js_graph_results(results: list[JSGraphResult])`.

- [ ] **Bước 3: Index** — Neo4j composite index:
  - `(:JSPatch) ON (target, patch_name, module, odoo_version)`
  - `(:OWLComp) ON (name, module, odoo_version)`

- [ ] **Bước 4: BOUND_TO edge** (best-effort) — nếu OWLComp có `bound_model` non-null AND model exists trong cùng version → MERGE `(:OWLComp)-[:BOUND_TO]->(:Model)`. Không match → skip silently (heuristic, không tạo placeholder).

---

## Task 5: pipeline.py — wire JS graph

**Files:**
- Modify: `src/indexer/pipeline.py`
- Modify: `tests/test_indexer_pipeline.py`

- [ ] **Bước 1: Failing test** — assertion sau khi `_index_repo()` return: trong `summary` có key `js_patches` + `owl_comps`.

- [ ] **Bước 2: Code** — trong `_index_repo` (tách từ embedding block):

```python
# JS graph extraction (always, không cần embedder)
js_graph = parser_js.parse_module_graph(info)
js_graph_results.append(js_graph)
total_js_patches += len(js_graph.patches)
total_owl_comps += len(js_graph.components)
```

Sau loop modules: `writer.write_js_graph_results(js_graph_results)`.

- [ ] **Bước 3: Counter propagation** — `index_profile` + `index_all` cộng dồn `js_patches` + `owl_comps`.

**Boil-the-lake:** JS graph extraction chạy độc lập với embedding. Repo không có Ollama vẫn extract graph được — `impact_analysis` không phụ thuộc embedder.

---

## Task 6: MCP `impact_analysis` tool

**Files:**
- Modify: `src/mcp/server.py`
- Modify: `tests/test_mcp_server.py`
- Create: `tests/test_mcp_impact_analysis.py`

- [ ] **Bước 1: Failing tests** — fixtures cho 3 entity_type:
  - `("field", "sale.order.amount_total", "17.0")` → list views target sale.order, methods cùng model có super-call, JS patches cho component bound to sale.order. Risk: HIGH nếu count > 10, MEDIUM 4–10, LOW <4.
  - `("method", "sale.order.action_confirm", "17.0")` → override chain (đã có ở `_resolve_method`) + JS patches của component bound to sale.order với patch name match `action_confirm`.
  - `("model", "sale.order", "17.0")` → all views, all extension modules, all OWL bound, all dependent modules.
  - Edge: entity không tồn tại → friendly error.

- [ ] **Bước 2: Code** — function signature:

```python
@mcp.tool()
def impact_analysis(
    entity_type: str,           # 'field' | 'method' | 'model'
    entity_name: str,           # 'sale.order.amount_total' | 'sale.order'
    odoo_version: str = "auto",
) -> str:
    """List everything affected if you change <entity>. Risk-scored."""
    return _impact_analysis(entity_type, entity_name, odoo_version)
```

- [ ] **Bước 3: Cypher** — single query per branch, document inline:

```cypher
// entity_type='field', entity_name='sale.order.amount_total'
MATCH (f:Field {name: $field, model: $model, odoo_version: $v})
OPTIONAL MATCH (m:Model {name: $model, odoo_version: $v})<-[:TARGETS_MODEL]-(view:View)
OPTIONAL MATCH (m)<-[:BOUND_TO]-(comp:OWLComp)<-[:PATCHES]-(jp:JSPatch)
OPTIONAL MATCH (mth:Method {model: $model, odoo_version: $v})
        WHERE mth.has_super_call = true
RETURN ...
```

- [ ] **Bước 4: Risk scoring** — pure function `_compute_risk(view_count, method_count, js_count) -> str`. Thresholds (v0): `total >= 10 → HIGH`, `4 <= total < 10 → MEDIUM`, `total < 4 → LOW`. Document trong docstring là tunable ở M6.

- [ ] **Bước 5: Output** — tree format giống các tool khác:

```
impact_analysis(field, sale.order.amount_total, 17.0)
├─ Risk: HIGH (18 affected entities)
├─ Views (8):
│   ├─ [sale]                view_sale_order_form
│   ├─ [viin_sale]           view_sale_order_form_inherit
│   └─ ...
├─ Methods with super (4):
│   ├─ [sale]                action_confirm
│   └─ ...
├─ JS patches (3):
│   └─ [viin_sale_ext] OrderFormView via patch (era3)
└─ Dependent modules (12): viin_sale, to_sale_ext, ...
```

---

## Task 7: Anti-drift contract tests

**Files:**
- Modify: `tests/test_output_snapshots.py`

- [ ] **Bước 1**: Add snapshot test cho `impact_analysis` shape — check header line + presence của 4 sections (Views/Methods/JS/Dependent) khi data có. Empty case → check graceful empty list message.

---

## Task 8: TASKS.md + README + architecture docs

**Files:**
- Modify: `TASKS.md`
- Modify: `README.md`
- Modify: `docs/thiet-ke-kien-truc.md`

- [ ] **TASKS.md drift fix**: M4 hiện liệt kê `parser_js.py: era-aware` chưa làm. Thực tế M3 đã có era detect + chunks. Sửa M4 thành rõ hơn:
  ```
  - [ ] parser_js.py: parse_module_graph() — extract JSPatchInfo + OWLCompInfo cho Neo4j
  ```

- [ ] **README.md**: cập nhật bảng "6 MCP tools" — thay "(planned)" của `impact_analysis` thành available khi merge.

- [ ] **architecture doc**: bỏ "(planned)" / "(assigned M4)" khỏi schema lines của TARGETS_MODEL, JSPatch, OWLComp.

- [ ] **CONTRIBUTING.md**: nếu có table modules touched, thêm 2 dòng cho parser_js graph + impact_analysis.

---

## Risk & Mitigation

| Rủi ro | Mitigation |
|---|---|
| `bound_model` heuristic sai → noise trong impact_analysis | M4 ship best-effort + flag trong docstring "M6 cải thiện". Test fixture cho cả 2 case (matched / unmatched). |
| OWL component có template chỉ string ref, model bind qua orm ở runtime | Không cố guess. `bound_model=None` → skip BOUND_TO. impact_analysis vẫn match qua TARGETS_MODEL của View tham chiếu component. |
| `parse_module_graph` chậm vì re-walk AST | Cache `(tree, source)` per file, share giữa `parse_module` (chunks) + `parse_module_graph`. Tested với benchmark file ~50KB. |
| Risk threshold v0 sai cho codebase Viindoo | Document là tunable, expose qua config sau (không hard-code constant trong query). |

## Rollback Plan

1. **Trigger**: nếu `impact_analysis` produce false-positive >30% trên test set Viindoo 17.0 → ROLLBACK risk scoring sang LOW/MEDIUM threshold (giảm sensitivity), không bỏ tool.
2. **Action**: revert chỉ Task 6 (MCP tool); giữ Task 1–5 (data layer) — JSPatch + OWLComp nodes có giá trị độc lập cho `find_examples` rerank.
3. **Owner**: David Tran — gate merge sau khi run pipeline trên 1 profile thật + spot-check 3 query.

## Definition of Done

- [ ] All 8 tasks `[x]`.
- [ ] `make lint` + `make test-all` green (target: 100% coverage cho code mới như M1–M3).
- [ ] `tests/test_output_snapshots.py` có contract test cho `impact_analysis`.
- [ ] TASKS.md drift fixed; README.md "6 MCP tools" available.
- [ ] Manual smoke: `python -m src.indexer --profile viindoo_17 && curl -X POST http://localhost:8002/mcp` → impact_analysis trả về sane output cho 1 field thật.
- [ ] Commit message convention: `[ADD] mcp: impact_analysis tool` / `[ADD] indexer: JS graph extraction` / etc.

---

## Lộ trình ngày-người (AI-assisted estimate)

| Phase | Tasks | AI-assisted thời gian |
|---|---|---|
| Data layer | 1, 2, 3, 4 | ~3 giờ |
| Pipeline integration | 5 | ~30 phút |
| MCP tool + tests | 6, 7 | ~2 giờ |
| Docs | 8 | ~30 phút |
| **Total** | | **~6 giờ AI-assisted** |

(Note: theo ETHOS §4.1.1 "Boil the Lake" — không cần optimize human-day, AI làm hoàn chỉnh chi phí ~ vài giờ.)
