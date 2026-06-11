# Zombie Transaction Detection and Termination

Runbook for detecting and terminating long-running ("zombie") Neo4j transactions in the
Odoo Semantic MCP stack. Covers the production incident pattern (issue #273) and the
layered defenses now in place (ADR-0048).

---

## Background

In production (2026-06), 11 Neo4j transactions from the ORM validation tools
(`resolve_orm_chain`, `validate_domain`, `validate_depends`, `validate_relation`)
ran for 19-24 hours without any automatic termination. Root causes:

- **K² same-name INHERITS mesh** (writer bug, now fixed): ~256k redundant edges per
  version caused the ORM traversal queries to enumerate up to 86 million paths.
- **No per-query driver timeout**: the Neo4j driver was making calls without any
  `timeout=` argument, so transactions ran indefinitely.
- **No global `db.transaction.timeout`**: no server-side backstop existed.

All three root causes have been fixed (ADR-0048). This runbook covers how to detect
and terminate any residual zombies and how to set up proactive monitoring.

---

## Symptoms

Look for these signs that zombie transactions are present:

| Symptom | Detail |
|---------|--------|
| MCP tool calls hang indefinitely | `resolve_orm_chain` / `validate_domain` / `validate_depends` / `validate_relation` never return |
| Rising Neo4j Bolt connections | `ss -tn state established '( dport = :7687 )'` shows ESTAB count climbing without dropping |
| Rising thread count in MCP process | `ps -eLf \| grep mcp \| wc -l` increases; each hung tool occupies one `asyncio.to_thread` worker |
| `metaData: {}` in SHOW TRANSACTIONS | Empty metadata map means the driver sent NO per-query timeout - pre-fix posture |
| Neo4j heap / GC pressure | Long-lived transactions hold open cursors; GC logs show frequent collection |

---

## Detection

### Quick check - run the wrapper script

```bash
# From repo root. Credentials resolved from env (see script header for full priority order).
ops/check_long_transactions.sh

# Raise threshold to 600s (indexer transactions can legitimately run longer):
THRESHOLD_SECONDS=600 ops/check_long_transactions.sh

# Custom host/port/password:
NEO4J_HOST=db.internal NEO4J_PASSWORD=secret ops/check_long_transactions.sh
```

Exit code 0 = clean. Exit code 1 = transactions found (stdout contains details). Exit code 2 = connection error.

### Manual Cypher (Neo4j 5.x)

```cypher
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
```

Full query is in `ops/check_long_transactions.cypher`.

### Via Neo4j Browser

Navigate to `http://localhost:7474` (or the Neo4j Browser URL for your deployment),
then run the query above.

### Via Docker exec (when cypher-shell is not in PATH locally)

```bash
docker exec -i odoo-semantic-mcp-neo4j-1 \
  cypher-shell -u neo4j -p "${NEO4J_PASSWORD:-password}" \
  "SHOW TRANSACTIONS YIELD transactionId, elapsedTime, status, username, metaData
   WHERE elapsedTime > duration('PT300S')
   RETURN transactionId, elapsedTime, status, username
   ORDER BY elapsedTime DESC;"
```

---

## Scheduled Monitoring (cron)

Add to root crontab (or the `odoo-semantic` system user) to check every 5 minutes:

```cron
*/5 * * * * /opt/odoo-semantic-mcp/ops/check_long_transactions.sh \
    >> /var/log/osm-zombie-check.log 2>&1
```

The script exits 1 on detection, making it compatible with monitoring tools that
alert on non-zero cron exit codes (e.g., `cronitor`, `healthchecks.io`, or a
Prometheus `node_exporter` textfile collector).

**Recommended alert thresholds:**

| Threshold | Action |
|-----------|--------|
| > 300s (5 min) | Investigate - may be a large indexer batch |
| > 600s (10 min) | Page on-call - `db.transaction.timeout` backstop should have fired |
| > 1800s (30 min) | Immediate action - `db.transaction.timeout` not effective; terminate manually |

---

## Termination

### Terminate a single transaction (Neo4j 5.x)

```cypher
TERMINATE TRANSACTION '<transactionId>';
```

Example: `TERMINATE TRANSACTION 'neo4j-transaction-42';`

### Terminate multiple transactions at once

```cypher
CALL dbms.terminateTransactions(['<id1>', '<id2>', '<id3>']);
```

### Terminate all transactions from the app user

Use this only in an emergency where you cannot identify specific IDs:

```cypher
SHOW TRANSACTIONS
YIELD transactionId, username
WHERE username = 'neo4j' AND transactionId <> ''
WITH collect(transactionId) AS ids
CALL dbms.terminateTransactions(ids)
YIELD transactionId, username, message
RETURN transactionId, username, message;
```

**Warning:** this terminates ALL transactions including ongoing indexer writes.
The indexer is idempotent and will recover on the next run, but partial writes
may leave the graph in a slightly inconsistent state until the next full reindex.

### After termination: verify cleanup

```cypher
SHOW TRANSACTIONS
YIELD transactionId, elapsedTime, status
WHERE elapsedTime > duration('PT60S')
RETURN count(*) AS remaining_long_txs;
```

Expected result after cleanup: 0 (or only legitimate indexer transactions).

---

## CRITICAL: Service restart does NOT clear Neo4j-side transactions

**Restarting the MCP service (`systemctl restart odoo-semantic-mcp`) does NOT terminate
Neo4j transactions.** It only kills the Python application threads on the app side.

When the MCP process restarts:
- The app-side threads that were blocked waiting on Neo4j are killed.
- But the Neo4j server-side transaction context continues running independently.
- The zombie transaction persists in Neo4j until it either completes, is terminated
  manually, or is reaped by `db.transaction.timeout`.

**To actually terminate a zombie transaction, you must either:**

1. Run `TERMINATE TRANSACTION '<id>'` in Neo4j directly (immediate effect), OR
2. Wait for the `db.transaction.timeout=600s` backstop to reap it automatically.
   The 600s backstop is wired into `docker-compose.yml` (ADR-0048 D7) and the
   nightly smoke CI. On bare-metal deployments, verify it is set:
   ```cypher
   CALL dbms.listConfig() YIELD name, value
   WHERE name = 'db.transaction.timeout'
   RETURN name, value;
   ```
   Expected: `600s`. If not set, apply it:
   ```cypher
   CALL dbms.setConfigValue('db.transaction.timeout', '600s')
   ```
   Then add `db.transaction.timeout=600s` to `neo4j.conf` for persistence
   across Neo4j restarts.

---

## Layered Defense Reference

These layers work together to prevent zombie transactions from persisting:

| Layer | Mechanism | Where configured |
|-------|-----------|-----------------|
| L1 - Per-query driver timeout | `neo4j.Query(text, timeout=30)` on all 5 ORM read call-sites | `src/mcp/orm.py` |
| L2 - ORM semaphore cap | `threading.BoundedSemaphore(ORM_QUERY_MAX_CONCURRENCY=8)` fast-rejects excess requests in 5s | `src/mcp/orm.py` + `src/constants.py` |
| L3 - Global server backstop | `db.transaction.timeout=600s` terminates any transaction exceeding 600s | `docker-compose.yml` + `neo4j.conf` |
| L4 - Writer topology fix | K×D edges (extender→definition only) instead of K² mesh - eliminates the 86M-path enumeration | `src/writer_neo4j.py` (ADR-0048 D1) |
| L5 - Ops detection | `ops/check_long_transactions.sh` detects survivors > 300s; cron-alertable | `ops/` (this runbook) |

Full details: `docs/operations/timeouts.md` (L1-L3 env vars, startup validation, bare-metal setup).

### Non-ORM reads (accepted posture)

Approximately 84 `session.run` calls in `src/mcp/server.py` (e.g., `impact_analysis`,
`_resolve_model` ranking) do NOT have a per-query L1 driver timeout. This is accepted:
all run in `@offload` worker threads (no event-loop wedge); L3 `db.transaction.timeout=600s`
backstops all. A slow non-ORM traversal can pin a thread up to 600s but this is degraded
throughput, not a #273-class zombie. Extending `_bounded()` to hot non-ORM paths is a
follow-up item (TASKS.md).

---

## Incident Response Checklist

When a zombie transaction is suspected:

- [ ] Run `ops/check_long_transactions.sh` to confirm and get IDs.
- [ ] Check `currentQuery_preview` to distinguish ORM tools from indexer batches.
- [ ] Check `metaData` - `{}` = pre-fix driver (no per-query timeout); populated = post-fix.
- [ ] For ORM tool zombies (recognize `MATCH (m:Model`, `INHERITS`, `DELEGATES_TO` in query): terminate immediately.
- [ ] For indexer batches (recognize `CALL {} IN TRANSACTIONS`, `MERGE (mod:Module`): leave unless > 600s.
- [ ] After termination, re-run detection query to verify 0 survivors.
- [ ] If `metaData: {}` is present, the MCP service is running a pre-ADR-0048 binary. Deploy the fix and restart the service.
- [ ] File a post-mortem if any transaction ran > 10 min (the L3 600s backstop should have caught it).
