---
status: draft
scope: tasks/phase-02-plan
date: 2026-04-22
reads-with:
  - ../roadmap.md
  - ../product_brief.md
  - ../specs/resolve_view.md
  - ../data-model/views.md
  - ../architecture/indexer.md
  - ../architecture/mcp-server.md
  - ../tasks/phase-01-plan.md
  - todo.md
---

# Phase 2 — Implementation Plan

Gate 1 passed 2026-04-22 for P2: `specs/resolve_view.md` status confirmed, `data-model/views.md` confirmed, schema (`views` + `view_patches`) already shipped in `migrations/001_init.sql`. This plan covers the 4 Work Packages that ship the `resolve_view` MCP tool. No P1 tool is modified outside regression testing.

## 1. P2 Scope & Exit Criteria

**Scope:** ship exactly one new MCP tool — `resolve_view`. MCP tool count goes from 3 to 4. No embedding (P3). No QWeb templates (P4). No Studio DB-origin views (permanently out of scope per `specs/resolve_view.md` §6).

**What is not scope:**
- Modifying or re-shipping any P1 handler (`resolve_model`, `resolve_field`, `resolve_method`)
- QWeb template resolution — separate tool, P4
- Dynamic Python-built `arch` — no static representation
- Schema changes — `views` + `view_patches` are already provisioned; P2 does not alter them

**Exit criteria (verbatim from `roadmap.md` + `specs/resolve_view.md` §7):**

| Criterion | Target | Evidence |
|---|---|---|
| Final XML diff vs live Odoo `_get_combined_arch()` | <5% mean across top-50 most-inherited views | `reports/phase-02-accept.md` |
| Token reduction vs raw-source baseline | ≥70% | `reports/phase-02-accept.md` |
| P50 latency for deep-chain views (`res.partner`, `sale.order`) | <100ms | `reports/phase-02-accept.md` |
| Multi-tenant: tenant views union with public | verified | WP-16 integration test |
| P1 regression suite | green | WP-17 |

## 2. Work Breakdown

Effort scale: **S** ≈ ≤1 dev-day, **M** ≈ 2–3 dev-days, **L** ≈ 4–6 dev-days.

### WP-14 — XML view parser + fixture extension

**Goal:** parse `<record model="ir.ui.view">` elements from module `views/*.xml` files and write `views` + `view_patches` rows. Extend the fixture corpus to cover view XML and create edge-case custom view modules.

**Background:** `tests/fixtures/odoo_ce_subset/` currently contains only `models/` subdirectories from 10 CE modules. `tests/fixtures/custom_addons/` contains 10 modules with Python only. Neither corpus has view XML. The `views` + `view_patches` tables exist in the schema but no indexer code touches them.

**Deliverables:**

- [ ] `osm/indexer/xml_parser.py` — public entry point `parse_view_file(path: Path) -> list[ParsedView]`.
  - `ParsedView` and `ParsedPatch` are frozen dataclasses:
    ```
    ParsedView(
        xmlid: str,
        model: str,
        view_type: str,
        inherit_xmlid: str | None,   # raw string from ref="...", NOT resolved to id here
        priority: int,               # default 16
        mode: str,                   # 'primary' | 'extension'
        arch_hash: str,              # blake2b-16 over arch bytes
        arch_xml: bytes,             # serialized <arch> subtree
        patches: list[ParsedPatch],
        file_path: str,
        start_line: int,
        end_line: int,
    )
    ParsedPatch(
        ordinal: int,
        expr: str,
        position: str,
        content: str,
    )
    ```
  - Parser uses `lxml.etree` throughout — not stdlib `xml.etree.ElementTree`. Rationale: lxml provides line-number tracking via `sourceline`, which is required for `start_line`/`end_line` accuracy.
  - `inherit_id` in source may appear as `<field name="inherit_id" ref="module.xmlid"/>`. Store the `ref` attribute value as raw `inherit_xmlid: str | None`; FK resolution to a `bigint` is deferred to WP-15 second-pass (mirrors how `override_of` is handled in `driver.py`).
  - `mode` and `priority` are read from their respective `<field>` children when present; fallback to `'primary'` / `16` per Odoo defaults.
  - `patches` are extracted from the XPath operations nested inside `<field name="arch">` — each direct child element of the arch for an extension view maps to one `ParsedPatch`. `position` is read from the element's `position` attribute (default `'inside'`). `expr` is the XPath expression on the element that targets the parent DOM node (use `lxml`'s `getpath()` as fallback if no `t-att` target is explicit, but in practice Odoo view patches carry explicit XPath on the patching element, not the container — read `ir_ui_view.py::apply_inheritance_specs` to clarify before coding).
  - Edge cases to handle:
    - Primary view that shares an xmlid with a previously-indexed row = upsert, not duplicate
    - `<record>` with missing `<field name="model">` — skip with warning (malformed)
    - `<record>` with missing `<field name="arch">` — skip with warning
    - Multiple `<record>` elements per file — iterate all; return all valid ParsedView entries
  - Does NOT handle `position="replace"` semantics here — that is WP-15's job. Parser just captures the raw patch content and position attribute.

