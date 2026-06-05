# ADR-0046 — MCP Embed Concurrency + Anti-Hang (#227)

**Status:** Accepted
**Date:** 2026-06-01
**Authors:** Engineering team
**Related:** ADR-0010 (embedding observability), ADR-0044 (token-bounded embedding),
  ADR-0045 (provider abstraction)

---

## Context

### Production failure — silent server wedge (~11 h)

Issue #227 was triggered by a production incident: the MCP server became completely
unresponsive for approximately 11 hours before being restarted. Post-mortem evidence:

- **TCP backlog growth:** `Recv-Q` on the MCP port grew from 113 to 147 during the
  wedge, confirming that new connections were accepted by the kernel but never serviced
  by the application.
- **Embed timeout far exceeded:** The embedder batch read-timeout was 1200s (20 min).
  The wedge lasted ~11h — approximately **30x** the configured timeout.
- **`/health` unresponsive:** Even the health endpoint returned no response, confirming
  the entire event loop was blocked (not a pool-exhaust / DB issue).

### Root cause — FastMCP runs `sync def` handlers on the event loop thread

FastMCP (the MCP framework used by OSM) invokes `sync def` tool handler functions
**directly on the asyncio event loop thread** — it does not wrap them in
`asyncio.to_thread` or `loop.run_in_executor`. This is by design for simple tools, but
it means any blocking call inside a `sync def` handler blocks the entire event loop.

The three query-embed tools (`find_examples`, `suggest_pattern`, `find_style_override`)
each called `embedder.embed(...)` synchronously. `embed()` makes a blocking HTTP
request via `httpx.Client`. Under the 1200s batch timeout, a single slow or hung
embedder request blocked the event loop for up to 20 minutes, making **all** other
requests (including `/health`) unresponsive.

### Secondary issue — unbounded concurrency

Even after fixing the blocking call, multiple concurrent embed requests can still
exhaust the upstream embedder's connection pool or processing queue. Without a cap,
a burst of simultaneous search queries would pile up `embed_async` calls without
backpressure, degrading performance or causing OOM.

### Hold-and-wait analysis

There is **no hold-and-wait deadlock** between embedding and Postgres:
- The embed call completes first (off-loop, in a worker thread).
- The Postgres connection is checked out from the pool **after** the embed returns.
- There is no circular wait: `embed → PG checkout` is strictly sequential, not
  mutually dependent.

The wedge was a pure event-loop blockage, not a deadlock.

---

## Decision

### D1 — Async hot path: `embed_async` off the event loop

All three query-embed tools (`_find_examples_impl`, `_suggest_pattern_impl`,
`_find_style_override_impl`) are converted to `async def` and embed via
`embedder.embed_async()`:

```python
async def _find_examples_impl(...):
    ...
    vec = await _embed_query(embedder, instruct, query)
    ...
```

`embed_async` (on `_BaseHttpEmbedder`) runs `embed()` in a worker thread via
`asyncio.to_thread`, so the blocking HTTP call never executes on the event loop.
The event loop is free to serve other requests (including `/health`) while an embed
is in progress.

`embed_async` accepts a `read_timeout` parameter. For the query path, a short timeout
(30s, `TIMEOUT_EMBEDDER_READ_QUERY`) is used instead of the 1200s batch timeout. If
the embedder does not respond within 30s, the embed fails fast and the tool returns an
error — no 20-minute freeze.

### D2 — `asyncio.Semaphore` cap (`EMBEDDER_MAX_CONCURRENCY`)

A module-level `asyncio.Semaphore` bounds the number of concurrent in-flight embed
requests across all tool handlers:

```python
_embed_semaphore: asyncio.Semaphore | None = None  # lazy init on first use

def _get_embed_semaphore() -> asyncio.Semaphore:
    # Double-checked lazy construction (must be built inside the running loop)
    ...
    _embed_semaphore = asyncio.Semaphore(EMBEDDER_MAX_CONCURRENCY)
```

`EMBEDDER_MAX_CONCURRENCY` defaults to 4 (override via env var). The semaphore is
constructed lazily on first use — not at import time — because `asyncio.Semaphore`
must be created inside a running event loop.

### D3 — Fast rejection on semaphore timeout (`EmbedOverloaded`)

A tool handler waits at most `EMBEDDER_SLOT_ACQUIRE_TIMEOUT_S` (default 5s) for a
semaphore slot. If the slot is not acquired in time:

```python
try:
    await asyncio.wait_for(sem.acquire(), timeout=_EMBED_SLOT_ACQUIRE_TIMEOUT_S)
except TimeoutError:
    raise EmbedOverloaded(f"Embed semaphore full (max {EMBEDDER_MAX_CONCURRENCY}); retry shortly")
```

`EmbedOverloaded` surfaces to the MCP client as an actionable overload message. The
caller knows to retry after a short delay rather than waiting indefinitely. This bound
must stay shorter than the embed read timeout so the rejection is genuinely fast.

### D4 — Query timeout separate from batch timeout

