# Milestone 4.6 — "Pattern Wow" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development hoặc superpowers:executing-plans để implement task-by-task. Steps dùng checkbox (`- [ ]`) syntax.
>
> **Nguyên tắc bắt buộc khi implement:**
> - **Boil the Lake (ETHOS §4.1.1):** seed ≥50 PatternExample đầy đủ với gotchas + file_ref real Odoo CE. Không ship 30 pattern half-curated.
> - **Keep it simple (ETHOS §4.1.3):** convention regex map + EE_CONFUSION dict đều là static data — không over-engineer auto-detect ML.
> - **Tests trước code (TDD):** mỗi WI có failing tests trước code.
> - **No schema ALTER (per ADR-0001):** PatternExample = Neo4j node + reuse `embeddings` table với chunk_type='pattern_example'. Module/Method enrichment qua SET property. Cite ADR-0003.
> - **No `Co-Authored-By: Claude` trailer (per ADR-009).**

**Goal:** AI gọi `suggest_pattern("computed field cross-model partner_id")` → 3-5 pattern thật từ Odoo CE + Viindoo addons với gotchas; `check_module_exists("knowledge", "17.0")` → `is_ee_confusion: Yes` + `viindoo_equivalent: null` + warning explicit "Do NOT depend on Odoo Enterprise module trên stack Viindoo Community"; `find_override_point("sale.order", "action_confirm", "17.0")` → `super_safety: always`, `super_ratio: 7/7` (100% real overrides), `anti_patterns: ['old-style super(ClassName, self)', 'missing return result']`.

**Why separate milestone (vs gộp M4.5):** M4.5 đặt schema foundation (CoreSymbol/LintRule/CLI nodes). M4.6 *consume* foundation đó qua edge USES_CORE_SYMBOL — phải ship M4.5 và verify trước. Pattern curation ~50 entry là data work review-heavy (mỗi pattern cần file_ref real + gotchas validate), scope tách biệt giúp PR nhỏ + CEO review tập trung. Module/Method enrichment thuần property SET, không thêm node label, độc lập với M4.5 — vẫn tách M4.6 cho coherence theme "Pattern Wow".

**Architecture (high-level):**

```
INDEXER:
  seed_patterns.py CLI → patterns.json (~50 curated entries)
                       → MERGE (:PatternExample) Neo4j
                       → make_pattern_chunks() → embeddings (chunk_type='pattern_example')
                       → MERGE (:PatternExample)-[:USES_CORE_SYMBOL]->(:CoreSymbol) [M4.5 dep]

NEO4J GRAPH (5 layer):
  (:PatternExample {pattern_id, intent_keywords[], file_ref, snippet_text,
                    gotchas[], odoo_version_min, language})
       └─[:USES_CORE_SYMBOL]→(:CoreSymbol)         [M4.5 binding]
  (:Module ..., edition, viindoo_equivalent_qname)  [enriched]
  (:Method ..., convention_kind, super_safety, return_required)  [enriched]

POSTGRESQL:
  embeddings (chunk_type='pattern_example', module='__patterns__' sentinel)
                                                  [reuse — no ALTER]

MCP SERVER:
  suggest_pattern(intent, ver, language)
       → pgvector ANN search filter chunk_type='pattern_example' + entity_name slug
       → Neo4j batch fetch metadata via UNWIND pattern_id list
       → tree-format output: pattern + score + gotchas + file_ref + snippet
  
  check_module_exists(name, ver)
       → Neo4j MATCH Module + EE_CONFUSION static dict lookup
       → return: exists_in_ce, edition, is_ee_confusion, viindoo_equivalent
  
  find_override_point(model, method, ver)
       → Neo4j MATCH Method nodes + INHERITS chain + anti-pattern list
       → return: convention_kind, super_safety, super_ratio, anti_patterns,
                 canonical_examples (từ PatternExample USES_CORE_SYMBOL)
```

**Tech Stack:** Re-use M1-M4 + M4.5 stack. Không thêm dep mới. Pattern seed = JSON file (`json` stdlib parse). Convention detection = `re` regex. Embedder Qwen3 đã có từ M3.

---

## Dependencies on M4.5

| M4.5 artifact | M4.6 dùng | Graceful skip nếu chưa land |
|---|---|---|
| `(:CoreSymbol)` node | `(:PatternExample)-[:USES_CORE_SYMBOL]->(:CoreSymbol)` edge | WI3 silent skip MERGE edge nếu CoreSymbol vắng. Backfill sau M4.5 land + re-run `seed_patterns.py`. |
| `(:LintRule)` node | `suggest_pattern` cross-ref gotcha → relevant LintRule (V0 not implemented, defer) | N/A V0 |
| `writer_neo4j.setup_indexes()` extended | M4.6 thêm `PatternExample` index, không re-setup toàn bộ | OK độc lập |
| `_latest_version()` numeric fix (WI1) | `_resolve_version("auto", session)` cho 3 tool mới | M4.6 dùng cùng helper — graceful skip nếu hardcode "17.0" còn sót |

**Rule:** M4.6 KHÔNG block nếu M4.5 chưa ship. Pattern seed có thể land trước; USES_CORE_SYMBOL edge fill sau.

---

## Cấu Trúc File

