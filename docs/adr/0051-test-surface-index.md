# ADR-0051: Test Surface Index (Neo4j graph layer + 6 MCP tools + 2 resources)

**Status:** Accepted (implementation complete WI-1..5)

**Date:** 2026-06-18

**Author:** Viindoo Engineering (Wave E — test surface indexing)

**Relates to:** ADR-0001 (schema evolution), ADR-0013 (is_definition ranking), ADR-0023 (tool output contract), ADR-0029 (session context), ADR-0048 (ORM read bounds + same-name topology)

---

## Context

OSM indexed Odoo source (models, fields, methods, views, lint rules, CLI commands, stylesheets) across v8-v19 but left the entire **automated-test surface unindexed**. AI agents working with Odoo must answer test-related questions from incomplete memory:

1. **No test discovery:** `find_examples("unit test for sale order confirmation")` returns production `action_confirm`, never a TestClass. Agents reinvent existing tests.
2. **No test semantics:** `suggest_pattern("write unit test")` returns production compute patterns. Agents don't know `TransactionCase` vs `SavepointCase` vs `HttpCase`.
3. **Version-specific test mechanics unknown:** most common: misusing `cr.commit()` in a transaction-isolated test (forbidden, causes rollback failure).
4. **Test class hierarchy dark:** agents don't know which helpers exist, what they set up, who extends them. Copy-paste mistakes cascade.
5. **Frontend test surface unindexed:** agents don't know Hoot vs QUnit vs web_tour patterns; no examples to cite.

This decision extends OSM to index test metadata as a parallel Neo4j graph layer (TestClass/TestMethod/TestHelper/JsTestSuite nodes) with coverage edges (COVERS_MODEL/COVERS_FIELD/COVERS_METHOD) linked to production models, exposing 6 new MCP tools + 2 new resources over the same zero-migration pgvector chunk system.

---

## Decision

Index the automated-test surface v8-v19 as a version-aware Neo4j graph layer mirroring the C1 model schema discipline.

### Node labels (4 new)

```cypher
(:TestClass {
  name,                           // e.g. 'TestSaleCommon'
  module,                         // e.g. 'sale'
  file_path,                      // e.g. 'tests/common.py' — repo-relative
  repo,                           // e.g. 'odoo_17.0' — repo dir basename (Defect H fix)
  odoo_version,                   // e.g. '17.0'
  test_type,                      // 'transaction'|'savepoint'|'single_transaction'|'http'|'form'|'unittest'|'unknown'
  base_classes_ordered,           // list, MRO order — preserves order (HIGH-1)
  tagged,                         // list, raw incl '-at_install' etc (MISSED)
  commit_allowed,                 // bool, True only for @standalone (contract PP3)
  defines_no_test_methods,        // bool, provisional; finalized in reconcile
  is_helper,                      // bool, finalized in reconcile (MISSED-1)
  docstring,                      // str or null
  line,                           // int, class line number
  profile                         // [list] of profiles seeing this node
})

(:TestMethod {
  name,                           // e.g. 'test_amount_total_computed'
  test_class,                     // str, parent class name (matches TestClass.name)
  module,                         // e.g. 'sale'
  file_path,                      // e.g. 'tests/common.py'
  repo,                           // e.g. 'odoo_17.0' — repo dir basename (Defect H fix)
  odoo_version,                   // e.g. '17.0'
  tagged,                         // list
  docstring,                      // str or null
  field_refs,                     // [list] self.<field> names found in method scope
  model_refs,                     // [list] self.env['model.name'] found in method scope
  method_refs,                    // [list] method calls found
  asserts_count,                  // int, number of assert/self.assert* lines
  line,                           // int, method line number
  profile                         // [list]
})

(:TestHelper {
  name,                           // e.g. 'TransactionCase' or 'AccountTestCommon'
  module,                         // 'sale', 'account', or '@framework' for odoo/tests bases
  odoo_version,                   // e.g. '17.0'
  origin,                         // 'addon' | 'framework'
  test_type,                      // 'transaction'|'savepoint'|'http'|'form'|'unittest'
  setup_summary,                  // [list] e.g. ['savepoint-per-method', 'auto-rollback']
  commit_allowed,                 // bool
  file_path,                      // str or null; null for framework helpers (module='@framework')
  line,                           // int or null
  profile                         // [list]
})

(:JsTestSuite {
  file_path,                      // e.g. 'static/tests/hoot/my_test.js' — repo-relative
  module,                         // e.g. 'web', 'sale'
  odoo_version,                   // e.g. '18.0'
  framework,                      // 'hoot'|'qunit'|'tour' — disambiguated by import/module path
  describe_blocks,                // [list] top-level describe(...) titles
  test_names,                     // [list] test(...) titles within blocks
  tags,                           // [list] e.g. ['@smoke', '@regression']
  mounts,                         // [list] e.g. ['web.FormRenderer', 'sale.FormDyn']
  mock_models,                    // [list] e.g. ['sale.order', 'account.move'] — hand-rolled mocks, NOT edges
  line,                           // int
  profile                         // [list]
})
```

