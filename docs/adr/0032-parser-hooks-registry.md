# ADR-0032 — Parser Version-Dispatch Registry

**Status:** Accepted  
**Date:** 2026-05-22  
**Milestone:** M11 RP WI-6

---

## Context

Before this ADR, three indexer parsers contained hard-coded `if major <= 9` or
`if major_version < 14` branching to select version-specific behaviour:

| Parser | Branch point | Constant used |
|--------|-------------|---------------|
| `parser_python.py` `_detect_era()` | `major <= LEGACY_ERA_MAX_MAJOR` → `"era1"` vs `"era2"` | `LEGACY_ERA_MAX_MAJOR` |
| `parser_odoo_core.py` `_version_prefix()` | `major <= ODOO_NAMESPACE_LEGACY_MAX_MAJOR` → `"openerp/"` vs `"odoo/"` | `ODOO_NAMESPACE_LEGACY_MAX_MAJOR` |
| `parser_js.py` `_extract_era3_patches()`, `_extract_era3_components()` | `major_version < 14` → skip OWL extraction | hard-coded `14` |

Adding a future Odoo v20 (or any version requiring new behaviour) required:
1. Finding every `if major` branch in each parser.
2. Adding a new `elif` and possibly updating constants.
3. Risk of missing a branch across multiple files.

The CLAUDE.md "Boil the Lake" principle demands that adding v20 be a 1-line
operation, not a multi-file hunt.

---

## Decision

Introduce **`src/indexer/version_registry.py`** — a minimal shared abstraction:

```python
class VersionRegistry[T]:
    """Sorted (min_major, max_major|None, handler) registry. First match wins."""

    def __init__(self, entries: list[tuple[int, int | None, T]]) -> None: ...
    def resolve(self, major: int, default: T | None = None) -> T | None: ...
    def resolve_version(self, odoo_version: str, default: T | None = None) -> T | None: ...
```

**Contract:**

- `entries` = list of `(min_major, max_major | None, handler)`.
- Entries are stored sorted ascending by `min_major` regardless of insertion order.
- `resolve(major)` iterates in ascending `min_major` order; the **first** entry
  where `min_major <= major <= max_major` (or `max_major is None`) is returned.
  **No fall-through** — iteration stops at the first match.
- `max_major = None` means "open-ended: this version and all newer ones".
- If no entry matches, `default` (caller-supplied, defaults to `None`) is returned.
- `resolve_version(odoo_version_str)` parses the leading integer from the dot-
  separated version string before delegating to `resolve`. Unparseable strings
  return `default` without raising.

**Three registries wired in the parsers:**

```python
# parser_python.py
_ERA_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8,  LEGACY_ERA_MAX_MAJOR, "era1"),   # v8–v9
    (10, None,                 "era2"),   # v10+, open-ended
])

# parser_odoo_core.py
_PREFIX_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8,  ODOO_NAMESPACE_LEGACY_MAX_MAJOR, "openerp/"),  # v8–v9
    (10, None,                            "odoo/"),      # v10+, open-ended
])

# parser_js.py
_OWL_ENABLED_REGISTRY: VersionRegistry[bool] = VersionRegistry([
    (14, None, True),   # v14+: OWL patch() and component class extraction enabled
])
```

**The content-based `_detect_era(source: str)` in `parser_js.py` is NOT changed.**
It is content-driven (string-matching `@odoo-module` / `odoo.define` / `import {`),
not version-driven, and must remain so.

---

## v20 — Adding a new version is localised to the registry list

To enable a hypothetical new Python AST strategy for Odoo v20, all changes are
confined to one registry list — no new `if` branches in parser logic, no grep
across multiple files:

```python
# Before (current):
_ERA_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8,  LEGACY_ERA_MAX_MAJOR, "era1"),
    (10, None,                 "era2"),      # open-ended: covers v10 and above
])

# After (v20 support added — 2 localised edits inside one list):
_ERA_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8,  LEGACY_ERA_MAX_MAJOR, "era1"),
    (10, 19,                   "era2"),      # cap: was open-ended, now bounded at v19
    (20, None,                 "era3"),      # new: v20 and above
])
```

When the new handler is identical (e.g. OWL remains enabled for v20), the
open-ended entry already covers it — zero changes required:

```python
# OWL registry — v20 automatically covered:
_OWL_ENABLED_REGISTRY: VersionRegistry[bool] = VersionRegistry([
    (14, None, True),   # v14+, including any future v20
])
```

No structural change to `VersionRegistry` itself. No new `if` branch anywhere
in parser logic.

---

## Relationship to ADR-0005

ADR-0005 (`core-coverage-version-paths`) documented the version-aware path
resolution in `parser_odoo_core.py` — specifically the `openerp/` → `odoo/`
prefix substitution and the v19+ `odoo/orm/` split handling.

This ADR **supersedes the prefix-selection portion** of ADR-0005: `_version_prefix()`
now delegates to `_PREFIX_REGISTRY` instead of an inline `if` expression. The v19
orm-split resolution (`_resolve_core_paths` fallback for `odoo/fields.py`,
`odoo/models.py`, `odoo/api.py`) is unchanged — it is a path-existence check, not
version-branching, and does not benefit from the registry.

ADR-0005 is kept as historical record and MUST NOT be deleted.

---

## Consequences

**Positive:**
- Adding Odoo v20 (or any future version) parser behaviour is a 1-line registry
  append in the relevant parser. No new `if` branches.
- The boundary constants (`LEGACY_ERA_MAX_MAJOR`, `ODOO_NAMESPACE_LEGACY_MAX_MAJOR`)
  remain in `src/constants.py` as the single source of truth; registry entries
  reference them, so the constants are not duplicated.
- `VersionRegistry` is generic (`Generic[T]`) and works for str, bool, or any
  callable handler — extensible without modification.
- Fully behavior-preserving: existing era1/era2/era3 fixture tests and all parser
  integration tests pass unchanged.

**Negative / Tradeoffs:**
- One additional import (`from .version_registry import VersionRegistry`) in each
  of the three parsers.
- The abstraction only makes sense for VERSION-based dispatch. Content-based dispatch
  (JS `_detect_era`) intentionally remains a plain function.

**Robustness note (PR #160 review):** `_OWL_ENABLED_REGISTRY.resolve_version()` returns `default=None` on an unparseable or `"unknown"` version string, whereas the previous inline `int()` call would have raised `ValueError`. Callers treat `None` as "disabled" (skip OWL extraction), which is the safe default. Net OWL output is identical for all valid version strings; behaviour is strictly more robust for malformed inputs.

---

## Files Changed

| File | Change |
|------|--------|
| `src/indexer/version_registry.py` | **New** — `VersionRegistry` class + `make_version_registry` |
| `src/indexer/parser_python.py` | `_ERA_REGISTRY` module constant; `_detect_era` delegates to it |
| `src/indexer/parser_odoo_core.py` | `_PREFIX_REGISTRY` module constant; `_version_prefix` delegates to it |
| `src/indexer/parser_js.py` | `_OWL_ENABLED_REGISTRY` constant; two `if major_version < 14` guards replaced |
| `src/indexer/parser_cli.py` | `_PKG_PREFIX_REGISTRY` module constant; `_pkg_prefix` delegates to it (PR#160 follow-up) |
| `tests/test_version_registry.py` | **New** — unit tests for registry semantics, boundaries, v20 append |
| `docs/adr/0032-parser-hooks-registry.md` | **This file** |
