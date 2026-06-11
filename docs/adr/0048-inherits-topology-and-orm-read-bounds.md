# ADR-0048 — INHERITS Topology: K×D extender→definition + ORM read bounds (#271 #273)

**Status:** Accepted
**Date:** 2026-06-10
**Authors:** Engineering team
**Related:** ADR-0013 (defined-in ranking heuristic — ranking is INDEPENDENT of this ADR),
  ADR-0023 (tool output completeness — banner V0.5 per §D9), ADR-0002 (spec schema policy —
  3-tier disclosure enforcement per §D9), ADR-0046 (MCP embed concurrency — semaphore pattern
  mirrored by D7)

---

## Context

### Issue #273 — ORM tools hung indefinitely on dense inheritance graphs (11 zombie transactions)

Four ORM tools (`resolve_orm_chain` / `validate_domain` / `validate_depends` / `validate_relation`)
were observed hanging in production, producing 11 zombie Neo4j transactions running 19-24 hours.

Root cause was three-layered:

**Layer 1 — K² same-name INHERITS mesh in the graph.**
`writer_neo4j.py` step W1 (self-extend same-name) issued a MATCH on all Model nodes sharing the
same `(name, odoo_version)` without requiring the target to be the definition node. With K copies of
`sale.order` at v17.0 (one per extending module), this produced K×(K-1) directed edges — a complete
directed graph on K vertices — instead of the intended K×D edges (extender → definition). Measured
prod values: ~256k same-name INHERITS edges per version, dominated by sale.order (K≈97 at v17.0;
product.template is K≈87). Per issue #273.

**Layer 2 — All-K anchor + VLP before LIMIT = full path enumeration.**
`orm.py` `_lookup_field` step-3 (field fallback on inherited models) used
`(start)-[:INHERITS|DELEGATES_TO*1..3]->(parent)` with `start` anchored on ALL K copies of the
model, then `ORDER BY parent.name ASC, f.module ASC` BEFORE `LIMIT 1`. On the K² mesh, Neo4j was
forced to enumerate all reachable paths before it could sort — 86M paths measured in production.
`validate_relation` step used `*1..5` on the same mesh with an existence check (MISMATCH case must
exhaust all paths before concluding "not a subtype").

**Layer 3 — No timeout at any level.**
The driver made no per-query timeout call; the server `db.transaction.timeout` was unset (no global
backstop either). One call from a fan-out AI agent could occupy a Neo4j thread for hours.

### Issue #271 — `lint_check` false-green on SQL injection

`lint_check` V0 used a token-overlap matcher: count of shared vocabulary tokens between the security
rule description and the submitted code fragment. SQL injection rules (W8140 `cr.execute` raw SQL,
E8501 from live pylint-odoo) had no overlap with typical violation code — the message vocabulary
("SQL injection", "parameterized") never appears in `cr.execute('SELECT %s' % val)` — so the
matcher permanently returned zero violations for the highest-severity class of rules.

Secondary bug: when no LintRule nodes were indexed for the requested version (e.g., version not yet
indexed via `index-core`), the tool returned an empty tree with no disclosure — a silent false-green
("no violations" when the truth was "no data").

---

## Decisions

### D1 — Same-name INHERITS topology: K×D edges (extender → definition only)

The W1 self-extend MATCH in `writer_neo4j.py` now requires the target tip node to have
`coalesce(tip.is_definition, false) = true`. This changes the emitted topology from K×(K-1)
(complete directed graph on all copies) to K×D (each extender connects only to definition copies,
where D is typically 1).

**Evidence that no consumer needed the K² mesh (verified grep + code review):**

- **R3/R4 (`_resolve_model`, `_resolve_model_structured`):** explicitly filter `WHERE p.name <> $name`
  — same-name edges are discarded at read time.
- **R1/R2 (orm.py step-3 and validate_relation):** same-name edges added no reachability (any
  cross-name parent is already reachable directly from the copy that declared it in `_inherit`);
  they only multiplied path count, causing bug #273.
- **ADR-0013 ranking heuristic:** uses `is_definition` node property + `field_count` +
  `COUNT { ()-[:DEPENDS_ON]->(mod) }` — no INHERITS edge traversal at all. Confirmed: reducing K²
  → K×D does not change ranking results.