**CRITICAL-1 (debate fix) + Defect H amendment:** TestClass MERGE key = (name, module, file_path, repo, odoo_version). Test classes are FILE-scoped, not module-scoped: `sale` v17 has two distinct `TestSaleCommon` at `tests/common.py:237` and `tests/test_common.py:10`. Without file_path in the key, the second would incorrectly overwrite the first. The `repo` field (dir basename, e.g. `odoo_17.0`) was added to the MERGE key to prevent cross-repo collisions: both the `odoo` and `enterprise` repos can contain `sale/tests/test_sale_order.py::TestSaleOrder` at the same version. Without repo in the key the second repo's write silently overwrites the first. TestMethod key includes both file_path and repo for the same reasons. Framework helpers (TestHelper with module='@framework') have null file_path and no repo — they omit both from the MERGE (they are version-scoped sentinels, not repo-owned). GC prune queries are also repo-scoped so a per-repo GC call cannot delete nodes belonging to another repo at the same version.

### Edge types (6 new)

```cypher
(:TestClass)-[:DEFINED_IN]->(:Module)
  — reuse existing edge; addon nodes only; framework TestHelper omits this.

(:TestMethod)-[:BELONGS_TO_TEST]->(:TestClass)
  — 1:N membership; every test method belongs to exactly one class.

(:TestClass)-[:INHERITS_TEST]->(:TestHelper|:TestClass)
  — MRO-ordered fanout; non-gated by TEST_BASE_CLASSES classification (HIGH-1).
     Every ClassDef in a test file emits a node, including non-Case mixins.
     Resolution by (name, odoo_version), scope-crossed (addon + framework).

(:TestHelper)-[:INHERITS_TEST]->(:TestHelper)
  — framework inheritance chain; e.g. HttpCase -> TransactionCase -> TestCase.

(:TestMethod)-[:COVERS_MODEL]->(:Model)
  — via in {setup, assert, body}; targets is_definition node only (ADR-0048 K×D, never K²).
     Extracted via self.env['model.name'] in class+method scope.

(:TestMethod)-[:COVERS_FIELD]->(:Field)
  — targets is_definition node; extracted via def-use pass.
     E.g. setUp: self.order = self.env['sale.order'].create(...)
          then in method: self.order.amount_total
          -> COVERS_FIELD amount_total.

(:TestMethod)-[:COVERS_METHOD]->(:Method)
  — targets is_definition node; extracted from method calls in test scope.

(:TestClass)-[:TESTS_TOUR]->(tour_name_str)
  — best-effort L6 indexing; targets web_tour.tours registry entries.
     No Tour node created; edge target is a string literal (pragmatic).
```

All ORM-read Cypher uses flat per-hop `OPTIONAL MATCH` + name-dedup `collect(DISTINCT ...)` (ADR-0048; never VLP `*1..N`, never `CALL { WITH }`).

### Merge semantics + reconciliation

1. **Parser emit (L1):** produces TestClassInfo + TestMethodInfo + TestHelperInfo + JsTestSuiteInfo dicts per file/module.
2. **Writer batch (L2):** `MERGE (:TestClass {name, module, file_path, odoo_version})` — idempotent per triple-index run. Multi-profile unions via standard `_profile_union_set` helper.
3. **Reconcile passes (L3, version-wide, post-single-repo loop):** run VERSION-WIDE (not repo-gated; required for incremental where cross-repo bases need resolution):
   - **reconcile_test_inherits:** resolve all `:INHERITS_TEST` edge targets by (name, odoo_version); scope-crossed; unknown bases are silently skipped (no dangling edges).
   - **reconcile_test_coverage:** resolve all `:COVERS_*` edge targets to is_definition nodes; graceful unknown-skip (no dangling edges).
   - **finalize is_helper:** set `is_helper=true` on TestClass/TestHelper nodes that define no test methods AND have no outbound :DEFINED_IN (framework sentinels stay is_helper=true).

