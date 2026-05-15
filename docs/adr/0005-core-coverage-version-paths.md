# ADR-0005: Version-aware Path Resolution for `index-core`

**Date:** 2026-05-10  
**Status:** Accepted, M5.5+

## Context

M4.5 introduced `index-core` to ingest Odoo upstream framework symbols as `CoreSymbol`
nodes (per ADR-0002). The 8-file allow-list covers the primary API surface:

```
odoo/tools/safe_eval.py   odoo/tools/query.py   odoo/tools/sql.py
odoo/fields.py            odoo/models.py        odoo/api.py
odoo/sql_db.py            odoo/exceptions.py
```

The original implementation checked `Path.is_file()` directly against the allow-list
paths, then silently returned `[]` on miss. This caused **two complete blind spots**:

**1. v8/v9 — wrong namespace prefix.**  
Odoo renamed its top-level package from `openerp/` to `odoo/` in v10. On a v8 or v9
source tree, all allow-list paths under `odoo/` are missing — none of the files exist.
Every allow-list entry returned empty; `index-core --version 8.0` produced 0
`CoreSymbol` nodes silently.

**2. v19+ — allow-list paths became package directories.**  
Starting with Odoo 19, three monolithic files were refactored into ORM packages:

| Logical path | Replaced by |
|---|---|
| `odoo/fields.py` | 10 split files under `odoo/orm/fields*.py` |
| `odoo/models.py` | `odoo/orm/models.py` + `odoo/orm/models_transient.py` |
| `odoo/api.py` | `odoo/orm/decorators.py` + `odoo/orm/environments.py` |

`Path(root / "odoo/fields.py").is_file()` returns `False` when that path is a
directory — the `is_file()` check silently skipped the entire ORM surface for v19.
`index-core --version 19.0` produced 0 `CoreSymbol` for all ORM classes, all field
types, and all `api.*` decorators.

A third, smaller issue was also found during the same investigation: `ValidationError`,
`AccessError`, `MissingError`, and `RedirectWarning` all inherit from `UserError`, not
directly from `Exception`. Because `_EXCEPTION_BASE_NAMES` did not include `UserError`,
these classes were classified as `kind=class` instead of `kind=exception`, causing
`find_deprecated_usage` to miss them in exception-type queries.

## Decision

### 1. `_version_prefix(version) -> str`

Returns the correct top-level package prefix for a given Odoo version:

```python
def _version_prefix(version: str) -> str:
    major = int(version.split(".")[0])
    return "openerp/" if major <= 9 else "odoo/"
```

Applied **before** any file-existence check: allow-list paths with `odoo/` prefix are
rewritten to `openerp/` on v8/v9 trees.

### 2. `_resolve_core_paths(odoo_root, logical_path, version) -> list[Path]`

Single function that maps one allow-list logical path to zero or more real paths:

```
Step 1 — prefix substitution (v8/v9):
    logical_path = prefix_new + logical_path[len("odoo/"):]
    if (odoo_root / logical_path).is_file() → return [it]

Step 2 — package-dir fallback (v19+, only when prefix == "odoo/"):
    odoo/fields.py  → glob odoo/orm/fields*.py  (sorted, all .py files)
    odoo/models.py  → [odoo/orm/models.py, odoo/orm/models_transient.py]  (if_file each)
    odoo/api.py     → [odoo/orm/decorators.py, odoo/orm/environments.py]  (if_file each)

Step 3 — missing → return []  (silent skip, caller continues to next allow-list entry)
```

This is the single authoritative resolution point — `parse_odoo_core` iterates
`_CORE_FILES` and delegates every path to `_resolve_core_paths`. No path logic is
duplicated elsewhere.

### 3. `_EXCEPTION_BASE_NAMES` extended to include `UserError`

```python
_EXCEPTION_BASE_NAMES = {"Exception", "Warning", "BaseException", "UserError"}
```