- [ ] Fixture extension — `tests/fixtures/odoo_ce_subset/`:
  - Add `views/` subdirectory to each of the 10 CE modules (base, web, mail, product, sale, account, stock, sale_management, contacts, bus). Pull frozen copies from Odoo CE 17.0 corresponding to the module's existing `models/` freeze. Only `views/*.xml` — no data, security, wizard, or demo files. Keep the `__manifest__.py` `data` key trimmed to match.
  - Update `tests/fixtures/README.md` to document the extension.

- [ ] New custom view modules — `tests/fixtures/custom_addons/` adds 5–10 view-focused modules:
  - `cv_basic_form` — one primary form view, zero extensions; sanity baseline
  - `cv_simple_ext` — one extension adding a field after an existing field (`position="after"`)
  - `cv_replace_and_sibling` — extension A replaces node N; extension B targets a sibling of N; verifies sibling still applies after replace
  - `cv_replace_orphan` — extension A replaces node N; extension B targets a descendant of original N; verifies `applied: false` + `replaced_ancestor` warning
  - `cv_multi_ext_same_target` — three extensions all targeting the same primary view in priority order; verifies `(priority ASC, load_order ASC)` application order
  - `cv_xpath_no_match` — extension with an XPath expression that matches nothing; verifies `xpath_no_match` warning, non-fatal
  - `cv_priority_tie` — two extensions with identical priority; verifies load_order as tiebreak
  - `cv_attributes_op` — extension using `position="attributes"` to add/modify attributes on an existing node

- [ ] Unit tests — `tests/indexer/test_xml_parser.py`:
  - One test per custom module edge case above
  - Test `parse_view_file` on a primary view returns `ParsedView` with `inherit_xmlid=None`, `mode='primary'`, correct `patches=[]`
  - Test multi-record file returns multiple `ParsedView` entries
  - Test malformed record (missing model/arch) logs warning and is excluded from return list

**Effort:** M
**Dependencies:** WP-1–WP-7 (schema + fixture pipeline already in place).
**Status:** not started.

---

### WP-15 — View inheritance resolver (DOM-level `apply_inheritance_specs`)

**Goal:** given a primary view `arch` bytes and an ordered list of extension view rows + their patches, produce the final merged XML matching Odoo's `ir.ui.view.apply_inheritance_specs`. This is the highest-risk WP — correctness deviation here directly drives the <5% diff exit criterion.

**Implementation note:** before coding, read `odoo/addons/base/models/ir_ui_view.py::apply_inheritance_specs` and `::apply_inheritance_spec_single` in full. The semantics described below are derived from that code; the implementation must match it exactly, not a simplified interpretation.

**Deliverables:**

