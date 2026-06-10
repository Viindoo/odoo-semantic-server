// Cleanup same-name INHERITS K² mesh edges after writer fix (#273).
//
// WHY: Before the writer fix (ADR topology change, PR #273), the self-extend
// branch (W1 in writer_neo4j.py) emitted an INHERITS edge from every extender
// Model node to EVERY other same-name Model node of the same version — the K²
// "mesh" topology.  On Odoo 17.0 this produced ~256 000 same-name INHERITS edges
// per version (~1.1 M total across all indexed versions), causing the ORM tools
// to enumerate 20-86 M paths and hang (zombie transactions lasting 19-24 h on prod).
//
// The writer now emits K×D edges: each extender targets only definition nodes
// (coalesce(is_definition, false) = true).  D is typically 1 (one canonical
// definition per model per version).  The old mesh edges are NOT removed by a
// reindex (the writer uses additive MERGE — it never deletes edges between live
// nodes).  This script performs the targeted cleanup.
//
// PREREQUISITES (must be done IN ORDER before running this script):
//
//   1. Writer fix deployed:
//        Ensure the new code (PR #273 merged + services restarted) is live.
//        Running cleanup BEFORE the fix means a subsequent reindex would
//        re-create part of the mesh for re-written modules.
//
//   2. Backup bundle (ADR-0018) created IMMEDIATELY before this script:
//        Deleting ~1.1 M edges is NOT reversible by this script alone.
//        The only rollback path is restoring from a backup bundle.
//        Command (adjust paths per your deployment):
//          python -m src.cli backup create --out /var/backups/osm/pre-cleanup-$(date +%Y%m%d).tar.gz
//        Or directly:
//          neo4j-admin database dump --database=neo4j --to-path=/tmp/neo4j-pre-cleanup.dump
//          pg_dump $DATABASE_URL > /tmp/pg-pre-cleanup.sql
//
//   3. db.transaction.timeout interaction:
//        Phase 1 (BACKFILL) and Phase 2 (DELETE) use
//        CALL { ... } IN TRANSACTIONS OF 10000 ROWS, which runs each inner
//        batch as its own auto-commit transaction.  Inner transactions of 10 000
//        rows complete well within 600 s.  HOWEVER, the outer coordinator
//        transaction for CALL IN TRANSACTIONS has been reported to be subject
//        to the global timeout on some Neo4j 5.x versions.
//
//        To be safe, either:
//
//        Option A — temporarily disable the global timeout (re-enable after):
//          CALL dbms.setConfigValue('db.transaction.timeout', '0');
//          -- run the script --
//          CALL dbms.setConfigValue('db.transaction.timeout', '600s');
//
//        Option B — run via the Python driver with a per-transaction timeout
//        set higher than the batch duration (valid on Neo4j 5.3+ including 5.26):
//          session.run(cypher, timeout=7200)  # 2 h per statement
//
//        Option A is simpler for an ad-hoc cypher-shell run.
//
// HOW TO RUN:
//   This script uses CALL { } IN TRANSACTIONS which requires an implicit
//   (auto-commit) transaction — it CANNOT run inside an explicit BEGIN/COMMIT
//   block.  Use cypher-shell with the :auto prefix or run each statement
//   individually:
//
//     cypher-shell -u neo4j -p <pass> --format plain < ops/cleanup_same_name_inherits_mesh.cypher
//
//   Or interactively via Neo4j Browser: paste each CALL block separately
//   (Browser wraps each statement in an explicit tx by default — use :auto mode).
//
// IDEMPOTENT: safe to run more than once.  After a clean run, the DIAGNOSE
// counts show 0 mesh edges and the DELETE / BACKFILL blocks are no-ops.
//
// PER-VERSION VARIANT: to process one version at a time (lower peak memory),
// replace `AND a.odoo_version = a.odoo_version` lines with
// `AND a.odoo_version = '17.0'` (or whichever version you want).
// Run the full script once per version, then run VERIFY for each version.

// ---------------------------------------------------------------------------
// PHASE: DIAGNOSE (run first — observe counts before any changes)
// ---------------------------------------------------------------------------
// Count same-name INHERITS edges that target a NON-definition node
// (these are the mesh edges to be deleted in Phase 2):
//
// MATCH (a:Model)-[r:INHERITS]->(b:Model)
// WHERE a.name = b.name AND a.odoo_version = b.odoo_version
//   AND NOT coalesce(b.is_definition, false)
// RETURN a.odoo_version AS version, count(r) AS mesh_edges_to_delete
// ORDER BY version;
//
// Count extender→definition edges already present (before backfill):
//
// MATCH (a:Model)-[r:INHERITS]->(b:Model)
// WHERE a.name = b.name AND a.odoo_version = b.odoo_version
//   AND coalesce(b.is_definition, false)
//   AND NOT coalesce(a.is_definition, false)
// RETURN a.odoo_version AS version, count(r) AS definition_edges_present
// ORDER BY version;
//
// Count extender nodes that have no edge to any definition (gap to fill):
//
// MATCH (ext:Model)
// WHERE NOT coalesce(ext.is_definition, false)
//   AND ext.module <> '__unresolved__'
// MATCH (def:Model)
// WHERE def.name = ext.name AND def.odoo_version = ext.odoo_version
//   AND coalesce(def.is_definition, false) AND def.module <> ext.module
// WHERE NOT (ext)-[:INHERITS]->(def)
// RETURN ext.odoo_version AS version, count(ext) AS extenders_missing_edge
// ORDER BY version;

