# Milestone 4.5 — "Spec Wow" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) hoặc superpowers:executing-plans để implement plan task-by-task. Steps dùng checkbox (`- [ ]`) syntax cho tracking.
>
> **Nguyên tắc bắt buộc khi implement:**
> - **Boil the Lake (ETHOS §4.1.1):** không bỏ era — extract đầy đủ CoreSymbol/LintRule/CLIFlag từ v17/v18/v19 + placeholder v8-v16 với metadata `_curate_status: pending`. Không giả ngơ silent-skip v8/v9 modules — Phase 0 fix 3 blocker NGAY.
> - **Keep it simple (ETHOS §4.1.3):** parser_odoo_core dùng allow-list 8 file core, KHÔNG walk toàn bộ Odoo source. Diff engine pure function — không gọi DB.
> - **Tests trước code (TDD):** mỗi WI có Bước 1 failing tests trước Bước 2 code.
> - **No schema ALTER (per ADR-0001):** M4.5 chỉ thêm Neo4j node label + edge type; KHÔNG ALTER PostgreSQL. Mọi quyết định schema cite ADR-0002.
> - **No `Co-Authored-By: Claude` trailer (per ADR-009):** commit message thuần [ADD|IMP|FIX|REF] prefix.

**Goal:** AI gọi `lookup_core_api("name_get", "18.0")` → `status: removed, replacement: display_name property`; `cli_help("server", "--longpolling-port", "18.0")` → `status: removed, replacement: --gevent-port`; `find_deprecated_usage("19.0")` → list user code đang dùng API deprecated v19. Đồng thời codebase index được Odoo v8/v9 modules (hiện đang silent-skip).

**Why separate milestone (M4.5 thay vì gộp M4 hoặc M5):** M4 hoàn chỉnh impact analysis trên user code (bottom-up). M4.5 thêm tri thức ngược chiều — Odoo core API thay đổi gì giữa versions (top-down). Yêu cầu 3 parser mới (Odoo upstream Python, lint rules, CLI argparse), schema Neo4j hoàn toàn mới (4 node label), cross-version diff engine. Scope đủ lớn để tách riêng, không nên nhồi vào M5 (Product Wow focus Web UI). M4.5 là prerequisite cho M4.6 (Pattern Wow consume CoreSymbol).

**Architecture (3 câu):** 4 node label mới (`CoreSymbol`, `LintRule`, `CLICommand`, `CLIFlag`) + 4 lifecycle edge (`ADDED_IN`, `REMOVED_IN`, `REPLACED_BY`, `DEPRECATED_IN`) + 1 binding edge (`USES_CORE_SYMBOL` từ user Method/Field) thêm vào Neo4j với composite key bao gồm `odoo_version`. Phase 0 unblocks v8/v9 bằng pluggable `ManifestFinder` Protocol (Modern v10+ vs Legacy v8-9 với `__openerp__.py` discovery) + era-aware text-regex dispatch trong `parser_python.py`. Năm tool MCP mới (`lookup_core_api`, `api_version_diff`, `find_deprecated_usage`, `lint_check`, `cli_help`) truy vấn theo pattern split Cypher + tree-format output (đồng nhất M1-M4).

**Tech Stack:** Re-use M1-M4 stack — Python 3.10+, neo4j driver, fastmcp, pytest, ruff. Không thêm dep mới. Static spec data v8-v16 là JSON file (`json` stdlib parse). AST parser dùng `ast` stdlib cho Python 3 source; Era1 fallback dùng `re` stdlib regex.

---

## Cấu Trúc File

```
src/indexer/
├── registry.py              -- MODIFY: ManifestFinder Protocol (Modern + Legacy)
├── parser_python.py         -- MODIFY: era-aware dispatch, FIELD_TYPES_LEGACY
├── parser_odoo_core.py      -- CREATE: extract CoreSymbol từ Odoo upstream Python source
├── parser_lint_rules.py     -- CREATE: extract LintRule từ pylint-odoo + ESLint + ruff
├── parser_cli.py            -- CREATE: extract CLICommand/CLIFlag từ odoo/cli/ + tools/config.py
├── diff_engine.py           -- CREATE: cross-version diff sinh ADDED_IN/REMOVED_IN/REPLACED_BY/DEPRECATED_IN edge
├── models.py                -- MODIFY: thêm CoreSymbolInfo, LintRuleInfo, CLICommandInfo, CLIFlagInfo dataclass + MethodInfo.core_symbol_refs
└── writer_neo4j.py          -- MODIFY: 4 node label writer + 4 edge writer + 4 index + USES_CORE_SYMBOL edge

src/indexer/spec_data/
├── lint_rules_8.0.json      -- CREATE: empty placeholder ({"_curate_status": "pending", ...})
├── lint_rules_9.0.json      -- CREATE: placeholder
├── ... (v10-v16)            -- CREATE: placeholder
├── cli_flags_8.0.json       -- CREATE: placeholder
└── ... (v9-v16)             -- CREATE: placeholder

src/mcp/
└── server.py                -- MODIFY: _latest_version() numeric fix + 5 tool mới + USES_CORE_SYMBOL guard

tests/
├── test_registry.py             -- MODIFY: ManifestFinder modern+legacy
├── test_parser_python.py        -- MODIFY: era1 + FIELD_TYPES_LEGACY + classify_method
├── test_parser_odoo_core.py     -- CREATE: 9 tests (function/class/decorator/orm_method/field_type extract)
├── test_diff_engine.py          -- CREATE: 4 tests (added/removed/replaced/stable)
├── test_parser_lint_rules.py    -- CREATE: 5 tests (pylint-odoo, ESLint, ruff parse + write)
├── test_parser_cli.py           -- CREATE: 6 tests (command + flag parse + diff write)
├── test_writer_neo4j.py         -- MODIFY: 4 node label writer + 4 index + USES_CORE_SYMBOL
├── test_mcp_spec_tools.py       -- CREATE: 5 tool × 3 case = 15 tests
├── test_mcp_server.py           -- MODIFY: _latest_version numeric compare + None-fallback
└── test_output_snapshots.py     -- MODIFY: 5 contract snapshot tests cho 5 tool mới

docs/
├── adr/0002-spec-schema-policy.md            -- (đã land Pre-work)
├── superpowers/plans/2026-05-08-milestone-4-5-spec-wow.md  -- (this document)
├── thiet-ke-kien-truc.md                     -- MODIFY (WI8): schema preview 4 node + 5 tool
└── (project root) CLAUDE.md                  -- MODIFY (WI8): gotcha v8/v9 + version-aware regex

TASKS.md                  -- MODIFY (WI8): M4.5 từ [ ] → [~] khi start, → [x] khi xong
README.md                 -- MODIFY (WI8): trạng thái M4.5 + tool count placeholder
CONTRIBUTING.md           -- MODIFY (WI8): ADR-0002 reference
```

---

## Work Item 0: ADR-0002 review + merge

**Files:**
- `docs/adr/0002-spec-schema-policy.md` (đã viết Pre-work)

- [ ] **Bước 1:** David review ADR-0002 — composite key per-version, lifecycle edge structure, USES_CORE_SYMBOL V0 scope, static spec policy v8-v16, ADR-0001 compliance.
- [ ] **Bước 2:** Status từ `Draft` → `Accepted` sau David approve.
- [ ] **Bước 3:** Reference ADR-0002 trong CONTRIBUTING.md (sẽ làm WI8).