- **`r.order` property (MRO future use):** has no read-consumer today (grep confirmed). With K×D
  topology, each extender still holds exactly one same-name edge carrying its own `r.order` value
  (position in `_inherit` list). MRO information is fully preserved — in fact, cleaner, since the
  K² mesh stamped the same order value on K-1 redundant edges.

**D > 1 accepted:** When multiple repos index the same (name, odoo_version) as definition (e.g.,
a fork and its upstream), K×D edges are produced. This is correct and deterministic; D is rarely
> 1 in practice.

### D2 — Read-side: per-hop name-dedup with depth-first semantics (formally adopted)

`_lookup_field` step-3 replaces the VLP `*1..3` (all-K anchor + ORDER BY before LIMIT) with a
CALL subquery that deduplicates ancestor names per hop, collecting only the minimum-depth occurrence
of each parent name, then joins Field on the deduplicated set.

**Depth-first semantics are now the official contract:** the nearest ancestor (minimum hop count)
wins; within the same depth the tiebreak is parent model name ASC, then the field's owning module
ASC (`ORDER BY depth ASC, pn ASC, f.module ASC`). The previous ORDER BY
`parent.name ASC` across all paths was an implementation accident, not a designed contract — it
required full path enumeration to sort, and on models where no field existed (the `validate_*` use
case) it had to exhaust all 86M paths before concluding "not found".

`validate_relation` subtype-check retains depth 5 (`*1..5`) but with the same per-hop name-dedup
shape. Note (L9): the subtype check is name-level — the scope predicate on the target is satisfied
by ANY same-name copy of the comodel that passes the tenant choke, not necessarily the specific
node reached along the matched path. With the fan-out to all-K same-name copies this is observably
equivalent; it only differs in the vanishingly rare case where same-name duplicates carry divergent
profile arrays.

### D3 — Per-hop unresolved filter (deliberate tightening)

The new query filters `NOT coalesce(<node>.unresolved, false)` on EVERY intermediate hop node, not
only the terminal node as before. This is an intentional tightening:

- `__unresolved__` placeholder nodes (created by W3 when a cross-model parent is not yet indexed)
  have no outgoing INHERITS edges by design — paths "through" them are unreachable in practice.
- `gc_unresolved_placeholders` removes them periodically, making any reachability through them
  non-deterministic across runs. The tighter filter makes behavior deterministic.
- This must be verified during Wave 0 ops on production data (expected 0): count valid paths that
  traverse an `unresolved` intermediate node before the read rewrite is relied upon. (Wave 0 runbook
  is pending operator confirmation at the time of this PR — the count has NOT yet been taken.)

### D4 — Post-pass reconciliation replaces write-order dependency

A set-based Cypher reconciliation pass runs unconditionally at the end of each `index_repo` /
`index_profile` run (adjacent to `gc_unresolved_placeholders` in `pipeline.py`). It creates any
extender→definition same-name edges that were missed because the extender module was indexed before
the definition module (cross-repo write-order gap). The pass is idempotent (MERGE) and
version-scoped.

**Why not a placeholder:** A `__unresolved__` placeholder for self-extend (mirroring W3 for
cross-model parents) would be consistent in pattern but increases placeholder churn and GC work
for an edge that no reader currently needs. Post-pass is richer (builds the correct edge once data
is available) with less graph noise.

**Deviation from initial plan (WI-5 merge order):** During implementation, the live-parse path
for lint rules was found to be first-write-wins (not last-write-wins as the initial plan assumed),
because `_add`/`seen` set deduplication occurs before the static merge loop. The remedy was a
`_apply_code_patterns_overlay` post-pass that patches `code_pattern` from static JSON onto ALL
rules after the merge loop completes, regardless of which source won the dedup race. The static
JSON remains SSOT for patterns; the overlay propagates patterns even to live-parse winners.

### D5 — W2 (cross-model INHERITS fan-out) and W4 (DELEGATES_TO) unchanged

Both fan-out patterns (W2 = cross-name mixin fan-out to all K_parent copies; W4 = DELEGATES_TO
fan-out to all K_target copies) are left unchanged in this wave. After the D2 per-hop name-dedup
rewrite, neither fan-out is harmful to query performance (each hop only deduplicates by name, not
by module). Anchoring W2/W4 to definition-only is a separate design decision deferred to a future
ADR.

### D6 — K×D accepted when D > 1

