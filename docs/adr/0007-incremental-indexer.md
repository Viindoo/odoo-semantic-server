# ADR-0007 — Incremental Indexer (M6 Wave 2)

**Status:** Accepted (2026-05-10)

**Context:** Pre-Wave-2 indexer always full-reindexed every repo on every run (~5-15 min per repo). M6 thesis ("Re-index chỉ mất vài giây") requires skip-unchanged + diff-filter logic. This ADR records the design decisions made while implementing Wave 2 Chain A (5 stacked WIs: W2-1, W2-2, W2-5, W2-3, W2-4) and Chain B (W2-6 + W2-7 auto-reseed).

## Decisions

### D1 — head_sha lives on `repos` table (Postgres), not Neo4j

The "indexer state" (last successful HEAD per repo) is operational metadata, not domain data. Postgres is already the source of truth for the registry (`profiles`, `repos`, `indexer_jobs`, `api_keys`, etc.); Neo4j stores domain knowledge graph.

**Per-module `last_commit_sha`** lives on Neo4j Module nodes — that IS domain data (provenance: "this code came from this commit"). Surfaces in `resolve_model`/etc. as supplementary info. NOT in the MERGE key (mutable SET property per ADR-0001).

Two-table state means:
- repos.head_sha = "the last successful run wrote up to this commit"
- Module.last_commit_sha = "this specific module was last touched at this commit"

These can diverge harmlessly (e.g., repo HEAD advances but only some modules changed → only their `last_commit_sha` advances; repos.head_sha advances to the new HEAD anyway).

### D2 — Force-push fallback via `git merge-base --is-ancestor`

When `repos.head_sha` exists but is not an ancestor of current HEAD, history was rewritten (force-push, rebase, branch reset). The diff `git diff old..new` would either fail or show wrong results.

Behaviour: log warning, treat `last_head` as None → full reindex. The new HEAD becomes the new baseline. Old orphaned Module nodes (for files that no longer exist on the new history line) remain — see D5.

`is_ancestor()` returns False on any git error (graceful) so non-repo, missing-sha, etc. all fall back to full reindex safely.

**Edge case:** When `get_repo_head()` returns None (git error, no commits, detached
without HEAD), the function falls through to a full reindex without advancing
`head_sha` — so the next run repeats the same situation. A warning log surfaces
this state.

### D3 — head_sha advances ONLY after full success

If `_index_repo()` raises mid-write (Neo4j down, parser error, etc.), `head_sha` must NOT update — next run will retry the same diff range (or fall back to full reindex if force-push happened in between).

Implementation: `repo_registry.update_repo_head_sha()` is the LAST statement in `_index_repo()` after all writes/commits succeed. Exception-on-write → never reaches that line → head_sha stays at last successful value.

This is a one-direction state machine: head_sha can only advance, never roll back. Partial-failure is the same as no-progress.

### D4 — `--full` flag is the operator's reset button

The CLI flag `--full` bypasses the skip-unchanged check + the diff filter — forcing a full reindex of every module the scanner finds. This is the documented escape hatch for D5 cleanup.

`--full` does NOT reset head_sha to NULL — at the end of a successful `--full` run, head_sha advances to current HEAD just like an incremental run. Operationally, `--full` is "re-write what we have, then continue normally."

### D5 — Module rename: explicit `--gc` flag available (M7 C4)

When `addons/stock` is renamed to `addons/inventory`, `git diff --name-only` shows both paths as changed. The scanner finds the new `addons/inventory` module → Module node MERGEd; the old `addons/stock` directory no longer exists → no scanner pass → its Module node remains in Neo4j as a stale orphan.

**M7 C4 delivers the `--gc` flag** (`python -m src.indexer index-repo --gc`) that explicitly cleans up stale Module nodes:

```
python -m src.indexer index-repo --profile viindoo_17 --gc
python -m src.indexer index-repo --all --gc
```

Implementation (`gc_stale_modules` in `writer_neo4j.py`):
```cypher
MATCH (m:Module {repo: $repo, odoo_version: $version})
WHERE NOT m.path IN $live_paths
DETACH DELETE m
RETURN count(m) AS n
```

DETACH DELETE removes the Module node plus all incident edges (DEFINED_IN, DEPENDS_ON, etc.). Model/Field/Method nodes that were DEFINED_IN the stale module are NOT deleted by this query — they remain as orphan domain nodes. This is intentional: orphan domain nodes are inert (no DEFINED_IN edge to follow) and will be overwritten if the module re-appears. A future pass may clean these too, but the current scope is Module-level GC only.