**Effort:** ~30 phút (chỉ review).
**Dependencies:** Không có. Khởi đầu milestone.

---

## Work Item 1: Phase 0 — v8/v9 Enablement

**Files:**
- Modify: `src/indexer/registry.py` — `ManifestFinder` Protocol
- Modify: `src/indexer/parser_python.py` — era-aware dispatch + FIELD_TYPES_LEGACY
- Modify: `src/mcp/server.py` — `_latest_version()` numeric compare + None fallback
- Modify: `tests/test_registry.py`, `tests/test_parser_python.py`, `tests/test_mcp_server.py`

### Bước 1: Failing tests (8 tests)

```python
# test_registry.py
def test_legacy_manifest_finder_finds_openerp_py(tmp_path):
    """LegacyManifestFinder rglob __openerp__.py."""
    (tmp_path / "v8mod" / "__openerp__.py").parent.mkdir(parents=True)
    (tmp_path / "v8mod" / "__openerp__.py").write_text("{'name': 'V8 Module'}")
    finder = LegacyManifestFinder()
    paths = finder.find(str(tmp_path))
    assert any("__openerp__.py" in p for p in paths)

def test_modern_manifest_finder_ignores_openerp(tmp_path):
    """ModernManifestFinder không match __openerp__.py."""
    (tmp_path / "v8mod" / "__openerp__.py").parent.mkdir(parents=True)
    (tmp_path / "v8mod" / "__openerp__.py").write_text("{'name': 'X'}")
    finder = ModernManifestFinder()
    assert finder.find(str(tmp_path)) == []

def test_get_manifest_finder_dispatches_by_version():
    assert isinstance(get_manifest_finder("9.0"), LegacyManifestFinder)
    assert isinstance(get_manifest_finder("10.0"), ModernManifestFinder)
    assert isinstance(get_manifest_finder("17.0"), ModernManifestFinder)

def test_build_registry_v8_module(tmp_path):
    """Registry với __openerp__.py extract đúng name + version + depends."""
    # ... fixture với manifest v8 syntax → assert ModuleInfo populated

# test_parser_python.py
def test_parser_python_era1_columns_dict():
    """Parse `_columns = {'amount': fields.float('Amount')}` → FieldInfo legacy type."""
    src = "class X(osv.osv):\n    _name='x'\n    _columns={'a': fields.float('A')}"
    models = _parse_era1_text(src, ModuleInfo(name='m', odoo_version='8.0', ...))
    assert models[0].fields[0].name == 'a'
    assert models[0].fields[0].ttype == 'float'

def test_parser_python_era1_python2_print_statement_no_crash():
    """Python 2 syntax `print x` không crash AST.parse — graceful skip."""
    src = "print 'hello'\n_columns = {'a': fields.char('A')}"
    result = parse_file_content(src, ModuleInfo(odoo_version='8.0', ...))
    # Không raise SyntaxError; trả empty hoặc partial
    assert isinstance(result, list)

def test_parser_python_legacy_field_types_detected():
    """fields.function, fields.related, fields.dummy, fields.sparse được detect."""
    src = "_columns = {'x': fields.function(_compute_x)}"
    models = _parse_era1_text(src, ModuleInfo(odoo_version='8.0', ...))
    assert models[0].fields[0].ttype == 'function'

# test_mcp_server.py
def test_latest_version_numeric_compare(neo4j_session):
    """DB có 9.0 + 17.0 → trả 17.0 (không phải 9.0 lexicographic)."""
    neo4j_session.run("MERGE (m:Module {name:'a',odoo_version:'9.0'})")
    neo4j_session.run("MERGE (m:Module {name:'b',odoo_version:'17.0'})")
    assert _latest_version(neo4j_session) == "17.0"

def test_latest_version_returns_none_when_empty(neo4j_session):
    """DB rỗng → None, KHÔNG hardcode '17.0'."""
    neo4j_session.run("MATCH (m:Module) DETACH DELETE m")
    assert _latest_version(neo4j_session) is None
```

### Bước 2: Code

**`registry.py`:**
```python
from typing import Protocol

class ManifestFinder(Protocol):
    def find(self, repo_path: str) -> list[str]: ...

class ModernManifestFinder:
    def find(self, repo_path: str) -> list[str]:
        return [str(p) for p in Path(repo_path).rglob("__manifest__.py")]

class LegacyManifestFinder:
    def find(self, repo_path: str) -> list[str]:
        return [str(p) for p in Path(repo_path).rglob("__openerp__.py")]

def get_manifest_finder(odoo_version: str) -> ManifestFinder:
    try:
        major = int(odoo_version.split(".")[0])
    except (ValueError, IndexError):
        return ModernManifestFinder()  # default
    return LegacyManifestFinder() if major <= 9 else ModernManifestFinder()
```

`build_registry()` accept `odoo_version` argument, dùng `get_manifest_finder(odoo_version).find(repo_path)`.

`parse_manifest()` cho `__openerp__.py`: dùng `ast.literal_eval` (cùng pattern hiện tại) — nếu fail (Python 2 syntax mismatch) → text-regex fallback extract `'name':`, `'version':`, `'depends': [...]` qua regex.

**`parser_python.py`:**
```python
FIELD_TYPES_LEGACY = {
    'function', 'related', 'dummy', 'sparse',
    'float', 'integer', 'char', 'text', 'boolean',
    'date', 'datetime', 'binary', 'selection',
    'many2one', 'one2many', 'many2many',
}

def _detect_era(odoo_version: str) -> str:
    try:
        major = int(odoo_version.split(".")[0])
    except (ValueError, IndexError):
        return "era2"  # default modern
    return "era1" if major <= 9 else "era2"

def _parse_era1_text(source: str, module_info: ModuleInfo) -> list[ModelInfo]:
    """Text-regex extract _name, _inherit, _columns dict cho Python 2 v8/v9."""
    # Regex extract:
    # _name = '...' / "..." 
    # _inherit = '...' / [...]
    # _columns = {'<field>': fields.<type>(...)}
    # Return ModelInfo list (best-effort, no method body)
    ...

def parse_file(filepath: str, module_info: ModuleInfo) -> list[ModelInfo]:
    """Era-aware dispatch. Era1 try AST, fallback text-regex on SyntaxError."""
    era = _detect_era(module_info.odoo_version)
    try:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return []
    
    if era == "era1":
        try:
            return _parse_era2_ast(source, module_info)  # try AST first
        except SyntaxError:
            return _parse_era1_text(source, module_info)  # fallback
    return _parse_era2_ast(source, module_info)
```

**`mcp/server.py`:**
```python
def _latest_version(session) -> str | None:
    """Return latest indexed odoo_version by numeric compare. None if no data."""
    rec = session.run("""
        MATCH (m:Module)
        WITH DISTINCT m.odoo_version AS v
        WHERE v <> 'unknown' AND v =~ '\\\\d+\\\\.\\\\d+'
        RETURN v ORDER BY toInteger(split(v,'.')[0]) DESC,
                          toInteger(split(v,'.')[1]) DESC
        LIMIT 1
    """).single()
    return rec["v"] if rec else None

# Tất cả caller:
def _resolve_version(version_arg: str, session) -> str:
    if version_arg != "auto":
        return version_arg
    v = _latest_version(session)
    if v is None:
        raise ValueError("No data indexed. Run `python -m src.indexer --profile <name>` first.")
    return v
```

