# PR #11 Boil-the-Lake Fix Plan

> **Status:** ✓ DONE — PR11 merged 2026-05-08

**Created**: 2026-05-08
**Target PR**: https://github.com/Viindoo/odoo-semantic-mcp/pull/11
**Target branch**: `feat/m45-spec-wow` (continue, no new PR)
**Reviewer findings**: 2 BLOCKER + 5 ISSUE — fix all in one pass per ETHOS §4.1.1.

## Triết lý

ETHOS §4.1.1 Boil the Lake — implementation hoàn chỉnh chỉ tốn thêm vài phút so với phương án tắt. Lake này (~1200 LOC) đủ nhỏ để đun cạn 1 lần. Mỗi WI: test-first, commit riêng, pass-criterion verifiable.

## Verification gate cuối (PHẢI đạt trước khi push commit cuối)

| Check | Command | Expected |
|---|---|---|
| Unit tests | `make test` | ≥210 passed (195 cũ + 15 mới) |
| Integration tests | `make test-integration` | ≥135 passed (127 cũ + 8 mới) |
| Lint | `make lint` | clean |
| Smoke v17 index-core | `python -m src.indexer index-core --source ~/git/odoo17 --version 17.0` | log "502 CoreSymbol, 16 LintRule, 12 CLICommand, 80 CLIFlag" |
| Smoke MCP tool | `lookup_core_api("safe_eval", "17.0")` via local MCP server | structured data, không "not found" |
| Smoke v8 unblock | `git clone -b 8.0 ...; python -m src.indexer index-repo --version 8.0` | ≥10 modules indexed (no silent skip) |
| Smoke lifecycle | Cypher `MATCH (cs:CoreSymbol) WHERE cs.added_in IS NOT NULL RETURN cs LIMIT 5` | ≥1 row sau khi index 2 versions liên tiếp |
| ADR-0002 sync | grep `docs/adr/0002-spec-schema-policy.md` §2 | wording match implementation thực tế |

---

## Phần 1 — BLOCKER

### WI-F1: Wire spec parsers vào CLI indexer (fix B1)

**Mục tiêu**: `python -m src.indexer index-core <odoo_root> --version 17.0` populate 4 spec node labels vào DB thật.

**Test trước** (`tests/test_indexer_cli_index_core.py`, ~120 LOC):

1. `test_index_core_subcommand_writes_core_symbols` — fixture mini-odoo tree (8 file allow-list giả, version 99.0) → assert ≥1 CoreSymbol node.
2. `test_index_core_subcommand_writes_lint_rules` — JSON `lint_rules_99.0.json` có 2 rule giả → assert 2 LintRule nodes.
3. `test_index_core_subcommand_writes_cli_commands` — JSON `cli_commands_99.0.json` + `cli_flags_99.0.json` giả → assert ≥1 CLICommand + ≥1 CLIFlag + edge `OF_COMMAND`.
4. `test_index_core_subcommand_idempotent` — chạy 2 lần → count nodes không tăng.
5. `test_index_core_subcommand_diff_edges_emitted` — index v17 rồi v18 (fixture giả) → assert ≥1 CoreSymbol có property `added_in`/`removed_in` (sau khi WI-F2 xong).

**Implementation**:

- `src/indexer/__main__.py`: Refactor `argparse` thành subparser. Subcommands `index-repo` (rename current default behavior) + `index-core`. `index-core` nhận `--source <path>`, `--version <ver>`, `--profile <name>`.
- `src/indexer/pipeline.py`: Thêm function `_index_core(source_root: Path, odoo_version: str, writer: Neo4jWriter)`:
  ```python
  def _index_core(source_root, odoo_version, writer):
      # CoreSymbol từ allow-list 8 file
      symbols = parse_odoo_core(source_root, odoo_version)
      writer.write_core_symbols(symbols, odoo_version)

      # LintRule từ spec_data JSON
      rules = parse_lint_rules_for_version(odoo_version)
      writer.write_lint_rules(rules, odoo_version)

      # CLI từ spec_data JSON
      commands = parse_cli_commands(odoo_version)
      writer.write_cli_commands(commands, odoo_version)
      flags = parse_cli_flags(odoo_version)
      writer.write_cli_flags(flags, odoo_version)

      # Diff với version trước (nếu có)
      previous = _find_previous_indexed_version(odoo_version, writer)
      if previous:
          old_symbols = writer.fetch_core_symbols(previous)
          diff = compute_diff(old_symbols, symbols)
          writer.write_diff_edges(diff)
  ```