```
src/indexer/
├── models.py                -- MODIFY: thêm PatternExample dataclass + ModuleInfo.edition + MethodInfo.convention_kind
├── parser_python.py         -- MODIFY: _detect_module_edition() + _classify_method_convention()
├── writer_neo4j.py          -- MODIFY: write_pattern_examples() + Module SET edition + Method SET convention_kind + index PatternExample
├── writer_pgvector.py       -- MODIFY: make_pattern_chunks() helper
└── seed_patterns.py         -- CREATE: one-shot CLI đọc patterns.json → ghi Neo4j + embed

src/data/
├── patterns.json            -- CREATE: ~50 curated PatternExample entries
└── ee_modules.py            -- CREATE: EE_CONFUSION dict (16 entries với Viindoo equivalent)

src/mcp/
└── server.py                -- MODIFY: _suggest_pattern + _check_module_exists + _find_override_point + 3 @mcp.tool() wrappers

tests/
├── test_models.py               -- MODIFY: PatternExample dataclass + Module/Method new fields
├── test_parser_python.py        -- MODIFY: _detect_module_edition + _classify_method_convention
├── test_writer_neo4j.py         -- MODIFY: write_pattern_examples + Module edition + Method convention_kind + index
├── test_writer_pgvector.py      -- MODIFY: make_pattern_chunks
├── test_seed_patterns.py        -- CREATE: JSON valid + required IDs + non-empty + CLI smoke
├── test_mcp_pattern_tools.py    -- CREATE: 12 tests cho 3 tool mới
└── test_output_snapshots.py     -- MODIFY: 3 contract test

docs/
├── adr/0003-pattern-example-storage.md         -- (đã land Pre-work)
├── superpowers/plans/2026-05-08-milestone-4-6-pattern-wow.md -- (this document)
└── thiet-ke-kien-truc.md                       -- MODIFY (WI7): PatternExample schema + 3 tool

TASKS.md      -- MODIFY (WI7): M4.6 từ [ ] → [~]/[x]
README.md     -- MODIFY (WI7): trạng thái M4.6 + tool count
```

---

## Work Item 0: ADR-0003 review + merge

- [ ] David review ADR-0003 — PatternExample = Neo4j node + reuse embeddings; Module/Method enrichment qua SET; language slug encoding; ADR-0001 compliance.
- [ ] Status `Draft` → `Accepted` sau approve.
- [ ] Reference ADR-0003 trong CONTRIBUTING.md (sẽ làm WI7).

**Effort:** ~15 phút (review only).
**Dependencies:** Không có.

---

## Work Item 1: Module enrichment — `edition` + `viindoo_equivalent_qname`

**Files:**
- Modify: `src/indexer/parser_python.py` — thêm `_detect_module_edition()`
- Modify: `src/indexer/models.py` — `ModuleInfo` thêm 2 field
- Modify: `src/indexer/writer_neo4j.py` — Module MERGE thêm SET edition + viindoo_equivalent_qname
- Create: `src/data/ee_modules.py` — `EE_CONFUSION` dict
- Modify: `tests/test_parser_python.py`, `tests/test_writer_neo4j.py`

### Bước 1: Failing tests (7 tests)

```python
def test_detect_edition_viindoo_prefix():
    assert _detect_module_edition({}, "viin_helpdesk", "/any/path") == "viindoo"
    assert _detect_module_edition({}, "to_quality", "/any/path") == "viindoo"

def test_detect_edition_viindoo_path():
    assert _detect_module_edition({}, "anymod", "/home/x/acme_addons17/anymod") == "viindoo"
    assert _detect_module_edition({}, "anymod", "/home/x/acme_enterprise17/anymod") == "viindoo"

def test_detect_edition_oca():
    assert _detect_module_edition({"license": "OCA-AGPL-3"}, "x", "/path") == "oca"

def test_detect_edition_community():
    assert _detect_module_edition(
        {"license": "LGPL-3"}, "sale", "/home/x/odoo17/odoo/addons/sale"
    ) == "community"

def test_detect_edition_fallback_custom():
    assert _detect_module_edition({}, "x", "/path") == "custom"

def test_module_info_has_edition_default():
    m = ModuleInfo(name="x", odoo_version="17.0", path="/x", manifest_path="/x/__manifest__.py")
    assert m.edition == "community"  # default
    assert m.viindoo_equivalent_qname is None

def test_writer_module_edition_set(neo4j_session):
    m = ModuleInfo(name="x", odoo_version="17.0", edition="viindoo",
                  viindoo_equivalent_qname="viin_helpdesk", ...)
    writer.write_modules([m])
    rec = neo4j_session.run(
        "MATCH (m:Module {name:'x'}) RETURN m.edition AS ed, m.viindoo_equivalent_qname AS vvq"
    ).single()
    assert rec["ed"] == "viindoo"
    assert rec["vvq"] == "viin_helpdesk"
```

### Bước 2: Code

**`src/data/ee_modules.py`:**
```python
"""EE confusion list — Odoo Enterprise modules vắng trên stack Community/Viindoo.

Source: 2026-05-08 survey 16 modules verified absent từ
~/git/odoo{17,18,19}/odoo/addons/. Mapping → Viindoo equivalent
từ acme_addons17 + acme_enterprise17 surveyed addons.

DO NOT DEPEND on these modules in Viindoo Community stack — vi phạm
GPL/Enterprise license boundary (per CLAUDE.md §2 stack rule).
"""

_SOURCE_DATE = "2026-05-08"

EE_CONFUSION: dict[str, str | None] = {
    # module_name: viindoo_equivalent_qname (None = no equivalent)
    "knowledge": None,
    "documents": "viin_document",
    "helpdesk": "viin_helpdesk",
    "marketing_automation": None,
    "quality": "to_quality",
    "industry_fsm": None,
    "appointment": "viin_appointment",
    "planning": None,
    "sign": "viin_sign",
    "social": "viin_social",
    "voip": None,
    "whatsapp": None,
    "mrp_plm": "to_mrp_plm",
    "accountant": "to_account_accountant",
    "web_studio": None,
    "web_enterprise": None,
}
```

**`src/indexer/parser_python.py`:**
```python
def _detect_module_edition(manifest: dict, module_name: str, module_path: str) -> str:
    """Detect edition: viindoo|enterprise|community|oca|custom."""
    # Viindoo: prefix or path
    if module_name.startswith(("viin_", "to_")):
        return "viindoo"
    if any(seg in module_path for seg in ("acme_addons", "acme_enterprise")):
        return "viindoo"
    # OCA license
    license_v = manifest.get("license", "").upper()
    if "OCA" in license_v:
        return "oca"
    # Community: Odoo CE addons path + LGPL/GPL
    if license_v in ("LGPL-3", "LGPL-3.0", "GPL-3", "AGPL-3"):
        if "/odoo/addons/" in module_path or "/addons/" in module_path:
            return "community"
    return "custom"

def _detect_viindoo_equivalent(module_name: str) -> str | None:
    """Lookup EE_CONFUSION dict cho Viindoo equivalent."""
    from src.data.ee_modules import EE_CONFUSION
    return EE_CONFUSION.get(module_name)
```

