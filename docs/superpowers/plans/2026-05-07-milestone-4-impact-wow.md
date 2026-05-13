# Milestone 4 — "Impact Wow" Implementation Plan

> **Status:** ✓ DONE — M4 shipped 2026-05-07

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Nguyên tắc bắt buộc khi implement:**
> - **Boil the Lake:** Không bỏ qua era nào (Era 1 Widget.extend → Era 3 OWL/patch). Edge case: patch không có target string, OWL component không có template, view có model trỏ đến model chưa index. Làm đúng 100% từ đầu.
> - **Ship Wow Product:** `impact_analysis` output phải đẹp dạng cây — show được "đổi field này → 8 view + 3 method override + 2 JS patch + risk: HIGH" trong 1 màn hình, AI/human đọc xong biết ngay phải test gì.
> - **No schema ALTER (per ADR-0001):** M4 chỉ thêm node label + edge type mới vào Neo4j; KHÔNG ALTER PostgreSQL. M6 mới adopt migration tool.

**Goal:** `impact_analysis("field", "sale.order.amount_total", "17.0")` trả về danh sách views target model `sale.order`, methods cùng model có super-call, JS patches đụng tới component bound vào model — kèm risk_level (low/medium/high).

> **Field-level scope (M4 limitation):** Khi `entity_type='field'`, output là **model-scoped**, không phải field-specific. Method/JS-patch list không filter theo field name. Lý do: M4 chưa có `(:Method)-[:USES_FIELD]->(:Field)` edge — defer M5. Output có Note line + label rename `Methods on <model> with super() (N) — field-level filter not yet implemented (M5)` để user/AI agent không nhầm.

**Architecture:** 3 lớp bổ sung lên foundation M1–M3:

1. **TARGETS_MODEL edge** (defer từ M2) — `(:View)-[:TARGETS_MODEL]->(:Model)` — cho phép truy ngược từ Model → all Views. Prerequisite của `impact_analysis` query.
2. **JS graph extraction** — `parser_js.py` hiện chỉ produce `JSChunk` cho pgvector; M4 thêm `parse_module_graph` extract `JSPatchInfo` + `OWLCompInfo` cho Neo4j.
3. **`impact_analysis` MCP tool** — **5 split Cypher queries** (verify-exists / views / methods / JS-patches / dependent-modules) per entity_type + `_compute_risk` scoring function. Tách query thay vì UNION-style để tránh cross-product fan-out (TARGETS_MODEL + BOUND_TO multi-edge).

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

**Boil-the-lake check:** `bound_model` left as Optional. M4 ship best-effort heuristic (xem Task 2 Bước 4); M6 sẽ refine với JS-side static analysis sâu hơn (props.resModel runtime, template→view→model chain).

---

## Task 2: parser_js.parse_module_graph()

**Files:**
- Modify: `src/indexer/parser_js.py`
- Modify: `tests/test_parser_js.py`

- [x] **Bước 1: Failing tests** — fixtures cho 5 patterns chính:
  1. Era 1: `var Foo = Widget.extend({ ... })` → JSPatchInfo(era='extend', target='Widget', patch_name='Foo')
  2. Era 2: `odoo.define('module.Foo', function (require) { ... })` (no `.include`) → no patch (just module)
  3. Era 2: `Foo.include({ ... })` → JSPatchInfo(era='include', target='Foo')
  4. Era 3: `patch(MyComponent.prototype, { ... })` → JSPatchInfo(era='patch', target='MyComponent')
  5. Era 3: `class FormView extends Component { static template = "x.y"; }` → OWLCompInfo(name='FormView', extends='Component', template='x.y')

- [x] **Bước 2: Code** — function `parse_module_graph(module_info) -> JSGraphResult`. **Lưu ý drift vs plan ban đầu**: KHÔNG implement caching `_parse_tree` shared giữa `parse_module` + `parse_module_graph` — mỗi function tự parse AST riêng. Lý do: `parse_module` đã được M3 stabilize, không muốn refactor risk; AST parse cost ~10ms/file × ~400 module ≈ 4s — acceptable. Re-evaluate ở M6 nếu re-index speed bottleneck.