- `_find_previous_indexed_version`: Cypher query `MATCH (cs:CoreSymbol) RETURN DISTINCT cs.odoo_version`, sort numeric, pick version < current.
- README.md: thêm section "Indexing Odoo core symbols" sau "Local E2E Quickstart" step 2:
  ```
  # Index core API symbols + lint rules + CLI for version 17.0
  python -m src.indexer index-core --source ~/git/odoo_17.0 --version 17.0
  ```

**Pass criterion**:
- 5 test mới green.
- Smoke real Odoo 17 source tree → log số liệu khớp PR description (502/16/12/80).
- `lookup_core_api("safe_eval", "17.0")` qua MCP server trả structured response.

**Commit**: `[ADD] indexer: index-core CLI subcommand wires spec parsers (PR#11 fix WI-F1)`

---

### WI-F2: Implement 3 lifecycle properties còn lại (fix B2)

**Mục tiêu**: Tuân thủ ADR-0002 §2 — emit `added_in`, `removed_in`, `deprecated_in` ngoài `REPLACED_BY`. Implement đúng spec, KHÔNG amendment.

**Quyết định kỹ thuật**: lifecycle dùng **property trên CoreSymbol** (`cs.added_in`, `cs.removed_in`, `cs.deprecated_in`), KHÔNG tạo Version node + edge tách. Lý do:
- Đơn giản, query nhanh hơn (`WHERE cs.added_in = '18.0'` vs join Version node).
- Không tạo node degree-explosion (mỗi version × thousands symbols = bùng nodes).
- `REPLACED_BY` là edge thật vì nối 2 CoreSymbol khác nhau (graph traversal có ý nghĩa).

ADR-0002 §2 wording PHẢI update để reflect: "Lifecycle expressed as `added_in`/`removed_in`/`deprecated_in` properties on CoreSymbol; `REPLACED_BY` is the only true edge between symbols. Original draft listed 4 edges; revised to 1 edge + 3 properties for query simplicity."

**Test trước** (`tests/test_diff_engine.py` extend, ~80 LOC):

1. `test_diff_added_emits_added_in_property` — symbol `foo` ở v18 không có ở v17 → DiffResult có entry `added=[foo]`, writer set `cs.added_in = '18.0'`.
2. `test_diff_removed_emits_removed_in_property` — symbol `name_get` ở v17 mất ở v18 → `removed=[name_get]`, writer set `cs.removed_in = '18.0'` trên node v17 cũ.
3. `test_diff_deprecated_emits_deprecated_in_property` — symbol có `status='deprecated'` ở v18, `status='active'` ở v17 → `deprecated=[symbol]`, writer set `cs.deprecated_in = '18.0'`.
4. `test_diff_replaced_by_unchanged` — regression cho `REPLACED_BY` edge đã có.

**Implementation**:

- `src/indexer/diff_engine.py`: `DiffResult` dataclass thêm fields:
  ```python
  @dataclass
  class DiffResult:
      added: list[CoreSymbolInfo]
      removed: list[CoreSymbolInfo]
      deprecated: list[CoreSymbolInfo]
      replaced: list[tuple[CoreSymbolInfo, str]]  # đã có
  ```
- `compute_diff(old, new)` so 2 set theo `qualified_name`:
  - In `new` ∖ `old` → `added`.
  - In `old` ∖ `new` → `removed`.
  - In both, `new.status == 'deprecated' AND old.status != 'deprecated'` → `deprecated`.
  - In both, có `replacement_qname` (giữ logic cũ) → `replaced`.
