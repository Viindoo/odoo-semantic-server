// Diagnostic — READ-ONLY. Run this BEFORE cleanup_v8_ghosts.cypher.
// Reports count of v8.0 ghost Module nodes: no path, no profile, no incoming
// DEPENDS_ON edges (i.e. unreferenced stubs left from a partial v8 index run).
// Audit date: 2026-05-16 — 205 nodes found.
//
// If count differs significantly from 205, STOP and investigate before running
// cleanup_v8_ghosts.cypher.

MATCH (m:Module {odoo_version:'8.0'})
WHERE m.path IS NULL AND m.profile IS NULL
  AND NOT EXISTS { ()-[:DEPENDS_ON]->(m) }
RETURN count(m) AS ghost_count;
