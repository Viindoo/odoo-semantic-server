// Cleanup stale test PatternExample nodes that leaked into production Neo4j.
// Audit date: 2026-05-16 — 9 nodes matching prefix/version criteria found.
//
// Safe to run idempotently; DETACH DELETE is a no-op when no nodes match.
// Run the RETURN-only diagnostic first (replace DETACH DELETE with RETURN count(p))
// if you want to verify count before deleting.

MATCH (p:PatternExample)
WHERE p.pattern_id STARTS WITH 't-'
   OR p.pattern_id STARTS WITH 'test-'
   OR p.pattern_id STARTS WITH 'snap-'
   OR p.pattern_id STARTS WITH 'pipeline-seed-'
   OR p.odoo_version_min IN ['99.0','93.0']
DETACH DELETE p;