`scanner.py` hoặc `registry.py` populate edition + viindoo_equivalent_qname vào `ModuleInfo` khi build.

**`models.py`:**
```python
@dataclass
class ModuleInfo:
    # existing fields...
    edition: str = "community"
    viindoo_equivalent_qname: str | None = None
```

**`writer_neo4j.py`:** Module MERGE SET thêm `m.edition = $edition, m.viindoo_equivalent_qname = $vvq`.

### Bước 3: Verify

- 7 test mới green
- Manual: index repo Viindoo → query `MATCH (m:Module) RETURN m.edition, count(*)` → distribution viindoo/community/custom phân biệt rõ

**Effort:** ~45 phút
**Dependencies:** Không có M4.5 dep.

---

## Work Item 2: Method enrichment — `convention_kind` + `super_safety` + `return_required`

**Files:**
- Modify: `src/indexer/parser_python.py` — `_classify_method_convention()`
- Modify: `src/indexer/models.py` — `MethodInfo` thêm 3 field
- Modify: `src/indexer/writer_neo4j.py` — Method MERGE SET 3 prop
- Modify: `tests/test_parser_python.py`, `tests/test_writer_neo4j.py`

### Bước 1: Failing tests (8 tests)

```python
def test_classify_compute():
    assert _classify_method_convention("_compute_amount") == ("compute", "never", False)

def test_classify_inverse():
    assert _classify_method_convention("_inverse_amount") == ("inverse", "never", False)

def test_classify_search_method():
    assert _classify_method_convention("_search_partner_id") == ("search", "never", False)

def test_classify_action():
    assert _classify_method_convention("action_confirm") == ("action", "always", True)

def test_classify_crud_create():
    assert _classify_method_convention("create") == ("crud", "always", True)

def test_classify_prepare():
    assert _classify_method_convention("_prepare_invoice_values") == ("prepare", "usually", False)

def test_classify_public_no_underscore():
    assert _classify_method_convention("compute_total") == ("public", "usually", False)

def test_writer_method_convention_set(neo4j_session):
    m = MethodInfo(name="action_confirm", model="sale.order", module="sale",
                  odoo_version="17.0", convention_kind="action",
                  super_safety="always", return_required=True)
    writer.write_methods([m])
    rec = neo4j_session.run(
        "MATCH (mth:Method {name:'action_confirm'}) RETURN mth.convention_kind AS k"
    ).single()
    assert rec["k"] == "action"
```

### Bước 2: Code

**`src/indexer/parser_python.py`:**
```python
import re

# Order matters — first match wins
_CONVENTION_MAP: list[tuple[re.Pattern, tuple[str, str, bool]]] = [
    (re.compile(r"^_compute_"),         ("compute",  "never",   False)),
    (re.compile(r"^_inverse_"),         ("inverse",  "never",   False)),
    (re.compile(r"^_search_"),          ("search",   "never",   False)),
    (re.compile(r"^_get_default_"),     ("default",  "never",   False)),
    (re.compile(r"^_get_"),             ("builder",  "usually", False)),
    (re.compile(r"^_prepare_"),         ("prepare",  "usually", False)),
    (re.compile(r"^_check_"),           ("check",    "usually", False)),
    (re.compile(r"^action_"),           ("action",   "always",  True)),
    (re.compile(r"^(create|write|unlink|copy)$"), ("crud", "always", True)),
    (re.compile(r"^_"),                 ("private",  "usually", False)),
]
_DEFAULT_CONVENTION: tuple[str, str, bool] = ("public", "usually", False)

def _classify_method_convention(method_name: str) -> tuple[str, str, bool]:
    """Trả (convention_kind, super_safety, return_required)."""
    for pattern, result in _CONVENTION_MAP:
        if pattern.match(method_name):
            return result
    return _DEFAULT_CONVENTION
```

**`models.py`:**
```python
@dataclass
class MethodInfo:
    # existing fields...
    convention_kind: str = "private"
    super_safety: str = "usually"
    return_required: bool = False
```

`_extract_class_methods()` populate 3 field qua `_classify_method_convention(method.name)`.

**`writer_neo4j.py`:** Method MERGE SET thêm `mth.convention_kind = $ck, mth.super_safety = $ss, mth.return_required = $rr`.

### Bước 3: Verify

- 8 test mới green
- Manual: index Odoo CE → `MATCH (m:Method) RETURN m.convention_kind, count(*)` → distribution có compute/action/crud/prepare phân biệt

**Effort:** ~30 phút
**Dependencies:** Không có M4.5 dep.

---

## Work Item 3: PatternExample schema — Neo4j node + embed pipeline

**Files:**
- Modify: `src/indexer/models.py` — `PatternExample` dataclass
- Modify: `src/indexer/writer_neo4j.py` — `write_pattern_examples()` + index
- Modify: `src/indexer/writer_pgvector.py` — `make_pattern_chunks()`
- Modify: `tests/test_writer_neo4j.py`, `tests/test_writer_pgvector.py`

### Bước 1: Failing tests (7 tests)

