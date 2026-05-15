# ADR-0010 — Embedding Observability (M7 C5)

**Status:** Accepted (2026-05-11)

**Context:** Admins had no way to see how many embedding API calls a run made
or how many embedding rows are stored in pgvector. Debugging cost issues (Ollama
timeouts, storage growth) required ad-hoc SQL queries. M7 C5 adds lightweight
observability: a run-level log line and a /health field, both readable without
re-running the indexer.

## Decisions

### D1 — Thread-safe `call_count` on embedder instances (not a global counter)

`call_count: int` is an instance attribute on both `FakeEmbedder` and
`Qwen3Embedder`, protected by a `threading.Lock`. Rationale:

- Per-instance scope matches the existing embedder lifecycle (one embedder per
  indexer run, passed through the call stack).
- Thread-safety is required because `index_profile()` supports parallel repo
  workers (`--max-workers N`) sharing a single embedder instance across threads.
- Instance scope also makes tests trivial: create a fresh `FakeEmbedder`, call
  `embed()`, assert `call_count`. No global state to reset between tests.

`FakeEmbedder.call_count` mirrors `Qwen3Embedder.call_count` exactly — tests
can assert the same observability contract without a real Ollama instance.

### D2 — `write_module_embeddings` returns embed call delta (int)

`write_module_embeddings()` now returns `int` instead of `None`. The return
value is the number of `embed()` calls made during that invocation (0 if chunks
is empty, ≥1 otherwise). `pipeline._index_repo` aggregates these into
`total_embed_calls` and logs the summary line.

Alternative considered: pass the embedder into pipeline and read `call_count`
directly at the end of the run. Rejected: tighter coupling; `write_module_embeddings`
already knows what it called, so returning the delta is cleaner and avoids
reading shared mutable state from outside the writer.

### D3 — `COUNT(*)` is acceptable at current scale; no Prometheus this push

`/health` adds `embeddings_total` via `SELECT COUNT(*) FROM embeddings`. At
current scale (<10M rows, ~20 GB), a sequential count takes <50 ms (PostgreSQL
can short-circuit via index-only scan on the primary key). This is acceptable
for an admin health endpoint that is not on the hot path.

Postgres `pg_class.reltuples` was considered as a cheaper approximate count but
it can be stale (only updated at VACUUM/ANALYZE). Exact count is more useful for
admins debugging storage growth.

No Prometheus/OpenTelemetry integration this push — that belongs to a dedicated
observability milestone. The log line and /health field give 80% of the value
with <5% of the complexity.

### D4 — `None` sentinel for pgvector-absent graceful degradation

`_get_embeddings_total()` returns `None` (not 0) when the embeddings table is
absent or the connection fails. The dashboard renders `'N/A'` for `None`.
This preserves correctness — 0 means "table exists, no rows"; `None` means
"unknown/unavailable". Existing `health_handler` defensive pattern (try/except
→ `"error:..."` string) is mirrored for the new field.

## Consequences

- Admins can confirm embedding pipeline is running by checking the log:
  `"Indexer run: 42 modules, 42 embed calls, 8341 rows written"`.
- `/health` response gains `embeddings_total` field (int or null). Existing
  consumers that don't read the field are unaffected (JSON is additive).
- `FakeEmbedder.call_count` enables unit tests for observability without Docker.
- `write_module_embeddings` return type changed `None → int`. Internal callers
  in `pipeline.py` updated. Any external caller that ignores the return value
  (common pattern) is unaffected.

## Alternatives Considered

- **Statsd/Prometheus push from embedder**: too heavy for this milestone.
- **`embeddings_total` via pgvector `pg_class.reltuples`**: approximate, can be
  stale. Rejected in favour of exact `COUNT(*)` at current scale.
- **Global module-level counter**: creates shared mutable state, complicates
  parallel tests, and doesn't reset between runs. Rejected.

## v2 — Chunk-Type Breakdown (2026-05-15)

**Status:** Accepted (2026-05-15)

**Context:** Initial audit (M8 pre-launch) revealed that `/health` returned
`embeddings_total: 121132` but hid the distribution across chunk types. The fact
that `pattern_example=0` (audit target) was not visible required ad-hoc SQL
queries to investigate embedding coverage. This decision extends `/health` with a
breakdown by chunk_type to support ongoing observability.

### D5 — `/health` adds `embeddings_by_chunk_type` breakdown

The `/health` response now includes:

```json
{
  "status": "ok",
  "embeddings_total": 121132,
  "embeddings_by_chunk_type": {
    "method": 42715,
    "field": 41461,
    "view": 13971,
    "qweb": 8386,
    "js_era2": 6824,
    "js_era3": 6202,
    "js_era1": 1573,
    "pattern_example": 0
  },
  ...
}
```

Implementation:
- New `_get_embeddings_by_chunk_type()` function queries `SELECT chunk_type, COUNT(*) FROM embeddings GROUP BY chunk_type`.
- Uses existing `idx_embeddings_filter` index (already present for fast chunk_type queries) — GROUP BY is cheap (<5ms).
- Returns `dict[str, int]` on success, `None` on any DB error (mirrors `_get_embeddings_total` defensive pattern).
- Returns empty `{}` (not `None`) in the JSON response body when pgvector is unavailable — preserves /health liveness.

Rationale for `dict` return in JSON (not list of tuples):
- Easier for admins to read: `"pattern_example": 0` vs finding the tuple in a list.
- Clients (dashboards) can directly reference `response.embeddings_by_chunk_type["method"]`.
- Backward-compatible: clients that ignore the new field are unaffected.

### D6 — Additive only; maintains backward compatibility

- Existing `embeddings_total` field unchanged (remains int or null).
- New field `embeddings_by_chunk_type` is additive.
- Existing /health consumers that don't read the field pass through unaffected.
- Sum of `embeddings_by_chunk_type` values == `embeddings_total` (audit invariant).

## Consequences (v2)

- Admins can instantly see chunk_type distribution at `/health` without SQL.
- Audit detection is simpler: if `pattern_example=0`, pattern embedding pipeline
  may need investigation.
- `/health` response size grows by ~100 bytes (8 chunk types × ~10 chars each).
- Tests verify: (1) endpoint structure, (2) sum invariant, (3) graceful degradation
  when pgvector unavailable.