- [ ] `osm/indexer/view_resolver.py` — pure function module, no DB access.
  - Primary entry point:
    ```python
    def resolve_chain(
        primary_arch: bytes,
        extensions: list[tuple[ViewRow, list[PatchRow]]],
    ) -> ResolvedView:
        ...
    ```
    where `extensions` is already sorted by `(priority ASC, load_order ASC)` by the caller (WP-16 handler). `ViewRow` and `PatchRow` are typed dicts or dataclasses matching the columns in `data-model/views.md`.
  - `ResolvedView` dataclass:
    ```python
    @dataclass(frozen=True)
    class ResolvedView:
        final_xml: bytes
        patch_log: list[PatchLogEntry]
        warnings: list[str]
    ```
  - `PatchLogEntry`:
    ```python
    @dataclass(frozen=True)
    class PatchLogEntry:
        from_xmlid: str
        ordinal: int
        expr: str
        position: str
        applied: bool
        reason: str | None   # 'replaced_ancestor' | 'xpath_no_match' | 'malformed_expr' | None
    ```
  - XPath execution via `lxml.etree._Element.xpath()`. Each patch targets the live DOM (mutated by previous patches in application order — not the original primary arch).
  - Position handlers:
    - `after` — insert patch content as next siblings after the matched node
    - `before` — insert patch content as previous siblings before the matched node
    - `inside` — append patch content as last children of the matched node
    - `replace` — remove matched node, insert patch content in its place; track replaced node identity for `replaced_ancestor` detection
    - `attributes` — for each `<attribute name="X">val</attribute>` child in patch content, set or update attribute X on matched node
  - **`replaced_ancestor` detection:** after a `replace` operation, maintain a set of original node identities (using lxml element identity, not xpath). For each subsequent patch, before executing XPath, check if the match would resolve into a node that was a descendant of any replaced node. If so: `applied=False`, `reason='replaced_ancestor'`. This check must use the pre-replace DOM snapshot — the replaced node is no longer in the tree after replacement.
  - **Non-fatal xpath failure:** XPath returning empty list → `applied=False`, `reason='xpath_no_match'`. Do not abort. Continue to next patch.
  - **Malformed XPath:** `lxml.etree.XPathEvalError` or `lxml.etree.XPathSyntaxError` → `applied=False`, `reason='malformed_expr'`. Log warning.
  - `final_xml` is serialized via `lxml.etree.tostring(root, encoding='unicode').encode()` — round-trip consistent bytes.

- [ ] Driver integration — extend `osm/indexer/driver.py`:
  - `_index_xml_files(conn, module_id, addon_path, tenant, git_sha)` method that:
    1. Discovers `views/*.xml` under the addon path
    2. Calls `parse_view_file` for each
    3. Upserts `views` rows (by `xmlid` + `module_id`) with `arch_hash` comparison for delta detection
    4. Upserts `view_patches` rows (delete-and-reinsert by `view_id` if arch changed — simpler than diffing ordinals)
    5. Records `cache_metadata` for each XML file processed
  - Second pass — `inherit_id` FK resolution: after all `views` rows for the current run are upserted, resolve `inherit_xmlid` strings to `bigint` ids. Strategy mirrors `resolver.py`'s `override_of` approach: cross-schema lookup via UNION across `(tenant, public)`. If no match found, log warning and leave `inherit_id` NULL.
  - Call `_index_xml_files` from the main `index()` loop after the Python indexing pass, per module.
  - Delta detection: XML files use the same blake2b-16 hash vs `cache_metadata.content_hash` gate as Python files. If file unchanged, skip parse + upsert. If changed, re-parse and upsert (view-level, not patch-level granularity — simpler and sufficient).

- [ ] Unit tests — `tests/indexer/test_view_resolver.py` (minimum 10 scenarios):
  - `test_no_extensions` — primary only, `patch_log=[]`, `final_xml` equals original arch
  - `test_after_insert` — single extension, `position="after"`, node appears after target
  - `test_before_insert` — single extension, `position="before"`
  - `test_inside_insert` — single extension, `position="inside"`
  - `test_replace_then_sibling` — corresponds to `cv_replace_and_sibling` fixture; sibling patch applies after replace
  - `test_replace_then_descendant_orphan` — corresponds to `cv_replace_orphan`; descendant patch produces `applied=False`, `reason='replaced_ancestor'`
  - `test_multi_ext_order` — 3 extensions applied in `(priority, load_order)` order; result reflects all 3 in sequence
  - `test_xpath_no_match` — non-matching XPath produces `applied=False`, `reason='xpath_no_match'`, `warnings` non-empty
  - `test_malformed_xpath` — syntactically invalid XPath produces `applied=False`, `reason='malformed_expr'`
  - `test_attributes_op` — `position="attributes"` correctly sets/overwrites attribute on matched node
  - `test_priority_tie_load_order` — when priorities equal, load_order determines application order

