// One-time cleanup of stale WRONG CoreSymbol nodes left by #364 tools_symbols
// *corrections* (issue #364 follow-up).
//
// WHY: CoreSymbol is deliberately PRUNE-EXEMPT (ADR-0055 "Follow-up" section).
// Its cross-version lifecycle — added_in / removed_in / deprecated_in properties
// plus REPLACED_BY edges — is exactly what api_version_diff, find_deprecated_usage
// and lookup_core_api consume, so a standing "delete stale-version CoreSymbol"
// prune (like the LintRule/CLICommand/CLIFlag prune added for #364 F2) would
// DESTROY that history. There is therefore NO prune_core_symbols writer method,
// and a regression test (test_no_prune_core_symbols_method_exists) guards that
// there never is one. Do NOT turn this file into a standing prune.
//
// But #364 did not only add lifecycle events — it CORRECTED two flat
// re-export names that a PRIOR index-core had already written to Neo4j under a
// WRONG (qualified_name, odoo_version). Because index-core's CoreSymbol write is
// MERGE-only and never deletes, and because these names were CURATED-only (never
// parsed from source, so a re-index cannot overwrite them in place either), the
// wrong nodes survive a re-index forever. This file removes exactly those wrong
// nodes, once. It is NOT a lifecycle removal (nothing was "removed in" a later
// version) - it is deleting rows that never should have existed.
//
// The two confirmed corrections, verified against
// `git diff 959a879..ece0cc7 -- src/indexer/spec_data/tools_symbols_*.json`
// (the ONLY qualified_name lines removed in that diff):
//
//   1. odoo.tools.pycompat @ {8.0, 9.0, 10.0}
//      odoo/tools/pycompat.py (and openerp/tools/pycompat.py) does NOT exist
//      until v11 — the module was never importable pre-v11, so the flat curated
//      entry at v8-v10 was wrong. pycompat @ v11-v18 is CORRECT and is NOT
//      touched here. (v19 dropped it from odoo.tools.__init__ and the curated
//      set legitimately omits it there — that is a lifecycle removal, handled by
//      re-index + omission, not by this cleanup.)
//
//   2. odoo.tools.image_process @ 19.0
//      The flat re-export `from .image import image_process` was dropped from
//      odoo/tools/__init__.py at v19, so `odoo.tools.image_process` no longer
//      resolves at v19 - the real path is `odoo.tools.image.image_process`
//      (now curated at v19, written fresh by re-index — no cleanup needed for
//      the correct node). The flat name @ v13-v18 is CORRECT (still re-exported
//      there) and is NOT touched here.
//
// qualified_name is stored verbatim from the curated JSON (parser_tools_symbols
// .load_tools_symbols -> CoreSymbolInfo.qualified_name -> _write_core_symbols_batch
// `MERGE (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})`), so the exact
// literals below match the stored keys.
//
// WHEN: Run ONCE, during the deploy's re-index phase, AFTER a full index-core has
// re-run for the affected versions with the corrected spec_data (so the correct
// nodes — pycompat@v11+, odoo.tools.image.image_process@19.0 — already exist).
// Running it is order-independent w.r.t. the correct nodes: it only ever deletes
// the four wrong (name, version) pairs enumerated below.
//
// SAFETY: idempotent + minimal + version-precise. Each statement targets exactly
// one (qualified_name, odoo_version) pair - on a graph that has already been
// cleaned (or never had the wrong nodes) every MATCH finds nothing and DETACH
// DELETE is a no-op. DETACH DELETE also removes any bogus REPLACED_BY / lifecycle
// edges incident to a wrong node. Diagnose first with the counts below.

// --- Diagnose (run first; each should report the stale node if still present) --
// MATCH (cs:CoreSymbol {qualified_name: 'odoo.tools.pycompat',      odoo_version: '8.0'})  RETURN count(cs) AS pycompat_8;
// MATCH (cs:CoreSymbol {qualified_name: 'odoo.tools.pycompat',      odoo_version: '9.0'})  RETURN count(cs) AS pycompat_9;
// MATCH (cs:CoreSymbol {qualified_name: 'odoo.tools.pycompat',      odoo_version: '10.0'}) RETURN count(cs) AS pycompat_10;
// MATCH (cs:CoreSymbol {qualified_name: 'odoo.tools.image_process', odoo_version: '19.0'}) RETURN count(cs) AS image_process_19;

// --- Cleanup (exactly the four confirmed (qualified_name, odoo_version) pairs) --
MATCH (cs:CoreSymbol {qualified_name: 'odoo.tools.pycompat', odoo_version: '8.0'})
DETACH DELETE cs;

MATCH (cs:CoreSymbol {qualified_name: 'odoo.tools.pycompat', odoo_version: '9.0'})
DETACH DELETE cs;

MATCH (cs:CoreSymbol {qualified_name: 'odoo.tools.pycompat', odoo_version: '10.0'})
DETACH DELETE cs;

MATCH (cs:CoreSymbol {qualified_name: 'odoo.tools.image_process', odoo_version: '19.0'})
DETACH DELETE cs;