### Schema versioning

- **pgvector new chunk_types (TEXT column, zero migration):** `test_class`, `test_method`, `js_test`.
- **Intent headers (async embedder):** asymmetric per chunk type, e.g. `[test] Model.field via Class.method (v17, transaction)`.
- **Framework base seeding:** odoo/tests walk per version creates TestHelper nodes with origin='framework', module='@framework' (sentinel, not a real module). Enables all 12 versions to answer `test_base_classes` with real graph nodes (falls back to static data if seeding incomplete).

### `is_definition` ranking for COVERS_* targets

When extracting a COVERS_FIELD edge to `sale.order.amount_total`:

1. Query for the `is_definition=true` Field node at the target version.
2. If multiple Field nodes exist (same-name across modules — rare), use the highest `field_count` (heuristic from ADR-0013).
3. Link to the `is_definition` node only (ADR-0048 K×D rule: never K² mesh).
4. Unknown refs (no definition node found) are silently skipped (graceful degradation, no dangling edges).

### Static reference coverage semantics

COVERS_* edges represent **static mention** of a model/field/method in test scope, **not executed coverage**. A test that has `self.env['sale.order'].search(...)` in a docstring comment will be indexed as if the code runs; in practice, this is rare and acceptable trade-off (simpler parser, fewer false negatives). Tools emit "static reference coverage" caveat in output (e.g., test_coverage_audit).

### JavaScript test surface (file-grained, no JS->Model edges)

JsTestSuite nodes are **file-grained** (one node per test file). Framework detection:

- Import `@odoo/hoot` → framework='hoot'
- Call `QUnit.module` / `QUnit.test` → framework='qunit'
- Call `web_tour.tour` / registry entry → framework='tour'

Per-file metadata: describe_blocks (top-level describe titles), test_names (test titles), tags (raw from `@tag()` decorator), mounts (component/model names for OWL render testing), mock_models (hand-rolled mock class `_name` values from `defineModels` / `class extends models.Model`).

**NO JS->Model edges** are emitted (MED-1). Hoot v18+ provides hand-rolled mock models (`models.Model = class { _name = 'sale.order.mock'; ... }`) that are not real ORM models. Linking them to production models would pollute coverage semantics. Instead, mock_models are stored as-is in the JsTestSuite node for introspection (agents can see the mock contract without false coverage claims).

### Era1 degraded path (v8-v9)

v8-v9 test classes use legacy `_columns` syntax and lack full AST support (pre-Python-3 idioms). Parser falls back to text-regex extraction:

- `test_type` set to 'unknown' (cannot reliably classify without AST).
- Base classes extracted via regex; unknown bases skipped.
- No def-use pass (too error-prone).
- Nodes emit as-is, marking `test_type='unknown'` so tools can flag degraded data.
- No silent drops; always emit a node (HIGH-1 principle: never gate emission).

### Incremental indexer + stale-GC

- **Incremental flow:** after `parser_test.parse_module(info)` yields test results, `writer.write_test_results(...)` executes the merge+batch+reconcile sequence per version.
- **Cross-version reconciles:** `reconcile_test_inherits` / `reconcile_test_coverage` run VERSION-WIDE (gated by version, not by "repo changed"). Enables cross-repo base resolution in incremental runs.
- **Stale-GC (--full mode):** when `--full --drop-stale-nodes` is used, `writer` removes all TestClass/TestMethod/TestHelper/JsTestSuite nodes from the target version (prior to re-indexing) + all their :COVERS_* edges. Additive design ensures re-index rebuilds correct graph without dangling references.

---

## MCP Tools + Resources (+6 tools, +2 resources, 25->31, 7->9)

### 6 new tools

All tools:
- Emit **ADR-0023 tree grammar** output (English-only).
- End with **`Next:` footer** (drill-down hint).
- Accept **odoo_version** with **active-version default** (ADR-0029).
- Are **read-only** against the indexed graph.
- Include **concurrent-subagent note** in docstring: pass odoo_version explicitly to avoid session pin race.

**Tool 1: find_test_examples**

Semantic search restricted to test chunks (test_method, test_class, js_test). Never returns production method/field chunks. Kind filter: 'js' -> js_test only; 'transaction'/'http'/'form'/'savepoint' -> backend test types only. Optional model post-filter.

Example trigger: "show me a test for sale order", "how is amount_total tested", "viết test mẫu cho computed field".

