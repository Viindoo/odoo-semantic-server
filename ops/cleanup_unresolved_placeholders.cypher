// Cleanup __unresolved__ placeholder nodes that accumulated in production Neo4j.
//
// PROVENANCE: The writer creates placeholder nodes (Model, View, QWebTmpl, OWLComp)
// when a referenced parent has not been indexed yet.  A 2026-05-26 zero-trust audit
// found 2,068 such nodes on prod (Model 260 / View 629 / OWLComp 806 / QWebTmpl 373)
// plus 5,404 {unresolved:true} edges, and 54 "shadow" View pairs where the old
// 3-property MERGE key (xmlid + module='__unresolved__' + odoo_version) diverged
// from the real View key (xmlid + odoo_version) — leaving two View nodes for the
// same xmlid+version.
//
// WHY SAFE: server.py already filters ALL these nodes at read time
// (module <> '__unresolved__' / coalesce(unresolved,false) = false) across 30+
// Cypher sites, so deleting them changes nothing visible to MCP clients or the Web UI.
// DETACH DELETE removes both nodes and their incident {unresolved:true} edges.
//
// WHEN: Run ONCE against prod after deploying the writer fix in this PR.
// The writer fix prevents new shadows going forward; --gc will clean up future
// accumulation automatically.  This one-time ops script clears the existing backlog.
//
// IDEMPOTENT: safe to run multiple times; returns 0 on a clean graph.
//
// Run against the prod container (NEO4J_PASSWORD from NEO4J_AUTH in the compose .env):
//   docker compose exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
//     -f /dev/stdin < ops/cleanup_unresolved_placeholders.cypher
//
// Expected result on first run (approximate, from audit 2026-05-26):
//   model_deleted   ≈ 260
//   view_deleted    ≈ 629
//   qweb_deleted    ≈ 373
//   owlcomp_deleted ≈ 806
// All subsequent runs should return 0.

// --- Diagnose (run individually to verify counts before cleanup) ---------------
// MATCH (n:Model)    WHERE n.module='__unresolved__' AND coalesce(n.unresolved,false) RETURN count(n) AS model_placeholders;
// MATCH (n:View)     WHERE n.module='__unresolved__' AND coalesce(n.unresolved,false) RETURN count(n) AS view_placeholders;
// MATCH (n:QWebTmpl) WHERE n.module='__unresolved__' AND coalesce(n.unresolved,false) RETURN count(n) AS qweb_placeholders;
// MATCH (n:OWLComp)  WHERE n.module='__unresolved__' AND coalesce(n.unresolved,false) RETURN count(n) AS owlcomp_placeholders;

// --- Cleanup ------------------------------------------------------------------

MATCH (n:Model)
WHERE n.module = '__unresolved__'
  AND coalesce(n.unresolved, false) = true
DETACH DELETE n
RETURN count(n) AS model_deleted;

MATCH (n:View)
WHERE n.module = '__unresolved__'
  AND coalesce(n.unresolved, false) = true
DETACH DELETE n
RETURN count(n) AS view_deleted;

MATCH (n:QWebTmpl)
WHERE n.module = '__unresolved__'
  AND coalesce(n.unresolved, false) = true
DETACH DELETE n
RETURN count(n) AS qweb_deleted;

MATCH (n:OWLComp)
WHERE n.module = '__unresolved__'
  AND coalesce(n.unresolved, false) = true
DETACH DELETE n
RETURN count(n) AS owlcomp_deleted;

// --- Verify shadow Views (must be 0 after cleanup) ----------------------------
// MATCH (a:View), (b:View)
// WHERE a.xmlid = b.xmlid AND a.odoo_version = b.odoo_version AND id(a) <> id(b)
// RETURN count(*) AS shadow_pairs;