### Bước 3: Verify

- `make test` green (8 new tests pass + existing tests không regression)
- `make lint` clean (ruff)
- Manual: tạo fake repo với `__openerp__.py` → `python -m src.indexer --profile test_v8 --version 8.0` → log INFO show `Module discovered: <name>` cho v8 modules

**Effort:** ~2.5 giờ AI-assisted
**Dependencies:** WI0 (ADR reviewed)

---

## Work Item 2: parser_odoo_core + diff_engine + CoreSymbol

**Files:**
- Create: `src/indexer/parser_odoo_core.py`
- Create: `src/indexer/diff_engine.py`
- Modify: `src/indexer/models.py` — thêm `CoreSymbolInfo`
- Modify: `src/indexer/writer_neo4j.py` — write CoreSymbol + 4 edge + 1 index
- Create: `tests/test_parser_odoo_core.py`, `tests/test_diff_engine.py`
- Modify: `tests/test_writer_neo4j.py`

### Bước 1: Failing tests (9 tests)

```python
# test_parser_odoo_core.py
def test_extract_function_symbol():
    """def safe_eval(...) → CoreSymbolInfo(kind='function')."""
    src = "def safe_eval(expr, context=None): pass"
    syms = _extract_from_source(src, "odoo.tools.safe_eval", "19.0")
    assert syms[0].kind == "function"
    assert syms[0].qualified_name == "odoo.tools.safe_eval.safe_eval"

def test_extract_class_symbol():
    """class Query: → CoreSymbolInfo(kind='class')."""
    src = "class Query:\n    def __init__(self, env): pass"
    syms = _extract_from_source(src, "odoo.tools.query", "18.0")
    assert syms[0].kind == "class"
    assert "Query" in syms[0].qualified_name

def test_extract_decorator_symbol():
    """@api.deprecated decorator definition → kind='decorator'."""
    src = "def deprecated(message): ..."
    syms = _extract_from_source(src, "odoo.api", "19.0")
    # filter by name 'deprecated' → assert kind detected from context

def test_extract_orm_method_marked_deprecated():
    """Method có @api.deprecated → status='deprecated'."""
    src = "class BaseModel:\n    @api.deprecated('Use display_name')\n    def name_get(self): pass"
    syms = _extract_from_source(src, "odoo.models", "17.0")
    nm = next(s for s in syms if s.qualified_name.endswith(".name_get"))
    assert nm.status == "deprecated"

def test_extract_field_type_class():
    """class Float(Field): → kind='field_type'."""
    src = "class Float(Field):\n    aggregator = 'sum'"
    syms = _extract_from_source(src, "odoo.fields", "18.0")
    assert syms[0].kind == "field_type"

# test_diff_engine.py
def test_diff_symbol_added():
    """Symbol có v18, không có v17 → ADDED_IN edge."""
    v17 = []
    v18 = [CoreSymbolInfo(qualified_name="x", kind="function", odoo_version="18.0")]
    diff = compute_diff(v17, v18)
    assert len(diff.added) == 1

def test_diff_symbol_removed():
    """Symbol v17, không có v18 → REMOVED_IN."""
    v17 = [CoreSymbolInfo(qualified_name="name_get", kind="orm_method", odoo_version="17.0")]
    v18 = []
    diff = compute_diff(v17, v18)
    assert len(diff.removed) == 1

def test_diff_symbol_replaced():
    """group_operator@v17 + replacement_qname=aggregator → REPLACED_BY edge."""
    v17 = [CoreSymbolInfo(qualified_name="group_operator", kind="field_type",
                          odoo_version="17.0", replacement_qname="aggregator")]
    v18 = [CoreSymbolInfo(qualified_name="aggregator", kind="field_type",
                          odoo_version="18.0")]
    diff = compute_diff(v17, v18)
    assert len(diff.replaced) == 1
    assert diff.replaced[0] == ("group_operator", "aggregator")

def test_diff_symbol_stable_no_edge():
    """Symbol cùng tên ở 2 version, không thay đổi status → không sinh edge."""
    v17 = [CoreSymbolInfo(qualified_name="safe_eval", kind="function", odoo_version="17.0")]
    v18 = [CoreSymbolInfo(qualified_name="safe_eval", kind="function", odoo_version="18.0")]
    diff = compute_diff(v17, v18)
    assert len(diff.added) == 0 and len(diff.removed) == 0 and len(diff.replaced) == 0
```

### Bước 2: Code

**`models.py`:**
```python
@dataclass
class CoreSymbolInfo:
    qualified_name: str       # "odoo.tools.safe_eval.safe_eval"
    kind: str                 # 'function'|'class'|'decorator'|'exception'|'field_type'|'orm_method'|'cursor_method'
    odoo_version: str
    signature: str | None = None
    file_path: str | None = None
    line: int | None = None
    status: str = "stable"    # 'stable'|'deprecated'|'removed'|'added'
    replacement_qname: str | None = None
```

**`parser_odoo_core.py`:**
```python
# Allow-list cố định — KHÔNG walk toàn bộ Odoo source
_CORE_FILES = [
    "odoo/tools/safe_eval.py",
    "odoo/tools/query.py",
    "odoo/tools/sql.py",
    "odoo/fields.py",
    "odoo/models.py",
    "odoo/api.py",
    "odoo/sql_db.py",
    "odoo/exceptions.py",
]

def parse_odoo_core(odoo_source_root: str, odoo_version: str) -> list[CoreSymbolInfo]:
    """Extract CoreSymbol từ allow-list cố định. Skip nếu file không tồn tại."""
    symbols = []
    for relpath in _CORE_FILES:
        full = Path(odoo_source_root) / relpath
        if not full.exists():
            continue
        try:
            source = full.read_text(encoding="utf-8")
        except OSError:
            continue
        module_qname = relpath.replace("/", ".").removesuffix(".py")
        symbols.extend(_extract_from_source(source, module_qname, odoo_version,
                                            file_path=str(full)))
    return symbols

def _extract_from_source(source: str, module_qname: str, odoo_version: str,
                         file_path: str | None = None) -> list[CoreSymbolInfo]:
    """AST visit top-level def/class. Detect @deprecated decorator → status."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    symbols = []
    for node in tree.body:  # top-level only — KISS
        if isinstance(node, ast.FunctionDef):
            symbols.append(_make_function_symbol(node, module_qname, odoo_version, file_path))
        elif isinstance(node, ast.ClassDef):
            symbols.append(_make_class_symbol(node, module_qname, odoo_version, file_path))
            # nested methods of class
            symbols.extend(_extract_class_methods(node, module_qname, odoo_version, file_path))
    return symbols
```