```python
def test_pattern_example_dataclass():
    pe = PatternExample(
        pattern_id="computed-field-cross-model",
        intent_keywords=["computed", "depends", "cross-model"],
        file_ref="addons/sale/models/sale_order.py:245",
        snippet_text="@api.depends(...)\ndef _compute(self): ...",
        gotchas=["Missing Many2one root in path"],
        odoo_version_min="17.0",
        language="python",
    )
    assert pe.core_symbol_names == []  # default empty

def test_make_pattern_chunks_chunk_type():
    pe = PatternExample(pattern_id="x", language="python", odoo_version_min="17.0",
                       snippet_text="x", gotchas=["g1"], file_ref="f", intent_keywords=[])
    chunks = make_pattern_chunks([pe])
    assert chunks[0].chunk_type == "pattern_example"

def test_make_pattern_chunks_module_sentinel():
    pe = PatternExample(...)
    chunks = make_pattern_chunks([pe])
    assert chunks[0].module == "__patterns__"

def test_make_pattern_chunks_entity_name_slug():
    pe = PatternExample(pattern_id="computed-field", language="python", ...)
    chunks = make_pattern_chunks([pe])
    assert chunks[0].entity_name == "python__computed-field"

def test_write_pattern_example_node_created(neo4j_session):
    pe = PatternExample(pattern_id="p1", ...)
    writer.write_pattern_examples([pe])
    rec = neo4j_session.run(
        "MATCH (p:PatternExample {pattern_id:'p1'}) RETURN p"
    ).single()
    assert rec is not None

def test_write_pattern_example_uses_core_symbol_edge_when_target_exists(neo4j_session):
    """USES_CORE_SYMBOL edge to existing CoreSymbol."""
    neo4j_session.run("MERGE (cs:CoreSymbol {qualified_name:'odoo.api.depends', odoo_version:'17.0'})")
    pe = PatternExample(pattern_id="p1", odoo_version_min="17.0",
                       core_symbol_names=["odoo.api.depends"], ...)
    writer.write_pattern_examples([pe])
    rec = neo4j_session.run("""
        MATCH (p:PatternExample {pattern_id:'p1'})-[:USES_CORE_SYMBOL]->(cs)
        RETURN cs.qualified_name AS qn
    """).single()
    assert rec["qn"] == "odoo.api.depends"

def test_write_pattern_example_skip_edge_when_core_symbol_missing(neo4j_session):
    """USES_CORE_SYMBOL silent skip nếu CoreSymbol vắng (M4.5 chưa ship)."""
    pe = PatternExample(pattern_id="p2", core_symbol_names=["nonexistent_xyz"], ...)
    writer.write_pattern_examples([pe])
    rec = neo4j_session.run(
        "MATCH (p:PatternExample {pattern_id:'p2'})-[:USES_CORE_SYMBOL]->() RETURN count(*) AS c"
    ).single()
    assert rec["c"] == 0
```

### Bước 2: Code

**`models.py`:**
```python
@dataclass
class PatternExample:
    pattern_id: str
    intent_keywords: list[str]
    file_ref: str            # 'addons/sale/models/sale_order.py:245'
    snippet_text: str        # 3-5 lines canonical
    gotchas: list[str]
    odoo_version_min: str
    language: str            # 'python'|'xml'|'js'
    core_symbol_names: list[str] = field(default_factory=list)
```

**`writer_pgvector.py`:**
```python
def make_pattern_chunks(patterns: list[PatternExample]) -> list[EmbeddingChunk]:
    """Encode language vào entity_name slug để tránh ALTER embeddings (per ADR-0003)."""
    chunks = []
    for p in patterns:
        # Combine snippet + gotchas vào 1 text cho embedding
        text = p.snippet_text + "\n---\n" + "\n".join(p.gotchas)
        chunks.append(EmbeddingChunk(
            chunk_type="pattern_example",
            module="__patterns__",
            odoo_version=p.odoo_version_min,
            entity_name=f"{p.language}__{p.pattern_id}",
            file_path=p.file_ref,
            chunk_idx=0,
            text=text,
            # vec populated by embedder pipeline
        ))
    return chunks
```

**`writer_neo4j.py`:**
```python
def write_pattern_examples(self, patterns: list[PatternExample]) -> None:
    with self._driver.session() as sess:
        for batch in _chunked(patterns, 200):
            sess.execute_write(self._write_pattern_examples_batch, batch)

def _write_pattern_examples_batch(tx, patterns):
    for p in patterns:
        tx.run("""
            MERGE (pe:PatternExample {pattern_id: $pid})
            SET pe.intent_keywords = $kw,
                pe.file_ref = $fr,
                pe.snippet_text = $sn,
                pe.gotchas = $g,
                pe.odoo_version_min = $vmin,
                pe.language = $lang
        """, pid=p.pattern_id, kw=p.intent_keywords, fr=p.file_ref,
             sn=p.snippet_text, g=p.gotchas, vmin=p.odoo_version_min, lang=p.language)
        # USES_CORE_SYMBOL edge — silent skip nếu CoreSymbol không tồn tại
        for cs_name in p.core_symbol_names:
            tx.run("""
                MATCH (pe:PatternExample {pattern_id: $pid})
                MATCH (cs:CoreSymbol {odoo_version: $v})
                WHERE cs.qualified_name = $cs OR cs.qualified_name ENDS WITH '.' + $cs
                MERGE (pe)-[:USES_CORE_SYMBOL]->(cs)
            """, pid=p.pattern_id, v=p.odoo_version_min, cs=cs_name)

# setup_indexes() thêm:
# CREATE INDEX pattern_id_idx IF NOT EXISTS FOR (n:PatternExample) ON (n.pattern_id, n.odoo_version_min)
```

### Bước 3: Verify

- 7 test green
- Manual: gọi `write_pattern_examples` với 3 sample → query `MATCH (p:PatternExample) RETURN count(*)` = 3

