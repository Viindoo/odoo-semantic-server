# ADR-0025 — CSS/SCSS Indexing: :Stylesheet Node, IMPORTS Edge, tree-sitter-css

**Status:** Accepted
**Date:** 2026-05-17
**Milestone:** Coverage-Fill (WI-A1)

---

## Context

Odoo modules ship CSS/SCSS files in `static/src/scss/`, `static/src/css/`, and `static/css/`.
These files define:
- Branding variables (`--primary-color`, `--font-family`, etc.)
- Component-scoped selectors
- Theme overrides via `@import` chains
- UI customisation hooks: `$variable` SCSS variables consumed by base themes

Prior to WI-A1, OSM indexed **zero** CSS/SCSS content. The gap manifests as:

1. `find_examples` returning no results for queries like "branding variable override" or "theme_primary".
2. No graph path connecting `:Module` to stylesheet files — `impact_analysis` cannot report stylesheet dependents.
3. Branding/theme analysis (e.g. "which modules override `--o-color-primary`?") requires manual file search.

The gap spans v8–v19 (CSS has always been in Odoo). SCSS usage grows significantly from v12+ (when Odoo adopted Bootstrap SCSS variables). Theme modules (e.g. `theme_*`) have **no Python files** — without CSS/SCSS indexing they produce zero semantic content.

---

## Decision

### §1 Neo4j schema: :Stylesheet node

New label `:Stylesheet` with composite MERGE key `(file_path, module, odoo_version)`.

**Properties:**
| Property | Type | Description |
|---|---|---|
| `file_path` | string | Absolute path on disk (part of composite key) |
| `module` | string | Odoo module name (part of composite key) |
| `odoo_version` | string | Odoo version label e.g. "17.0" (part of composite key) |
| `language` | string | `"css"` or `"scss"` |
| `selector_count` | int | Number of rule-sets / selectors found |
| `variable_count` | int | CSS custom properties (`--*`) or SCSS `$variable` declarations |
| `import_count` | int | Number of `@import`/`@use`/`@forward` directives |
| `mixin_count` | int | SCSS `@mixin` definitions (always 0 for CSS) |
| `profile` | string[] | Ancestor profile name array (ADR-0016 Option Y) |

**Rationale for per-file granularity:** One `:Stylesheet` node per file rather than per-module aggregate because:
1. `@import` chain analysis requires file-level source/target nodes.
2. `find_examples` ANN results reference `file_path` — clients need the exact file for click-through.
3. Future MCP tools (`resolve_stylesheet`, `find_style_override`) will query at file level.

### §2 Relationships

**`:Stylesheet -[:DEFINED_IN]-> :Module`**
Always written. Mirrors `:Model -[:DEFINED_IN]-> :Module` pattern. Enables "list all stylesheets in module X" queries.

**`:Stylesheet -[:IMPORTS]-> :Stylesheet`**
Written for each resolved `@import` path. Resolution: try direct path + `_partial.scss` convention (parser_scss._resolve_scss_import). Silent skip when target file is not yet indexed (per §D3 below). This enables `@import` chain traversal: `MATCH (src)-[:IMPORTS*]->(base:Stylesheet {module: 'web'})`.

**Not implemented (deferred to Future Work):**
- `:Stylesheet -[:OVERRIDES]-> :Stylesheet` for SCSS `@extend` chains (requires full AST type resolution).
- `:Stylesheet -[:USES_MODULE]-> :Module` for `@import` that cross module boundaries.

### §3 pgvector chunk types

New `chunk_type` values added to `VALID_CHUNK_TYPES` in `src/constants.py`:
- `"css"` — produced by `make_css_chunks()` in `writer_pgvector.py`
- `"scss"` — produced by `make_scss_chunks()` in `writer_pgvector.py`

**Semantic units (what gets embedded):**
| chunk_kind | Content | Best for |
|---|---|---|
| `variable` | Block of `--*` / `$var` declarations | Branding variable search |
| `selector` | Selector + rule-set | Component style lookup |
| `mixin` | `@mixin name {...}` definition | Reusable style pattern search |
| `media` | `@media condition {...}` block | Responsive breakpoint lookup |
| `import` | Single `@import` directive | Import chain discovery |
| `raw` | Sliding window (no named entity found) | Fallback coverage |

For SCSS, `entity_name` in pgvector is encoded as `"<kind>:<name>"` (e.g. `"mixin:o_form_view"`) so ANN results can be filtered by kind without schema changes.

---

## D-section: Scope & Trade-off Decisions

### D1 — Composite MERGE key rationale

Key: `(file_path, module, odoo_version)`.

- `file_path` alone is not unique across profiles (two repos may ship a file at the same relative path).
- `module + odoo_version` alone is not unique when one module ships multiple CSS files.
- The triple is unique in practice (one Stylesheet node per physical file per version context).
- Mirrors the Module/Model/Field composite key convention from ADR-0001.

### D2 — tree-sitter-css vs regex fallback

