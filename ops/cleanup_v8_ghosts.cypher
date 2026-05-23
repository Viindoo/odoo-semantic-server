// Cleanup v8.0 ghost Module nodes (no path, no profile, no incoming DEPENDS_ON).
// These are unreferenced stubs left from a partial v8 index run.
//
// SAFETY: Run cleanup_v8_ghosts_diagnose.cypher FIRST to get the ghost count
// for your instance. Only run this file if that count matches your expectation
// for stale stubs. If the count is unexpectedly large, STOP and investigate.
//
// Safe to run idempotently; DETACH DELETE is a no-op when no nodes match.

MATCH (m:Module {odoo_version:'8.0'})
WHERE m.path IS NULL AND m.profile IS NULL
  AND NOT EXISTS { ()-[:DEPENDS_ON]->(m) }
DETACH DELETE m;