`TIMEOUT_EMBEDDER_READ_QUERY` (default 30s) is a distinct constant from
`TIMEOUT_EMBEDDER_READ` (default 1200s). The query path passes the short timeout
through `embed_async(read_timeout="query")`, which routes to a lazily-built short-timeout
`httpx.Client` (shared per embedder instance, not per request). The batch indexer path
continues to use the 1200s timeout.

Rationale: a query embeds one short text; if the embedder cannot respond in 30s, the
request should fail fast rather than block a user for 20 minutes. A batch embedding
50 texts legitimately needs up to 20 minutes on slow hardware.

### D5 — `uvicorn` connection backpressure (`limit_concurrency`)

At server startup, `limit_concurrency` is set to `EMBEDDER_MAX_CONCURRENCY * 16`:

```python
_limit_concurrency = int(os.getenv("MCP_LIMIT_CONCURRENCY", str(EMBEDDER_MAX_CONCURRENCY * 16)))
uvicorn.run(_app, ..., limit_concurrency=_limit_concurrency)
```

When the number of active connections exceeds this ceiling, `uvicorn` returns HTTP 503
immediately (not queuing). The multiplier of 16 provides headroom for cheap non-embed
tools and `/health` while the semaphore independently bounds the expensive embed slots.
The env var `MCP_LIMIT_CONCURRENCY` allows tuning without a code change.

### D6 — `/health` is a pure liveness probe (no DB I/O)

`GET /health` performs **no** database I/O — no Neo4j query, no Postgres pool checkout,
no `SELECT COUNT(*)` scan. It returns 200 immediately if the event loop can serve the
request, reflecting only "the process is alive".

The pre-fix `/health` response included `embeddings_total` and `embeddings_by_chunk_type`
counts obtained by scanning the `embeddings` table (591k rows in production). Under pool
exhaustion or a blocked event loop, this scan would time out, turning a liveness probe
into a false-503 that could trigger needless restarts. More critically, the scan itself
was a blocking DB call that could delay other requests.

Post-fix, `/health` reads a module-level cache populated by `/ready` hits:

```python
def _peek_ready_cache() -> dict[str, object] | None:
    return _ready_cache  # non-blocking module global read
```

The counts are surfaced for backward compatibility (`embeddings_total`,
`embeddings_by_chunk_type` in the response body) but are `null` until the first `/ready`
hit populates the cache. They are never fetched on the liveness path.

### D7 — `/ready` is the readiness probe (cached, heavy)

`GET /ready` includes the heavyweight DB checks: Neo4j connectivity + Postgres
connectivity + embedding count scan. Results are cached in-memory for 60s
(`_READY_CACHE_TTL_S`). A burst of readiness probes triggers at most one DB scan per
60s window. Double-checked locking (`_ready_cache_lock`) ensures only one coroutine
refreshes the cache at a time.

`/ready` is a **new HTTP endpoint** (not an MCP tool) — tool count stays **24**.
Callers: Kubernetes readiness probe, nginx upstream health, monitoring dashboards.

---

## Consequences

### Positive

- A single slow or hung embed request can no longer freeze the entire MCP server.
- `/health` (liveness) always responds in O(1) time, independent of DB pool state.
- Concurrent query bursts are rejected fast (`EmbedOverloaded` in ~5s) rather than
  silently queuing to OOM or indefinitely blocking.
- The separate query timeout (30s) keeps interactive search responsive even when the
  batch indexer has the embedder under heavy load.

### Negative / Trade-offs

- `EMBEDDER_MAX_CONCURRENCY=4` (default) limits simultaneous interactive searches.
  Operators with dedicated GPU inference servers should increase this value. Too high
  and the upstream embedder queues unboundedly; too low and interactive requests fail
  fast under burst load.
- The semaphore is process-local. In a multi-process uvicorn deployment (multiple
  workers), each worker maintains its own semaphore. Cross-worker coordination would
  require Redis or a shared counter — deferred (single-process deployment is the current
  norm for OSM).
- `EMBEDDER_SLOT_ACQUIRE_TIMEOUT_S=5` means a user may see an overload error even when
  only 4 embed slots are occupied if a request arrives within 5s of the slot being
  freed. This is preferable to hanging for 20 minutes.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDER_MAX_CONCURRENCY` | `4` | Max concurrent embed requests (semaphore size) |
| `EMBEDDER_TIMEOUT_READ_QUERY` | `30` | Per-query embed read timeout (seconds) |
| `EMBEDDER_SLOT_ACQUIRE_TIMEOUT` | `5` | Max wait for a semaphore slot before fast-reject |
| `MCP_LIMIT_CONCURRENCY` | `EMBEDDER_MAX_CONCURRENCY * 16` | uvicorn connection ceiling |
| `EMBEDDER_TIMEOUT_READ` | `1200` | Batch-indexing embed read timeout (unchanged) |

### New HTTP endpoint

`GET /ready` — readiness probe. Returns JSON with `status`, `neo4j`, `postgres`,
`embeddings_total`, `embeddings_by_chunk_type`. Not an MCP tool; not counted in the 25
tool surface.
