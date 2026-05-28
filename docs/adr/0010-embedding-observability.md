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

## Follow-up (M10 ops) — WI-A7 absorption

### D7 (implemented — M10C) — `embedder_batch_duration_seconds` Prometheus histogram

**Context:** D1's thread-safe `call_count` provides a per-run counter, and D3
deferred Prometheus/OpenTelemetry to "a dedicated observability milestone". M10
ops is that milestone — Stripe billing requires per-tenant usage metering and
the natural unit is "embed call latency", which feeds both billing (per-call
cost projection) and SRE alerting (Ollama timeout regression).

**Decision (as implemented):**
- Histogram metric `embedder_batch_duration_seconds` recorded once per
  `_embed_one()` network round-trip, labelled by `embedder_type` ∈ `{qwen3}`.
  A small `embed()` call (≤ `_MAX_BATCH` texts) does exactly one round-trip and
  therefore one observation; a large `embed()` call fans into
  `ceil(n / _MAX_BATCH)` sub-batches and records one observation **per
  sub-batch** — deliberately, so the histogram stays a faithful per-network-call
  latency distribution rather than blending several round-trips into one sample.
  This is distinct from `call_count` (D1), which counts **whole `embed()` calls**
  (one increment per call regardless of sub-batch fan-out).
  - Only the real `Qwen3Embedder` is instrumented. `FakeEmbedder` (the CI / test
    double — no GPU, no network) is **intentionally not** instrumented: it must
    not emit synthetic latency samples that would pollute the `/metrics` series a
    production scraper reads. The `{fake}` label is therefore NOT part of the
    contract.
- Bucket boundaries `[0.1, 0.25, 0.5, 1.0, 1.5, 2.5, 5.0, 10.0, 30.0, 60.0]` —
  the `0.1`…`30.0` band is chosen from production indexer logs showing median
  batch ≈1.5s and P99 ≈8s; the trailing `60.0` bucket captures slow/near-timeout
  batches (Ollama read timeout regime) so a latency-regression alert can fire on
  the `30.0`–`60.0` band instead of dumping everything into `+Inf`.
- Exposed via FastAPI `/metrics` endpoint (new route, mounted on the existing
  app — no separate Prometheus exporter sidecar).
- Reuses D1's `threading.Lock` pattern for thread-safety under
  `--max-workers > 1`. Both the histogram `observe()` and `call_count += 1`
  writes are taken under `self._lock`. They sit in the **same** critical section
  only on the single-batch path; on the large-batch path each `observe()` takes
  the lock once per sub-batch and the single `call_count += 1` takes it once
  after the loop (because the two signals have different cardinality — per
  round-trip vs per call — co-location is neither possible nor desirable there).
  `prometheus_client` is itself thread-safe, so no histogram datum is ever torn;
  the lock here serialises only this class's own `call_count` mutation. The two
  signals are independently consistent, not jointly atomic — a `/metrics` scrape
  that lands between a sub-batch `observe()` and the trailing `call_count += 1`
  sees a valid histogram and a valid (slightly lagging) counter, never garbage.

**Tracked in:** `TASKS.md` Milestone 10 § M10C item
"Pgvector observability — Prometheus `embedder_batch_duration_seconds` histogram".

**Not invalidating D3:** D3 deferred Prometheus "for now"; M10 is the lift that
adds it. The `/health` endpoint and log line from D1/D2 remain — D7 is additive,
not replacement.

> **Tracking:** Implementation tracked at `TASKS.md` → M10C "Prometheus `embedder_batch_duration_seconds` histogram".
