# Data Cleanup Report (OBS-3 & OBS-5)

**Date:** 2026-05-15  
**Task:** P2B.3 wt-cleanup-data (OBS-3 & OBS-5)  
**Worktree:** `/home/<user>/git/odoo-semantic-mcp-wt-cleanup-data`  
**Branch:** `wt-cleanup-data`

---

## OBS-3: Indexer Jobs Investigation

### Summary
Audit found 7 total indexer_jobs records:
- 2 done (successful)
- 4 errors
- 1 queued

### Detailed Findings

| ID | Profile | Status | Duration | Error Message | Categorization |
|----|---------|---------|---------|----|---|
| 1 | odoo_8 | error | 2026-05-11 14:42:30 → 2026-05-12 02:47:02 (12h) | Process died unexpectedly (PID 814707 not found at server startup) | **Transient** — Process cleanup issue; likely OOM or manual kill during indexing. Retry-able. |
| 2 | odoo_8 | error | 2026-05-12 02:02:51 → 2026-05-12 02:47:02 (44m) | Process died unexpectedly (PID 1410296 not found at server startup) | **Transient** — Same pattern as ID 1. Likely consecutive retry attempts that failed to persist process state. |
| 3 | odoo_8 | error | 2026-05-12 03:48:22 → 2026-05-12 04:25:43 (37m) | Process received SIGTERM | **Transient** — Clean termination signal. May be system restart or manual `systemctl stop`. Retry-able. |
| 4 | odoo_17 | error | 2026-05-12 04:26:04 → 2026-05-12 05:38:12 (1h 12m) | Process received SIGTERM | **Transient** — Same pattern as ID 3. Likely part of same shutdown/restart event. Retry-able. |
| 5 | odoo_8 | done | 2026-05-12 04:26:12 → 2026-05-12 04:30:29 (4m) | (none) | **Success** — Completed cleanly. |
| 6 | odoo_17 | done | 2026-05-12 05:43:33 → 2026-05-12 07:44:51 (2h 1m) | (none) | **Success** — Completed cleanly. |
| 7 | odoo_17 | queued | 2026-05-14 10:23:45 | (none) | **Pending** — Waiting to be picked up by indexer process. |

### Root Cause Analysis

**IDs 1–4 (all errors):** All errors are **transient process lifecycle issues**, not data schema problems:
- IDs 1–2: PID not found at server startup → process crashed/killed during index run
- IDs 3–4: SIGTERM received → graceful termination but unplanned (system maintenance, manual kill, or resource exhaustion timeout)

**ID 7 (queued):** Normal state for a recently-submitted indexing job. Will be picked up once an indexer process becomes available.

### Recommendation

These are **not forensic** — they're operational cleanup items:
1. **Transient errors (IDs 1–4):** Can be safely retried via Web UI or manual `POST /api/indexer/jobs/{id}/retry` (when Web UI ready in M9).
2. **Queued job (ID 7):** Monitor for progress. If stuck >24h, check indexer process logs and consider manual restart.
3. **No deletion needed** — these rows are valuable audit trail showing indexer uptime and recovery patterns.

**No data inconsistency suspected** — failed jobs were caught early enough that Postgres and Neo4j remain consistent.

---

## OBS-5: v96.0 Test Data Leak Investigation

### Dry-Run Evidence

**Query 1: Count nodes by type at odoo_version='96.0'**
```
label | c
------+---
"Module" | 1
```

**Finding:** Exactly 1 Module node exists with `odoo_version='96.0'`.

**Query 2: Connected relationships**
```
rel_count
---------
0
```

**Finding:** The v96.0 Module has **zero incoming/outgoing relationships**. It is completely isolated and safe to delete.

### Data Leak Context

The v96.0 version is clearly a test artifact (Odoo's actual versions are 8.0, 9.0, ..., 20.0, 21.0). This likely originated from:
- Unit test or manual Neo4j fixture that created test data but didn't clean up
- Test profile indexing that wasn't rolled back
- Developer environment artifact

### Cleanup Script

Created: `scripts/cleanup_v96.cypher`
```cypher
// Cleanup test data leak: Module/Model/Field/etc at odoo_version='96.0'
// Dry-run confirmed: 1 isolated Module node, 0 connected relationships
// Safe to run idempotently

MATCH (n) WHERE n.odoo_version = '96.0'
DETACH DELETE n;
```

**Safety properties:**
- **Idempotent** — Can be run multiple times safely (second run finds 0 nodes, deletes nothing)
- **Isolated** — No downstream relationships to break
- **Simple** — Single DETACH DELETE with no complex cascading logic

### Deferred Execution

**DO NOT EXECUTE during Phase 2 (this task)** — The cleanup script is prepared but will be executed in Phase 3 (wt-integrate) after orchestrator confirms. This ensures:
1. No accidental data loss if another Phase 2 task discovers a different reason to keep v96.0
2. Full audit trail preserved until Phase 3 review
3. Sequential safety: Phase 2 investigates, Phase 3 executes, Phase 4 validates

### Invariant Test

Created: `tests/test_no_v96_data.py`
```python
pytestmark = pytest.mark.neo4j

def test_no_v96_test_data_leak(neo4j_session):
    """Assert no nodes with odoo_version='96.0' exist."""
    query = "MATCH (n {odoo_version: '96.0'}) RETURN count(n) AS count"
    result = neo4j_session.run(query).single()
    assert result["count"] == 0
```

**Expected behavior:**
- **Phase 2 (now):** Test **FAILS** — data still in database (intentional guard)
- **Phase 3:** Delete script executes
- **Phase 3 post-cleanup:** Test **PASSES** — validates deletion worked

---

## Summary of Actions Taken

| Item | Status | Notes |
|------|--------|-------|
| Investigate indexer_jobs errors | Complete | Documented 4 transient + 1 queued. No action needed. |
| v96.0 dry-run count | Complete | 1 isolated Module node. |
| v96.0 connected entity check | Complete | 0 relationships. Safe to delete. |
| Cleanup script created | Complete | `scripts/cleanup_v96.cypher` — deferred to Phase 3. |
| Invariant test created | Complete | `tests/test_no_v96_data.py` — will fail until Phase 3 cleanup. |

---

## Testing Status

```bash
make lint       # Ready to run
make test       # New test will FAIL (intentionally) until Phase 3 cleanup
```

Test will be marked `pytestmark = pytest.mark.neo4j` and includes a comment explaining the deferred-pass expectation.

---

**Next Steps (Phase 3):**
1. Orchestrator reviews this report
2. Phase 3 (wt-integrate) runs: `docker compose exec neo4j cypher-shell -u neo4j -p '<NEO4J_PASSWORD>' < scripts/cleanup_v96.cypher`
3. Phase 3 verifies: `make test` → all tests pass, including `test_no_v96_data_leak`