**Effort:** L (replace semantics + byte-level diff vs live Odoo is the risk concentration point).
**Dependencies:** WP-14 (ParsedView/ParsedPatch types, fixture XML exists).
**Status:** not started.

---

### WP-16 — `resolve_view` MCP handler

**Goal:** ship the `resolve_view` tool through FastMCP, tenant-scoped, returning the envelope defined in `specs/resolve_view.md` §3.

**Deliverables:**

- [ ] `osm/server/handlers/resolve_view.py`:
  - Pydantic input model matching `specs/resolve_view.md` §2:
    ```python
    class ResolveViewInput(BaseModel):
        xmlid: str
        include_final_xml: bool = True
        include_patch_log: bool = True
    ```
  - Lookup: raw SQL UNION-ALL across tenant + public schemas to find the primary `views` row by `xmlid`. If not found → `NotFoundError` (404). Uses `osm/server/db.py::union_all()` per existing convention.
  - Chain fetch: recursive CTE on `inherit_id` starting from the primary row; returns the full inheritance chain ordered by `(priority ASC, load_order ASC)` via join to `modules` for `load_order`. Tenant rows and public rows are interleaved in the UNION before sorting — this is the multi-tenancy model from ADR-0004.
  - Patch fetch: for each extension row in the chain, fetch its `view_patches` rows ordered by `ordinal`.
  - Staleness check: use `effective_indexed_at_sha()` from `osm/server/db.py` across all joined rows. Mismatch → `StaleIndexError` (409).
  - Merge: call `view_resolver.resolve_chain(primary_arch, extensions)` with extensions in chain order (already sorted by CTE).
  - Serialize output envelope:
    - `result.chain` — list of `{xmlid, module, priority, mode}` per chain row
    - `result.patch_log` — included iff `include_patch_log=True`; list of `PatchLogEntry` dicts from `ResolvedView.patch_log`
    - `result.final_xml` — included iff `include_final_xml=True`; `ResolvedView.final_xml` decoded to string
    - `warnings` — union of `ResolvedView.warnings` + any indexer-origin warnings from the chain rows
  - Warnings from `view_resolver` propagate into the envelope `warnings` list — same field used by P1 handlers.

- [ ] Register in `osm/server/app.py::build_app()`:
  - Add `resolve_view` tool via FastMCP tool decorator
  - After this change, `build_app()._tool_manager.list_tools()` must enumerate exactly 4 tools: `['resolve_model', 'resolve_field', 'resolve_method', 'resolve_view']`

- [ ] Golden test file — `tests/fixtures/golden/resolve_view.json`:
  - Minimum 20 entries covering: CE-only primary view (no extensions), CE primary + 1 extension, CE primary + N extensions, tenant-private view, view where an extension has a non-matching XPath, view with replace semantics, xmlid-not-found 404 case, `include_final_xml=False` (chain-only), `include_patch_log=False`
  - Generated via a `scripts/regenerate_golden_views.py` script analogous to `scripts/regenerate_golden.py` from WP-8. Script must preserve `TODO` skeleton entries.

- [ ] Integration tests — `tests/server/test_handlers_resolve_view.py` (DB-gated, `DATABASE_URL`-gated):
  - Boot throwaway tenant schema via `create_tenant`
  - Run WP-14/15 pipeline via `driver.index()` over the WP-14 extended fixture corpus
  - For each labeled golden entry, assert handler output is byte-equal to golden (modulo `indexed_at_sha`)
  - Assert 404 for unknown xmlid
  - Assert 409 when public schema is re-indexed mid-session and tenant chain row's `indexed_at_sha` diverges
  - Assert tenant-private view (defined in tenant schema only) resolves correctly and does not bleed into public

**Effort:** M
**Dependencies:** WP-15 (resolver must exist and be correct; golden entries depend on resolver output).
**Status:** not started.

---

### WP-17 — Top-50 accept test + numerical benchmark