- [x] **Bước 3: Edge cases** (đều cover):
  - Minified file >200KB → skip (giống logic hiện tại).
  - `lib/` `tests/` dir → skip.
  - File OSError → trả empty.
  - `patch()` không có string arg → patch_name = file stem.
  - Class extends nhưng tên superclass khác `Component` (vd `LegacyComponent`) → vẫn ghi, để query layer filter.

- [x] **Bước 4: bound_model heuristic** (added in fix-review wave, addresses I2 dead-code):
  - `_detect_bound_model_from_class_body()` walk AST class body extract bound_model qua 2 pattern:
    - **ORM call**: `this.orm.read("<model>", ...)`, `this.orm.readGroup("<model>", ...)`, `this.orm.searchRead("<model>", ...)` → `bound_model = "<model>"`
    - **kwargs**: `{ resModel: "<model>" }` hoặc `{ model: "<model>" }` trong action call → `bound_model = "<model>"`
  - **Graceful fallback**: Dynamic expression (vd `this.orm.read(this.props.model, ...)`) → `bound_model = None` (không guess).
  - 3 fixture mới: `test_parse_module_graph_era3_bound_model_orm_call`, `test_parse_module_graph_era3_bound_model_resmodel_kwarg`, `test_parse_module_graph_era3_bound_model_none_when_dynamic`.

---

## Task 3: writer_neo4j — TARGETS_MODEL edge

**Files:**
- Modify: `src/indexer/writer_neo4j.py`
- Modify: `tests/test_writer_neo4j.py`

- [x] **Bước 1: Failing test** — write một ParseResult có Model `sale.order` + ViewParseResult có ViewInfo(model='sale.order') → assert `(:View)-[:TARGETS_MODEL]->(:Model)` tồn tại.

- [x] **Bước 2: Code** — trong `_write_view_result` (hoặc tương đương), sau khi tạo View node:

```cypher
MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
MATCH (m:Model {name: $model_name, odoo_version: $ver})
MERGE (v)-[:TARGETS_MODEL]->(m)
```

Multi-Model nodes (C1 schema): query trỏ tới *tất cả* Model node có cùng name (mỗi module 1 node) — TARGETS_MODEL fan-out là intentional, vì view có thể hiển thị field từ extension module.

- [x] **Bước 3: Index** — `(:View)` composite index `(xmlid, odoo_version)` đã có; Model composite index covers query.

- [ ] **Bước 4: Update `_resolve_view`** (M2) — KHÔNG land trong M4 (deferred). Nice-to-have: surface "Targets model: X (N module nodes)". Để M5/M6 nếu cần.

---

## Task 4: writer_neo4j — JSPatch + OWLComp nodes + edges

**Files:**
- Modify: `src/indexer/writer_neo4j.py`
- Modify: `tests/test_writer_neo4j.py`

- [x] **Bước 1: Failing tests** — separate tests cho:
  - JSPatch node MERGE key `(target, patch_name, module, odoo_version)`
  - OWLComp node MERGE key `(name, module, odoo_version)`
  - PATCHES edge: `(:JSPatch)-[:PATCHES]->(:OWLComp)` khi target match component name có sẵn → unresolved placeholder nếu không match (giống pattern INHERITS unresolved).
  - EXTENDS edge: `(:OWLComp)-[:EXTENDS]->(:OWLComp)` khi extends match (silent skip không placeholder — superclass có thể là class library bên ngoài như `Component`).
  - DEFINED_IN edge: cả JSPatch + OWLComp đều phải có DEFINED_IN → Module.
  - BOUND_TO edge: silent skip khi `bound_model` None hoặc Model không tồn tại.

- [x] **Bước 2: Code** — thêm `_write_js_graph_result(tx, result: JSGraphResult)` theo pattern `_write_parse_result`. Public API: `Neo4jWriter.write_js_graph_results(results: list[JSGraphResult])`.

- [x] **Bước 3: Index** — Neo4j composite index:
  - `(:JSPatch) ON (target, patch_name, module, odoo_version)`
  - `(:OWLComp) ON (name, module, odoo_version)`

