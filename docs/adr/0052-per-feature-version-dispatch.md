# ADR-0052: Per-feature version-dispatch convention (each feature group owns its VersionRegistry)

**Status:** Accepted (reference implementation: the asset-bundle parser, this wave)

**Date:** 2026-06-27

**Author:** Viindoo Engineering (indexer zero-warning wave — WI-D)

**Relates to:** ADR-0002 (spec schema per-version), ADR-0005 (core coverage version paths), ADR-0032 (parser version-dispatch registry — `VersionRegistry`), ADR-0025 (stylesheet indexing)

---

## Context

OSM parses Odoo source spanning v8-v19. Behaviour differs by Odoo major in many,
INDEPENDENT places: the manifest filename (`__openerp__.py` v8-9 vs `__manifest__.py`
v11+), the core filesystem prefix (`openerp/` vs `odoo/`), OWL detection (v14+),
RelaxNG view validation (v15+), the stylesheet language (LESS v9-11 vs SCSS v12+),
and — new in this wave — the asset-bundle declaration form (XML `<template>` bundles
v8-14 vs `__manifest__.py` `'assets'` dict v15+).

ADR-0032 introduced `VersionRegistry[T]` (`src/indexer/version_registry.py`): a sorted
`(min_major, max_major | None, handler)` list, first-match-wins, where adding v20 is a
one-line append. It already hosts FOUR independent dispatch singletons:

| Registry | File | Boundary | Selects |
|---|---|---|---|
| `_PREFIX_REGISTRY` | `parser_odoo_core.py` | (8-9) / (10+) | core filesystem prefix |
| `_OWL_ENABLED_REGISTRY` | `parser_js.py` | (14+) | OWL/`patch()` detection |
| `_RELAXNG_GATE` | `parser_xml.py` | (15+) | view RelaxNG validation |
| `_PKG_PREFIX_REGISTRY` | `parser_cli.py` | (8-9) / (10+) | CLI package prefix |

WI-G (the stylesheet wave, same release) added two more to
`version_registry.py` itself — `STYLESHEET_LESS_REGISTRY` (9-11) and
`STYLESHEET_SCSS_REGISTRY` (12+) — each with its OWN boundary, exposed via the
`less_active()` / `scss_active()` helpers.

The recurring pattern across all six is clear, but it had never been written down as
a CONVENTION. Two anti-patterns it must rule out:

1. **A shared global "era" enum.** Tempting to define one `Era = {ERA1: v8-9, ERA2: v10+}`
   and branch every feature on it. WRONG: the boundaries genuinely differ per feature
   (OWL flips at v14, RelaxNG at v15, LESS->SCSS at v12, assets at v15, core prefix at v10).
   A shared era would force every feature to a single boundary and break the others.
2. **Scattering `if major >= N` branches** through the parser body — the exact thing
   ADR-0032 replaced for the Python/JS/core/CLI parsers.

---

## Decision

**Each feature group declares its OWN `VersionRegistry` instance with ITS OWN version
boundaries, fans out to per-era handlers, and is exposed via ONE aggregate dispatcher
function (a single call point).** There is NO shared global "era" — the era is a
property of a feature, not of the codebase.

Concretely, a feature group MUST:

1. **Own its registry.** Declare a module-level `VersionRegistry[T]` singleton whose
   entries encode that feature's real boundaries (e.g. assets: era-A handler v8-14,
   era-B handler v15-19). The handler type `T` is the feature's choice — a callable,
   a bool gate, a string prefix.
2. **Fan out to per-era handlers.** Each era's logic lives in its own function. The
   registry maps `major -> handler`; the handlers never inspect the version again.
3. **Expose ONE aggregate dispatcher.** A single public entry — e.g.
   `parse_assets(module_path, odoo_version, manifest, ...)` — does the
   `registry.resolve_version(...)` lookup and invokes the resolved handler. Callers
   (the pipeline) call ONLY this aggregate; they never see the registry or the eras.
   First-match-wins + `max_major=None` (open-ended) means v20 is a one-line append to
   the registry with no caller change.

**Precedents this generalizes:** the four ADR-0032 singletons + the two WI-G
stylesheet registries above. The asset-bundle parser (`src/indexer/parser_assets.py`,
this wave) is the REFERENCE IMPLEMENTATION of the full shape — own registry + per-era
handlers + single `parse_assets()` call point — because it is the first feature whose
two eras require genuinely DIFFERENT parsing strategies (XML tree walk vs Python-literal
manifest dict), not just an on/off gate.

### Migration of existing dispatch sites (FOLLOW-UPS, not this wave)

Two existing dispatch sites do NOT yet follow the full convention and are flagged for
later migration (out of scope for WI-D — no refactor here):

- **Python parser era1/era2** (`parser_python.py`): dispatch is an IMPLICIT
  try-AST-then-text-regex-on-`SyntaxError` fallback, not a `VersionRegistry` lookup.
  Functionally correct, but it is not declared as a registry + aggregate.
- **CLI v19 namespace** (`parser_cli.py`): has `_PKG_PREFIX_REGISTRY` but additional
  v19-specific namespace handling is branched inline rather than fanned out through a
  single aggregate dispatcher.

Migrating these is mechanical and behaviour-preserving; do it when those files are next
touched, citing this ADR.

---

## Consequences

**Positive:** Each feature's version boundary is declared in exactly one place (SSOT),
co-located with its handlers; adding a new Odoo major is a one-line append per affected
feature; no cross-feature coupling (changing the assets boundary cannot affect RelaxNG);
the single call point keeps the pipeline ignorant of era internals.

**Negative / trade-off:** N small registries instead of one big enum — but that IS the
point (the boundaries differ), and `version_registry.py` already documents each with a
boundary rationale comment, so the registries are self-describing.

**Testing:** every feature registry gets a dispatch test asserting the boundary
(e.g. v14 -> era-A, v15 -> era-B for assets) so a future boundary edit fails loudly.