**Risk gate:** `--gc` only executes when the scanner returned ≥1 module for the repo+version. If the scanner returns 0 modules (filesystem permission error, empty repo, git error), GC is skipped with a WARNING log line. This prevents accidentally wiping all Module nodes when the scanner fails silently.

**Rename caveat:** when a module dir is renamed (e.g. `addons/stock` → `addons/inventory`), both the old and new path appear in `git diff --name-only`. The scanner re-indexes the new path; `--gc` then detects the old path as absent from `live_paths` and DETACH DELETEs its Module node. Net result: one GC pass cleanly removes the stale orphan.

**Recommendation:** run `--gc` monthly or after any module directory rename. `--full --gc` together is the safest cleanup (forces full re-scan of all modules + removes stale ones).

### D6 — Auto-reseed gated via `_SeedMeta` Neo4j sentinel + sha256

`seed_patterns()` is called at the end of every `index_profile()` so admins don't need to remember a separate command. To make this cheap, the seed function:

1. Computes sha256 of `src/data/patterns.json`.
2. MATCH `(:_SeedMeta {key:'patterns_neo4j'})` to read stored sha.
3. If equal → log "Patterns unchanged — skipping" + return (no Neo4j MERGE on PatternExample, no Ollama embed).
4. Otherwise → run normal seed flow + MERGE sentinel SET sha256=current.

The sentinel is updated AFTER successful seed (same partial-failure rule as D3 — failure preserves last successful sha).

`--force` CLI flag bypasses sentinel for "I edited patterns.json and want to be sure" cases.

Auto-reseed failure inside the indexer is logged at WARN level but does NOT fail the indexer run — pattern catalogue is supplementary; an indexed code graph without fresh patterns is still useful.

### D6-split — Sentinel split into patterns_neo4j and patterns_pgvector (F2 fix)

**Problem (F2):** A `--no-embed` CLI run at 2026-05-11T13:36 updated the single `{key:'patterns'}` sentinel before pgvector was ever populated. Every subsequent auto-reseed call saw "patterns unchanged — skipping." Result: Neo4j had 92 PatternExample nodes, pgvector had 0 pattern embeddings — dual-store divergence.

**Root cause:** The original `--no-embed` path unconditionally updated the sentinel even though only Neo4j was written. The single sentinel could not distinguish "both stores done" from "Neo4j only done."

**Fix:** Split the sentinel into two separate `_SeedMeta` nodes:

- `{key: 'patterns_neo4j'}` — set only after Neo4j PatternExample write succeeds.
- `{key: 'patterns_pgvector'}` — set only after pgvector embed+write succeeds.

**Invariants enforced:**

1. `--no-embed` (or `embedder=None`) writes `patterns_neo4j` after Neo4j succeeds, but NEVER writes `patterns_pgvector`.
2. A subsequent full run (without `--no-embed`) sees `patterns_pgvector` absent → runs the embed step → writes `patterns_pgvector`.
3. Auto-reseed via `run()` checks both sentinels independently — Neo4j and pgvector can be at different states without either blocking the other.
4. `--force` bypasses both sentinels.

**Legacy backward compatibility:** The old `{key:'patterns'}` sentinel (from pre-split deployments) is read as a fallback for the `patterns_neo4j` check only. It is never written by the new code. A `--force` run will migrate the deployment to split sentinels (writes both `patterns_neo4j` and `patterns_pgvector`).

**Operational recovery for F2:**
```bash
# Reset the stale single-key sentinel and repopulate pgvector
python -m src.indexer.seed_patterns --force
```
This bypasses both sentinels, writes all patterns to Neo4j, embeds and writes to pgvector, then sets both split sentinels correctly.

### D7 — `_SeedMeta` label is project-private

Prefixed with `_` to denote internal/operational metadata, distinct from domain labels (Module, Field, View, etc.). No queries from MCP tools. Future internal sentinel needs (e.g., last `index-core` version per CoreSymbol set) can reuse the label with different `key` values.

## Implementation references

