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

- **R3 (`_resolve_model`):** explicitly filters `WHERE p.name <> $name` — same-name edges are
  discarded at read time. (The former `_resolve_model_structured` dual-channel companion shared this
  filter; it was physically removed with the retired structured subsystem under ADR-0028 — PR #284.)
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

> **SUPERSEDED by the D5 amendment below (zero-warning indexer wave, PR #345).** The deferred
> decision is now MADE: W2 (cross-name INHERITS) and W4 (DELEGATES_TO) collapse to a single
> prefer-definition target. See "D5 amendment — cross-name INHERITS (W2) + DELEGATES_TO (W4) now
> collapse to a single prefer-definition target" at the end of this file.

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

## Amendment - Read-side list/detail now inheritance-aware (fields INHERITS|DELEGATES_TO; methods INHERITS-only)

**Date:** 2026-06-11

### Background

After the D2 per-hop name-dedup rewrite made the four ORM-validation tools
(`resolve_orm_chain` / `validate_domain` / `validate_depends` / `validate_relation`)
correctly traverse INHERITS edges via `_lookup_field` (orm.py), a read-side
asymmetry remained: `model_inspect(method='fields')`, `entity_lookup(kind='field')`,
`model_inspect(method='methods')`, and `find_override_point` all used flat exact-match
queries (`MATCH (f:Field {model: $m, ...})`) that could not see fields or methods
declared on mixin ancestors.

Live confirmation: `entity_lookup(kind='field', model='viin.approval.request', field='res_ref')`
returned "not found" while `resolve_orm_chain('viin.approval.request', 'res_ref')`
resolved the same field correctly. The field was indexed under
`Field.model='abstract.approval.request.fields'` (one INHERITS hop from the child) -
data was correct, only the read path was wrong.

### Fix

The read-side list/detail helpers (`_list_fields`, `_resolve_field`, `_list_methods`,
`_resolve_method` in `server.py`) now use the same **per-hop name-dedup depth-3** shape
that `_lookup_field` (orm.py) established in D2:

1. **Shape reused verbatim** from `_lookup_field` (orm.py:210-251): per-hop `collect(DISTINCT
   hX.name)` aggregates ancestors by name before each expansion; `hX.name <> prev` pruning
   ensures the BFS never re-enters a same-name mesh; `_bounded()` 30s driver timeout applies
   to all traversal queries. No VLP (`*1..N`) introduced. This is the SSOT shape that
   eliminated the 86M-path enumeration (D2/D8 amendment).

2. **Depth cap = 3** (same as `_lookup_field`). Live verification: `res_ref` resolves at
   depth-1; `analytic_distribution` resolves at depth-2 (child -> abstract -> analytic.mixin);
   `analytic.mixin` itself has no further `Inherits from` chain, confirming depth-3 provides
   one full hop of headroom for the deepest known production mixin stack.

3. **Field-name dedup** (depth-first, nearest ancestor wins): when a field exists on both the
   child model (depth 0) and an ancestor, the child's version is returned. This matches the
   Odoo runtime override semantic and is consistent with `_lookup_field`'s tiebreak order
   (`depth ASC, pn ASC, f.module ASC`).

4. **DELEGATES_TO included — FIELDS ONLY** (same as `_lookup_field`): fields exposed through
   `_inherits` delegation (e.g., `res.users` delegating to `res.partner`) are now visible in
   list and detail. This is an intentional semantic expansion matching the ORM-validation tools.
   Provenance labels distinguish the two traversal kinds (see ADR-0023 amendment below).

5. **Summary count INHERITS-aware**: the `Fields: N` and `Methods: N` lines in
   `model_inspect(method='summary')` previously came from a flat exact-match COUNT and would
   diverge from the paginated list after this fix. Both counts now use the same traversal +
   name dedup before counting, keeping summary and list consistent (fields traverse
   INHERITS|DELEGATES_TO; methods traverse INHERITS only — see point 6).

6. **Method symmetry — but INHERITS ONLY** (correction, 2026-06-11): `_list_methods` and
   `_resolve_method` receive the same shape fix as the field path. Live confirmation:
   `model_inspect(method='methods', method_name='_compute_res_ref')` on `viin.approval.request`
   returned "not found" before this fix; methods declared on mixin ancestors are now listed and
   resolvable. `find_override_point` resolves via `_resolve_method` and inherits the fix without
   code changes. **However, methods are inherited via `INHERITS` ONLY — they are NOT carried by
   `_inherits`/DELEGATES_TO delegation.** Python MRO inherits methods through `_inherit`, but
   `_inherits` delegation gives the child the parent's FIELDS ONLY (related proxy on a separate
   table); methods are NEVER forwarded (unanimous across Odoo v8→v19; v9 core `orm.rst:942-943`
   states fields-only explicitly). The earlier draft of this amendment described the method path
   as "INHERITS|DELEGATES_TO-aware" — that was WRONG and would have advertised every method of a
   delegated parent (e.g. every `res.partner` method on `res.users`) as inherited on the child,
   active misinformation to an AI client. Corrected: the method helpers
   (`_list_methods_with_inherited` / `_count_methods_with_inherited` / `_resolve_method_inherited`)
   use a dedicated `_ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY` (built by `_ancestor_tagged_prologue("INHERITS")`);
   the field helpers keep `_ANCESTOR_TAGGED_PROLOGUE` (built with `"INHERITS|DELEGATES_TO"`). On
   the method path `edge_kind` is always `inherits` and there is no delegation label.

7. **Override marker / chain on inherited methods** (2026-06-11): the `(*)` override marker in
   `_list_methods` previously counted distinct modules per method name on the CHILD model only,
   so an inherited method (whose owner is a mixin) was never marked even when overridden N times
   on its owner. It now computes the override set per `(method_name, owner_model)` over the same
   INHERITS-only ancestor set (NOT DELEGATES_TO), keyed by owner so a same-named method on two
   different owners cannot cross-contaminate the marker. Correspondingly, the inherited-method
   DETAIL (`_resolve_method` → `_render_inherited_method`) previously printed a hardcoded
   `Override chain (1)` naming only the owner's single declaring module; it now renders the REAL
   multi-module override chain on the owner model, using the same ADR-0013 5-tier ranking as own
   methods (extracted to the shared `_method_override_chain` helper, SSOT for the ranking).

8. **Code-review hardening (review #283, 2026-06-11):** a follow-up review of PR #283 closed six
   findings without changing any read semantics:

   - **`_method_override_chain` is now bounded (FIX-1, availability).** It previously ran a raw
     `session.run(...).data()` with NO `_bounded()` wrapper and NO tx-timeout mapping — the exact
     unbounded INHERITS/COUNT-heavy query class #273/#276 closed elsewhere, and it is called up to
     2× per inherited-method resolve (own + GAP-3 owner chain). It now wraps the query text in
     `_bounded()` and maps a tx-timeout `ClientError` to `OrmQueryTimeout` via the shared
     `_is_tx_timeout` gate. Its callers `_resolve_method` (own + inherited fallback) and
     `_resolve_field` (inherited fallback) now catch `OrmQueryTimeout` and return its
     `user_message` as a clean ADR-0023 string — because `model_inspect` / `entity_lookup` wrap
     these handlers in plain `@offload` (which, unlike `@offload_bounded`, does not catch the
     timeout), an uncaught raise would otherwise surface as a protocol-level 500.
   - **Magic-field dedup BFS deduplicated + crash-safe (FIX-2).** The inline 3-hop BFS in
     `_list_fields` (magic-name dedup) was a hand-rolled re-implementation of the field-listing
     prologue and, although `_bounded()`, had no `try/except` — a tx-timeout `ClientError` crashed
     the whole list. It now calls the new shared `_ancestor_owner_names(model, version, session,
     profile)` helper (orm.py — same `_ANCESTOR_TAGGED_PROLOGUE`, `_bounded`, `ClientError →
     OrmQueryTimeout`) and on timeout degrades to a flat own-model magic dedup rather than
     crashing.
   - **Inherited-entity next-step hints target the OWNER model (FIX-3).** `_render_inherited_field`
     and `_render_inherited_method` previously keyed their `impact_analysis` /
     `find_override_point` / `find_examples` drill-down hints by the CHILD model. The field/method
     NODE lives on the owner (the mixin), where `impact_analysis` flat-matches it — so a
     child-keyed hint returned an EMPTY blast radius and misled the agent into "no impact". The
     hints are now keyed by `owner` (the model the entity is actually declared on); the tree
     header still names the child (what the user asked about).
   - **Provenance wording SSOT (FIX-6) + edition-rank SSOT (FIX-5).** The list-row provenance token
     wording (`inherited from …` / `delegated via … (separate table, fields-only)`) is now a single
     `_provenance_token(owner_model, model, edge_kind, via_field)` helper called from both
     `_fmt_field_row` and `_fmt_method` (grammar byte-identical to before). The duplicate
     `_edition_rank_cypher` definition in `server.py` was deleted; `server.py` now imports the
     `orm.py` copy (the documented SSOT, sourced from `EDITION_PRIORITY`).

### No reindex required

INHERITS edges and Field/Method nodes were already correctly indexed - the gap was
purely in the read queries. No Postgres migration. No changes to
`parser_python.py` or `writer_neo4j.py`.

### Performance account

The list-gom query is heavier than a flat exact-match (3 per-hop traversals + dedup
before SKIP/LIMIT). Mitigation:

- The per-hop prune+aggregate shape (D2 structural fix) bounds each hop to one pass
  over the distinct ancestor NAME set (typically <=16 names on production), not K anchor
  rows. Measured: 443ms on K=120 un-cleaned same-name mesh for a single-field lookup.
- The two Ops indexes recommended in this ADR's Ops Notes (`model_name_version_idx`,
  `field_model_version_idx`) directly serve the ancestor expansion and the Field join,
  respectively. These should be present before deploying this read-side change on models
  with deep inheritance (e.g., `account.move`, `sale.order`).
- For `_resolve_field` and `_resolve_method` (single-entity detail), an exact-first fast
  path is tried first; the traversal query runs only on a MISS. Native fields (depth-0)
  are unaffected in latency.

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

## Amendment - D5 decision MADE: cross-name INHERITS (W2) + DELEGATES_TO (W4) now collapse to a single prefer-definition target (zero-warning indexer wave, PR #345)

D5 originally LEFT W2 (cross-name mixin fan-out to all K_parent copies) and W4 (DELEGATES_TO fan-out
to all K_target copies) unchanged, calling "anchoring W2/W4 to definition-only ... a separate design
decision deferred to a future ADR." This amendment records that the deferred decision is now MADE in
the zero-warning indexer wave.

**Decision:** when an extender's cross-name parent (W2, e.g. `mail.thread`) or a delegated target
(W4) resolves to multiple same-name Model nodes (the C1 K-per-module schema), the writer now collapses
to exactly ONE target via:

```cypher
WHERE NOT coalesce(parent.unresolved, false)
WITH m, parent
ORDER BY coalesce(parent.is_definition, false) DESC,
         coalesce(parent.field_count, 0) DESC, parent.module ASC
LIMIT 1
```

(Implemented in `src/indexer/writer_neo4j_orm.py` for both the cross-name INHERITS branch and the
DELEGATES_TO branch.)

**Why now, and why this exact form:**

1. **Kills the `.single()` multi-row warning.** With D > 1 (a fork + upstream both indexed, D6), the
   prior fan-out wrote K×D edges and a `.single()` read over the parent set raised a multi-row
   warning. `LIMIT 1` makes the lookup single-row by construction — silencing the warning AND removing
   the spurious extra K-edges that added no reachability (the same class of redundant edge the D2
   same-name dedup already eliminated for W1).

2. **PREFER, not REQUIRE, `is_definition`.** A hard `WHERE parent.is_definition = true` filter was
   tried and REJECTED: a mixin / AbstractModel parent (e.g. `mail.thread`) has NO `is_definition=true`
   node — it is injected via `_inherit`, never self-declared with an explicit `_name` outside its own
   inherit list — so a hard filter returned 0 rows and DROPPED the edge, making the parent disappear
   (`purchase.order` losing `mail.thread`; `sale.order.message_ids` becoming unresolvable). The
   ordering form drops only placeholders (`NOT unresolved`), then ranks `is_definition DESC` first so
   the canonical definition wins WHEN ONE EXISTS, and falls through to the best non-definition node
   (the mixin) otherwise. It therefore NEVER drops a legitimate parent while still collapsing to one
   target. `field_count` is not a stored property here (coalesces to 0), so the effective tiebreak
   after `is_definition` is `module ASC` — deterministic.

3. **Relationship to D6 (K×D accepted when D > 1).** D6 described W1 (same-name extender → definition)
   where K×D is the accepted, deterministic outcome. This amendment narrows W2/W4 (cross-name and
   delegate edges) to a SINGLE prefer-definition target — the two are not in conflict: D6 still governs
   same-name INHERITS; W2/W4 now anchor to one canonical target as D5 deferred.

**Read-side impact:** none beyond fewer redundant edges. The ADR-0048 per-hop name-dedup ORM read
already deduplicates by name at each hop, so collapsing the write-side fan-out to one target does not
change resolvability — it removes write-time noise and the `.single()` warning. CI is green; this
amendment is the docs/governance reconciliation for the already-landed, already-correct writer Cypher.