**Goal:** produce the correctness, token-reduction, and latency evidence required by the P2 exit criteria. Same structure as WP-9/WP-11 for P1 — transport-bypass harness; external Claude Code driving deferred to P5.

**Deliverables:**

- [ ] `tests/accept/top50_views.json`:
  - Query: on a full Odoo CE 17.0 index (not the subset fixture), `SELECT root_xmlid, COUNT(extension_count) FROM views GROUP BY inherit root ORDER BY count DESC LIMIT 50`. Persist the resulting list of xmlids + extension counts. Run once, commit, regenerate only on Odoo pin bump.
  - Include at least: `base.view_res_partner_form`, `sale.view_order_form`, `account.view_move_form`, `product.product_template_form_view`, `stock.view_picking_form`.

- [ ] `tests/accept/dump_live_odoo_views.py`:
  - Boots Odoo CE 17.0 (or connects to a running instance), iterates the 50 xmlids, calls `env['ir.ui.view'].browse(id)._get_combined_arch()` on each, writes the canonical XML to `tests/fixtures/golden/resolve_view_live/<xmlid_escaped>.xml`.
  - Canonicalization: `lxml.etree.canonicalize()` applied to both sides before comparison — removes attribute order and whitespace noise.
  - Invocation documented in `tests/accept/README.md` (update existing file or create if absent). Documents: Python env required, `--database` flag, expected duration, how to regenerate.
  - One-shot: run once per Odoo CE pin; output committed. Do not auto-run in CI (Odoo boot is too slow).
  - Risk: WSL2 dev host may not boot Odoo cleanly. Fallback: invoke via SSH on `osm-dev` host, or document manual dump procedure (psql dump of `ir.ui.view` + offline `_get_combined_arch()` reimplementation). Prefer SSH path as it preserves the actual Odoo logic.

- [ ] `tests/accept/runner_p2.py` (extends or complements `runner.py`):
  - Resolves all 50 xmlids via `resolve_view` handler (in-process, same transport-bypass pattern as WP-9)
  - For each xmlid:
    - Canonicalize handler `final_xml` output via `lxml.etree.canonicalize()`
    - Canonicalize live-Odoo golden from `resolve_view_live/` directory
    - Compute diff% as: `len(unified_diff_lines) / max(len(golden_lines), len(handler_lines))` — same formula must be documented in the report header
    - Measure token count: handler merged XML via `tiktoken cl100k_base`; raw-source baseline = concatenated bytes of all XML files in the chain (from `file_path` + `start_line`/`end_line`) tokenized the same way
    - Measure latency: 100-iteration loop per xmlid, record P50 + P99 per view, summarize across all 50
  - Writes `reports/phase-02-accept.md` + `reports/phase-02-accept-raw.json`
  - Asserts all exit criteria pass; exits non-zero if any criterion fails

- [ ] `reports/phase-02-accept.md` (generated):
  - Table columns: `xmlid | diff% | tokens_merged | tokens_raw | reduction% | P50_ms | P99_ms`
  - Summary rows: mean diff%, pass count (diff <5%), headline token reduction, overall P50/P99
  - Human-readable narrative: which views had the highest diff% and why (e.g., `replaced_ancestor` warnings for dynamic arch sections)

- [ ] `reports/phase-02-exit-criteria.md` (hand-authored after runner completes):
  - Cross-reference each roadmap P2 criterion → evidence file + pass/fail
  - Template mirrors `reports/phase-01-exit-criteria.md`

- [ ] `tests/accept/questions.md` — extend with 5 `resolve_view` questions:
  - Q11: "What does the final `res.partner` form view look like in our tenant after all installed modules?" — deep chain, tenant overlay
  - Q12: "What fields has `sale_margin` added to `sale.view_order_form`?" — patch_log attribution
  - Q13: "Show the final form view for `sale.order`, omitting raw XML, just the chain metadata" — `include_final_xml=False` path
  - Q14: "Resolve view `nonexistent.view_foo`" — expected 404
  - Q15: "Show me the view for `account.view_move_form` but only patches from our tenant modules" — tests tenant-scoped overlay; if not directly answerable, document in report

- [ ] P1 regression: re-run WP-9 `runner.py` against the same DB after WP-16 is merged. All 10 P1 questions must still pass. Record outcome in `reports/phase-02-exit-criteria.md` under "P1 regression" section.