**Tool 2: tests_covering**

List TestMethods with COVERS_MODEL/COVERS_FIELD/COVERS_METHOD edges to a target (model, optional field, optional method). Groups results by via type (assert / setup / body). Static reference coverage only; index may lag behind live runs. Falls back to suggest semantic search if no edges found.

Example trigger: "which tests cover sale.order", "what tests exist for amount_total", "test coverage của trường này".

**Tool 3: test_class_inspect**

Inspect a TestClass or TestHelper by name (case-sensitive). Returns base chain, setUpClass fixtures, test methods, subclassed-by count. Method discriminator: 'summary'|'hierarchy'|'methods'|'setup'. Resolves by name + optional module/file_path narrowing (handles same-name collisions from CRITICAL-1).

Example trigger: "inspect TestSaleCommon", "who inherits AccountTestInvoicingCommon", "show test class hierarchy".

**Tool 4: test_base_classes**

Authoritative framework base-class menu (TransactionCase, SavepointCase, HttpCase, Form, SingleTransactionCase per version) + **cursor contract: cr.commit() FORBIDDEN — isolation is savepoint rollback** (PP3 contract, always output).

Queries graph for TestHelper(origin='framework', module='@framework') nodes; falls back to static known data for v8-v19 if seeding incomplete.

Example trigger: "what base class should I use", "TransactionCase vs SavepointCase", "can I use cr.commit() in a test".

**Tool 5: test_coverage_audit**

Module-wide audit: list fields/methods with zero COVERS_* edges. Returns count + percentage coverage, unreferenced field/method list grouped by model. Caveat: "static mention, not executed coverage".

Example trigger: "what fields have no test coverage in sale", "coverage gaps in module X", "audit test coverage".

**Tool 6: js_test_inspect**

List JsTestSuite nodes for a module, optionally filtered by framework ('hoot'|'qunit'|'tour'). Shows framework mix, file paths, sample test names, describe blocks, mounts. Note: no JS->Model coverage edges; mock_models are displayed as-is for reference.

Example trigger: "what Hoot tests exist in account", "JS tests for sale", "OWL component test nào".

### 2 new resources

**Resource 1: odoo://{version}/test/{module}/{class_name}**

Markdown output: TestClass definition, inheritance chain, setup summary, test methods, subclassed-by list (like `test_class_inspect` tree).

**Resource 2: odoo://{version}/testcoverage/{model}**

Markdown output: Test coverage audit for a model (like `test_coverage_audit` tree).

Both resources: LRU 1000 entries / 300s TTL / per-tenant cached (ADR-0034).

---

## Files changed (WI-1..5)

| File | Change |
|------|--------|
| `src/indexer/models.py` | ADD 5 dataclasses: TestMethodInfo, TestClassInfo, TestHelperInfo, JsTestSuiteInfo, TestParseResult |
| `src/indexer/parser_test.py` | NEW: era2 AST + era1 regex path; ordered/qualified base helper; @tagged/@standalone extraction; self.env['model'] def-use pass; asserts_count; framework seeding |
| `src/indexer/parser_js_test.py` | NEW: JsTestSuite extraction (framework, describe/test titles, tags, mounts, mock_models) |
| `src/indexer/parser_odoo_core.py` | ADD framework-base seeding into odoo/tests walk per version |
| `src/indexer/pipeline_repo.py` | ADD parser_test/parser_js wiring + writer.write_test_results |
| `src/indexer/writer_neo4j.py` | ADD write_test_results(), indexes, reconcile_test_inherits(), reconcile_test_coverage(), finalize is_helper, stale-GC for --full |
| `src/indexer/writer_pgvector.py` | ADD make_test_chunks() with intent header |
| `src/constants.py` | ADD test_class, test_method, js_test to VALID_CHUNK_TYPES |
| `src/mcp/tools/test_tools.py` | NEW: 6 tools + implementation helpers, @mcp.tool() registration |
| `src/mcp/test_query.py` | NEW: Cypher builders (flat OPTIONAL MATCH) for all 6 tools |
| `src/mcp/resources.py` | ADD 2 new resource handlers |
| `src/mcp/tools/inspect.py` | EXTEND module_inspect method='tests' discriminator |
| `src/mcp/tools/discovery.py` | EXTEND suggest_pattern category='test' filter |
| `src/mcp/server.py` | REGISTER test_tools module at reload-pop + import block |
| `src/data/patterns.json` + schema | ADD ~8 test-* patterns, category field |
| `site/src/lib/constants.ts` | BUMP TOOL_COUNT=31, RESOURCE_COUNT=9, SITE_VERSION=0.15.0 |
| `pyproject.toml` | BUMP [project].version = 0.15.0 |
| `README.md` | UPDATE tool/resource counts, add test surface section |
| `CHANGELOG.md` | ADD v0.15.0 entry |
| `docs/adr/INDEX.md` | APPEND ADR-0051 one-liner |

