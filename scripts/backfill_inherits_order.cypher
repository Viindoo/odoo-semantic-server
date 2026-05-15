// scripts/backfill_inherits_order.cypher
//
// One-shot backfill: set r.order = 0 on every INHERITS edge that is missing
// the property.  This corrects the 765 stale edges created by a pre-ADR-0013
// CLI run before the `order` property was introduced.
//
// WHY order=0:
//   When we cannot recover the original _inherit list position from the DB
//   (the list is stored in source code, not in Neo4j) the safest fallback is
//   0 (first / only parent).  Self-extension edges (Pattern D mixin injection)
//   are the most common case and those models almost always have a single
//   parent — so 0 is correct for the vast majority.  On the next full re-index
//   the writer will overwrite with the precise index from the parsed source.
//
// IDEMPOTENCY:
//   coalesce(r.order, 0) leaves any edge that already has a numeric order
//   untouched.  Safe to re-run multiple times.
//
// USAGE (cypher-shell or Neo4j Browser):
//   :source scripts/backfill_inherits_order.cypher
//
// Or via cypher-shell CLI (adjust credentials as needed):
//   cypher-shell -a bolt://localhost:7687 -u neo4j -p password \
//       --file scripts/backfill_inherits_order.cypher
//
// Phase 3 (integration / P3) will run this against the live Docker volume.
// Do NOT run against production without reading Phase 3 runbook first.

MATCH ()-[r:INHERITS]->()
WHERE r.order IS NULL
SET r.order = 0
RETURN count(r) AS backfilled_edges;