**`diff_engine.py`:**
```python
@dataclass
class DiffResult:
    added: list[CoreSymbolInfo] = field(default_factory=list)
    removed: list[CoreSymbolInfo] = field(default_factory=list)
    stable: list[tuple[CoreSymbolInfo, CoreSymbolInfo]] = field(default_factory=list)
    replaced: list[tuple[str, str]] = field(default_factory=list)  # (old_qname, new_qname)

def compute_diff(symbols_old: list[CoreSymbolInfo],
                 symbols_new: list[CoreSymbolInfo]) -> DiffResult:
    """Pure function — không gọi DB."""
    by_qname_old = {s.qualified_name: s for s in symbols_old}
    by_qname_new = {s.qualified_name: s for s in symbols_new}
    
    added = [s for qn, s in by_qname_new.items() if qn not in by_qname_old]
    removed = [s for qn, s in by_qname_old.items() if qn not in by_qname_new]
    stable = [(by_qname_old[qn], by_qname_new[qn])
              for qn in by_qname_old.keys() & by_qname_new.keys()]
    
    replaced = []
    for s in symbols_old:
        if s.replacement_qname and s.replacement_qname in by_qname_new:
            replaced.append((s.qualified_name, s.replacement_qname))
    return DiffResult(added=added, removed=removed, stable=stable, replaced=replaced)
```

**`writer_neo4j.py`:**
```python
def write_core_symbols(self, symbols: list[CoreSymbolInfo]) -> None:
    with self._driver.session() as sess:
        for batch in _chunked(symbols, 500):
            sess.execute_write(self._write_core_symbols_batch, batch)

def _write_core_symbols_batch(tx, symbols):
    for s in symbols:
        tx.run("""
            MERGE (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
            SET cs.kind = $kind,
                cs.signature = $sig,
                cs.file_path = $fp,
                cs.line = $line,
                cs.status = $status,
                cs.replacement_qname = $repl
        """, qn=s.qualified_name, v=s.odoo_version, kind=s.kind,
             sig=s.signature, fp=s.file_path, line=s.line,
             status=s.status, repl=s.replacement_qname)

def write_diff_edges(self, diff: DiffResult, from_version: str, to_version: str) -> None:
    """ADDED_IN, REMOVED_IN, REPLACED_BY, DEPRECATED_IN edges."""
    # ADDED: edge từ "absent" → new symbol (no source node, just property on target)
    # Simpler: SET cs.status = 'added' + add property cs.added_in = to_version
    # Implementation choice: edge cho REPLACED_BY (cần 2 node), property cho ADDED/REMOVED
    ...

# setup_indexes() thêm:
# CREATE INDEX core_symbol_qn IF NOT EXISTS FOR (n:CoreSymbol) ON (n.qualified_name, n.odoo_version)
```

### Bước 3: Verify

- `make test` green với 9 test mới
- Manual: `python -c "from src.indexer.parser_odoo_core import parse_odoo_core; print(len(parse_odoo_core('~/git/odoo17/odoo', '17.0')))"` → output ~500-800 CoreSymbol
- Cypher `MATCH (cs:CoreSymbol) RETURN count(cs)` sau index → > 0

**Effort:** ~3 giờ AI-assisted
**Dependencies:** WI0 (ADR), WI1 (parser_python pattern established)

---

## Work Item 3: parser_lint_rules + LintRule

**Files:**
- Create: `src/indexer/parser_lint_rules.py`
- Create: `src/indexer/spec_data/lint_rules_<version>.json` (placeholder v8-v16)
- Modify: `src/indexer/models.py` — thêm `LintRuleInfo`
- Modify: `src/indexer/writer_neo4j.py` — write LintRule node + index
- Create: `tests/test_parser_lint_rules.py`
- Modify: `tests/test_writer_neo4j.py`

### Bước 1: Failing tests (5 tests)

```python
def test_parse_pylint_odoo_checker():
    """Parse _odoo_checker_sql_injection.py → LintRuleInfo(rule_id='E8501')."""
    src = '''
class CheckSqlInjection(BaseChecker):
    msgs = {
        "E8501": ("SQL injection risk", "sql-injection", "..."),
    }
'''
    rules = _parse_pylint_odoo_source(src, "17.0")
    assert rules[0].rule_id == "E8501"
    assert rules[0].kind == "pylint-odoo"

def test_parse_eslint_no_restricted_syntax():
    """Parse eslintrc no-restricted-syntax cho OWL → LintRule."""
    config = {
        "rules": {
            "no-restricted-syntax": ["error", {
                "selector": "ClassDeclaration[superClass.name='Component']:not(:has(...))",
                "message": "Component must declare static template",
            }],
        }
    }
    rules = _parse_eslint_config(config, "18.0")
    assert any("Component must declare" in r.message for r in rules)

def test_parse_ruff_toml():
    """Parse ruff.toml v19 → LintRule list."""
    toml = '''
[lint]
select = ["BLE", "E", "I", "UP"]
ignore = ["E501"]
'''
    rules = _parse_ruff_toml(toml, "19.0")
    assert any(r.rule_id.startswith("BLE") for r in rules)

def test_parse_static_placeholder_v10():
    """spec_data/lint_rules_10.0.json placeholder → empty list + curate_status pending."""
    rules = parse_lint_rules_for_version("10.0", static_data_dir="src/indexer/spec_data")
    assert rules == []  # placeholder rỗng

def test_write_lint_rule_node(neo4j_session):
    """Write LintRuleInfo → Neo4j node với composite key."""
    rule = LintRuleInfo(rule_id="E8501", odoo_version="17.0", kind="pylint-odoo",
                       message="SQL injection risk")
    writer.write_lint_rules([rule])
    rec = neo4j_session.run(
        "MATCH (l:LintRule {rule_id:'E8501', odoo_version:'17.0'}) RETURN l"
    ).single()
    assert rec is not None
```

### Bước 2: Code

**`models.py`:**
```python
@dataclass
class LintRuleInfo:
    rule_id: str             # "E8501", "I001", "no-restricted-syntax"
    odoo_version: str
    kind: str                # 'pylint-odoo'|'pylint-stdlib'|'eslint-odoo'|'ruff-builtin'
    message: str | None = None
    severity: str = "warning"
    file_pattern: str | None = None
    fix_template: str | None = None
    core_symbol_qname: str | None = None  # link CoreSymbol nếu rule check 1 symbol
```

**`parser_lint_rules.py`:**
```python
def parse_lint_rules_for_version(odoo_version: str,
                                  odoo_source_root: str | None = None,
                                  static_data_dir: str = "src/indexer/spec_data") -> list[LintRuleInfo]:
    """Code-extract for v17+ (có Odoo source); static placeholder for v8-v16."""
    rules = []
    
    # Code extraction (only if Odoo source available + version supports)
    if odoo_source_root and _version_has_test_lint(odoo_version):
        checker_dir = Path(odoo_source_root) / "addons/test_lint/tests"
        for f in checker_dir.glob("_odoo_checker_*.py"):
            rules.extend(_parse_pylint_odoo_source(f.read_text(), odoo_version))
        eslintrc = checker_dir / "eslintrc"
        if eslintrc.exists():
            rules.extend(_parse_eslint_config(json.loads(eslintrc.read_text()), odoo_version))
        ruff_toml = Path(odoo_source_root) / "ruff.toml"
        if ruff_toml.exists():
            rules.extend(_parse_ruff_toml(ruff_toml.read_text(), odoo_version))
    
    # Static fallback (always merged — placeholder for v8-v16)
    static_path = Path(static_data_dir) / f"lint_rules_{odoo_version}.json"
    if static_path.exists():
        data = json.loads(static_path.read_text())
        for r in data.get("rules", []):
            rules.append(LintRuleInfo(**r, odoo_version=odoo_version))
    
    return rules

def _version_has_test_lint(odoo_version: str) -> bool:
    """test_lint addon từ v17 trở lên."""
    try:
        major = int(odoo_version.split(".")[0])
    except (ValueError, IndexError):
        return False
    return major >= 17
```

