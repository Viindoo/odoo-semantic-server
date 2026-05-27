// Heal stale unresolved=true flags on already-resolved View/QWebTmpl nodes and
// their incident edges.
//
// PROVENANCE: After the placeholder-key convergence fix (PR #194 / commit e6d2d68),
// View and QWebTmpl MERGE keys became {xmlid, odoo_version} — the same for
// placeholders and real writes.  A real write now lands on the same node as its
// former placeholder and clears unresolved=false going forward.
//
// HOWEVER, nodes that were resolved BEFORE the fix accumulated a different residual:
// when a real write landed on the old 3-key placeholder node
// (xmlid + module='__unresolved__' + odoo_version), the old real-write SET block
// updated module=<real> but never cleared unresolved=true.  The subsequent
// ops/cleanup_unresolved_placeholders.cypher only deleted nodes where
// module='__unresolved__', leaving these behind.
//
// LIVE PROD COUNTS (2026-05-27 audit):
//   View  nodes  (module<>'__unresolved__', unresolved=true):  63
//   QWebTmpl nodes (module<>'__unresolved__', unresolved=true): 90
//   INHERITS_VIEW edges {unresolved:true} to real targets:      63
//   EXTENDS_TMPL  edges {unresolved:true} to real targets:     263
//   Confirmed real (sample): website.layout (10.0),
//     website_sale.snippet_options (14.0), mass_mailing.snippet_options (15.0)
//
// WHY SAFE: a node with module<>'__unresolved__' was written by a real indexer
// pass — its module property was set to the real module name.  Its unresolved=true
// is a stale artefact from the old placeholder path; clearing it restores the
// correct visible state.  An edge whose target node has module<>'__unresolved__'
// is a resolved relationship; its unresolved=true is likewise stale.
// This script only SETs flag properties; it does NOT delete any nodes or edges.
//
// IDEMPOTENT: safe to run multiple times; returns 0 after the first successful run.
//
// Run against the prod container (NEO4J_PASSWORD from NEO4J_AUTH in the compose .env):
//   docker compose exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
//     -f /dev/stdin < ops/cleanup_resolved_unresolved_flags.cypher
//
// Expected result on first run (approximate, from audit 2026-05-27):
//   nodes_healed ≈ 153  (View 63 + QWebTmpl 90)
//   edges_healed ≈ 326  (INHERITS_VIEW 63 + EXTENDS_TMPL 263)
// All subsequent runs should return 0.

// --- Diagnose (run individually to verify counts before cleanup) ---------------
// MATCH (n:View)     WHERE coalesce(n.unresolved,false)=true AND coalesce(n.module,'')<>'__unresolved__' RETURN count(n) AS view_stale;
// MATCH (n:QWebTmpl) WHERE coalesce(n.unresolved,false)=true AND coalesce(n.module,'')<>'__unresolved__' RETURN count(n) AS qweb_stale;
// MATCH ()-[r]->(t)  WHERE r.unresolved=true AND coalesce(t.module,'')<>'__unresolved__' RETURN count(r) AS edge_stale;

// --- Heal real View/QWebTmpl nodes -------------------------------------------

MATCH (n)
WHERE (n:View OR n:QWebTmpl)
  AND coalesce(n.unresolved, false) = true
  AND coalesce(n.module, '') <> '__unresolved__'
SET n.unresolved = false
RETURN count(n) AS nodes_healed;

// --- Heal edges pointing to real (already-resolved) target nodes --------------

MATCH ()-[r]->(t)
WHERE r.unresolved = true
  AND coalesce(t.module, '') <> '__unresolved__'
SET r.unresolved = false
RETURN count(r) AS edges_healed;
