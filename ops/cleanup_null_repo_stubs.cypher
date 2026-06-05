// cleanup_null_repo_stubs.cypher
//
// PURPOSE (PR #267 FU-1)
// ----------------------
// Remove residual contentless dep-stub :Module nodes left by the PRE-#267
// dependency-MERGE that stamped profiles on dep targets. A dep target MERGE
// (writer_neo4j.py `MERGE (d:Module {name, odoo_version})`) creates a bare node
// with repo_id=NULL, repo=NULL, path=NULL and NO :DEFINED_IN children. Two such
// nodes (viin_web_gantt@15.0, web_responsive@15.0) were additionally polluted
// with a 2-element profile[] across two indexer runs; two more
// (certificate@18.0, certificate@19.0) are single-profile but equally empty.
//
// WHY --gc DOES NOT FIX THESE
// ----------------------------
// gc_stale_modules (writer_neo4j.py) matches
//   `MATCH (m:Module {repo: $repo, odoo_version: $version})`
// using a concrete non-NULL repo value (the repo root name string, e.g.
// "viindoo_15.0"). Dep-stubs have repo=NULL, so the GC query NEVER matches them
// — they are invisible to the GC filter entirely.
//
// Additionally, the post-#267 dep-MERGE no longer stamps profiles on dep targets
// (ON CREATE leaves profile absent). However, ON MATCH still touches NOTHING on
// the dep-target node — an existing stub's polluted profile[] is left UNCHANGED.
// So re-indexing alone cannot heal already-existing stubs: GC misses them, and
// the dep-MERGE does not clear them.
//
// WHY DELETE IS SAFE
// ------------------
// These nodes carry zero content: no :DEFINED_IN children (no fields, methods,
// models, or views), no real file path, no module metadata. After this cleanup,
// the next indexer run will re-MERGE each as a FRESH profile-less stub (correct
// post-#267 ON CREATE behaviour: profile absent → fail-closed by the ADR-0034
// size>0 choke for scoped tenants, visible only to admin). DEPENDS_ON edges
// from the depender modules are recreated by that same re-MERGE. Surgical: only
// nodes with repo_id IS NULL AND zero :DEFINED_IN children are touched.
//
// SCOPE / SAFETY
// --------------
//   - DETACH DELETE only :Module nodes with repo_id IS NULL AND 0 DEFINED_IN
//     children. A stub that (impossibly) gained a real child is left untouched.
//   - Idempotent: a second run finds 0 matches (the dependers re-MERGE happens
//     only on the next INDEX run, not here).
//   - Run AFTER any profile[] correction (correct_node_owning_profile.cypher),
//     not before — though they touch disjoint node sets (that script skips
//     repo_id-less nodes in Phase 1; see its SCOPE/SAFETY note re: nodes with
//     no repo_id being left untouched by Phase 1).
//   - Run when no indexer job is active (check indexer_jobs.status != 'running')
//     to avoid a mid-flight MERGE landing on a just-deleted stub.
//
// ROLLBACK: none needed. Delete-only of contentless nodes; re-MERGE on the next
// indexer run recreates them correctly as profile-less stubs. If a node was
// deleted in error, the next indexer run recreates it.
//
// WHEN: Run AFTER the PR #267 writer fix is deployed, to heal the pre-existing
// stubs that the fix alone cannot clear. This is NOT necessarily one-shot: any
// indexer run that encounters a dependency never indexed under its own profile
// re-creates a fresh (profile-less, harmless, admin-only) stub via the
// dep-target MERGE. Such stubs are benign — fail-closed for scoped tenants —
// but accumulate as graph cruft, so re-run this script as periodic hygiene if
// the Phase A count grows.
//
// FOLLOW-UP: the durable GC function `gc_null_repo_dep_stubs` now exists in
// Neo4jWriter (src/indexer/writer_neo4j.py) and is wired into index_all()
// under the --gc flag (FUFU-1, PR #268 follow-up wave). It runs automatically
// once per unique odoo_version after ALL profiles complete, collecting
// childless repo_id-NULL stubs that accumulate between full runs.
// This script is now an EXPEDITED ONE-TIME HEAL tool for existing prod stubs —
// run it once (when no indexer job is active) to clear the current cruft
// without waiting for the next scheduled --gc pass. It is no longer the only
// cleanup mechanism.

// ---------------------------------------------------------------------------
// PHASE A — ADVISORY (read-only): list all :Module nodes that WILL be deleted.
// Review this output before proceeding to Phase B.
// Expected on the targeted prod snapshot: exactly 4 nodes —
//   viin_web_gantt@15.0, web_responsive@15.0, certificate@18.0, certificate@19.0.
// If the output contains unexpected names or more than ~4 nodes, do NOT run
// Phase B until the discrepancy is understood.
// ---------------------------------------------------------------------------
MATCH (m:Module)
WHERE m.repo_id IS NULL
OPTIONAL MATCH (m)<-[:DEFINED_IN]-(c)
WITH m, count(c) AS child_count
WHERE child_count = 0
RETURN m.name AS module, m.odoo_version AS version,
       m.profile AS profile, m.repo_id AS repo_id
ORDER BY module, version;

// ---------------------------------------------------------------------------
// PHASE B — DELETE. Run only after Phase A output has been reviewed and
// confirmed to match the expected set (or is otherwise understood).
// ---------------------------------------------------------------------------
MATCH (m:Module)
WHERE m.repo_id IS NULL
OPTIONAL MATCH (m)<-[:DEFINED_IN]-(c)
WITH m, count(c) AS child_count
WHERE child_count = 0
DETACH DELETE m;

// ---------------------------------------------------------------------------
// PHASE C — VERIFY (read-only): count remaining repo_id IS NULL + 0-children
// :Module nodes. Expected: 0.
// A nonzero result means a stub was added between Phase B and Phase C (a
// concurrent indexer run re-MERGEd one) — rerun Phase B to catch it, or
// accept it as a freshly-created profile-less stub (safe post-#267).
// ---------------------------------------------------------------------------
MATCH (m:Module)
WHERE m.repo_id IS NULL
OPTIONAL MATCH (m)<-[:DEFINED_IN]-(c)
WITH m, count(c) AS child_count
WHERE child_count = 0
RETURN count(m) AS residual_null_repo_stubs;
