// backfill_module_profile.cypher
//
// PURPOSE
// -------
// Repair :Module nodes whose `profile` array is empty/NULL due to the ADR-0016 D5
// violation fixed in WI-1 (#259). Those nodes were created by bare MERGE statements
// in the model/view/QWebTmpl/OWLComp/JSPatch writer loops that never ran
// `SET mod.profile`, leaving them invisible to every profile-scoped MCP query.
//
// MECHANISM
// ---------
// For every :Module with size(coalesce(mod.profile,[]))=0, collect the union of
// `profile` values from all child nodes that link to it via a DEFINED_IN edge.
// Child types: :Model, :View, :QWebTmpl, :OWLComp, :JSPatch, :Stylesheet.
// Write the union back with SET mod.profile = <union>.
//
// ORDERING
// --------
// 1. Deploy WI-1 writer fix so future writes are correct.
// 2. Run this backfill (repairs existing snapshot, no source re-parse, no downtime).
// 3. Run the VERIFY query below; expect residual = 0 for all indexed modules.
// 4. Schedule `--full` reindex per version off-peak (v17 first, then v18/v19):
//    - authoritative remedy prescribed by ADR-0016 "Negative consequences"
//    - also stamps profile on data-only/i18n modules (0 DEFINED_IN children)
//      which this backfill CANNOT repair (see CAVEAT below).
//
// CAVEAT: data-only modules (modules that index zero Model/View/JS/Stylesheet
// nodes) have no DEFINED_IN children, so their :Module node will STILL have
// profile=[] after this backfill. These require the off-peak --full reindex.
// This is expected behavior and does not affect the majority of modules.
//
// SAFETY: idempotent. Re-running when profile is already populated is a no-op
// (the WHERE size(coalesce(mod.profile,[]))=0 guard skips already-stamped nodes).
// No node is deleted; only the `profile` property is SET.
//
// RELATIONSHIP NAMES (confirmed from src/indexer/writer_neo4j.py + src/constants.py):
//   DEFINED_IN   - used by Model, View, QWebTmpl, OWLComp, JSPatch, Stylesheet -> Module
//
// Run with: cat ops/backfill_module_profile.cypher | cypher-shell -u neo4j -p <pw>
// or paste into Neo4j Browser. STEP 1 mutates; STEP 2 + STEP 3 are read-only
// verification queries (safe to run after STEP 1). Each statement is terminated
// by ';' so they execute as a sequence.

// ---------------------------------------------------------------------------
// STEP 1: BACKFILL
// For each :Module with an empty/NULL profile, collect the union of all child
// profiles via DEFINED_IN edges and set mod.profile to that union.
// ---------------------------------------------------------------------------
MATCH (mod:Module)
WHERE size(coalesce(mod.profile, [])) = 0
OPTIONAL MATCH (child)-[:DEFINED_IN]->(mod)
WHERE child.profile IS NOT NULL AND size(child.profile) > 0
WITH mod, collect(child.profile) AS all_profiles_lists
WITH mod,
     [p IN reduce(acc = [], lst IN all_profiles_lists | acc + lst)
      WHERE p IS NOT NULL | p] AS flat
WITH mod,
     reduce(acc = [], p IN flat | CASE WHEN p IN acc THEN acc ELSE acc + [p] END)
     AS union_profile
WHERE size(union_profile) > 0
SET mod.profile = union_profile
RETURN count(mod) AS modules_backfilled;

// ---------------------------------------------------------------------------
// STEP 2: VERIFY (read-only)
// After backfill, count residual :Module nodes still at profile=[].
// Expected: only data-only/i18n modules (no DEFINED_IN children) remain.
// Those need the off-peak --full reindex.
// ---------------------------------------------------------------------------
MATCH (mod:Module)
WHERE size(coalesce(mod.profile, [])) = 0
RETURN count(mod) AS residual_modules_no_profile;

// ---------------------------------------------------------------------------
// STEP 3: DRILL-DOWN (read-only, diagnostic)
// List any residual modules that DO have DEFINED_IN children — these signal a
// NEW D5 violation (the backfill should have stamped them). A zero result
// confirms the backfill is complete; any row needs investigation.
// ---------------------------------------------------------------------------
MATCH (mod:Module)
WHERE size(coalesce(mod.profile, [])) = 0
OPTIONAL MATCH (child)-[:DEFINED_IN]->(mod)
  WHERE child.profile IS NOT NULL AND size(child.profile) > 0
WITH mod, count(child) AS child_count
WHERE child_count > 0
RETURN mod.name AS name, mod.odoo_version AS odoo_version, child_count
ORDER BY mod.odoo_version, mod.name
LIMIT 50;
