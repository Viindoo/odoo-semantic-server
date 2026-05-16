// Backfill is_definition=false on __unresolved__ placeholder Model nodes where
// the property was left NULL by older writer versions.
// Audit date: 2026-05-16 — 235 nodes with is_definition IS NULL found.
//
// Safe to run idempotently. The writer now sets is_definition=false at write
// time (ON CREATE SET); this cypher cleans up pre-existing NULL rows only.

MATCH (m:Model {module:'__unresolved__'})
WHERE m.is_definition IS NULL
SET m.is_definition = false
RETURN count(m) AS backfilled;