**Static placeholder JSON (cho v8-v16, repeat per version):**
```json
{
  "_curate_status": "pending",
  "_generated_at": "2026-05-08",
  "_note": "Curate manual or community contribution. M6 will auto-extract from upstream sources where possible.",
  "rules": []
}
```

**`writer_neo4j.py`:**
```python
def write_lint_rules(self, rules: list[LintRuleInfo]) -> None:
    with self._driver.session() as sess:
        for batch in _chunked(rules, 500):
            sess.execute_write(self._write_lint_rules_batch, batch)

def _write_lint_rules_batch(tx, rules):
    for r in rules:
        tx.run("""
            MERGE (l:LintRule {rule_id: $id, odoo_version: $v})
            SET l.kind = $kind, l.message = $msg, l.severity = $sev,
                l.file_pattern = $fp, l.fix_template = $fix,
                l.core_symbol_qname = $cs
        """, id=r.rule_id, v=r.odoo_version, kind=r.kind, msg=r.message,
             sev=r.severity, fp=r.file_pattern, fix=r.fix_template,
             cs=r.core_symbol_qname)

# setup_indexes thêm:
# CREATE INDEX lint_rule_id IF NOT EXISTS FOR (n:LintRule) ON (n.rule_id, n.odoo_version)
```

### Bước 3: Verify

- 5 test mới green
- Manual: `python -c "from src.indexer.parser_lint_rules import parse_lint_rules_for_version; print(parse_lint_rules_for_version('17.0', '~/git/odoo17/odoo'))"` → output ≥3 rule (E8501, E8502, E8503)
- v8 placeholder: `parse_lint_rules_for_version('8.0', None, 'src/indexer/spec_data')` → empty list

**Effort:** ~2 giờ AI-assisted
**Dependencies:** WI2 (model + writer pattern)

---

## Work Item 4: parser_cli + CLICommand/CLIFlag

**Files:**
- Create: `src/indexer/parser_cli.py`
- Create: `src/indexer/spec_data/cli_flags_<version>.json` (placeholder v8-v16)
- Modify: `src/indexer/models.py` — thêm `CLICommandInfo`, `CLIFlagInfo`
- Modify: `src/indexer/writer_neo4j.py` — write CLICommand + CLIFlag + 2 edges + 2 indexes
- Create: `tests/test_parser_cli.py`
- Modify: `tests/test_writer_neo4j.py`

### Bước 1: Failing tests (6 tests)

```python
def test_parse_cli_command_class():
    """class Server(Command): → CLICommandInfo(name='server')."""
    src = "class Server(Command):\n    'Run Odoo server'\n    def run(self, args): pass"
    cmds = _parse_cli_module(src, "17.0", "odoo/cli/server.py")
    assert cmds[0].name == "server"

def test_parse_cli_flag_argparse():
    """parser.add_argument('--longpolling-port', ...) → CLIFlagInfo."""
    src = '''
parser.add_argument("--longpolling-port", type=int, default=8072)
'''
    flags = _parse_argparse_calls(src, "17.0", command_name="server")
    assert flags[0].flag_name == "--longpolling-port"
    assert flags[0].type == "int"

def test_parse_cli_flag_diff_v17_to_v18_removed():
    """--longpolling-port có v17 không có v18 → status='removed' khi diff."""
    flags_v17 = [CLIFlagInfo("--longpolling-port", "server", "17.0")]
    flags_v18 = []
    diff = compute_cli_flag_diff(flags_v17, flags_v18)
    assert "--longpolling-port" in [f.flag_name for f in diff.removed]

def test_write_cli_command_node(neo4j_session):
    cmd = CLICommandInfo(name="server", odoo_version="17.0")
    writer.write_cli_commands([cmd])
    rec = neo4j_session.run(
        "MATCH (c:CLICommand {name:'server', odoo_version:'17.0'}) RETURN c"
    ).single()
    assert rec is not None

def test_write_cli_flag_of_command_edge(neo4j_session):
    """CLIFlag → CLICommand qua OF_COMMAND edge."""
    writer.write_cli_commands([CLICommandInfo("server", "17.0")])
    writer.write_cli_flags([CLIFlagInfo("--workers", "server", "17.0")])
    rec = neo4j_session.run("""
        MATCH (f:CLIFlag {flag_name:'--workers'})-[:OF_COMMAND]->(c:CLICommand)
        RETURN c.name AS name
    """).single()
    assert rec["name"] == "server"

def test_write_cli_flag_replaced_by_edge(neo4j_session):
    """--longpolling-port@v17 → REPLACED_BY → --gevent-port@v17."""
    writer.write_cli_flags([
        CLIFlagInfo("--longpolling-port", "server", "17.0", status="deprecated",
                   replacement_flag_name="--gevent-port"),
        CLIFlagInfo("--gevent-port", "server", "17.0"),
    ])
    rec = neo4j_session.run("""
        MATCH (a:CLIFlag {flag_name:'--longpolling-port'})-[:REPLACED_BY]->(b:CLIFlag)
        RETURN b.flag_name AS name
    """).single()
    assert rec["name"] == "--gevent-port"
```

### Bước 2: Code

**`models.py`:**
```python
@dataclass
class CLICommandInfo:
    name: str
    odoo_version: str
    description: str | None = None
    file_path: str | None = None

@dataclass
class CLIFlagInfo:
    flag_name: str         # "--longpolling-port"
    command_name: str      # "server"
    odoo_version: str
    status: str = "stable" # 'stable'|'deprecated'|'removed'|'added'
    default: str | None = None
    type: str | None = None
    replacement_flag_name: str | None = None
    env_name: str | None = None
    posix_only: bool = False
```

**`parser_cli.py`:**
```python
def parse_cli_commands(odoo_source_root: str, odoo_version: str) -> list[CLICommandInfo]:
    """Scan odoo/cli/*.py, detect class X(Command)."""
    cli_dir = Path(odoo_source_root) / "odoo/cli"
    if not cli_dir.exists():
        return []
    cmds = []
    for f in cli_dir.glob("*.py"):
        if f.stem.startswith("_") or f.stem == "command":
            continue
        cmds.extend(_parse_cli_module(f.read_text(), odoo_version, str(f)))
    return cmds

def parse_cli_flags(odoo_source_root: str, odoo_version: str) -> list[CLIFlagInfo]:
    """AST parse odoo/tools/config.py, detect parser.add_argument calls."""
    config_path = Path(odoo_source_root) / "odoo/tools/config.py"
    if not config_path.exists():
        # Fallback static
        return _load_static_cli_flags(odoo_version)
    flags = _parse_argparse_calls(config_path.read_text(), odoo_version, "server")
    # Merge với static cho command non-default (db, shell, etc.)
    flags.extend(_load_static_cli_flags(odoo_version))
    return flags
```

**Static placeholder + writer pattern tương tự WI3.**

### Bước 3: Verify

