// Cleanup v8.0 ghost Module nodes (no path, no profile, no incoming DEPENDS_ON).
// Audit date: 2026-05-16 — 205 ghost nodes found.
//
// SAFETY: Run cleanup_v8_ghosts_diagnose.cypher FIRST — only run this file if
// the count is approximately 205 (the audited number from 2026-05-16).
// If the count is significantly different, STOP and investigate.
//
// Safe to run idempotently; DETACH DELETE is a no-op when no nodes match.

MATCH (m:Module {odoo_version:'8.0'})
WHERE m.path IS NULL AND m.profile IS NULL
  AND NOT EXISTS { ()-[:DEPENDS_ON]->(m) }
DETACH DELETE m;