If multiple repos independently define the same (model_name, odoo_version) — e.g., a community
fork and its upstream both indexed in the same profile — each extender will connect to D definition
nodes (K×D edges total). This is accepted: the behavior is deterministic, both definitions are
visible in the graph, and the ranking heuristic (ADR-0013) selects the canonical one independently.

### D7 — Two-tier timeout: driver-side 30s per ORM query + ops db.transaction.timeout 600s

**Driver-side (per-ORM-query):** All 5 read call-sites in `orm.py` wrap their Cypher text with
`neo4j.Query(text, timeout=NEO4J_QUERY_TIMEOUT_SECONDS)` (default 30s, env-configurable). On
timeout, the driver raises `ClientError` with status code matching
`...Transaction.TransactionTimedOut` (prefix-matched to cover both the driver-side
`TransactionTimedOutClientConfiguration` variant and the server-side `TransactionTimedOut` variant).
The exception is caught and surfaced as `OrmQueryTimeout(user_message)` — English, ADR-0023 tone,
no Cypher text leaked.

**Ops-side (global Neo4j backstop):** `db.transaction.timeout` should be set to **600s** (not 60s)
via `CALL dbms.setConfigValue('db.transaction.timeout','600s')` and persisted in `neo4j.conf`.
The 60s value was considered and rejected: it would kill indexer transactions (`delete_modules_scoped`,
`gc_stale_modules`, `_write_parse_result`) that legitimately exceed 60s on large repos.
600s kills zombie ORM hangs (19-24h) while leaving indexer headroom.

**Semaphore (pool-drain guard):** An `asyncio.Semaphore(ORM_QUERY_MAX_CONCURRENCY)` (default 8)
wraps the four ORM tool wrappers in `server.py` via the `offload_bounded` decorator, mirroring the
embed concurrency pattern (ADR-0046). Slot release is tied to worker-thread completion, not
coroutine cancellation, to avoid the release-ordering trap: if a slot were released on `wait_for`
cancel while the Neo4j thread still runs, the cap would be silently violated during the 30s drain
window.

### D8 — Rollout safety matrix

All code/data combinations are safe except the pre-fix baseline:

| Code version | Data (INHERITS topology) | Safe? | Notes |
|---|---|---|---|
| Old (VLP all-K) | Old (K² mesh) | **NO** | Bug #273 — current prod before fix |
| New (per-hop dedup) | Old (K² mesh) | YES | Dedup still works; K² adds no hop count |
| Old (VLP all-K) | New (K×D, cleanup run) | YES | Fewer paths; old query is fast on clean data |
| New (per-hop dedup) | New (K×D, cleanup run) | YES | Optimal — intended end state |

The cleanup script (`ops/cleanup_same_name_inherits_mesh.cypher`) is the only irreversible step.
A backup bundle (ADR-0018: `postgres.sql + neo4j.dump`) must be taken immediately before running
the script. Rollback path: restore bundle, redeploy old code (old code on clean data is safe per
the matrix above).

### D9 — Lint: pattern-first hybrid (V0.5) + 3-tier disclosure (ADR-0002 §4 enforcement)

**Matcher:** `lint_check` now uses a pattern-first hybrid (V0.5). When a `code_pattern` (regex) is
present on a LintRule node, per-line `re.search` runs first. If it matches, the violation is labeled
`[pattern]`. If no `code_pattern` exists, the fuzzy token-overlap fallback runs and labels the
violation `[fuzzy]`. The banner wording is updated to "Hybrid matcher (V0.5)". The `noqa` per-line
suppression mechanism continues to work for both match kinds.

**Data:** 12 lint_rules_*.json files now carry `code_pattern` (regex string or null) for rules in
the mechanical/security group. A `_apply_code_patterns_overlay` post-pass ensures static JSON
patterns propagate even to live-parse rule winners (see D4 deviation note above).

**3-tier disclosure (ADR-0002 §4):**
- Tier 1: `rules == []` OR `curate_status is None` → hard disclosure: "no lint rules indexed for
  {version} — result is NOT a clean bill of health." This is the fix for the silent false-green bug.
- Tier 2: `curate_status == 'pending'` with rules present → soft banner (existing "limited results"
  wording preserved).
- Tier 3: `curate_status == 'complete'` → normal output, no banner.