- 6 test mới green
- Manual: `parse_cli_commands('~/git/odoo17/odoo', '17.0')` → output ≥8 cmd (server/shell/scaffold/db/deploy/populate/neutralize/cloc)
- Manual: `parse_cli_flags(...)` → output ≥30 flag chính

**Effort:** ~2 giờ AI-assisted
**Dependencies:** WI2, WI3 (writer + diff pattern)

---

## Work Item 5: 5 MCP Tool Implementation

**Files:**
- Modify: `src/mcp/server.py` — thêm 5 private function + `@mcp.tool()` wrapper
- Create: `tests/test_mcp_spec_tools.py`

### Bước 1: Failing tests (15 tests = 5 tool × 3 case)

```python
# tests/test_mcp_spec_tools.py
class TestLookupCoreApi:
    def test_happy_path(self, neo4j_session, mcp_server):
        # Seed CoreSymbol(name_get, 18.0, status=removed, replacement=display_name)
        result = mcp_server._lookup_core_api("name_get", "18.0")
        assert "removed" in result.lower()
        assert "display_name" in result.lower()
    
    def test_not_found(self, mcp_server):
        result = mcp_server._lookup_core_api("non_existent_xyz", "18.0")
        assert "not found" in result.lower()
    
    def test_auto_version(self, neo4j_session, mcp_server):
        # Seed nhiều version, test auto picks latest
        ...

class TestApiVersionDiff:
    def test_happy_path_diff_added_removed(self, neo4j_session, mcp_server):
        result = mcp_server._api_version_diff("safe_eval", "17.0", "19.0")
        assert "added" in result.lower() or "signature change" in result.lower()
    
    def test_same_version(self, mcp_server):
        result = mcp_server._api_version_diff("x", "17.0", "17.0")
        assert "no diff" in result.lower() or "same version" in result.lower()
    
    def test_symbol_not_in_either(self, mcp_server):
        result = mcp_server._api_version_diff("nonexistent", "17.0", "18.0")
        assert "not found" in result.lower()

class TestFindDeprecatedUsage:
    def test_happy_path(self, neo4j_session, mcp_server):
        # Seed Method với USES_CORE_SYMBOL → CoreSymbol(status=deprecated)
        result = mcp_server._find_deprecated_usage("18.0")
        assert "name_get" in result.lower()  # method dùng deprecated symbol
    
    def test_empty_result(self, mcp_server):
        result = mcp_server._find_deprecated_usage("99.0")  # không có data
        assert "no deprecated usage" in result.lower()
    
    def test_filter_by_category(self, neo4j_session, mcp_server):
        result = mcp_server._find_deprecated_usage("18.0", category="orm")
        # assert filter applied

class TestLintCheck:
    def test_happy_path_e8507_missing_gettext(self, neo4j_session, mcp_server):
        code = "raise UserError('Hello')"  # missing _()
        result = mcp_server._lint_check(code, "19.0", "python")
        assert "E8507" in result or "missing-gettext" in result.lower()
    
    def test_no_violations(self, neo4j_session, mcp_server):
        code = "pass"
        result = mcp_server._lint_check(code, "19.0", "python")
        assert "no violations" in result.lower()
    
    def test_invalid_language(self, mcp_server):
        result = mcp_server._lint_check("x", "19.0", "fortran")
        assert "valid" in result.lower() and "python" in result.lower()

class TestCliHelp:
    def test_command_only(self, neo4j_session, mcp_server):
        result = mcp_server._cli_help("server", None, "17.0")
        assert "--workers" in result or "--http-port" in result
    
    def test_specific_flag_status(self, neo4j_session, mcp_server):
        # --longpolling-port @v18 = removed
        result = mcp_server._cli_help("server", "--longpolling-port", "18.0")
        assert "removed" in result.lower()
        assert "--gevent-port" in result  # replacement
    
    def test_command_not_found(self, mcp_server):
        result = mcp_server._cli_help("nonexistent_cmd", None, "17.0")
        assert "not found" in result.lower()
```

### Bước 2: Code

5 private function trong `src/mcp/server.py`:

```python
def _lookup_core_api(name: str, odoo_version: str = "auto") -> str:
    """Trả signature, status, replacement của 1 CoreSymbol."""
    with self._driver.session() as sess:
        v = _resolve_version(odoo_version, sess)
        rec = sess.run("""
            MATCH (cs:CoreSymbol)
            WHERE cs.odoo_version = $v
              AND (cs.qualified_name = $n OR cs.qualified_name ENDS WITH '.' + $n)
            RETURN cs ORDER BY length(cs.qualified_name) ASC LIMIT 1
        """, n=name, v=v).single()
        if not rec:
            return f"lookup_core_api({name!r}, {v!r})\n└─ not found in indexed Odoo core"
        s = rec["cs"]
        return _format_lookup_core_api(s, v)

def _api_version_diff(symbol: str, from_version: str, to_version: str) -> str:
    """Diff 1 symbol giữa 2 version."""
    if from_version == to_version:
        return f"api_version_diff({symbol!r}): same version, no diff"
    # Query both → compute diff → format
    ...

def _find_deprecated_usage(odoo_version: str = "auto", category: str | None = None) -> str:
    """Quét user code dùng CoreSymbol có status deprecated/removed."""
    with self._driver.session() as sess:
        v = _resolve_version(odoo_version, sess)
        cypher = """
            MATCH (mth:Method {odoo_version: $v})-[:USES_CORE_SYMBOL]->(cs:CoreSymbol)
            WHERE cs.status IN ['deprecated', 'removed']
        """
        if category:
            cypher += " AND cs.kind = $cat"
        cypher += """
            RETURN mth.module AS module, mth.model AS model, mth.name AS method,
                   cs.qualified_name AS deprecated_symbol, cs.status AS status,
                   cs.replacement_qname AS replacement
            ORDER BY mth.module, mth.model, mth.name
        """
        records = sess.run(cypher, v=v, cat=category).data()
        return _format_deprecated_usage(records, v)

def _lint_check(code: str, odoo_version: str = "auto", language: str = "python") -> str:
    """Pattern-match code chunk vs LintRule. V0: substring/regex check."""
    if language not in ("python", "javascript", "xml"):
        return f"Invalid language. Valid: python, javascript, xml"
    with self._driver.session() as sess:
        v = _resolve_version(odoo_version, sess)
        rules = sess.run("""
            MATCH (l:LintRule {odoo_version: $v})
            WHERE l.kind STARTS WITH $lang_kind
            RETURN l
        """, v=v, lang_kind=("pylint" if language == "python" else "eslint")).data()
        violations = []
        for r in rules:
            if _match_rule(code, r["l"]):
                violations.append(r["l"])
        return _format_lint_violations(violations, v, code)

def _cli_help(command: str | None, flag: str | None, odoo_version: str = "auto") -> str:
    """Return CLICommand spec hoặc CLIFlag status + replacement."""
    with self._driver.session() as sess:
        v = _resolve_version(odoo_version, sess)
        if command and flag:
            rec = sess.run("""
                MATCH (f:CLIFlag {flag_name: $flag, command_name: $cmd, odoo_version: $v})
                OPTIONAL MATCH (f)-[:REPLACED_BY]->(repl:CLIFlag)
                RETURN f, repl.flag_name AS replacement
            """, flag=flag, cmd=command, v=v).single()
            if not rec:
                return f"cli_help({command!r}, {flag!r}, {v!r}): flag not found"
            return _format_cli_flag(rec["f"], rec["replacement"], v)
        elif command:
            # List all flags of command
            ...
        else:
            # List all commands
            ...

# 5 @mcp.tool() public wrappers với docstring đầy đủ Args/Returns/Example.
```

