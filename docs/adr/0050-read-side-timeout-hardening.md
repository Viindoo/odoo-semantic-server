# ADR-0050 — Read-side timeout hardening (@offload_neo4j pool-less pattern) (#287)

**Status:** Accepted
**Date:** 2026-06-13
**Authors:** Engineering team
**Related:** ADR-0023 (tool output completeness — the raw-text contract this hardening
  defends: a tool must always return a clean English string, never a FastMCP
  protocol `isError`), ADR-0046 (MCP embed concurrency / anti-hang — the
  `asyncio.to_thread` offload + in-thread `threading.BoundedSemaphore` machinery
  reused here), ADR-0048 (ORM read bounds + `@offload_bounded` / `@offload_bounded_nonorm`
  pools — the bounded siblings this ADR distinguishes from the pool-less variant),
  ADR-0030 (`odoo://` resource URI scheme + LRU cache — the resource bodies this
  hardening keeps from being poisoned)

---

## Context

A read audit of the 25 MCP tools + 7 `odoo://` resources (issue #287) found a
systemic hole: a Neo4j per-query / transaction timeout could escape a tool body
as a FastMCP protocol `isError` instead of the clean English string ADR-0023
mandates. Three sub-shapes:

1. **Bare reads.** Many tools issued `session.run(cypher).single()/.data()`
   directly. The driver had no per-query `timeout=` and no tx-timeout →
   `OrmQueryTimeout` conversion, so a dense-graph query could either hang
   (the #273 zombie-transaction class) or surface a raw `ClientError`.
2. **No backstop.** Even where a timeout was converted to `OrmQueryTimeout`,
   nothing caught it at the tool boundary, so it propagated out as `isError`.
3. **Resource cache poisoning.** A resource handler that cached the result of a
   transient timeout would pin a stale error body in the LRU for the full TTL
   (300s, ADR-0030) — one 30s Neo4j blip becomes a 5-minute outage for that URI.

The fix landed across three PRs (PR-1 #289, PR-2, PR-3), all under the ADR-0023
raw-text contract, tool count 25 / resource count 7, no migration. This ADR is
the SSOT for the resulting read-side timeout-hardening pattern so the design does
not die with the closed issue.

## Decision

### 1. Two-layer pattern: bound at every query, catch at the boundary

Every read site routes its bare `session.run(...)` through one of two converting
helpers in `server.py`:

- `_single_bounded(session, cypher, label, **params)` — for a `.single()` read
  (one row guaranteed).
- `_data_bounded(session, cypher, label, **params)` — for a `.data()` read
  (0-N rows).

Both wrap the Cypher in `neo4j.Query(text, timeout=NEO4J_QUERY_TIMEOUT_SECONDS)`
(default 30s) and convert a tx-timeout `ClientError` into `OrmQueryTimeout` via
`_nonorm_timeout(label)`. `label` is a short noun phrase describing WHAT is being
resolved (e.g. `"core API symbol 'sale.order' (Odoo 17.0)"`) — it is rendered to
the client, so it must never leak raw Cypher.

The boundary catch that turns a *raised* `OrmQueryTimeout` into the clean string
+ the `nonorm_query_timeout_total{tool}` metric is supplied differently by
handler class (next section).

### 2. Four offload decorator variants — division of labour

| Decorator | Pool? | OrmQueryTimeout catch? | Used for |
|---|---|---|---|
| `@offload` | no | NO | sync handlers that do non-Neo4j work (Postgres, on-disk file reads) or that embed on the event loop before the `to_thread` hop. A timeout catch here would be pointless (no Neo4j) or wrong (mislabel another subsystem's error). |
| `@offload_neo4j` | **no (pool-less)** | yes | the read-surface discriminator tools that do single bounded queries or a small fixed multi-query helper — `lookup_core_api`, `api_version_diff`, `find_deprecated_usage`, `lint_check`, `cli_help`, `check_module_exists`, `find_override_point`, `list_available_versions`, `resolve_stylesheet` — PLUS the inspect/overview superset tools (`model_inspect`, `module_inspect`, `describe_module`, `profile_inspect`), whose `_resolve_field` / `_resolve_method` / `_list_*` / `_describe_module` / `_module_dep_closure` helper bodies run the same bounded reads under the decorator. (`entity_lookup` is `async` and self-catches inline — see §3, not this row.) |
| `@offload_bounded` | yes (`ORM` 8-slot) | yes | the 4 ORM-validation tools (`resolve_orm_chain` / `validate_domain` / `validate_depends` / `validate_relation`) whose dense traversals must not drain the Neo4j pool under fan-out (ADR-0048). |
| `@offload_bounded_nonorm` | yes (`NONORM` 8-slot) | yes | non-ORM heavy fan-outs (`impact_analysis`, a 6-query fan-out) — a SEPARATE pool so one read class cannot starve the other (ADR-0048 G5/G6). |

**Why `@offload_neo4j` is POOL-LESS (no semaphore).** The tools it wraps are
each individually 30s-bounded by the per-query timeout — not the heavy fan-out
drain that the bounded pools were built to contain. Putting them behind the
8-slot non-ORM pool would create a NEW starvation surface and make them newly
emit the "server busy" string (a client-visible wire change these tools never
had). Concurrency containment is already provided by the per-query bound +
uvicorn `limit_concurrency` + the shared `to_thread` executor. A future profiling
pass can PROMOTE any single tool to `@offload_bounded_nonorm` with a one-line
decorator swap if it turns out to be a genuine fan-out drain.

The `@offload_neo4j` metric is recorded IN-THREAD (so a coroutine cancelled by a
client disconnect still counts it — the #276 cancel-path invariant), then the
exception re-raises to the async wrapper which only *returns* `user_message` (no
re-record) — so the metric fires exactly once.

### 3. Inline catch for async EMBED + mutating tools

The 3 EMBED tools (`suggest_pattern`, `find_examples`, `find_style_override`) are
`async def` that embed the query on the event loop BEFORE `asyncio.to_thread`, so
no sync-body decorator wraps the Neo4j read; the mutating `set_active_version`
runs under bare `@offload` (which has no timeout catch). Each therefore catches
`OrmQueryTimeout` inline in its own body. That two-line body
(`_metric_nonorm_query_timeout(tool); return exc.user_message`) is consolidated
into `_nonorm_timeout_response(exc, tool)` in `server.py` — every inline catch is
now `return _srv._nonorm_timeout_response(exc, "<tool>")`. The
`except OrmQueryTimeout as exc:` handler is preserved verbatim at each site (only
the body collapses) so the per-read structural guard
(`tests/test_resolve_timeout_guard.py`) still sees a timeout-catching `try`.

`set_active_version` keeps its `except OrmQueryTimeout` ordered BEFORE the
existing `except Exception` catch-all, so the timeout is distinguished (counted +
clean string) rather than swallowed by the generic handler.

### 4. Resource path — anti-poison `_reraise_timeout` + single counting site

The 7 `odoo://` resource handlers must not cache a transient timeout body. The
shared renderers accept `_reraise_timeout=True`, which RE-RAISES `OrmQueryTimeout`
out of `ResourceCache.get_or_compute` BEFORE the LRU put (no-poison). The resolver
(`_resolve_model` / `_resolve_field` / `_resolve_method`) therefore does NOT count
the re-raised timeout — the resource handler is the single counting site. The 7
handlers are consolidated behind one async module-level helper
`_serve_resource_with_metric(cache, version, kind, entity, render_fn, *,
metric_tool, tenant_keyed=True)` (the name `_serve_resource_blocking` was already
taken). Its `from src.mcp import server as _srv` import is kept INSIDE the catch
(lazy) to avoid a circular import at module load. `metric_tool` maps:
model/field/method → `model_inspect`, module → `module_inspect`, view →
`entity_lookup`, pattern → `suggest_pattern` (the only `tenant_keyed=False` kind,
since patterns are global spec data), stylesheet → `resolve_stylesheet`.

### 5. Metric uniformity (M1-M4) — no double-count

The tool path of three resolver/list functions previously returned the clean
string WITHOUT recording the metric (the `@offload_neo4j` backstop only counts a
*raised* timeout, and these return a string from an inherited/list fallback). M1
`_resolve_field` inherited-fallback, M2 `_resolve_method`, M3 `_list_fields`,
M4 `_list_methods` now emit `_metric_nonorm_query_timeout("model_inspect")` on the
tool path. M1/M2 place the metric AFTER `if _reraise_timeout: raise`, so the
resource path (which re-raises and counts in its handler) never double-counts.
M3/M4 are list-path only — no resource reaches them, so there is no double-count
risk. All four use the `"model_inspect"` label (issue #287 decision).

### Metric

A single counter `nonorm_query_timeout_total{tool}` records every non-ORM read
timeout, labelled by the public tool name, so ops can distinguish which read
class is hitting the per-query Neo4j timeout (separate from
`orm_query_timeout_total`). Exactly-once accounting is the invariant: in-thread
for the decorator path, single-site for the resource path, single helper call for
the inline path.

### Why not the alternatives

- **Put `@offload_neo4j` tools behind a bounded pool.** Rejected: see §2 — it adds
  a starvation surface and a client-visible "server busy" wire change for tools
  that are already 30s-bounded and are not fan-out drains.
- **One catch in `@offload` covering both Neo4j and non-Neo4j work.** Rejected:
  it would mislabel or swallow a timeout from a different subsystem (Postgres,
  file read) — `@offload_neo4j` is intentionally separate, not a retrofit.
- **Cache the timeout body like any other resource result.** Rejected: a 30s blip
  would pin a stale error for the full 300s TTL — hence `_reraise_timeout`.

## Consequences

**Positive.** Every read in the surface (25 tools + 7 resources + the mutating
session pin + the dead-wired discovery index) is bounded at the query and caught
at the boundary; a Neo4j timeout always returns a clean ADR-0023 string. Resource
bodies are never poisoned by a transient timeout. One Prometheus counter gives
per-tool timeout visibility. Tool count stays 25, resource count stays 7, no
migration.

**Negative (accepted).** `@offload_neo4j` is pool-less, so a pathological fan-out
of one of its tools is bounded only by the 30s per-query timeout + uvicorn
`limit_concurrency`, not by a dedicated semaphore. Mitigation: the one-line
promotion to `@offload_bounded_nonorm` is available if profiling shows a genuine
drain. The `_resolve_model` ranking query remains a bare `session.run()` (an open
follow-up noted in ADR-0048) — out of scope for #287.

## Revert triggers

- A read tool is observed emitting a protocol `isError` on timeout → a site was
  missed; the structural guard `tests/test_resolve_timeout_guard.py` should have
  caught it (extend the guard, do not loosen it).
- `@offload_neo4j` is profiled as a fan-out drain for a specific tool → promote
  that ONE tool to `@offload_bounded_nonorm` (one-line decorator swap), do not
  pool the whole variant.