**Effort:** M–L (risk: Odoo boot for golden dump, xmlid escaping, canonicalize edge cases).
**Dependencies:** WP-16.
**Status:** not started.

---

## 3. Cross-Cutting Concerns

### Fixture debt — view XML additions

`tests/fixtures/odoo_ce_subset/` currently has `models/` only. WP-14 adds `views/` for the same 10 modules. To prevent fixture bloat:

- Pull only `views/*.xml` — no `data/`, `security/`, `wizards/`, `demo/`. Files that import records from `data/` will parse cleanly because the parser only processes `<record model="ir.ui.view">` elements and ignores all other record types.
- Each module's `__manifest__.py` `data` list stays trimmed to only the XML files actually present in the frozen copy.
- Document the trimming rationale in `tests/fixtures/README.md` under a new "View XML extension" section.

### Golden regen convention

`scripts/regenerate_golden_views.py` inherits the same constraints as `scripts/regenerate_golden.py` (WP-8):

- Idempotent: re-running on an unchanged DB produces bit-identical output
- Preserves `TODO` skeleton entries (entries with `"skip_handler": true`)
- Entries for the 404 path (unknown xmlid) stay as `{"xmlid": "...", "expected_error": "NotFoundError"}` — not overwritten by the regen script

### Performance risk — lxml.xpath on large arch

`res.partner` form view in CE 17 is approximately 1800–2200 LOC of XML. Applying N extension patches via lxml XPath is fast in isolation but may compound. WP-15 should include a standalone benchmark: build a mock primary arch of 2000-line XML + 10 extension patches, measure `resolve_chain()` wall time in isolation (no DB). If P50 exceeds 80ms in this microbenchmark, investigate:

1. Pre-compiling XPath expressions via `lxml.etree.XPath()` (reuse compiled expr object across extensions targeting the same view)
2. Minimizing `tostring()` intermediate calls — serialize once at the end, not per-patch

Do NOT introduce chain SHA caching in P2. Per `specs/resolve_view.md` §9, this is explicitly deferred: profile first in WP-17, file ADR if breach is confirmed.

### Live Odoo golden dump — fallback path

Primary path: SSH into `osm-dev` host, activate the Odoo 17 venv, run `dump_live_odoo_views.py` against an Odoo CE DB with all CE addons installed. Commit golden output.

If `osm-dev` is unavailable:

