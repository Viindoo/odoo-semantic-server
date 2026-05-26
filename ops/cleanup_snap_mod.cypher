// Cleanup the `snap_mod` test artifact Module node that leaked into production Neo4j.
// odoo_version '96.0' is a reserved test sentinel (94-99 band) — this single node
// originated from a test run, not from real indexed data (TASKS.md follow-up).
//
// Safe to run idempotently; DETACH DELETE is a no-op when no node matches.
// Returns the count deleted (1 on first run, 0 thereafter).
//
// Run against the prod container (NEO4J_PASSWORD from NEO4J_AUTH in the compose .env):
//   docker compose exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
//     -f /dev/stdin < ops/cleanup_snap_mod.cypher
// or paste the MATCH..RETURN statement inline.

MATCH (m:Module {odoo_version: '96.0', name: 'snap_mod'})
DETACH DELETE m
RETURN count(m) AS deleted_test_artifacts;
