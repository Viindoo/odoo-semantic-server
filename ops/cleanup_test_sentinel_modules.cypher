// Cleanup test-sentinel Module nodes that leaked into production Neo4j.
//
// PROVENANCE: Two integration-test Module nodes were created against prod Neo4j
// when a test inadvertently pointed at the live DB (profile 'lt_globex_p' does
// not exist in Postgres `profiles`). They were identified during the v0.11.1
// zero-trust reindex-safety audit:
//
//   Module {name:'lt_globex_only',  odoo_version:'97.0', profile:['lt_globex_p']}
//   Module {name:'lt_globex_only2', odoo_version:'96.0', profile:['lt_globex_p']}
//
// Both have path=NULL, repo_id=NULL, and 0 edges (inert). Versions 94-99 are the
// test-sentinel band (documented in v0.11.1 release notes + TASKS.md follow-up);
// production must never carry live nodes at these versions.
//
// SAFETY: each MATCH is scoped by exact (name, odoo_version) — cannot match
// legitimate data. DETACH DELETE is a no-op when the node is already gone.
//
// IDEMPOTENT: safe to run multiple times; 0 deleted on subsequent runs.
//
// Run against the prod container (NEO4J_PASSWORD from NEO4J_AUTH in the compose .env):
//   docker compose exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
//     -f /dev/stdin < ops/cleanup_test_sentinel_modules.cypher
// Expected result: deleted=2 on first run, deleted=0 on subsequent runs.

MATCH (m:Module)
WHERE m.name IN ['lt_globex_only', 'lt_globex_only2']
  AND m.odoo_version IN ['97.0', '96.0']
DETACH DELETE m
RETURN count(m) AS deleted;