- [x] **Bước 4: BOUND_TO edge** — nếu OWLComp có `bound_model` non-null AND model exists trong cùng version → MERGE `(:OWLComp)-[:BOUND_TO]->(:Model)`. Không match → skip silently. Real-data path active sau khi Task 2 Bước 4 implement bound_model heuristic (fix-review wave I2).

---

## Task 5: pipeline.py — wire JS graph

**Files:**
- Modify: `src/indexer/pipeline.py`
- Modify: `tests/test_indexer_pipeline.py`

- [x] **Bước 1: Failing test** — assertion sau khi `_index_repo()` return: trong `summary` có key `js_patches` + `owl_comps`.

- [x] **Bước 2: Code** — trong `_index_repo` (tách từ embedding block):

```python
# JS graph extraction (always, không cần embedder)
js_graph = parser_js.parse_module_graph(info)
js_graph_results.append(js_graph)
total_js_patches += len(js_graph.patches)
total_owl_comps += len(js_graph.components)
```

Sau loop modules: `writer.write_js_graph_results(js_graph_results)`.

- [x] **Bước 3: Counter propagation** — `index_profile` + `index_all` cộng dồn `js_patches` + `owl_comps`. Backwards compat: keys cũ (`modules`, `views`, `qweb`, `embeddings`) giữ nguyên.

**Boil-the-lake:** JS graph extraction chạy độc lập với embedding. Repo không có Ollama vẫn extract graph được — `impact_analysis` không phụ thuộc embedder.

---

## Task 6: MCP `impact_analysis` tool

**Files:**
- Modify: `src/mcp/server.py`
- Modify: `tests/test_mcp_server.py`
- Create: `tests/test_mcp_impact_analysis.py`

- [x] **Bước 1: Failing tests** — fixtures cho 3 entity_type + edge cases (14 tests total):
  - `("field", "sale.order.amount_total", "17.0")` → **model-scoped** (NOT field-specific — defer M5): list views target sale.order, methods cùng model có super-call, JS patches cho component bound to sale.order. Risk: HIGH ≥10, MEDIUM 4–9, LOW <4.
  - `("method", "sale.order.action_confirm", "17.0")` → list method nodes + JS patches của component bound to model (model-scoped, not method-name-filter).
  - `("model", "sale.order", "17.0")` → all views, all extension modules, all OWL bound, all dependent modules.
  - Edge: entity không tồn tại → friendly "not found" error.
  - Edge: invalid entity_type → message liệt kê 3 valid options.
  - Edge: unparseable entity_name (field/method không có dot) → friendly "not found".
  - Edge (fix-review I3): placeholder Model `{module: '__unresolved__'}` → reject, không pass exists check.

- [x] **Bước 2: Code** — function signature:

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

- [x] **Bước 3: Cypher** — **5 split queries per entity_type** (KHÔNG single big query — drift fix vs plan ban đầu):

```cypher
-- Query 1: existence check (with __unresolved__ filter from fix-review I3)
MATCH (m:Model {name: $mn, odoo_version: $v})
WHERE coalesce(m.unresolved, false) = false AND m.module <> '__unresolved__'
RETURN count(m) AS exists_count

-- Query 2: views targeting model (DISTINCT vì TARGETS_MODEL fan-out N module nodes)
MATCH (m:Model {name: $mn, odoo_version: $v})<-[:TARGETS_MODEL]-(view:View)
RETURN DISTINCT view.xmlid AS xmlid, view.module AS module

-- Query 3: methods (filter has_super_call cho entity_type='field')
MATCH (mth:Method {model: $mn, odoo_version: $v})
WHERE mth.has_super_call = true   -- field/method branch
RETURN DISTINCT mth.name AS name, mth.module AS module

-- Query 4: JS patches via BOUND_TO chain
MATCH (m:Model {name: $mn, odoo_version: $v})<-[:BOUND_TO]-(comp:OWLComp)<-[:PATCHES]-(jp:JSPatch)
RETURN DISTINCT jp.target, jp.patch_name, jp.module, jp.era

-- Query 5: dependent modules
MATCH (m:Model {name: $mn, odoo_version: $v})-[:DEFINED_IN]->(defmod:Module)<-[:DEPENDS_ON]-(depmod:Module)
RETURN DISTINCT depmod.name AS name
```