### Bước 3: Verify

- 15 test mới green
- Manual smoke: `lookup_core_api("name_get", "18.0")` qua MCP → output có "removed" + "display_name"
- Manual: `cli_help("server", "--longpolling-port", "18.0")` → "removed" + "--gevent-port"

**Effort:** ~2.5 giờ AI-assisted
**Dependencies:** WI1, WI2, WI3, WI4

---

## Work Item 6: USES_CORE_SYMBOL edge từ user code

**Files:**
- Modify: `src/indexer/parser_python.py` — AST visitor detect deprecated API call/decorator
- Modify: `src/indexer/models.py` — `MethodInfo.core_symbol_refs: list[str]`
- Modify: `src/indexer/writer_neo4j.py` — write USES_CORE_SYMBOL edge
- Modify: `tests/test_parser_python.py`, `tests/test_writer_neo4j.py`

### Bước 1: Failing tests (4 tests)

```python
def test_detect_name_get_call_in_method_body():
    """Method body gọi self.name_get() → core_symbol_refs = ['name_get']."""
    src = '''
class X(models.Model):
    _name = "x"
    def foo(self):
        return self.name_get()
'''
    models = _parse_era2_ast(src, ModuleInfo(...))
    foo = models[0].methods[0]
    assert "name_get" in foo.core_symbol_refs

def test_detect_safe_eval_import_usage():
    """from odoo.tools import safe_eval; safe_eval(x) → ref detected."""
    src = '''
from odoo.tools import safe_eval
class X(models.Model):
    def foo(self):
        return safe_eval("1+1")
'''
    models = _parse_era2_ast(src, ModuleInfo(...))
    assert "safe_eval" in models[0].methods[0].core_symbol_refs

def test_write_uses_core_symbol_edge_when_target_exists(neo4j_session):
    """Method có refs + CoreSymbol exists → MERGE edge."""
    # Seed CoreSymbol(name_get, status=deprecated)
    method = MethodInfo(name="foo", model="x", module="m", odoo_version="17.0",
                       core_symbol_refs=["name_get"])
    writer._write_uses_core_symbol_edges([method])
    rec = neo4j_session.run("""
        MATCH (mth:Method {name:'foo'})-[:USES_CORE_SYMBOL]->(cs:CoreSymbol)
        RETURN cs.qualified_name AS qn
    """).single()
    assert "name_get" in rec["qn"]

def test_no_edge_when_core_symbol_missing(neo4j_session):
    """Method có refs nhưng CoreSymbol chưa index → silent skip."""
    method = MethodInfo(name="bar", core_symbol_refs=["unknown_xyz"], ...)
    writer._write_uses_core_symbol_edges([method])
    rec = neo4j_session.run(
        "MATCH (mth:Method {name:'bar'})-[:USES_CORE_SYMBOL]->() RETURN count(*) AS c"
    ).single()
    assert rec["c"] == 0
```

### Bước 2: Code

**`models.py`:**
```python
@dataclass
class MethodInfo:
    # existing fields...
    core_symbol_refs: list[str] = field(default_factory=list)
```

**`parser_python.py`:**
```python
# Set hẹp V0 — chỉ check deprecated/removed symbols để giảm noise
_DEPRECATED_API_SYMBOLS = {
    "name_get",          # removed v18
    "name_search",       # signature changed
    "read_group",        # deprecated v19
    "group_operator",    # renamed v18
    "safe_eval",         # signature changed v19
}

def _extract_core_symbol_refs(method_node: ast.FunctionDef) -> list[str]:
    """Walk method body, detect calls to deprecated symbols."""
    refs = set()
    for node in ast.walk(method_node):
        # self.name_get(), self.foo.name_get()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _DEPRECATED_API_SYMBOLS:
                refs.add(node.func.attr)
        # safe_eval(...) direct call
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _DEPRECATED_API_SYMBOLS:
                refs.add(node.func.id)
    return list(refs)
```

**`writer_neo4j.py`:**
```python
def _write_uses_core_symbol_edges(self, methods: list[MethodInfo]) -> None:
    with self._driver.session() as sess:
        for m in methods:
            for ref in m.core_symbol_refs:
                sess.run("""
                    MATCH (mth:Method {name: $mn, model: $model, module: $mod, odoo_version: $v})
                    MATCH (cs:CoreSymbol {odoo_version: $v})
                    WHERE cs.qualified_name ENDS WITH '.' + $ref
                      AND cs.status IN ['deprecated', 'removed']
                    MERGE (mth)-[:USES_CORE_SYMBOL]->(cs)
                """, mn=m.name, model=m.model, mod=m.module, v=m.odoo_version, ref=ref)
                # Silent skip nếu CoreSymbol không tồn tại (no MERGE placeholder)
```

### Bước 3: Verify

- 4 test mới green
- Manual: index 1 module có method gọi `name_get()` + index Odoo core 18.0 → query `MATCH (mth)-[:USES_CORE_SYMBOL]->(cs) RETURN count(*)` > 0

**Effort:** ~1.5 giờ AI-assisted
**Dependencies:** WI2 (CoreSymbol nodes), WI5 (tool consume edge)

---

## Work Item 7: Tests + snapshots + integration

**Files:**
- Modify: `tests/test_output_snapshots.py` — 5 contract test
- Modify: `tests/test_writer_neo4j.py` — index creation test

### Bước 1: 5 snapshot contract test (1 test/tool)

```python
def test_lookup_core_api_output_contract(neo4j_seeded):
    output = mcp_server._lookup_core_api("name_get", "18.0")
    # Header line
    assert output.startswith("lookup_core_api")
    # Required sections
    assert "Status:" in output
    # Tree connectors
    assert "├─" in output or "└─" in output
    # No None leak
    assert "None" not in output

def test_api_version_diff_output_contract(neo4j_seeded):
    output = mcp_server._api_version_diff("safe_eval", "17.0", "19.0")
    assert output.startswith("api_version_diff")
    # 4 expected sections
    for sec in ["Added", "Removed", "Changed", "Stable"]:
        assert sec in output or "no" in output.lower()

def test_find_deprecated_usage_output_contract(neo4j_seeded):
    output = mcp_server._find_deprecated_usage("18.0")
    assert output.startswith("find_deprecated_usage")
    # Empty render gracefully
    if "no deprecated" not in output.lower():
        assert "├─" in output

def test_lint_check_output_contract(neo4j_seeded):
    output = mcp_server._lint_check("raise UserError('x')", "19.0", "python")
    assert output.startswith("lint_check")

def test_cli_help_output_contract(neo4j_seeded):
    output = mcp_server._cli_help("server", "--longpolling-port", "18.0")
    assert output.startswith("cli_help")
    assert "Status:" in output
```

### Bước 2: Index creation integration test