**V1 direction (deferred):** A future V1 engine would run real pylint-odoo per-era in an isolated
subprocess (3 pinned venv environments for era1/era2/era3, sandbox via `setrlimit` + no-network,
a `noqa` ↔ rule-id two-way ID mapping, and a pin-matrix HTTP endpoint). This requires a separate
ADR (infra design for 3-venv lifecycle + security policy for `setrlimit` sandbox). V0.5 "lint_check
is a hint" stance is maintained until V1 ships.

### D10 — Cleanup mesh: targeted script only (ops/cleanup_same_name_inherits_mesh.cypher)

Full reindex does NOT remove the existing K² mesh: writer uses additive MERGE — no code path deletes
INHERITS edges between live nodes. The targeted script performs two batched steps. The correct batch
shape is an OUTER driving `MATCH` followed by `CALL { WITH <row> ... } IN TRANSACTIONS OF n ROWS`
(`IN TRANSACTIONS` splits the INPUT rows of the outer query — a `MATCH` placed INSIDE the `CALL`
with no outer driving clause would run everything in a SINGLE transaction, defeating batching; the
same shape is used by `delete_modules_scoped` in `writer_neo4j.py`):

1. **Backfill** (outer `MATCH` of (extender, definition, order) rows → `CALL { WITH ... MERGE ... }
   IN TRANSACTIONS OF 10000 ROWS`): for each extender missing the edge, create the correct
   extender→definition edge (MERGE, idempotent), copying `r.order` from the best existing same-name
   out-edge.
2. **Delete mesh** (outer `MATCH (a)-[r:INHERITS]->(b)` of mesh rows → `CALL { WITH r DELETE r }
   IN TRANSACTIONS OF 10000 ROWS`): remove all same-name INHERITS edges whose target is not a
   definition node.

Both steps batch the INNER transactions to respect `db.transaction.timeout=600s`. The script header
documents the interaction with `db.transaction.timeout` and mandates a backup bundle (ADR-0018)
before execution.

**Outer-tx timeout (M6, verified Neo4j 5.26.25, 2026-06-10):** batching bounds each INNER
transaction, but the OUTER coordinating transaction of `CALL IN TRANSACTIONS` IS itself subject to
`db.transaction.timeout`. Empirically, with `db.transaction.timeout = 3s` a batched run whose total
elapsed reached ~4s was terminated mid-run
(`Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration`); already-committed inner
batches persisted, the in-flight batch rolled back. Therefore a full ~1.1M-edge cleanup whose total
wall-clock exceeds the configured timeout will have its outer tx killed part-way. The script is
idempotent (a re-run resumes), but to complete in one pass the operator MUST raise or disable the
timeout first (`CALL dbms.setConfigValue('db.transaction.timeout','0')`, re-enable after) — Option A
in the script header. The same caveat applies to `delete_modules_scoped` for very large repo
deletes (now documented in its docstring).

---

## Consequences

**Positive:**
- ORM tool queries complete in 0.47-0.97s (measured on prod-scale graph, same-name per-hop dedup
  eliminates the 86M path enumeration).
- Lint SQL injection rules now fire correctly; `[pattern]`/`[fuzzy]` labels make matcher confidence
  visible to AI agents.
- Silent empty-index false-green eliminated (Tier-1 disclosure).
- Graph cleaner: ~1.1M redundant same-name INHERITS edges removed (after cleanup script).

**Behavior changes (flagged):**
- `resolve_orm_chain` and related tools: field resolution semantics change from "alphabetical across
  all paths" (implementation accident) to "depth-first, alphabetical tiebreak within depth" (formal
  contract). In practice, the output changes only when the same field name exists on two ancestors at
  different depths with different types — a rare but possible case in heavily-extended models.
- Per-hop unresolved filter: any path through a `__unresolved__` intermediate is no longer
  traversed. Pre-deploy verification showed 0 such paths in production data.
- Lint banner: "V0 fuzzy matcher" → "Hybrid matcher (V0.5)"; violation lines now include
  `[pattern]` or `[fuzzy]` label.
- Empty-version lint: returns a structured "NOT a clean bill" warning instead of empty tree.

