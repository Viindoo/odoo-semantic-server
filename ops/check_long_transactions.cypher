// ops/check_long_transactions.cypher
// Detect Neo4j transactions running longer than a threshold (default: 300s).
//
// WHY: In production (issue #273), 11 zombie ORM transactions ran 19-24 hours
// before being detected via client-side symptoms. This script surfaces long-running
// transactions proactively so ops can terminate them before they drain the thread pool.
//
// NEO4J 5.x SYNTAX: `SHOW TRANSACTIONS` is a Cypher administrative command available
// in Neo4j 5.x. The `elapsedTime` column is a Duration value. Comparison against a
// Duration literal (`duration('PT300S')`) is the correct Neo4j 5.x idiom.
// Do NOT use millisecond integer comparisons — Neo4j 5.x does not support
// `elapsedTime.milliseconds > 300000` directly on the raw Duration type.
//
// HOW TO RUN:
//   cypher-shell -u neo4j -p <password> --format verbose < ops/check_long_transactions.cypher
//   Or via the ops/check_long_transactions.sh wrapper (handles credential extraction).
//
// READING THE RESULTS:
//   transactionId  - use this to terminate: TERMINATE TRANSACTION '<transactionId>'
//   elapsedTime    - how long the transaction has been running (ISO-8601 duration, e.g. PT305.123S)
//   status         - typically "Running" for zombie ORM transactions; "Terminated" means
//                    db.transaction.timeout has already killed it (600s backstop, ADR-0048 D7)
//   currentQuery   - the Cypher query in progress; ORM zombies often show a VLP with INHERITS
//                    edges or appear as `MATCH (m:Model ...` fragments
//   username       - the Neo4j user running the transaction (typically "neo4j" for app connections)
//   metaData       - empty map `{}` for transactions that lack per-query timeout metadata
//                    (a `metaData: {}` entry means the driver sent NO per-query timeout --
//                    this was the pre-fix posture; after ADR-0048 all ORM calls use
//                    `neo4j.Query(text, timeout=30)` which populates this map)
//
// THRESHOLD: 300s (5 minutes). Adjust the duration literal to suit your environment.
// The per-query driver timeout (NEO4J_QUERY_TIMEOUT_SECONDS=30s default) should prevent
// ORM tools from ever reaching 300s post-fix. Any transaction above 300s is either:
//   (a) a legitimate long-running indexer batch (expected, leave it alone), or
//   (b) a pre-fix zombie that survived a deploy without a service restart (terminate it).
// Use `currentQuery` and `username` to distinguish (a) from (b).

SHOW TRANSACTIONS
YIELD transactionId, currentQuery, elapsedTime, status, username, metaData
WHERE elapsedTime > duration('PT300S')
RETURN
  transactionId,
  elapsedTime,
  status,
  username,
  metaData,
  left(currentQuery, 200) AS currentQuery_preview
ORDER BY elapsedTime DESC;
