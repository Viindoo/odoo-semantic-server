# ADR-0033 — odoo.tools Symbol Coverage (Curated, Version-Aware)

**Status:** Accepted  
**Date:** 2026-05-22  
**Extends:** ADR-0002 (spec schema policy)

---

## Context

`lookup_core_api` and `find_deprecated_usage` are effectively blind to `odoo.tools.*` APIs because the indexer's allow-list for `parser_odoo_core.py` covers only a small subset (~3 of ~45 `odoo/tools/` files). AI clients frequently misuse `odoo.tools` in three categories:

1. **Wrong import path** — `from odoo.tools import safe_eval` instead of `from odoo.tools.safe_eval import safe_eval`.
2. **Version-absent API** — using `odoo.tools.SQL` (v17+) in code targeting v16 or earlier.
3. **Removed API** — calling `image_resize_image` (removed v13) in a v14+ module.

Auto-parsing all ~45 `odoo/tools/` files would yield noise (internal helpers, private symbols) and false positives. The goal is **misuse semantics**, not exhaustive coverage.

---

## Decision

Introduce a curated, version-aware `tool_export` kind within the existing `CoreSymbolInfo` machinery:

1. **12 static JSON files** (`tools_symbols_{8.0,...,19.0}.json` in `spec_data/`), one per supported Odoo version. Each file contains ~15-20 misuse-prone symbols with correct import path, lifecycle status, and optional replacement.

2. **`tool_export` kind discriminator** — added to `CoreSymbolInfo.kind` enum docstring. Reuses all existing CoreSymbol fields (`qualified_name`, `status`, `signature`, `replacement_qname`) without schema changes.

3. **`parser_tools_symbols.py`** — `_load_static_tools_symbols(version, dir)` mirrors `_load_static_lint_rules`. Returns `list[CoreSymbolInfo]`.

4. **pipeline.py `index_core`** — tool symbols are merged into `symbols` BEFORE `write_core_symbols` and `compute_diff`. This means:
   - They are persisted as regular `CoreSymbol` nodes.
   - `diff_engine.compute_diff(old_symbols, symbols)` computes `added_in`/`removed_in`/`deprecated_in` lifecycle props automatically — no diff_engine changes.
   - `old_symbols` is fetched from Neo4j (`fetch_core_symbols(previous_version)`), so prior-run tool symbols are already included in the diff's baseline.

5. **`_DEPRECATED_API_SYMBOLS`** in `parser_python.py` — extended with image API short-names (`image_resize_image*`, removed v13) and `pycompat` (dropped from `odoo.tools.__init__` v19) to create `USES_CORE_SYMBOL` edges for `find_deprecated_usage`.

---

## Rationale: Curate vs. Auto-Parse

| Criterion | Auto-parse | Curate |
|---|---|---|
| Accuracy | Picks up private helpers, internal utils | Only misuse-prone, public symbols |
| Import path correctness | Cannot infer from AST alone | Explicitly set (e.g. `safe_eval` submodule) |
| Lifecycle notes | Not stored in source | Can attach "introduced v13", "BREAKING v19" |
| Maintenance | Automatic but noisy | Manual, bounded to ~20 symbols/version |

Given that the problem is **AI misuse patterns** (not API discovery), curated is strictly better.

---

## Scope

| Symbol group | Included | Reasoning |
|---|---|---|
| `odoo.tools.SQL` | v17+ | Classic version-hallucination target |
| `odoo.tools.safe_eval.safe_eval` | all versions | Coverage via `parser_odoo_core` (parses `odoo/tools/safe_eval.py`) — NOT via curation. Curated entry is excluded at dedup time (parsed node wins). Listed here for scope completeness. |
| `image_resize_image*` | v8-v12 (stable), absent v13+ | Removal at v13 is high-impact breakage |
| `odoo.tools.image_process` | v13+ | Replacement for image_resize_image |
| `format_datetime`, `format_amount`, `get_lang` | v13+ | Introduced v13; absent v8-v12 |
| `odoo.tools.date_utils` | v12+ | Introduced v12 |
| `odoo.tools.js_transpiler` | v15+ | Introduced v15 |
| `odoo.tools.pycompat` | v8-v18 (stable/deprecated), absent in __init__ v19 | Removal trip-wire |
| `float_compare`, `float_round`, `float_is_zero` | all versions | Common misuse of re-export path |
| `html_escape` | all versions (deprecated v17+) | markupsafe.escape preferred |
| `ustr`, `config`, format constants | all versions | Stable, included for completeness |

Symbols NOT included: private helpers (`_`-prefixed), internal-only utils, symbols where version history is uncertain.

---

## Lifecycle Wiring

Sequential v8→v19 reindex produces correct `added_in`/`removed_in`/`deprecated_in` props:

- `odoo.tools.SQL`: absent in v8-v16 JSON files, present in v17+ → `added_in=17.0`
- `image_resize_image`: present in v8-v12, absent in v13+ → `removed_in=13.0`
- `format_datetime`: absent in v8-v12, present in v13+ → `added_in=13.0`
- `pycompat`: deprecated status from v18 → `deprecated_in=18.0`

No changes to `diff_engine.py` required.

---

## Consequences

- `lookup_core_api("odoo.tools.SQL", "16.0")` → not found (correct: SQL absent v16).
- `lookup_core_api("odoo.tools.SQL", "17.0")` → stable (correct).
- `find_deprecated_usage("17.0")` surfaces modules calling `image_resize_image` (via `USES_CORE_SYMBOL` edge from `_DEPRECATED_API_SYMBOLS`).
- `api_version_diff("odoo.tools.SQL", "16.0", "17.0")` → added in 17.0.
- Maintenance: add new symbols by editing the relevant `tools_symbols_<version>.json` files. No code changes required for new entries.