**Invariants unchanged:**
- Tool count: **25** (no new MCP tools).
- No Postgres migration.
- ADR-0013 ranking heuristic unchanged (independent of INHERITS edge topology).
- ADR-0034 tenant choke preserved in all read paths (scope predicate on Field remains).
- The `r.order` property on INHERITS edges is preserved (each extender→definition edge carries the
  extender's own `_inherit` list position).

---

## Supersedes / Amends

- **ADR-0013** (defined-in ranking heuristic): NOT amended. Ranking is independent of INHERITS
  topology and unchanged by this ADR.
- **ADR-0023** (tool output completeness): banner wording updated to V0.5; 3-tier disclosure
  formally codified. This ADR's §D9 is the authoritative reference for the lint disclosure contract.
- **ADR-0002** (spec schema policy §4 disclosure): D9 enforces the disclosure requirement that was
  previously documented but not fully implemented for empty-version case.

---

## Amendment - PR #275 review round 3 (2026-06-10)

### D8 amendment - "new code x old data" safety re-validated after per-hop pruning fix

The original D8 row "New code x Old (K² mesh): YES - dedup still works" was empirically false.
The first per-hop rewrite applied `pn <> $mn` only at the final WHERE and ran each per-hop CALL
subquery once-per-anchor-row (K=97-237 rows on prod). On the un-cleaned K² mesh:
`sale.order/message_ids` timed out at 25s; `res.config.settings` (K=237) timed out on
nonexistent-field exhaustive-negative. Root cause: (1) hop1 still collected `$mn`-named nodes
(same-name edges), so hop2/hop3 re-expanded the full mesh; (2) the anchor `MATCH (start {name:$mn})`
returns K rows and each per-hop CALL ran once-per-row.

Two structural fixes (PR #275 wi/r3-fix-a):

1. Prune same-name DURING expansion: `h1.name <> $mn`, `h2.name <> pn1`, etc. so the BFS never
   re-enters a same-name mesh node. Lossless on old data: per-hop MATCH re-anchors by NAME on all
   nodes of that name, so anything reachable via a same-name intermediate is already reachable
   directly from the same-name expansion.
2. Aggregate to a SINGLE ROW before each subsequent hop via flat OPTIONAL MATCH +
   `WITH collect(DISTINCT ...)` (replaces CALL subquery - also removes the Neo4j 5.26 deprecation).

Measured on testcontainers K=120 un-cleaned mesh: `_lookup_field` inherited resolve = 443ms;
`validate_relation` MISMATCH (exhaustive-negative) = 109ms. Both << the 5s tripwire.
Reviewer offered to re-run against the production graph before merge.

Updated D8 table:

| Code version | Data (INHERITS topology) | Safe? | Notes |
|---|---|---|---|
| Old (VLP all-K) | Old (K² mesh) | **NO** | Bug #273 - current prod before fix |
| First per-hop cut | Old (K² mesh) | **NO** | CRITICAL-1 empirically proven - 12.6s..TIMEOUT |
| New (per-hop prune+aggregate) | Old (K² mesh) | **YES** | 443ms/109ms measured (K=120); reviewer-validated |
| Old (VLP all-K) | New (K×D, cleanup run) | YES | Fewer paths; old query is fast on clean data |
| New (per-hop prune+aggregate) | New (K×D, cleanup run) | YES | Optimal - intended end state |

Deploy order remains: cleanup AFTER deploy is acceptable. The per-hop pruning makes new code safe
on old data, so the cleanup is a graph hygiene step (removes ~1.1M redundant edges), not a safety
prerequisite.

### D7 amendment - non-ORM session.run posture; thread-held semaphore; isError semantics; env validation

**Non-ORM reads accepted posture (FOLLOW-UP #8 inline comment):** Approximately 84 `session.run`
calls in `server.py` (e.g., `impact_analysis` ~9 queries, `_resolve_model` ranking) are NOT wrapped
with `neo4j.Query(timeout=...)`. This is an accepted, bounded risk:
- neo4j-driver 5.x has no driver/session-level default query timeout; per-call `neo4j.Query` is the
  only lever.
- All these tools run in `@offload` worker threads - they cannot wedge the event loop.
- The global `db.transaction.timeout = 600s` backstops all transactions including these.
- A slow non-ORM traversal can pin a `asyncio.to_thread` pool thread for up to 600s under fan-out
  load, but this is degraded throughput, not a #273-class zombie wedge.

Extending `_bounded()` to the hottest non-ORM read paths is a follow-up item - see TASKS.md.
`impact_analysis` is now done (#278 G5: each heavy read runs through `_data_bounded` /
`_single_bounded`, and the tool is wrapped by `@offload_bounded_nonorm`). The `_resolve_model`
ranking query (`src/mcp/server.py:1941`) remains a bare `session.run()` and is the remaining open
item.

**Thread-held semaphore (CRITICAL-2 fix):** The original D7 text described `asyncio.Semaphore`
with `sem.release()` in the coroutine `finally` block. This was empirically shown to release the
slot on coroutine cancellation (client disconnect) WHILE the worker thread still held the Neo4j
connection - the exact #276 drain pattern the decorator exists to prevent.

Fix (PR #275 wi/r3-fix-b): replaced with `threading.BoundedSemaphore(ORM_QUERY_MAX_CONCURRENCY)`.
Acquire/release run INSIDE the worker thread function, so the slot is tied to thread lifetime, not
coroutine lifetime. Cancellation can no longer free a slot early. `BoundedSemaphore` also turns
any over-release into an immediate `ValueError`. Cancel-path metrics (`orm_query_timeout_total`,
`orm_overloaded_total`) are now incremented in-thread, so cancel-storms are visible in Prometheus.

**isError semantics (MED inline #4):** `OrmOverloaded` is now caught in the async wrapper and
returned as a plain string (uniform with embed `EmbedOverloaded` per ADR-0023 raw-text posture).
`OrmQueryTimeout` was already returned as a string. Both conditions are now consistent: transient
"server busy" surfaces as a structured English string, never as `isError=true`.

**Env fail-fast validation (HIGH #3):** `_validate_orm_env()` called once at `__main__` entry
(post `init_dotenv`, not at import-time). Raises `SystemExit` when:
- `NEO4J_QUERY_TIMEOUT_SECONDS <= 0` (neo4j driver treats 0 as no-timeout, silently reverts #273 fix)
- `ORM_QUERY_MAX_CONCURRENCY <= 0` (every ORM call fast-rejects forever)
- `ORM_SLOT_ACQUIRE_TIMEOUT >= NEO4J_QUERY_TIMEOUT_SECONDS` (reject can never be faster than timeout)

`.env.example` documents these constraints with explicit warnings.

**SSOT knobs moved to constants.py:** `ORM_QUERY_MAX_CONCURRENCY` and `ORM_SLOT_ACQUIRE_TIMEOUT`
moved from inline `os.getenv()` in `server.py` to `src/constants.py` (same pattern as
`NEO4J_QUERY_TIMEOUT_SECONDS` and `EMBEDDER_MAX_CONCURRENCY`).

### D1/D5 note - reconcile_same_name_inherits hoisted to per-version (agent D fix)

The initial implementation ran `reconcile_same_name_inherits` once per `_index_repo` call
(R redundant full-scan passes per profile run). PR #275 wi/r3-fix-d (agent D) hoists this
to run once per version in `index_profile`, after all repos for that version complete. This
reduces cost by R× and makes the deferred `Model(odoo_version)` index less urgent. Concurrent
same-version reconciles from `--profile-workers` may produce MERGE-deadlocks; these are caught
by the warn-and-continue policy and do not leave hard gaps (idempotent post-pass on next run).

### D9 amendment - Tier-1 gate split; merge-order test now locks real order; W8140 tuple form fixed

**Tier-1 gate split (HIGH #1 inline finding):** The original D9 text stated:
"Tier 1: `rules == []` OR `curate_status is None` - hard disclosure". This was wrong for the case
`rules present + curate_status is None` (crash between `write_lint_rules` and `write_spec_metadata`
sessions, or a version indexed before `write_spec_metadata` existed). The hard return suppressed all
real findings with a false "no rules indexed" message.

Fix (PR #275 wi/r3-fix-c): split into two sub-cases:
- `rules == []` alone triggers the hard "NOT a clean bill of health" return.
- `rules present + curate_status is None` runs the matcher AND prepends a distinct soft banner:
  "curation status unknown for Odoo {v} - rules are indexed but SpecMetadata is missing; results
  may be incomplete." This is Tier-1b (distinct from the pending Tier-2 soft banner).

**W8140/E8501 tuple interpolation form now fires (HIGH #2):** The `(?!\()` lookahead after `%\s`
in branch 0 blocked `cr.execute("... %s" % (val,))` - arguably the most common legacy injection
shape. Lookahead removed across all 12 `lint_rules_*.json` files. Must-fire tests added for the
tuple form. The safe parameterized form `cr.execute("... %s", (val,))` remains silent (no
quote-then-`%` operator in that form).

**W8178 multi-line false-positive fixed (MED #3):** Pattern tightened to require `)` on the same
line (`(?=[^)]*\))` lookahead added). Multi-line `fields.Html(` opening lines no longer fire; all
single-line unsanitized `fields.Html(...)` still fire.

**Merge-order test now locks real order (HIGH r3 #5):** The previous overlay test called
`_apply_code_patterns_overlay` directly and did not lock the actual merge order. Replaced with a
test that drives `parse_lint_rules_for_version` with a temp Odoo source tree (live-parse WINS dedup)
and asserts E8501 carries the live-parse rule message BUT the static SSOT `code_pattern` (overlay
applied AFTER). The test fails-red if the overlay is removed or reordered. The "overlay merge order
locked by test" claim in the prior CHANGELOG entry is now accurate (it was previously overstated).

---

## Ops Notes

**Pre-deploy (Wave 0, before code deploy):**
1. Record and terminate 11 zombie transactions: `SHOW TRANSACTIONS` → `TERMINATE TRANSACTION <id>`.
2. Set `db.transaction.timeout=600s`: `CALL dbms.setConfigValue('db.transaction.timeout','600s')` +
   persist in `neo4j.conf`. *(Docker Compose deployments: this is now automatic — see IaC note below.)*
3. Create 2 indexes (idempotent, background population):
   `CREATE INDEX model_name_version_idx IF NOT EXISTS FOR (m:Model) ON (m.name, m.odoo_version)`
   `CREATE INDEX field_model_version_idx IF NOT EXISTS FOR (f:Field) ON (f.model, f.odoo_version)`

**Post-deploy (after writer fix is live):**
4. Take backup bundle (ADR-0018): `postgres.sql + neo4j.dump`.
5. Run `ops/cleanup_same_name_inherits_mesh.cypher` off-peak. Verify counts before/after.
6. Run `index-core` for all 12 versions to populate `code_pattern` on LintRule nodes.
7. Smoke: `resolve_orm_chain("product.product", "categ_id", "17.0")` < 5s;
   `lint_check` on SQL injection snippet → ≥1 `[pattern]` W8140 violation.

**Environment variables added (see docs/operations/timeouts.md):**
- `NEO4J_QUERY_TIMEOUT_SECONDS` (default 30) — per-ORM-query driver timeout.
- `ORM_QUERY_MAX_CONCURRENCY` (default 8) — semaphore cap for ORM tool slots.
- `ORM_SLOT_ACQUIRE_TIMEOUT` (default 5) — fast-reject if slot not acquired within N seconds.

---

## Amendment - IaC wiring of db.transaction.timeout backstop (issue #276)

**Date:** 2026-06-11

The D7 ops recommendation (`db.transaction.timeout=600s` applied manually via `CALL dbms.setConfigValue`
+ `neo4j.conf`) was a pre-deploy ops step. A `docker compose up` or `docker compose recreate`
after the initial apply would silently reset the timeout to `0s` (disabled), reverting the global
backstop and re-exposing the zombie-transaction leak pattern.

**IaC fix:** `NEO4J_db_transaction_timeout=600s` is now set in `docker-compose.yml`
(`services.neo4j.environment`) and mirrored in `.github/workflows/nightly-smoke.yml` (all three
Neo4j service containers: `smoke-real-odoo-17`, `smoke-real-odoo-8`, `recall-benchmark`). Any
compose lifecycle event (up/recreate/pull) now applies the backstop automatically without operator
intervention.

A static test (`tests/test_compose_neo4j_backstop.py`) asserts:
- The `NEO4J_db_transaction_timeout` env key is present in the `neo4j` service block and its
  numeric value (in seconds) exceeds `NEO4J_QUERY_TIMEOUT_SECONDS` (default 30) — enforcing the
  D7 invariant that the global backstop is always larger than the per-query driver timeout.
- An integration test (`@pytest.mark.neo4j`) queries `SHOW SETTINGS` to verify the setting is
  applied by Neo4j at runtime (covers the env-name → config mapping that static parsing cannot).

**Bare-metal / systemd deployments** (no Docker Compose) still require the manual `neo4j.conf`
step documented in the original Ops Notes above. The IaC fix covers Compose-managed instances only.