**Effort:** ~45 phút
**Dependencies:** M4.5 (USES_CORE_SYMBOL — graceful skip nếu chưa ship; test #6 skipped khi CoreSymbol vắng).

---

## Work Item 4: Pattern seed curation — ~50 entries + seed_patterns.py CLI

**Files:**
- Create: `src/data/patterns.json` — ~50 PatternExample entries
- Create: `src/indexer/seed_patterns.py` — one-shot CLI
- Create: `tests/test_seed_patterns.py`

### Pattern coverage bắt buộc (15 ID core + ~35 biến thể)

| Pattern ID | Language | Mô tả |
|---|---|---|
| `computed-field-cross-model` | python | `@api.depends('partner_id.country_id.name')` |
| `computed-field-lang-context` | python | `@api.depends_context('lang')` cho translatable |
| `create-multi-v17` | python | `@api.model_create_multi` thay `@api.model + create(vals)` |
| `write-read-before-super` | python | Đọc old value trước `super().write()` |
| `xpath-avoid-replace` | xml | `position="inside/after"` thay `position="replace"` |
| `xpath-specific-expr` | xml | Specific xpath tránh ambiguous match |
| `owl-patch-v17` | js | `patch(Class.prototype, {...})` thay `Class.include()` |
| `inherits-vs-inherit` | python | `_inherits` (delegation) vs `_inherit` (extension) |
| `action-return-super` | python | `result = super().action_*(); ...; return result` |
| `crud-return-value` | python | `create/write/unlink` phải return + preserve super result |
| `old-style-super` (anti) | python | `super(ClassName, self)` → use `super()` |
| `missing-return-override` (anti) | python | Override không `return` result |
| `store-computed-field` | python | `store=True` computed cần index nếu dùng trong domain |
| `model-create-multi-batch` | python | `@api.model_create_multi(vals_list)` not single dict |
| `depends-full-dotted-path` | python | Include Many2one root + dotted path |

+ ~35 biến thể: `aggregator-not-group-operator` (anti), `name-get-removed-v18` (anti), `field-states-removed` (anti), `safe-eval-v19-signature` (anti), `read-group-v19-deprecated` (anti), `query-init-cr-vs-env` (anti), `mail-thread-mixin-inherit`, `mail-activity-mixin`, `recompute-store-true`, `compute-recursive-true`, `monetary-currency-field`, `selection-extend`, `many2many-relation-table`, `domain-leaf-tuple-format`, `context-as-frozendict`, `with-company-context`, `sudo-vs-with-user`, `record-rule-domain-force`, `sql-constraint-name-uniq`, `sql-injection-cr-execute-safe`, `pattern-orm-search-fetch-v17` ...

### Bước 1: Failing tests (5 tests)

```python
def test_patterns_json_valid():
    """patterns.json parse valid + non-empty."""
    data = json.loads(Path("src/data/patterns.json").read_text())
    assert isinstance(data, list)
    assert len(data) >= 50

def test_patterns_required_ids_present():
    """15 core IDs phải có."""
    data = json.loads(Path("src/data/patterns.json").read_text())
    ids = {p["pattern_id"] for p in data}
    required = {
        "computed-field-cross-model", "computed-field-lang-context",
        "create-multi-v17", "write-read-before-super", "xpath-avoid-replace",
        "owl-patch-v17", "inherits-vs-inherit", "action-return-super",
        "crud-return-value", "old-style-super", "missing-return-override",
        "store-computed-field", "model-create-multi-batch", "xpath-specific-expr",
        "depends-full-dotted-path",
    }
    missing = required - ids
    assert not missing, f"Missing required pattern IDs: {missing}"

def test_patterns_no_empty_snippet():
    data = json.loads(Path("src/data/patterns.json").read_text())
    for p in data:
        assert p["snippet_text"].strip(), f"Empty snippet for {p['pattern_id']}"

def test_patterns_no_empty_gotchas():
    data = json.loads(Path("src/data/patterns.json").read_text())
    for p in data:
        assert len(p["gotchas"]) >= 1, f"No gotchas for {p['pattern_id']}"

def test_seed_cli_smoke(tmp_path, monkeypatch):
    """seed_patterns.py --no-embed chạy không crash."""
    # Mock Neo4j writer + skip embedder
    result = run_seed_cli(["--version", "17.0", "--no-embed"])
    assert result.returncode == 0
```

### Bước 2: Code

**`src/data/patterns.json`** (sample 3 entries; full ~50 cần curate manual):
```json
[
  {
    "pattern_id": "computed-field-cross-model",
    "intent_keywords": ["computed", "depends", "cross-model", "partner_id", "country", "related"],
    "file_ref": "addons/account/models/account_move.py:1460",
    "snippet_text": "@api.depends('company_id.account_fiscal_country_id', 'fiscal_position_id', 'fiscal_position_id.country_id')\ndef _compute_tax_country_id(self):\n    for record in self:\n        record.tax_country_id = record.company_id.account_fiscal_country_id",
    "gotchas": [
      "Missing Many2one root in @api.depends path — use full dotted path 'fiscal_position_id' AND 'fiscal_position_id.country_id'",
      "Don't add @api.depends on related= field — Odoo auto-declares dependency chain",
      "Cross-company field needs additional @api.depends_context('company') if multi-company aware"
    ],
    "odoo_version_min": "17.0",
    "language": "python",
    "core_symbol_names": ["odoo.api.depends"]
  },
  {
    "pattern_id": "create-multi-v17",
    "intent_keywords": ["create", "model_create_multi", "vals_list", "batch", "override"],
    "file_ref": "addons/sale/models/sale_order.py:813",
    "snippet_text": "@api.model_create_multi\ndef create(self, vals_list):\n    for vals in vals_list:\n        if vals.get('name', _('New')) == _('New'):\n            vals['name'] = self.env['ir.sequence'].next_by_code('sale.order') or _('New')\n    return super().create(vals_list)",
    "gotchas": [
      "Don't use @api.model + def create(self, vals) — silently broken on batch import",
      "super().create(vals_list) takes the full list, not single dict in loop",
      "Old @api.model_create_single is for backward compat only — defaults to multi in v17+"
    ],
    "odoo_version_min": "17.0",
    "language": "python",
    "core_symbol_names": ["odoo.api.model_create_multi"]
  },
  {
    "pattern_id": "old-style-super",
    "intent_keywords": ["super", "anti-pattern", "python2-style", "method-resolution"],
    "file_ref": "addons/sale_crm/models/sale_order.py:14",
    "snippet_text": "# ANTI-PATTERN — Python 2 idiom, dùng cho legacy nhưng không cần thiết Python 3:\nres = super(SaleOrder, self.with_context(...)).action_confirm()\n\n# CORRECT — Python 3 super():\nres = super().action_confirm()",
    "gotchas": [
      "Python 3 super() tự bind ClassName — không cần truyền explicit",
      "Exception: khi cần super() trên context-modified self → super(ClassName, self.with_context(...)) vẫn đúng",
      "Mixed style trong cùng class gây đọc khó — chọn 1 style nhất quán"
    ],
    "odoo_version_min": "8.0",
    "language": "python",
    "core_symbol_names": []
  }
]
```

**`src/indexer/seed_patterns.py`:**
```python
"""One-shot CLI: load patterns.json → write Neo4j + embed pgvector.

Usage:
    python -m src.indexer.seed_patterns [--version 17.0] [--no-embed]
"""
import argparse
import json
from pathlib import Path

from src.indexer.models import PatternExample
from src.indexer.writer_neo4j import Neo4jWriter
from src.indexer.writer_pgvector import PgvectorWriter, make_pattern_chunks

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="17.0", help="odoo_version_min for patterns")
    parser.add_argument("--no-embed", action="store_true", help="Skip pgvector embed step")
    parser.add_argument("--patterns-file", default="src/data/patterns.json")
    args = parser.parse_args()
    
    raw = json.loads(Path(args.patterns_file).read_text())
    patterns = [PatternExample(**p) for p in raw if p["odoo_version_min"] == args.version]
    
    print(f"Loading {len(patterns)} patterns for v{args.version}...")
    
    n4j = Neo4jWriter(...)
    n4j.setup_indexes()
    n4j.write_pattern_examples(patterns)
    print(f"Neo4j: {len(patterns)} PatternExample nodes written")
    
    if not args.no_embed:
        chunks = make_pattern_chunks(patterns)
        # ... embed + write pgvector
        pg = PgvectorWriter(...)
        pg.write_chunks(chunks)
        print(f"Embeddings: {len(chunks)} chunks written")

if __name__ == "__main__":
    main()
```

### Bước 3: Verify

- 5 test green
- Manual: `python -m src.indexer.seed_patterns --version 17.0 --no-embed` → output `50 PatternExample nodes written`
- Manual full: chạy với embed → query Neo4j: `MATCH (p:PatternExample) RETURN count(p)` = 50; query pgvector: `SELECT count(*) FROM embeddings WHERE chunk_type='pattern_example'` = 50

**Effort:** ~2 giờ (content curation chiếm phần lớn)
**Dependencies:** WI3 (PatternExample dataclass + writer)

---

## Work Item 5: 3 MCP Tool Implementation

**Files:**
- Modify: `src/mcp/server.py` — `_suggest_pattern`, `_check_module_exists`, `_find_override_point` + 3 wrapper
- Create: `tests/test_mcp_pattern_tools.py`

### Bước 1: Failing tests (12 tests)

```python
class TestSuggestPattern:
    def test_happy_path(self, neo4j_seeded, mcp_server):
        result = mcp_server._suggest_pattern(
            "compute field across related model partner",
            "17.0", "python")
        # Top match should be computed-field-cross-model
        assert "computed-field-cross-model" in result
        assert "Many2one root" in result  # gotcha
    
    def test_no_embedder_graceful_fallback(self, mcp_server_no_embedder):
        result = mcp_server_no_embedder._suggest_pattern("x", "17.0", "python")
        # Fallback to Neo4j text match — should not crash
        assert "suggest_pattern" in result
    
    def test_empty_intent(self, mcp_server):
        result = mcp_server._suggest_pattern("", "17.0", "python")
        assert "intent" in result.lower() and ("required" in result.lower() or "empty" in result.lower())
    
    def test_invalid_language(self, mcp_server):
        result = mcp_server._suggest_pattern("x", "17.0", "fortran")
        assert "valid" in result.lower()

class TestCheckModuleExists:
    def test_community_module(self, neo4j_seeded, mcp_server):
        # sale module indexed in CE
        result = mcp_server._check_module_exists("sale", "17.0")
        assert "Yes" in result or "Indexed" in result
        assert "community" in result.lower()
    
    def test_ee_confusion_not_indexed(self, mcp_server):
        # knowledge module — EE only, never indexed in Viindoo
        result = mcp_server._check_module_exists("knowledge", "17.0")
        assert "EE" in result or "Enterprise" in result
        assert "Do NOT" in result or "warning" in result.lower()
    
    def test_ee_confusion_with_viindoo_equivalent(self, mcp_server):
        result = mcp_server._check_module_exists("helpdesk", "17.0")
        assert "viin_helpdesk" in result
    
    def test_module_not_found_not_ee(self, mcp_server):
        result = mcp_server._check_module_exists("nonexistent_xyz", "17.0")
        assert "not found" in result.lower() or "Indexed: No" in result
    
    def test_viindoo_module_edition(self, neo4j_seeded, mcp_server):
        result = mcp_server._check_module_exists("viin_helpdesk", "17.0")
        assert "viindoo" in result.lower()

class TestFindOverridePoint:
    def test_action_method_super_ratio(self, neo4j_seeded, mcp_server):
        # 7/7 overrides của sale.order.action_confirm gọi super()
        result = mcp_server._find_override_point("sale.order", "action_confirm", "17.0")
        assert "always" in result.lower()  # super_safety
        assert "anti-pattern" in result.lower()
    
    def test_compute_method_super_never(self, neo4j_seeded, mcp_server):
        result = mcp_server._find_override_point("sale.order", "_compute_amount_total", "17.0")
        assert "never" in result.lower() or "compute" in result.lower()
    
    def test_method_not_found(self, mcp_server):
        result = mcp_server._find_override_point("sale.order", "nonexistent", "17.0")
        assert "not found" in result.lower()
```

### Bước 2: Code

**`src/mcp/server.py`** (3 private + 3 wrapper):

```python
def _suggest_pattern(intent: str, odoo_version: str = "auto",
                     language: str = "python", limit: int = 5) -> str:
    if not intent.strip():
        return "suggest_pattern: intent is required (empty)"
    if language not in ("python", "xml", "js", "all"):
        return f"suggest_pattern: invalid language. Valid: python, xml, js, all"
    
    with self._driver.session() as sess:
        v = _resolve_version(odoo_version, sess)
    
    # 1. Embed intent + pgvector ANN search
    embedder = self._embedder  # may be None
    if embedder:
        intent_vec = embedder.embed([intent])[0]
        with self._pg_conn.cursor() as cur:
            lang_filter = f"AND entity_name LIKE '{language}__%'" if language != "all" else ""
            cur.execute(f"""
                SELECT entity_name, 1 - (vec <=> %s::vector) AS cosine
                FROM embeddings
                WHERE chunk_type = 'pattern_example'
                  AND odoo_version = %s
                  {lang_filter}
                ORDER BY vec <=> %s::vector ASC
                LIMIT %s
            """, [intent_vec, v, intent_vec, limit])
            ranked = cur.fetchall()
    else:
        # Fallback: Neo4j text contains
        ranked = self._fallback_text_search(intent, v, language, limit)
    
    if not ranked:
        return f"suggest_pattern({intent!r}, {v!r}): no matches"
    
    # 2. Fetch metadata from Neo4j by pattern_id list
    pattern_ids = [name.split("__", 1)[1] for (name, _score) in ranked]
    with self._driver.session() as sess:
        records = sess.run("""
            UNWIND $ids AS pid
            MATCH (p:PatternExample {pattern_id: pid})
            RETURN p.pattern_id AS id, p.intent_keywords AS kw, p.file_ref AS fr,
                   p.snippet_text AS sn, p.gotchas AS g, p.language AS lang
        """, ids=pattern_ids).data()
    
    return _format_suggest_pattern(records, ranked, intent, v)

def _check_module_exists(name: str, odoo_version: str = "auto") -> str:
    from src.data.ee_modules import EE_CONFUSION
    
    is_ee_confusion = name in EE_CONFUSION
    viindoo_equivalent = EE_CONFUSION.get(name) if is_ee_confusion else None
    
    with self._driver.session() as sess:
        v = _resolve_version(odoo_version, sess)
        rec = sess.run("""
            MATCH (m:Module {name: $n, odoo_version: $v})
            RETURN m.edition AS edition, m.viindoo_equivalent_qname AS vvq
        """, n=name, v=v).single()
    
    indexed = rec is not None
    edition = rec["edition"] if rec else None
    
    return _format_check_module_exists(
        name=name, version=v, indexed=indexed, edition=edition,
        is_ee_confusion=is_ee_confusion, viindoo_equivalent=viindoo_equivalent,
    )

def _find_override_point(model: str, method: str, odoo_version: str = "auto") -> str:
    with self._driver.session() as sess:
        v = _resolve_version(odoo_version, sess)
        records = sess.run("""
            MATCH (mth:Method {name: $method, model: $model, odoo_version: $v})
            RETURN mth.module AS module, mth.convention_kind AS ck,
                   mth.super_safety AS ss, mth.return_required AS rr,
                   coalesce(mth.has_super_call, false) AS has_super
        """, method=method, model=model, v=v).data()
    
    if not records:
        return f"find_override_point({model!r}, {method!r}, {v!r}): method not found"
    
    super_count = sum(1 for r in records if r["has_super"])
    super_ratio = f"{super_count}/{len(records)}"
    
    convention_kind = records[0]["ck"]
    super_safety = records[0]["ss"]
    return_required = records[0]["rr"]
    anti_patterns = _anti_patterns_for_convention(convention_kind)
    
    return _format_find_override_point(
        model=model, method=method, version=v, records=records,
        super_ratio=super_ratio, convention_kind=convention_kind,
        super_safety=super_safety, return_required=return_required,
        anti_patterns=anti_patterns,
    )

def _anti_patterns_for_convention(kind: str) -> list[str]:
    base = ["Old-style super(ClassName, self) — use super() in Python 3"]
    if kind in ("action", "crud"):
        base.append("Missing return — must return result of super()")
    if kind == "crud" and kind == "create":
        base.append("Missing @api.model_create_multi decorator")
    if kind == "compute":
        return ["super() should NOT be called in compute method (no inheritance chain)"]
    return base

# 3 @mcp.tool() wrappers với docstring đầy đủ
```

### Bước 3: Verify

- 12 test green
- Manual smoke (sau khi seed_patterns + index Viindoo CE 17):
  - `suggest_pattern("compute field cross-model partner_id", "17.0", "python")` → top result `computed-field-cross-model` + 3 gotchas
  - `check_module_exists("knowledge", "17.0")` → `is_ee_confusion: Yes`, warning
  - `check_module_exists("helpdesk", "17.0")` → suggest `viin_helpdesk`
  - `find_override_point("sale.order", "action_confirm", "17.0")` → super_ratio 7/7, super_safety always

**Effort:** ~2 giờ
**Dependencies:** WI1 (Module.edition), WI2 (Method.convention_kind), WI3 (PatternExample), WI4 (seed)

---

## Work Item 6: Tests + snapshots

**Files:**
- Modify: `tests/test_output_snapshots.py` — 3 contract test
- Create: tests đã làm rải rác trong WI1-5; review tổng

### 3 contract snapshot test

```python
def test_suggest_pattern_output_contract(neo4j_seeded):
    output = mcp_server._suggest_pattern("compute field", "17.0", "python")
    # Header
    assert output.startswith("suggest_pattern")
    # Tree connectors
    assert "├─" in output or "└─" in output
    # No None leak
    assert "None" not in output

def test_check_module_exists_output_contract(neo4j_seeded):
    output = mcp_server._check_module_exists("knowledge", "17.0")
    assert output.startswith("check_module_exists")
    # Required sections
    for sec in ["Indexed", "Is EE confusion"]:
        assert sec in output

def test_find_override_point_output_contract(neo4j_seeded):
    output = mcp_server._find_override_point("sale.order", "action_confirm", "17.0")
    assert output.startswith("find_override_point")
    for sec in ["Convention", "Super safety", "Return required", "Anti-patterns"]:
        assert sec in output
```

**Effort:** ~45 phút
**Dependencies:** WI3, WI5

---

## Work Item 7: Docs M4.6

**Files:**
- Modify: `TASKS.md` — M4.6 từ `[ ]` → `[~]/[x]`
- Modify: `README.md` — bỏ "(planned)" cho 3 tool M4.6; status M4.6 → `[x]`
- Modify: `docs/thiet-ke-kien-truc.md` — bỏ "(planned)" cho PatternExample + 2 enrichment

**Effort:** ~20 phút
**Dependencies:** WI0-WI6

---

## Risk & Mitigation

| Rủi ro | Mitigation |
|---|---|
| M4.5 chưa ship khi WI3 start | USES_CORE_SYMBOL edge graceful skip nếu CoreSymbol vắng. Backfill sau M4.5 land + re-run `seed_patterns.py`. Test #6 (existing CoreSymbol) skipped khi không có data — no fail. |
| `suggest_pattern` recall thấp với 50 seed | 7 survey pattern + ~43 biến thể đảm bảo coverage 90% common Odoo dev tasks. Fallback Neo4j text search khi embedder offline. M6 mở rộng seed (~200) + feedback loop. |
| Module `edition` false positive cho custom addon | Manifest `license` field thường thiếu trong custom addon → fallback `'custom'`. Test cover missing license case. Tool output hiển thị edition rõ ràng để user verify, không silent. |
| `convention_kind` regex sai → `super_safety` wrong | Map validate vs 7/7 real `action_confirm` overrides survey + sample khác. Test explicit cho từng prefix. |
| EE_CONFUSION dict stale (Odoo move module CE↔EE giữa version) | `_SOURCE_DATE: 2026-05-08` annotation. M6 auto-detect từ manifest `license = 'OEEL-1'` + path scan upstream Odoo CE repo. |
| Pattern seed mislead AI | Mọi snippet trỏ `file_ref` real Odoo CE v17+. David review patterns.json trước commit WI4 (gate trong DoD). |
| `suggest_pattern` vs `find_examples` user confused | Header text khác nhau ("suggest_pattern" vs "find_examples"). Docstring làm rõ: `suggest_pattern` = curated how-to + gotchas; `find_examples` = raw code search no curation. chunk_type filter tách biệt domain. |

---

## Rollback Plan

1. **Trigger:** `suggest_pattern` recall <60% trên 5 query thủ công David test (vd `"override write to read old value"`, `"computed field cross-model"`, `"avoid xpath replace"`, `"OWL component patch"`, `"check EE module"`).
2. **Action:** Revert WI5 (3 MCP tools); giữ WI1-4 (data layer Module/Method enrichment + PatternExample seed có giá trị độc lập cho `find_examples` rerank). Module/Method enrichment property độc lập với existing tool — không hỏng M1-M4.
3. **Owner:** David Tran — gate merge WI5 sau khi run `seed_patterns.py` thật + chạy 5 query thủ công + accept ≥3/5 helpful.

---

## Definition of Done

- [ ] All 8 WI `[x]`.
- [ ] `make lint` clean.
- [ ] `make test-all` green (~42 test mới + existing không regression).
- [ ] `src/data/patterns.json` ≥50 entry, mọi entry có `snippet_text` + ≥1 `gotchas` + `file_ref` non-empty.
- [ ] David review patterns.json trước commit WI4 — pattern_id 15 core đầy đủ + sample biến thể OK.
- [ ] `tests/test_output_snapshots.py` có 3 contract test cho 3 tool mới.
- [ ] Manual smoke: `python -m src.indexer.seed_patterns --version 17.0` chạy không crash; `MATCH (p:PatternExample) RETURN count(p)` = 50.
- [ ] Manual smoke: `suggest_pattern("override write to read old value", "17.0", "python")` trả ≥1 result với gotcha về "đọc old value trước super().write()".
- [ ] Manual smoke: `check_module_exists("knowledge", "17.0")` trả `is_ee_confusion: Yes` + warning explicit.
- [ ] Manual smoke: `find_override_point("sale.order", "action_confirm", "17.0")` trả `super_ratio: 7/7` (sau index Odoo CE thật).
- [ ] ADR-0003 status `Accepted` + reference trong CONTRIBUTING.md.
- [ ] Commit prefix `[ADD|IMP|FIX|REF]`, KHÔNG `Co-Authored-By: Claude` trailer.

---

## Effort Estimate

| WI | Tên | AI-assisted |
|----|-----|-------------|
| WI0 | ADR-0003 review | 15m |
| WI1 | Module enrichment | 45m |
| WI2 | Method enrichment | 30m |
| WI3 | PatternExample schema | 45m |
| WI4 | Pattern seed curation | 2h |
| WI5 | 3 MCP tool | 2h |
| WI6 | Tests + snapshots | 45m |
| WI7 | Docs M4.6 | 20m |
| **Total** | | **~7.5h AI-assisted** |

---

## Open Questions / Nice-to-Have Defer

**Đã decide:**
1. PatternExample lưu ở Neo4j + reuse embeddings — ADR-0003.
2. Language filter qua entity_name slug — ADR-0003.
3. EE_CONFUSION dict hardcode 16 entry — survey verified 2026-05-08.

**Defer M5+:**
- Pattern feedback loop (helpful/not) — cần API key layer M5 trước.
- Auto-reseed patterns trên indexer run — defer M6.

**Defer M6:**
- `viindoo_equivalent_qname` auto-populate từ Neo4j graph traversal (thay hardcode dict).
- Seed mở rộng ~200 patterns với community contribution path.
- `find_override_point` cross-version diff — show pattern thay đổi giữa v17 vs v18.

---

## References

- ADR-0001: Schema Evolution Policy (PostgreSQL)
- ADR-0003: PatternExample Storage — Neo4j + reuse embeddings
- ETHOS §4.1.1 (Boil the Lake), §4.1.3 (Keep it simple)
- M4 plan precedent: `docs/superpowers/plans/2026-05-07-milestone-4-impact-wow.md`
- M4.5 dep: `docs/superpowers/plans/2026-05-08-milestone-4-5-spec-wow.md`
- Survey: 7 base patterns + ~43 variations từ Odoo CE v17 + Viindoo addons
- CLAUDE.md §2: Stack Viindoo ≠ Odoo upstream — 16 EE confusion list