`UserError` is Odoo's own base exception. All standard Odoo exceptions
(`ValidationError`, `AccessError`, `MissingError`, `RedirectWarning`) inherit from it
in one step. Adding `UserError` to the set means `_classify_class` correctly assigns
`kind=exception` to the full shallow hierarchy. Deeper trees (3+ levels of custom
subclassing) would require a recursive AST climb; that is deferred per ADR-0002 §6 and
documented as a known limitation in `src/indexer/parser_odoo_core.py`.

### Out of scope (explicit non-goals, deferred per ADR-0002 §6)

- Walking the full `odoo/` source tree beyond the 8-file allow-list. Adding
  `odoo/http.py`, `odoo/loglevels.py`, `odoo/release.py`, the full `odoo/tools/`
  subtree, and addon-specific files is M6+.
- Backfilling static JSON placeholders for v8–v16 `lint_rules` and `cli_flags` — they
  remain `"curate_status": "pending"` per ADR-0002 §4.

### Future-proofing rule (release manager checklist)

When a new Odoo major version ships:

1. Run `index-core --version <new> --source <Odoo tree>` against a clean checkout.
2. Diff `CoreSymbol` count vs the prior version. A drop of >20% in any `kind` indicates
   a file path was relocated — inspect `_resolve_core_paths` and add/update the
   corresponding branch.
3. Add a unit test using `tmp_path` synthetic file trees that covers the new mapping.
4. Update this ADR with the version → path mapping discovered.

## Consequences

**Positive:**
- v8/v9 and v19+ re-index now produce non-empty `CoreSymbol`. Tools
  `lookup_core_api`, `api_version_diff`, and `find_deprecated_usage` are functional
  for these versions after running `index-core`.
- v19 ORM surface (all field types, all `api.*` decorators, `Environment`, model
  classes) is fully indexed — no silent gaps.
- `ValidationError`, `AccessError`, `MissingError`, `RedirectWarning` correctly carry
  `kind=exception` in query results.
- All file-path resolution logic lives in one place (`_resolve_core_paths`) — future
  Odoo source reorganisations require a one-function change, not a scattered search.

**Negative:**
- Existing v8/v9/v19 indexes (if any) are stale — a reindex is required to backfill
  (run `index-core` per version). No automatic migration.
- `parse_odoo_core` now iterates up to ~13 real files per version (vs 8 before) for
  v19+. This is still O(constant) and typically <2 s/version.

**Risk:**
- **Odoo v20+ may rename or split additional paths.** The `_resolve_core_paths`
  function will silently return `[]` for unmapped new layouts — count drop at step 2 of
  the release checklist is the detection mechanism.
- **`odoo/orm/` glob pattern `fields*.py`** may over-match if Odoo adds a
  `fields_compat_shim.py` or similar file that is not part of the canonical API surface.
  Mitigation: glob is restricted to `fields*.py` within `odoo/orm/` — broad enough for
  current split, narrow enough to exclude unrelated files.

## Alternatives Considered

1. **Hardcode version-range conditions inline in `parse_odoo_core`** — scatter path
   logic across the call site. Reject: harder to test and extend per version.

2. **Separate `_CORE_FILES_V8`, `_CORE_FILES_V19` lists** — duplication of the 5
   unchanged entries; future additions must be applied to every list. Reject: single
   `_CORE_FILES` allow-list + resolution function is DRY.

3. **Skip v8/v9 entirely** — project targets v8 → v19+ per ADR-0002 and
   `CONTRIBUTING.md`. Reject: explicit design commitment.

4. **Raise an error on path miss instead of returning `[]`** — breaks `index-core` on
   any version where an allow-list path legitimately does not exist yet (e.g.
   `odoo/tools/query.py` predates v16). Silent skip with count-diff detection at
   release time is the better trade-off.

## References

- ADR-0002 §6 — CoreSymbol scope: 8-file allow-list, pending curate for lint/CLI v8–v16.
- `src/indexer/parser_odoo_core.py` — `_version_prefix` and `_resolve_core_paths`.
- Upstream Odoo: `openerp/models.py` (v8/v9), `odoo/fields.py` (v10–v18),
  `odoo/orm/fields_*.py` (v19+).
- CLAUDE.md "Neo4j 5.x Gotchas" and "v8/v9 Enablement" — adjacent version-aware
  guidance used throughout the codebase.