**tree-sitter-css** (preferred): accurate parse tree, correct handling of string literals inside rules, comment stripping, proper block boundary detection. Added to `pyproject.toml` as `tree-sitter-css>=0.21`. Available on PyPI at v0.25.0 as of 2026-05-17.

**Regex fallback** (automatic when tree-sitter-css is not installed): handles 95%+ of real Odoo CSS/SCSS. Handles flat selectors, `@media`, `@import`, `$var` blocks, `@mixin`. Degrades gracefully on deeply nested SCSS. Used by default in test environments that skip native extension installation.

Both parsers expose the same `parse_file()` -> `(chunks, StylesheetInfo)` interface. The caller (`pipeline.py`) cannot distinguish which backend ran. If the tree-sitter parse raises an exception, the parser falls back to regex automatically (logged at WARNING).

### D3 — IMPORTS edge: silent skip policy

When a `:Stylesheet` node references an `@import "../../path/to/_partial"` that resolves to a file not yet indexed (or from an un-indexed module), the `MATCH` for the target will return no rows and the `MERGE` is simply not executed. This matches the pattern used for unresolved INHERITS edges in parser_python (ADR-0001 §3).

Consequence: IMPORTS graph is incrementally built — re-indexing after all repos are indexed produces a complete chain. The `--full` flag on `index-repo` triggers a full re-scan including re-writing all IMPORTS edges.

### D4 — Skip list and size limit

CSS/SCSS files are skipped when:
1. They reside in `static/lib/` or `static/tests/` directories (third-party / test content).
2. The file exceeds 200 KB (likely minified/generated — same threshold as `parser_js.py`).

This is intentionally conservative: generated CSS from build tools (`static/dist/`) is excluded because it duplicates source SCSS content and would pollute ANN results with duplicates.

### D5 — mixin_count = 0 for CSS

CSS has no `@mixin` concept. The `mixin_count` property is always 0 for `:Stylesheet {language: "css"}` nodes. This is intentional (avoids nullable schema) — SCSS nodes set `mixin_count` from the actual count found.

---

## Consequences

**Positive:**
- Theme modules (previously 0 semantic content) gain CSS/SCSS embeddings after re-index.
- `find_examples` can now surface branding variable patterns, selector overrides, mixin usages.
- `@import` chain graph enables "who overrides this stylesheet?" traversal queries.
- ADR-0009 pattern catalogue can now include CSS/SCSS patterns.

**Negative / Risks:**
- Re-index time increases by ~5–15% (CSS/SCSS files are small; tree-sitter parse is fast).
- Regex fallback may miss some edge cases in complex SCSS (e.g. deeply nested rules, string interpolation in selectors). Accuracy improves when tree-sitter-css is installed.
- Minified CSS (e.g. from asset bundles) is excluded by size limit — some legitimate large files may be skipped.

---

## Future Work

The following items are deferred and tracked in `TASKS.md` (WI-A7 absorption from plan `streamed-cuddling-phoenix.md`):

1. **MCP tool surface for Stylesheet** (`resolve_stylesheet`, `find_style_override`) — **M10A** (tracked in `TASKS.md` Milestone 10 § M10A "Tool Surface Expansion"). After B8 re-index populates `:Stylesheet` nodes, expose `resolve_stylesheet(module, odoo_version)` returning the stylesheet chain and variable list, and `find_style_override(selector_or_variable, odoo_version)` tracing which module last re-declares a CSS custom property / overrides a selector. Both tools must follow ADR-0023 tree-grammar contract (§1 header, §1.3 sublist indent, §4 Next-step hint) and update the routing matrix in [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/reference/mcp-tool-routing.md).
   
   > **Tracking:** Implementation tracked at `TASKS.md` → M10A "Stylesheet MCP tools (resolve_stylesheet, find_style_override)".

2. **Pattern catalogue CSS/SCSS entries** — **M11** community contribution track per ADR-0009 (tracked in `TASKS.md` Milestone 11 § "Pattern catalogue expansion 35 → 100+"). Curate 5–10 CSS/SCSS patterns per Odoo era: Bootstrap SCSS variable override (v12+), OWL component scoped CSS (v16+), legacy LESS-to-SCSS migration patterns (v10-v11). Counted toward the ≥100 patterns target.

3. **`:OVERRIDES` edge** for `@extend` chains — not yet scheduled to a milestone (no production demand surfaced). Requires resolving the extended selector to the originating `:Stylesheet` node. Tree-sitter-css provides `extend_statement` nodes with the target selector; resolution requires a lookup by selector text across all indexed stylesheets. When demand arrives, file a new ADR (or extend this one) before implementation — the schema addition touches `:Stylesheet` cardinality assumptions.

4. **Static spec deepening — ESLint SCSS rules** — **M11** (tracked in `TASKS.md` Milestone 11 § "Static spec_data deepening — lint rules 50+/version"). After WI-A4 baseline (per-version lint rules curated) is production-validated, add ESLint SCSS rules (e.g. `scss/no-duplicate-dollar-variables`, `scss/dollar-variable-pattern`) to the LintRule catalogue, counted toward the ≥50 rules per major version target.
