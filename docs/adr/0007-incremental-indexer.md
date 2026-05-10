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

### D3 — head_sha advances ONLY after full success

If `_index_repo()` raises mid-write (Neo4j down, parser error, etc.), `head_sha` must NOT update — next run will retry the same diff range (or fall back to full reindex if force-push happened in between).

Implementation: `repo_registry.update_repo_head_sha()` is the LAST statement in `_index_repo()` after all writes/commits succeed. Exception-on-write → never reaches that line → head_sha stays at last successful value.

This is a one-direction state machine: head_sha can only advance, never roll back. Partial-failure is the same as no-progress.

### D4 — `--full` flag is the operator's reset button

The CLI flag `--full` bypasses the skip-unchanged check + the diff filter — forcing a full reindex of every module the scanner finds. This is the documented escape hatch for D5 cleanup.

`--full` does NOT reset head_sha to NULL — at the end of a successful `--full` run, head_sha advances to current HEAD just like an incremental run. Operationally, `--full` is "re-write what we have, then continue normally."

### D5 — Module rename: stale Neo4j nodes accepted; cleanup deferred

When `addons/stock` is renamed to `addons/inventory`, `git diff --name-only` shows both paths as changed. The scanner finds the new `addons/inventory` module → Module node MERGEd; the old `addons/stock` directory no longer exists → no scanner pass → its Module node remains in Neo4j as a stale orphan.

Wave 2 chooses to **document and recommend periodic `--full` runs** (recommend monthly) rather than implement an explicit cleanup pass. Reasons:

- Auto-cleanup is risky (a missed scan due to filesystem permissions could DELETE legitimate Module nodes).
- Renames are rare in real Odoo codebases; stale orphans are query-time noise, not correctness failures.
- `--full` rebuilds from a clean slate (existing pipeline DELETEs by module name + odoo_version on full reindex).

Future Wave (M7 candidate): explicit "garbage collect Module nodes whose path no longer exists in current scan" pass, gated by a `--gc` flag.

### D6 — Auto-reseed gated via `_SeedMeta` Neo4j sentinel + sha256

`seed_patterns()` is called at the end of every `index_profile()` so admins don't need to remember a separate command. To make this cheap, the seed function:

1. Computes sha256 of `src/data/patterns.json`.
2. MATCH `(:_SeedMeta {key:'patterns'})` to read stored sha.
3. If equal → log "Patterns unchanged — skipping" + return (no Neo4j MERGE on PatternExample, no Ollama embed).
4. Otherwise → run normal seed flow + MERGE sentinel SET sha256=current.

The sentinel is updated AFTER successful seed (same partial-failure rule as D3 — failure preserves last successful sha).

`--force` CLI flag bypasses sentinel for "I edited patterns.json and want to be sure" cases.

Auto-reseed failure inside the indexer is logged at WARN level but does NOT fail the indexer run — pattern catalogue is supplementary; an indexed code graph without fresh patterns is still useful.

### D7 — `_SeedMeta` label is project-private

Prefixed with `_` to denote internal/operational metadata, distinct from domain labels (Module, Field, View, etc.). No queries from MCP tools. Future internal sentinel needs (e.g., last `index-core` version per CoreSymbol set) can reuse the label with different `key` values.

## Implementation references

- `src/indexer/incremental.py` — D2/D3 helpers (get_repo_head, is_ancestor, compute_changed_module_paths, filter_modules_by_changed)
- `src/indexer/pipeline.py::_index_repo` — D1/D2/D3/D4 wiring
- `src/indexer/seed_patterns.py::_get_stored_patterns_sha` / `_set_stored_patterns_sha` — D6/D7
- `src/indexer/scanner.py::get_module_commit_sha` — D1 per-module sha source
- `src/indexer/writer_neo4j.py` Module MERGE SET — D1 last_commit_sha persistence
- `src/db/migrate.py` — D1 head_sha column ALTER (per ADR-0001 M6 schema window)

## Tests

- `tests/test_incremental.py` — D2 helpers + force-push fixture
- `tests/test_pipeline_incremental.py` — D3 partial-failure + D4 --full + skip-unchanged + diff filter
- `tests/test_seed_patterns.py` — D6 sentinel hash gating + D7 label
- `tests/test_pipeline_seed_integration.py` — D6 wired into index_profile

## Out of scope (recorded for future ADRs)

- Module rename garbage collection (D5 deferred to M7).
- Cross-repo dependency change tracking (currently each repo's diff is computed independently; if repo A's module depends on repo B's module that changed, the dependency graph rebuild is implicit on next full reindex).
- Embedding cost analysis: per-module embedding incremental is implicit via `delete_embeddings_for_module` primitive; ADR-0007 doesn't formalize this — future tuning may add explicit metrics.