```python
def test_spec_schema_indexes_exist(neo4j_session):
    """Sau setup_indexes(), 4 index mới phải tồn tại."""
    writer.setup_indexes()
    indexes = neo4j_session.run("SHOW INDEXES").data()
    names = {i["name"] for i in indexes}
    expected = {"core_symbol_qn", "lint_rule_id", "cli_command_name", "cli_flag_name"}
    assert expected.issubset(names)
```

### Bước 3: Verify

- 6 test mới green (5 snapshot + 1 integration)
- `make test-all` toàn green

**Effort:** ~1 giờ AI-assisted
**Dependencies:** WI2-WI6

---

## Work Item 8: Docs M4.5

**Files:**
- Modify: `TASKS.md` — M4.5 từ `[ ]` → `[~]` start, → `[x]` xong
- Modify: `README.md` — bỏ "(planned)" cho 5 tool M4.5; legend update; status M4.5 → `[x]`
- Modify: `docs/thiet-ke-kien-truc.md` — bỏ "(planned)" cho 4 node mới
- Modify: `CLAUDE.md` (project root) — bỏ note "v8/v9 silent-skip" sau khi fix
- Modify: `CONTRIBUTING.md` — ADR-0002 reference

**Effort:** ~30 phút
**Dependencies:** WI0-WI7 complete

---

## Risk & Mitigation

| Rủi ro | Mitigation |
|---|---|
| `parser_odoo_core.py` parse 2000+ file v19 → quá chậm | **Allow-list 8 file cố định** (xem `_CORE_FILES` trong WI2). Không walk toàn bộ source. Performance test: <2s cho 3 version. |
| Python 2 v8/v9 regex fragile cho `_columns` dict phức tạp | Chỉ extract tên field + ttype basic. Skip method body (no AST). Graceful skip + WARN log khi regex fail. Test fixture với 3 sample real Odoo 8 modules (account, sale, stock). |
| `_latest_version() = None` → tool caller crash | Thống nhất error message "No data indexed. Run `python -m src.indexer --profile <name>` first." Test mọi tool với DB rỗng. |
| USES_CORE_SYMBOL sparse → `find_deprecated_usage` luôn empty | Tool docstring + output header note rõ requirement "Run indexer with `--index-core <path>` first". Show count `(0 results — verify Odoo core indexed)` không phải imply "no deprecation". |
| Static spec v8-v16 obsolete khi M6 curate | Field `_curate_status: pending` + `_generated_at: YYYY-MM-DD` trong JSON. Tool output hiển thị `data_source: "static/<date>" curate_status: pending` để user biết gap. |
| `_DEPRECATED_API_SYMBOLS` set V0 quá hẹp (5 symbol) → miss nhiều | Set bắt đầu nhỏ, expand based on production data ở M6. Comment explicit "v0 set — see ADR-0002 §3 for expansion roadmap". |
| Diff engine fuzzy match gây false REPLACED_BY edge | V0: chỉ MERGE REPLACED_BY khi `replacement_qname` explicit set trong CoreSymbolInfo (not auto-detect). M6 thêm fuzzy match qua `short_name` với confidence score + manual review gate. |

---

## Rollback Plan

1. **Trigger:** Indexer chậm hơn >200% baseline M4 (đo: `time python -m src.indexer --profile test_v17`) sau khi WI2 land. HOẶC `lint_check` false-positive >20% trên test set 5 query manual David chạy.
2. **Action:** Revert WI2-WI6 (5 parser + writer extension + USES_CORE_SYMBOL edge) — giữ WI1 (Phase 0 v8/v9 fix là bugfix độc lập, không phụ thuộc spec layer). Tools graceful return "spec data not indexed" nếu CoreSymbol/LintRule/CLI nodes vắng.
3. **Owner:** David Tran — gate merge WI3+ sau khi confirm WI2 indexer performance OK trên repo Viindoo thực tế.

---

## Definition of Done

- [ ] All 9 WI `[x]`.
- [ ] `make lint` clean (ruff).
- [ ] `make test` green (~52 test mới + existing không regression).
- [ ] `make test-integration` green (Neo4j integration tests).
- [ ] `_latest_version()` không hardcode "17.0", numeric compare `toInteger`.
- [ ] `registry.py` tìm `__openerp__.py` khi `odoo_version <= "9.0"`.
- [ ] `parser_python.py` không crash trên Python 2 file (graceful skip + WARN log).
- [ ] ADR-0002 status `Accepted` + reference trong CONTRIBUTING.md.
- [ ] `tests/test_output_snapshots.py` có 5 contract test cho 5 tool mới.
- [ ] Manual smoke: `lookup_core_api("safe_eval", "19.0")` → status + signature.
- [ ] Manual smoke: `cli_help("server", "--longpolling-port", "17.0")` → `status: stable`; v18 → `status: removed`, replacement `--gevent-port`.
- [ ] Manual smoke v8: clone Odoo 8 → `python -m src.indexer --profile odoo8 --version 8.0` → log INFO show ≥10 module discovered (sanity check).
- [ ] Commit prefix `[ADD|IMP|FIX|REF]`, KHÔNG `Co-Authored-By: Claude` trailer.

---

## Effort Estimate

| WI | Tên | AI-assisted |
|----|-----|-------------|
| WI0 | ADR-0002 review | 30m |
| WI1 | Phase 0 v8/v9 enablement | 2.5h |
| WI2 | parser_odoo_core + diff_engine + CoreSymbol | 3h |
| WI3 | parser_lint_rules + LintRule | 2h |
| WI4 | parser_cli + CLICommand/CLIFlag | 2h |
| WI5 | 5 MCP tool | 2.5h |
| WI6 | USES_CORE_SYMBOL edge | 1.5h |
| WI7 | Tests + snapshots | 1h |
| WI8 | Docs M4.5 | 30m |
| **Total** | | **~15.5h AI-assisted** |

(Theo ETHOS §4.1.1 "Boil the Lake": AI làm hoàn chỉnh, estimate based trên M4 velocity ~6h cho 8 task tương tự. M4.5 scope ~2.5× M4.)

---

## Open Questions / Nice-to-Have Defer

**Đã decide không cần hỏi David:**
1. **Odoo upstream source path** — profile config (`core_source_path` trong `odoo-semantic.conf`), không CLI flag.
2. **Static spec v8-v16 scope** — empty placeholder JSON, defer curate M6.
3. **`find_deprecated_usage` scope** — default current profile (version từ context), `--all-profiles` defer M6.

**Defer M6:**
- Full `USES_CORE_SYMBOL` bind (không chỉ deprecated — bind mọi API call) — cần `_DEPRECATED_API_SYMBOLS` mở rộng đầy đủ per version.
- Incremental spec re-index (chỉ diff khi Odoo version bump).
- `lint_check` chạy real subprocess (pylint/ruff thực tế thay vì pattern match) — performance risk.
- Static spec v8-v16 manual curation hoặc community contribution path.

---

## References

- ADR-0001: Schema Evolution Policy (PostgreSQL)
- ADR-0002: Spec Schema Policy (CoreSymbol/LintRule/CLI nodes) — pre-work của M4.5
- ETHOS §4.1.1 (Boil the Lake), §4.1.3 (Keep it simple)
- M4 plan precedent: `docs/superpowers/plans/2026-05-07-milestone-4-impact-wow.md`
- Survey notes: 3 turn discovery của ~80 changes v17→v18→v19 (CoreSymbol/LintRule/CLI surface area)