1. Documented manual fallback: run `psql -c "SELECT ..."` to dump `ir.ui.view` arch from a live Odoo DB, then apply `apply_inheritance_specs` offline via a thin reimplementation. This re-introduces the very risk we are trying to avoid (our impl vs Odoo's), so it is a last resort only.
2. Acceptable interim: run WP-17 with the top-50 list but only 20 views for which golden XML is available; note the remainder as `pending_golden_dump` in the report with a hard deadline before P2 gate closes.

### Regression — P1 tools

WP-17 explicitly re-runs `tests/accept/runner.py` after WP-16 merges. Any P1 regression blocks P2 gate. The handler registration change in `app.py` (adding the 4th tool) is the only surface where a P1 regression could originate — verify `list_tools()` still returns all 4 before closing WP-16.

---

## 4. Dependency Graph

```text
WP-14 ──> WP-15 ──> WP-16 ──> WP-17
  (xml      (DOM       (MCP      (accept +
  parser +  resolver)  handler)   benchmark)
  fixture)
```

Strictly sequential. Each WP has a hard gate: unit tests green before advancing. No parallelization opportunity in P2 given single-developer context and the fact that each WP's types (ParsedView, ResolvedView, handler output) are consumed by the next.

**Critical path:** WP-14 → WP-15 → WP-16 → WP-17. Estimated 10–16 dev-days.

---

## 5. Execution Sequence (Waves)

**Wave 1 (days 1–2):** WP-14 xml_parser.py (pure parsing logic, no DB). Unit tests green on `test_xml_parser.py`. Do not start fixture extension until parser is passing.

**Wave 2 (days 2–3):** WP-14 fixture extension — add `views/*.xml` to CE subset + create 8 custom view modules. Verify `test_xml_parser.py` covers all edge case modules.

**Wave 3 (days 3–5):** WP-14 driver integration — extend `driver.py` with `_index_xml_files` + `inherit_id` second-pass resolution. Integration test: `make index` on extended fixture produces `views` rows.

**Wave 4 (days 5–8):** WP-15 view_resolver.py — implement `resolve_chain`. Start with the 5 simplest scenarios (`test_no_extensions` through `test_inside_insert`), then tackle `replace` + `replaced_ancestor`. The replace semantics are the risk; budget 2 days for this sub-task alone.

**Wave 5 (days 8–10):** WP-16 handler + golden generation + integration tests.

**Wave 6 (days 10–13):** WP-17 — top-50 list, live Odoo golden dump, runner_p2, reports.

**Wave 7 (day 13–14):** P1 regression run + exit gate review.

---

## 6. Risk Register

| # | Risk | Likelihood | Impact | Trigger | Mitigation |
|---|------|------------|--------|---------|------------|
| R1 | `replaced_ancestor` detection incorrect — node identity after `lxml` replace is not trivially trackable because the removed element is detached from the tree | High | High — `replace` semantics are the hardest part of the spec; wrong behavior is directly observable in the <5% diff test | WP-15 unit test `test_replace_then_descendant_orphan` fails | Before coding: read `ir_ui_view.py::apply_inheritance_spec_single` (the canonical source) in full and write test cases from the code, not from the spec prose. Track replaced nodes using their object identity (`id()`) in a set before detachment. |
| R2 | Live Odoo golden dump not available on dev host — WSL2 cannot boot Odoo cleanly (missing postgres, addons path not configured) | Medium | Medium — WP-17 correctness evidence gap | `dump_live_odoo_views.py` fails with Odoo import error or DB not found | Use SSH to `osm-dev` as primary path. If unavailable, fall back to partial golden (20 of 50 views) and document remainder as pending. Do not delay P2 gate indefinitely — set a 3-day deadline from WP-17 kickoff. |
| R3 | lxml xpath performance on large arch exceeds 80ms microbenchmark threshold before P2 gate | Medium | Medium — exit criterion P50 <100ms may fail | WP-15 microbenchmark on 2000-line arch + 10 patches exceeds 80ms | Pre-compile XPath expressions via `lxml.etree.XPath()` as the first optimization. Do not introduce caching (deferred per spec §9). If microbenchmark still fails after pre-compilation, file an issue and profile carefully before P2 gate — this is measurable, not speculative. |
| R4 | `inherit_id` FK resolution fails silently — a view's `inherit_xmlid` resolves to NULL because the parent view's `views` row was not yet upserted when the second pass runs | Medium | High — chain is broken; resolver gets no extensions for affected views | WP-15 integration test shows view chain with only primary row when extensions are expected | Ensure second pass runs after ALL modules' `views` rows are upserted (global second pass, not per-module). Mirror the pattern from `resolver.py::compute_field_override_chains()` which runs after all rows exist. |
| R5 | XPath expressions in Odoo view patches use namespace prefixes (e.g., `t:` for QWeb within `ir.ui.view` arch) that lxml cannot evaluate without a namespace map | Low for CE form/tree views, Medium for `report.*` views | Medium — patches fail to apply, inflate diff% | `xpath_no_match` warnings appear in golden for a view that should apply cleanly | Out-of-scope views (QWeb report arch) should be excluded from the top-50 accept corpus. In the parser, detect `t:` prefixed arch content and emit an `indexer_notes` warning; the handler surfaces it. Exclude views with that warning from the <5% diff assertion. |
| R6 | Duplicate `views` rows per xmlid if multiple fixtures define the same xmlid (e.g., base views in both `odoo_ce_subset` and `custom_addons`) | Low | Medium — resolver gets duplicate primary rows | Integration test shows >1 primary row for a given xmlid | `UNIQUE(xmlid, module_id)` constraint in schema. Parser enforces: if fixture custom addon redefines a CE xmlid as primary, that is a separate `module_id` and a separate row. The resolver's chain-building CTE must follow `inherit_id` not `xmlid` matching. Document in fixture README. |

---

## 7. Exit Gate Checklist (P2 close)

Tick every box before advancing to P3.

### Specification

- [x] `specs/resolve_view.md` status = `confirmed` (confirmed 2026-04-22)
- [x] `data-model/views.md` status = `confirmed`
- [x] `migrations/001_init.sql` includes `views` + `view_patches` tables

### WP-14 — XML parser + fixture

- [ ] `osm/indexer/xml_parser.py` ships + `ruff` + `mypy` clean
- [ ] `tests/indexer/test_xml_parser.py` — all edge case scenarios green
- [ ] `tests/fixtures/odoo_ce_subset/` extended with `views/*.xml` for all 10 modules
- [ ] 8 custom view modules in `tests/fixtures/custom_addons/` merged
- [ ] `tests/fixtures/README.md` updated with view extension documentation
- [ ] Driver `_index_xml_files` + second-pass FK resolution integrated and tested

### WP-15 — View resolver

- [ ] `osm/indexer/view_resolver.py` ships + `ruff` + `mypy` clean
- [ ] `tests/indexer/test_view_resolver.py` — all 11 scenarios green
- [ ] `position="replace"` + `replaced_ancestor` detection correct (verified by `test_replace_then_descendant_orphan`)
- [ ] Non-fatal XPath failure verified by `test_xpath_no_match` + `test_malformed_xpath`
- [ ] lxml microbenchmark on 2000-line arch passes (P50 <80ms in isolation)

### WP-16 — Handler

- [ ] `osm/server/handlers/resolve_view.py` ships + `ruff` + `mypy` clean
- [ ] `build_app()._tool_manager.list_tools()` returns exactly 4 tools
- [ ] `tests/fixtures/golden/resolve_view.json` — ≥20 labeled entries, `scripts/regenerate_golden_views.py` idempotent
- [ ] `tests/server/test_handlers_resolve_view.py` — all labeled golden entries byte-equal
- [ ] 404 for unknown xmlid verified
- [ ] 409 stale index scenario verified
- [ ] Tenant-private view isolation verified

### WP-17 — Accept test + benchmark

- [ ] `tests/accept/top50_views.json` committed
- [ ] Live Odoo golden XML committed to `tests/fixtures/golden/resolve_view_live/` (at least 20 of 50 views; 50/50 preferred)
- [ ] `tests/accept/runner_p2.py` runs clean, exits 0
- [ ] `reports/phase-02-accept.md` published — mean diff <5%, token reduction ≥70%, P50 <100ms
- [ ] `reports/phase-02-exit-criteria.md` published — all P2 criteria green or explicitly documented miss with issue opened
- [ ] `tests/accept/questions.md` extended with Q11–Q15
- [ ] P1 regression: `runner.py` re-run, all 10 Q1–Q10 pass, recorded in `reports/phase-02-exit-criteria.md`

### Operational

- [ ] `ruff` + `mypy` clean on `main` after all WPs merged
- [ ] All unit + integration tests pass with `DATABASE_URL` live
- [ ] `code-reviewer` pass on every merged PR
- [ ] `security-reviewer` pass on WP-16 (handler handles user-supplied `xmlid` string — SQL injection surface via UNION query)

---

## 8. References

| Document | Role |
|---|---|
| `specs/resolve_view.md` | Authoritative spec for tool input/output + semantics |
| `data-model/views.md` | `views` + `view_patches` schema |
| `architecture/indexer.md` | Parser strategy, lxml rationale, parser/handler layering |
| `architecture/mcp-server.md` | Response envelope, error model, 4-tool list |
| `tasks/phase-01-plan.md` | WP numbering origin, style, WP-8/WP-9 patterns to reuse |
| `tasks/todo.md` | WP-1..WP-13 completion status, schema state |
| `odoo/addons/base/models/ir_ui_view.py::apply_inheritance_specs` | Canonical implementation WP-15 must match |
| `reports/phase-01-accept.md` | Token-reduction methodology (tiktoken cl100k_base) reused in WP-17 |
| `scripts/regenerate_golden.py` | Pattern for `scripts/regenerate_golden_views.py` |
| `tests/accept/runner.py` | Pattern for `tests/accept/runner_p2.py` |