// ---------------------------------------------------------------------------
// PHASE 1: BACKFILL — create missing extender-to-definition edges
// ---------------------------------------------------------------------------
// Must run BEFORE Phase 2 (DELETE) to ensure no extender is left without an
// edge to its definition node after the mesh is removed.
//
// Batch size 10 000: each inner transaction stays well under 600 s.
// The BACKFILL is also batched (debate requirement) because iterating over
// hundreds of thousands of extender+definition pairs in a single transaction
// would violate the db.transaction.timeout and risk heap pressure.

CALL {
    MATCH (ext:Model)
    WHERE NOT coalesce(ext.is_definition, false)
      AND ext.module <> '__unresolved__'
    // Collect the minimum order from any existing same-name out-edge on this
    // extender.  This preserves the MRO position recorded at write time.
    // Falls back to 0 when no such edge exists yet (pure cross-repo gap).
    OPTIONAL MATCH (ext)-[existing_r:INHERITS]->(same_name:Model)
    WHERE same_name.name = ext.name
      AND same_name.odoo_version = ext.odoo_version
    WITH ext, min(existing_r.order) AS edge_order
    // Find the definition node(s) for this model name+version.
    MATCH (def:Model)
    WHERE def.name = ext.name
      AND def.odoo_version = ext.odoo_version
      AND coalesce(def.is_definition, false) = true
      AND def.module <> ext.module
    WITH ext, def, edge_order
    // Only act on pairs that are missing the edge (idempotent).
    WHERE NOT (ext)-[:INHERITS]->(def)
    MERGE (ext)-[r:INHERITS]->(def)
    ON CREATE SET r.order = coalesce(edge_order, 0)
    RETURN count(r) AS backfilled
} IN TRANSACTIONS OF 10000 ROWS;

// ---------------------------------------------------------------------------
// PHASE 2: DELETE — remove same-name INHERITS edges to non-definition targets
// ---------------------------------------------------------------------------
// Deletes every same-name INHERITS edge whose TARGET is NOT a definition node.
// This covers:
//   - The K² mesh edges (extender→extender same-name).
//   - Any stale "definition-chuha→extender" reverse edges from the old writer
//     bug (noted in ADR-0013 Alternatives #5/#6 and khaosat §4).
//
// Models that have no definition node anywhere in the graph (e.g. Enterprise
// models not indexed) will have ALL their same-name edges deleted here.  This
// is CORRECT: the new writer also emits 0 edges for them (no definition to
// anchor to), so deleting the old mesh edges reaches the desired clean state.
// The read-side ORM tools are unaffected (proven in khaosat §1 — no query
// consumes same-name edges with semantic meaning; R3/R4 explicitly filter
// `p.name <> $name`).

CALL {
    MATCH (a:Model)-[r:INHERITS]->(b:Model)
    WHERE a.name = b.name AND a.odoo_version = b.odoo_version
      AND NOT coalesce(b.is_definition, false)
    DELETE r
    RETURN count(r) AS deleted
} IN TRANSACTIONS OF 10000 ROWS;

// ---------------------------------------------------------------------------
// PHASE: VERIFY (run after both phases complete)
// ---------------------------------------------------------------------------
// Expected after a clean run:
//   mesh_edges_remaining: 0 for every version.
//   definition_edges_after: N where N = number of extenders that have a
//     definition node in the graph (may be < total extenders when some models
//     only have extenders, no definition indexed — e.g. pure-EE models).
//
// MATCH (a:Model)-[r:INHERITS]->(b:Model)
// WHERE a.name = b.name AND a.odoo_version = b.odoo_version
//   AND NOT coalesce(b.is_definition, false)
// RETURN a.odoo_version AS version, count(r) AS mesh_edges_remaining
// ORDER BY version;
//
// MATCH (a:Model)-[r:INHERITS]->(b:Model)
// WHERE a.name = b.name AND a.odoo_version = b.odoo_version
//   AND coalesce(b.is_definition, false)
//   AND NOT coalesce(a.is_definition, false)
// RETURN a.odoo_version AS version, count(r) AS definition_edges_after
// ORDER BY version;
//
// NOTE: `definition_edges_after` being less than `extenders_missing_edge` from
// DIAGNOSE is expected and correct — those are extenders whose definition model
// is not indexed (e.g. EE-only modules).  Their same-name edges were 0 with the
// new writer and are 0 after cleanup.  No data is lost: the read path never
// relied on these edges (khaosat §1 proof).