- `src/indexer/writer_neo4j.py`: `write_diff_edges(diff)` extend:
  ```python
  for sym in diff.added:
      tx.run("MATCH (cs:CoreSymbol {qualified_name:$q, odoo_version:$v}) "
             "SET cs.added_in = $v", q=sym.qualified_name, v=sym.odoo_version)
  for sym in diff.removed:
      # remove ở version mới nghĩa là last-seen ở version cũ
      tx.run("MATCH (cs:CoreSymbol {qualified_name:$q, odoo_version:$prev}) "
             "SET cs.removed_in = $cur", q=sym.qualified_name,
             prev=sym.odoo_version, cur=current_version)
  for sym in diff.deprecated:
      tx.run("MATCH (cs:CoreSymbol {qualified_name:$q, odoo_version:$v}) "
             "SET cs.deprecated_in = $v", q=sym.qualified_name, v=sym.odoo_version)
  ```
- `docs/adr/0002-spec-schema-policy.md`: update §2 wording như trên (property approach + lý do).
- `src/mcp/server.py`: `_format_core_symbol` + `_format_api_diff` đọc 3 property mới và surface trong output (thay vì chỉ `cs.status`).

**Pass criterion**:
- 4 test green.
- Cypher `MATCH (cs:CoreSymbol) WHERE cs.added_in = '18.0' RETURN cs.name` trả ≥1 row sau khi index v17 + v18.
- ADR-0002 §2 wording match implementation.

**Commit**: `[ADD] diff_engine + writer: lifecycle properties (added_in/removed_in/deprecated_in) per ADR-0002 §2 (PR#11 fix WI-F2)`

---

## Phần 2 — ISSUE (đun cạn lake)

### WI-F3: I1 — Document USES_CORE_SYMBOL false-positive scope

**Test**: `test_uses_core_symbol_skipped_when_target_not_indexed` — Python source dùng `myhelper.read_group(...)` nhưng `read_group` chưa index trong DB → KHÔNG tạo edge.

**Implementation**:
- `src/indexer/parser_python.py:_extract_core_symbol_refs` thêm docstring giải thích false-positive scope + safety net Cypher writer side.
- `src/indexer/writer_neo4j.py` `write_uses_core_symbol_edges`: confirm WHERE clause filter `WHERE cs.qualified_name ENDS WITH '.' + $ref AND cs.status IN ['deprecated','removed']` đã có.
- ~30 LOC.

**Commit**: `[IMP] parser_python + writer: document USES_CORE_SYMBOL false-positive guard (PR#11 fix WI-F3)`

---

### WI-F4: I2 — Tokenizer-aware brace counter cho `_extract_columns_block`

**Test**:
1. `test_extract_columns_handles_brace_in_string_value` — input `_columns = {'help': 'Use {curly}', 'name': fields.Char()}` → trả full block.
2. `test_extract_columns_handles_nested_dict` — input `_columns = {'meta': {'a': 1}}` → trả full block (nested brace counter chuẩn).

**Implementation** (`src/indexer/parser_python.py:_extract_columns_block`):
- Dùng Python `tokenize` module: tokenize text, count brace level chỉ trên token `OP` `{` và `}`, KHÔNG đếm brace trong token `STRING`.
- Nếu `tokenize.TokenizeError` (Python 2 syntax) → fallback regex hiện tại + log warning.
- ~50 LOC.

**Commit**: `[FIX] parser_python: tokenize-aware brace counter for _columns block (PR#11 fix WI-F4)`

---

### WI-F5: I3 — Era1 method extraction qua regex

**Test**:
1. `test_era1_extracts_method_names_from_class_block` — Era1 source class với 2 method + Python 2 syntax outside → MethodInfo list có 2 entries.
2. `test_era1_method_decorator_captured` — `@api.multi\n  def baz(self):` → `decorators=['api.multi']`.
3. `test_era1_method_with_underscore_prefix_skipped` — `def _internal(self)` → skip per existing convention HOẶC include (theo existing parser_python era2 behavior — confirm trước).