Lý do split: OPTIONAL MATCH lồng nhau với 2+ fan-out path (TARGETS_MODEL N edges + BOUND_TO M edges) tạo cross-product N×M trong cùng 1 query — query plan optimizer Neo4j 5 KHÔNG dedupe được tốt. Split + DISTINCT per query an toàn hơn, dễ debug.

- [x] **Bước 4: Risk scoring** — pure function `_compute_risk(view_count, method_count, js_count) -> str`. Thresholds (v0): `total >= 10 → HIGH`, `4 <= total < 10 → MEDIUM`, `total < 4 → LOW`. Comment rationale (fix-review S3): `<4 changes = isolated, 4-9 = module-scope review needed, ≥10 = cross-module impact requiring full regression. M6 will recalibrate against held-out eval set.`

- [x] **Bước 5: Output** — tree format giống các tool khác. **Field branch có Note line + label rename** (fix-review I1):

```
impact_analysis(field, sale.order.amount_total, 17.0)
├─ Note: field-level impact requires F4 USES_FIELD edge (deferred to M5). Current scope: model-level.
├─ Risk: HIGH (18 affected entities)
├─ Views (8):
│   ├─ [sale]                view_sale_order_form
│   ├─ [viin_sale]           view_sale_order_form_inherit
│   └─ ...
├─ Methods on sale.order with super() (4) — field-level filter not yet implemented (M5):
│   ├─ [sale]                action_confirm
│   └─ ...
├─ JS patches (3):
│   └─ [viin_sale_ext] OrderFormView via patch (era3)
└─ Dependent modules (12): viin_sale, to_sale_ext, ...
```

Empty section render `├─ Views: none` (no "None" leak).

---

## Task 7: Anti-drift contract tests

**Files:**
- Modify: `tests/test_output_snapshots.py`

- [x] **Bước 1**: 3 snapshot test added — header line + presence của 4 sections (Views/Methods/JS/Dependent), Risk: LOW empty case (no "None" leak), Invalid entity_type message lists 3 valid options.

---

## Task 8: TASKS.md + README + architecture docs

**Files:**
- Modify: `TASKS.md`
- Modify: `README.md`
- Modify: `docs/thiet-ke-kien-truc.md`

- [x] **TASKS.md drift fix**: rephrase từ `parser_js.py: era-aware` → `parser_js.py: parse_module_graph() — extract JSPatchInfo + OWLCompInfo cho Neo4j`.

- [x] **README.md**: bỏ "(planned)" của `impact_analysis`; legend đổi từ "M1–M3 (available now)" → "M1–M4 (available now)"; status M4 từ `[ ]` → `[x]`.

- [x] **architecture doc**: bỏ "(assigned M4 — prerequisite cho impact_analysis)" khỏi TARGETS_MODEL line.

- [ ] **CONTRIBUTING.md**: KHÔNG land trong M4 — chưa có table modules-touched.

---

## Risk & Mitigation (status sau khi M4 land)

| Rủi ro | Mitigation đã apply |
|---|---|
| `bound_model` heuristic sai → noise trong impact_analysis | **Implemented heuristic 2 pattern** (orm calls + resModel/model kwarg). Dynamic expression → `None` graceful. 3 fixture test cover. M6 sẽ refine với template→view→model chain. |
| OWL component có template chỉ string ref, model bind qua orm ở runtime | `props.resModel` runtime KHÔNG cover ở M4 (cần JS-side flow analysis). `bound_model=None` → skip BOUND_TO; impact_analysis vẫn match qua TARGETS_MODEL của View tham chiếu component. |
| `parse_module_graph` chậm vì re-walk AST | **NOT mitigated** — drift vs plan: KHÔNG implement caching `_parse_tree` shared. Risk acceptable (~4s/400 module). M6 re-evaluate. |
| Risk threshold v0 sai cho codebase Viindoo | Comment rationale explicit (fix-review S3); tunable code constant. M6 recalibrate against held-out eval set. |
| `impact_analysis(field, ...)` semantic ambiguity (model-scoped, not field-specific) | **Mitigated** (fix-review I1) — Note line + label rename "field-level filter not yet implemented (M5)". |
| Placeholder Model `__unresolved__` lọt qua exists check | **Mitigated** (fix-review I3) — Cypher filter `m.module <> '__unresolved__' AND coalesce(m.unresolved, false) = false`. |