---

## Consequences

**Positive:**

- Agents can now ground test decisions on real test patterns, reducing reinvention.
- Version-aware test semantics available (cursor contract, framework base menu, test type classification).
- Test coverage visibility (what fields are tested, what gaps exist) enables intelligent test authoring.
- Static reference coverage (def-use link) bridges parser-scope to production-scope impacts.

**Risk/Limitation:**

- **Static, not executed:** coverage edges represent source mention, not runtime execution. Dead code with a test mention is counted as covered. Acceptable for AI grounding (simpler, fewer false negatives).
- **File-grained JS tests:** no intra-file JsMethod nodes. Agents can see suite structure and sample test names but cannot link individual JS tests to models. Pragmatic trade-off: Hoot uses hand-rolled mocks; real linking would be false.
- **Framework base seeding:** relies on parsing `odoo/tests/*.py`. If seeding incomplete, tools fall back to static known data. Low risk (framework bases change rarely).

---

## Alternatives considered + rejected

**Alt 1: Thin find_examples wrapper**

Instead of 6 new tools, extend existing find_examples with a test-only chunk filter. Rejected: insufficient surface. Tests need structured introspection (test_base_classes menu, test_class_inspect tree, coverage audit). A single filter-aware find_examples would not unblock the 5 pain points.

**Alt 2: PG coverage cache table**

Store COVERS_* edge summary in a denormalized Postgres table (field_id, test_count, coverage_pct) updated by writer. Rejected: SSOT violation. Neo4j edges are the source of truth; duplicating into PG invites sync bugs. Cypher queries on the graph are authoritative and fast enough (per-version, bounded concurrency via ADR-0048).

---

## Amendments

**Amendment 1 (PR #323 defect fixes):**

Three post-review defects were fixed in the same PR as the original WI-1 implementation:

- **Defect A (COVERS_METHOD never created):** `reconcile_test_coverage` was missing the COVERS_METHOD MERGE block. Added: resolves `method_refs` (plain method name strings on TestMethod) to `(:Method)` nodes on the is_definition Model node, mirroring the COVERS_FIELD join-via-COVERS_MODEL pattern (ADR-0048 K×D). Graceful-skip for unknown refs.

- **Defect H (GC cross-repo data loss):** `gc_stale_test_nodes` lacked repo-scoping. In a multi-repo profile (e.g. `odoo` + `enterprise` both having a `sale` module at 17.0), the per-repo GC call with only that repo's live_modules would delete the other repo's test nodes (whose module names are not in the current repo's live set). Fix: `repo` property added to TestClass/TestMethod MERGE key; both prune queries now filter by `repo: $repo` matching `gc_stale_modules`. Indexes updated accordingly.

- **Defect I (incremental GC deletes unchanged modules):** `live_test_files_by_version` was built from `test_results` (the incremental-filtered changed-module subset) but `live_module_names_by_version` (the full pre-incremental registry) was passed as the file-level prune scope. On an incremental run, unchanged modules never emit test files, so their file_paths would not appear in live_test_files_by_version. The file-level prune query `WHERE module IN $live_modules AND NOT file_path IN $live_files` then deletes all TestClass/TestMethod nodes of unchanged modules. Fix: a new `live_modules_for_file_gc` parameter scopes the file-level prune to only the modules actually re-parsed this run (the changed-module subset); unchanged modules are excluded from file-level prune entirely.

---

## References

- **Plan:** `/tmp/osm-test-survey/phase7/solution-design.md` (637 lines, full schema/Cypher/tool specs)
- **Debate:** `/tmp/osm-test-survey/phase8/debate.md` (144 lines, 5 ranked defects, this ADR embeds the fixes)
- **Related ADRs:** ADR-0001 (schema evolution), ADR-0013 (is_definition), ADR-0023 (tool contract), ADR-0029 (session context), ADR-0048 (ORM bounds + K×D topology)
- **CLAUDE.md:** Neo4j 5.x gotchas, C1 schema discipline, parsing eras, tool-count sync