- `src/indexer/incremental.py` — D2/D3 helpers (get_repo_head, is_ancestor, compute_changed_module_paths, filter_modules_by_changed)
- `src/indexer/pipeline.py::_index_repo` — D1/D2/D3/D4/D5 wiring
- `src/indexer/seed_patterns.py::_get_stored_patterns_sha` / `_set_stored_patterns_sha` — D6/D7
- `src/indexer/scanner.py::get_module_commit_sha` — D1 per-module sha source
- `src/indexer/writer_neo4j.py` Module MERGE SET — D1 last_commit_sha persistence
- `src/indexer/writer_neo4j.py::Neo4jWriter.gc_stale_modules` — D5 GC implementation
- `src/db/migrate.py` — D1 head_sha column ALTER (per ADR-0001 M6 schema window)

## Tests

- `tests/test_incremental.py` — D2 helpers + force-push fixture
- `tests/test_pipeline_incremental.py` — D3 partial-failure + D4 --full + skip-unchanged + diff filter
- `tests/test_seed_patterns.py` — D6 sentinel hash gating + D7 label + D6-split --no-embed behavior
- `tests/test_pipeline_seed_integration.py` — D6 wired into index_profile
- `tests/test_indexer_gc.py` — D5 GC flag: delete renamed module, risk gate, default-off
- `tests/test_dual_store_integrity.py` — D6-split invariants: --no-embed sets only patterns_neo4j, embedder=None leaves patterns_pgvector absent, legacy sentinel fallback, divergence detection

## Out of scope (recorded for future ADRs)

- Module rename garbage collection (D5 deferred to M7).
- ~~Cross-repo dependency change tracking~~ **Closed in M7 W14** — when an
  incremental run on repo A reports `changed_module_names`, `find_dependent_repos`
  queries Neo4j for Modules in other repos that have `DEPENDS_ON` edges into
  those changed modules, then `reset_head_sha` NULLs their `repos.head_sha` in
  PostgreSQL.  The next indexer run on those repos sees NULL → skips the
  unchanged-check → full reindex.  Implementation:
  `src/indexer/cross_repo.py::find_dependent_repos` +
  `src/db/repo_registry.py::reset_head_sha` +
  `src/db/repo_registry.py::get_repo_ids_by_local_path_basenames` +
  post-write hook in `src/indexer/pipeline.py::_index_repo`.
  Tests: `tests/test_cross_repo_dep_propagation.py`.

  **W14 trade-off notes (M7 closeout):**

  1. *Over-eager re-index* — `pipeline._index_repo` resets ALL downstream repos
     whenever any module changes, even if the change was cosmetic (whitespace,
     comment).  Content-level delta detection (diff Module node properties before
     and after write) would reduce unnecessary re-indexes but adds complexity.
     Over-eager reset is the safe default: graph consistency is guaranteed at the
     cost of extra re-index work.  M8 may add Module-node delta detection.

  2. *Basename collision* — `get_repo_ids_by_local_path_basenames` extracts the
     directory basename from `local_path` using a `regexp_replace` and matches it
     against the `Module.repo` Neo4j property (which also stores only the basename).
     Two repos whose checkout directories share the same final component (e.g.
     `/srv/odoo` and `/home/a/odoo` both have basename `odoo`) will BOTH be reset
     when either is the dependent target.  This is over-eager but safe (extra
     re-index, no data loss).  A proper fix would store the full `local_path` in
     `Module.repo` instead of only the basename — that is a schema change deferred
     to M8 (requires reindex of all repos after migration).  A log WARNING is emitted
     when `reset_head_sha` is called for more than 1 repo matching the same basename
     (see pipeline.py cross-repo dep propagation block).

- Embedding cost analysis: per-module embedding incremental is implicit via `delete_embeddings_for_module` primitive; ADR-0007 doesn't formalize this — future tuning may add explicit metrics.

## Embedder hang risk (added 2026-05-15)

Auto-reseed `seed_patterns` gọi embedder qua `_write_pgvector_with_embedder()`.
Trước v0.3.x, embedder dùng `urllib.request.urlopen(timeout=N)` — đây là
per-socket idle timeout, KHÔNG phải total wall-clock deadline. Embed backend
gửi 1 byte mỗi N-1 giây sẽ giữ socket vô hạn → indexer thread block →
ThreadPoolExecutor exhaust → pipeline freeze, không có exception nào raise.

Fix: replace urllib bằng `httpx.Client(timeout=httpx.Timeout(...))`. `read`
timeout của httpx áp giữa các chunk → server im lặng raise `ReadTimeout`
đúng nghĩa → retry loop hoạt động → repo status chuyển `error`.

**Workaround nếu chạy prod cũ chưa fix:** chạy indexer với `--no-embed`.
Backfill Neo4j-only ops sẽ skip embedder, không hang.