## Rollback Plan

1. **Trigger**: nếu `impact_analysis` produce false-positive >30% trên test set Viindoo 17.0 → ROLLBACK risk scoring sang LOW/MEDIUM threshold (giảm sensitivity), không bỏ tool.
2. **Action**: revert chỉ Task 6 (MCP tool); giữ Task 1–5 (data layer) — JSPatch + OWLComp nodes có giá trị độc lập cho `find_examples` rerank.
3. **Owner**: David Tran — gate merge sau khi run pipeline trên 1 profile thật + spot-check 3 query.

## Definition of Done

- [x] All 8 tasks `[x]` (xem checkbox từng Task ở trên).
- [x] `make lint` + `make test-all` green (84 passed, 16 skipped do local pgvector ext, 0 failed).
- [x] `tests/test_output_snapshots.py` có 3 contract test cho `impact_analysis`.
- [x] TASKS.md drift fixed; README.md "6 MCP tools" available.
- [ ] Manual smoke: `python -m src.indexer --profile viindoo_17` → MCP server → call `impact_analysis` với 1 field thật. **Pending after PR merge.**
- [x] Commit message convention: 18 commit từ `435691f` đến `2333c7d` đều prefix `[ADD|IMP|FIX|REF|MERGE]`. KHÔNG có `Co-Authored-By: Claude` trailer.

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

---

## Post-merge review feedback (fix-review wave)

Sau khi PR `m4-integration → master` được review bởi `viindoo-pr-reviewer` (2026-05-07), 3 ISSUE + 3 SUGGESTION được fix qua 3 commit (`a28784f`, `490bbdf`, `2333c7d`) trên branch `m4/fix-review-feedback`, merge vào `m4-integration`. Plan này đã được update để reflect changes.

### Findings & resolutions

| ID | Severity | Vấn đề | Fix |
|---|---|---|---|
| I1 | ISSUE | `impact_analysis(field, ...)` không thực sự field-aware — output cho `amount_total` vs `partner_id` giống nhau | Note line + label rename `Methods on <model> with super() (N) — field-level filter not yet implemented (M5)`. |
| I2 | ISSUE | BOUND_TO branch dead code — `bound_model=None` luôn → JS patches section luôn empty trên data thật | Implement `_detect_bound_model_from_class_body()` heuristic 2 pattern (orm calls + resModel kwarg) + 3 test fixture. |
| I3 | ISSUE | `impact_analysis` match cả `__unresolved__` placeholder Models → false-negative empty output | Cypher filter `m.module <> '__unresolved__' AND coalesce(m.unresolved, false) = false` + 1 test. |
| S1 | SUGGESTION | Test mutate `os.environ` global → flaky risk | Refactor sang `monkeypatch.setenv()` toàn bộ; thêm `monkeypatch_module` fixture vào conftest. |
| S2 | SUGGESTION | Dead variable `pytestmark_neo4j` ở `test_mcp_impact_analysis.py:13` | Xoá dòng. |
| S3 | SUGGESTION | Risk threshold magic numbers thiếu rationale | Thêm comment calibration explicit (`<4 isolated, 4-9 module-scope, ≥10 cross-module — M6 recalibrate against held-out eval set`). |

### PRAISE (3 — giữ nguyên)

- P1: TARGETS_MODEL silent-skip invariant test locked (`test_write_view_no_target_when_model_missing`).
- P2: Era3 patch dual-form handling (`MyComp` + `MyComp.prototype`) graceful.
- P3: Commit hygiene — 18/18 commit không có `Co-Authored-By: Claude` trailer.

### Final test gate (sau fix-review)

- 84 passed (+1 vs initial 83), 16 skipped (pgvector ext local missing), 0 failed.
- ruff: clean.
- +4 test mới (3 cho I2 heuristic + 1 cho I3 placeholder).