**Implementation** (`src/indexer/parser_python.py:_parse_era1_text`):
- Regex multiline: `^(\s+)(?:@(\S+)\s*\n\s+)?def\s+(\w+)\s*\(self`
- Skip body (chỉ cần signature).
- Populate `MethodInfo(name=..., decorators=[...], docstring=None, line=...)`.
- ~50 LOC.

**Commit**: `[ADD] parser_python: era1 method name extraction via regex (PR#11 fix WI-F5)`

---

### WI-F6: I4 — Tighten lint matcher + V0 banner

**Test**:
1. `test_lint_match_requires_two_token_overlap` — code có 1 token rule → KHÔNG fire.
2. `test_lint_match_fires_on_two_token_overlap` — code có 2 token → fire.
3. `test_lint_check_output_includes_v0_banner` — output có header `⚠ V0 fuzzy matcher — verify manually`.

**Implementation** (`src/mcp/server.py:_match_lint_rule`):
- Tokenize rule.message → set token (≥4 char, alpha-only, stopword filter `the/and/use/etc`).
- Match nếu `len(rule_tokens ∩ code_tokens) ≥ 2`.
- `_lint_check` output prepend constant `_LINT_V0_BANNER = "⚠ V0 fuzzy matcher — verify manually before action."`.
- ~50 LOC.

**Commit**: `[FIX] server: tighten lint matcher to ≥2 token overlap + V0 banner (PR#11 fix WI-F6)`

---

### WI-F7: I5 — Surface `_curate_status: pending` banner

**Test**:
1. `test_cli_help_pending_data_shows_curation_banner` — version có `_curate_status: pending` → output có `ℹ Spec data v8.0 pending curation — limited results`.
2. `test_lint_check_pending_data_shows_curation_banner` — same cho lint.

**Implementation**:
- `src/indexer/parser_lint_rules.py` + `parser_cli.py`: trả `_curate_status` string ngoài rules/flags list. Function signature đổi:
  ```python
  def parse_lint_rules_for_version(version) -> tuple[str, list[LintRuleInfo]]:
      # returns (curate_status, rules)
  ```
- `src/indexer/writer_neo4j.py`: thêm node label `SpecMetadata {kind: 'lint'|'cli', odoo_version, curate_status}` (composite key `(kind, odoo_version)`).
- `src/mcp/server.py:_lint_check`, `_cli_help`: query `SpecMetadata` cho version, prepend banner nếu `curate_status == 'pending'`.
- ~80 LOC.

**Commit**: `[ADD] spec metadata: surface curate_status pending banner in lint/cli tools (PR#11 fix WI-F7)`

---

## Quy tắc thực thi

1. **Test-first**: viết test trước, confirm RED, rồi implement đến GREEN.
2. **Commit riêng từng WI**: KHÔNG batch nhiều WI vào 1 commit.
3. **Format commit**: `[ADD|FIX|IMP] <scope>: <summary> (PR#11 fix WI-Fx)`.
4. **KHÔNG `Co-Authored-By: Claude`** trailer per ADR-009.
5. **KHÔNG suppress test/lint**: nếu fail → fix root cause.
6. **Sau mỗi WI**: chạy `make test` + `make lint` confirm green trước khi sang WI tiếp.
7. **Boil-the-Lake**: nếu phát hiện edge case nhỏ trong lúc implement (vd thêm 20 LOC test cho regression) → cứ làm, KHÔNG defer "future PR".

## Sequence

WI-F1 → WI-F2 → WI-F3 → WI-F4 → WI-F5 → WI-F6 → WI-F7 → Verification gate → Push.

WI-F1 và WI-F2 phụ thuộc nhau (WI-F1 test 5 cần WI-F2 xong) — implement F1 trước, F2 sau, sau đó quay lại F1 test 5.

## Output handoff cho David

Sau khi push commits xong, report:
- Số commit thêm (kỳ vọng ~7-10).
- Số test thêm/passed (kỳ vọng ≥15 test mới).
- Verification gate 8/8 ✅ (hoặc liệt kê fail nào, lý do).
- Link PR #11 (commits mới đã push).
