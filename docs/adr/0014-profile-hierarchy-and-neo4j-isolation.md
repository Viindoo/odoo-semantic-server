# ADR-0014 — Profile Hierarchy and Neo4j Node Isolation (Option Y)

**Date:** 2026-05-14
**Status:** Accepted — M8

## Context

The master-data seed (PR #87) registered 26 profiles arranged in three tiers
per Odoo version:

```
odoo_N            (Odoo CE base — owns the CE git repo)
  └─ standard_viindoo_N   (CE + Viindoo public addons)
       └─ viindoo_internal_N  (CE + public + internal repos)
```

Each profile currently owns only its *delta* repos (enforced by
`UNIQUE (url, branch)` on the `repos` table). An admin indexing
`viindoo_internal_17` must also separately index `odoo_17` and
`standard_viindoo_17` to see the full picture.

The MCP tools (`resolve_model`, `resolve_field`, etc.) filter nodes by
`odoo_version` but have no concept of which repos belong to a specific
deployment profile. This causes two problems:

1. **Namespace collision risk**: When two customer deployments use the same
   `odoo_version` but different repo sets, a query for
   `odoo_version = '17.0'` returns nodes from *all* 17.0 repos regardless
   of which profile they were indexed under.

2. **No inheritance traversal**: A query scoped to `viindoo_internal_17`
   should automatically include nodes from `odoo_17` and
   `standard_viindoo_17` (the ancestor chain), but there is no Postgres- or
   Neo4j-level link expressing that ancestry.

Two options were evaluated:

### Option X — Postgres-only hierarchy, no Neo4j change

Store `parent_profile_id` FK in Postgres only. MCP tools resolve the
ancestor chain at query time via the Postgres CTE and collect all relevant
`repo_id` values, then filter Neo4j queries by `repo` property.

*Rejected because:*
- Every MCP Cypher query would need a `repo IN $repo_list` clause, making
  them significantly more complex and harder to test.
- Large ancestor lists (3+ tiers) produce long `$repo_list` parameter arrays
  that hurt Neo4j query plan caching.
- When customer-fork profiles land at the same `odoo_version`, the
  `repo`-based filter still risks collision if two forks use repos with the
  same basename.

### Option Y — Neo4j nodes carry a `profile` array property

Store `parent_profile_id` FK in Postgres (same as Option X) and **also**
write a `profile: list[str]` property on every Neo4j node at index time.
The property contains the full ancestor chain: `[self, parent, grandparent,
..., root]`. MCP tools gain an optional `profile_name` parameter that
filters with `$profile_name IN m.profile`.

*Accepted because:*
- Cypher filter is one simple `WHERE` clause, consistent across all tools.
- Querying `viindoo_internal_17` automatically returns nodes from ancestor
  profiles — no extra Postgres lookup at MCP query time.
- The `profile` array is a first-class indexed Neo4j property, enabling
  future full-text or range queries on profiles without schema changes.
- Backward compatible: `profile_name=None` (default) bypasses the filter,
  preserving existing tool behavior for users who have not migrated.

## Decision

### D1 — Postgres: `profiles.parent_profile_id` self-FK

```sql
ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS parent_profile_id INTEGER
    REFERENCES profiles(id) ON DELETE RESTRICT;
```

`ON DELETE RESTRICT` is intentional: removing a parent profile while
children still reference it must be an explicit admin action (delete or
re-parent children first).

Application code enforces:
- **Cycle-free**: `_validate_parent()` walks the ancestor CTE upward; raises
  `ValueError` if the proposed parent would reach the child.
- **Version-match**: `parent.odoo_version` must equal `child.odoo_version`.

### D2 — Recursive CTE helpers in `RepoStore`

`get_ancestor_profile_names(profile_name)` → `list[str]`

Returns profile names from self (index 0) to root (last). Used by the
indexer to build the `profile` array written to Neo4j.

`get_ancestor_repos(profile_name)` → `list[dict]`

Returns repos for the full ancestor chain, depth-ordered (self first).
Available for future indexer modes that want to pre-fetch the full tree.

### D3 — 2-pass seed in `seed_master_data.py`

`_PROFILE_DEFS` widened to 4-tuples `(name, version, description, parent_name)`.
`seed_profiles()` runs a Pass-1 INSERT (all rows, no parent FK) then a
Pass-2 UPDATE that sets `parent_profile_id` for each row with a non-None
parent. Both passes are idempotent via `ON CONFLICT DO NOTHING` and
`IS DISTINCT FROM`.

### D4 — Indexer: `ancestor_profiles` propagated to writer

At the start of `index_profile()`, `get_ancestor_profile_names()` is
called once. The resulting list is passed through `_index_repo()` to all
three writer methods (`write_results`, `write_view_results`,
`write_js_graph_results`). Each writer function passes it to its inner
transaction helper as `profiles`.

A warning is emitted (not a failure) when any ancestor profile has no
indexed repos yet, guiding admins to index parent tiers before child tiers.

### D5 — Neo4j: `profile` list property on code element nodes

Every MERGE for `Module`, `Model`, `Field`, `Method`, `View`, `QWebTmpl`,
`OWLComp`, and `JSPatch` now includes:

```cypher
SET n.profile = $profiles
```

The `$profiles` parameter is the ancestor chain list `[self, ..., root]`
from `get_ancestor_profile_names()`. Each indexer run overwrites the list
(no append logic) — the array is always fresh and consistent.

### D6 — MCP tools: optional `profile_name` filter

Eight tools now accept an optional `profile_name: str | None = None`
parameter:

- `resolve_model` — filters on `Model` node `.profile`
- `resolve_field` — filters on `Field` node `.profile`
- `resolve_method` — filters on `Method` node `.profile`
- `resolve_view` — filters on `View` node `.profile` (primary + extensions)
- `impact_analysis` — filters on all 5 sub-queries:
  `Field`, `Method`, `View`, `JSPatch`, and dependent `Module` nodes
- `find_deprecated_usage` — filters on `Method` node `.profile`
- `check_module_exists` — filters on `Module` node `.profile`
- `find_examples` — filters on the Neo4j `Module` rerank step only
  (see caveat below)

When provided, Cypher adds:

```cypher
WHERE ($profile_name IS NULL OR $profile_name IN <node>.profile)
```

This pattern is backward compatible: the default `None` bypasses the
filter, preserving existing behavior for all current users.

**Caveat — `find_examples` pgvector path:** The `embeddings` table in
PostgreSQL is keyed by `(odoo_version, chunk_type, module, ...)` but has
no `profile` column. The ANN vector search therefore returns chunks from
all profiles sharing the same `odoo_version`. The `profile_name` filter
is applied only to the downstream Neo4j Module rerank step, which
adjusts the centrality score of chunks belonging to modules outside the
requested profile. This means chunks from out-of-profile modules may
still appear in results if their vector similarity is high and they share
the same `odoo_version`. Operators requiring strict profile isolation for
semantic search should add a `profile` column to the `embeddings` table
and extend the ANN WHERE clause in a future migration.

## Consequences

**Positive:**
- Delta-repo hierarchy is now queryable end-to-end from MCP tools.
- Ancestor-scoped queries work without additional Postgres lookups at
  query time.
- Seed is idempotent and safe to run on existing deployments.
- Cycle and version-mismatch are enforced server-side — UI and API both
  return clear error messages.

**Negative:**
- **One-time full reindex required** at rollout: existing Neo4j nodes have
  no `profile` property. A query with `profile_name` filter returns nothing
  until nodes are reindexed. The post-merge ops runbook
  (`docs/deploy.md §Post-M8 reindex`) covers the sequence:
  `index odoo_N` → `index standard_viindoo_N` → `index viindoo_internal_N`
  for each active version.
- **Startup warning surfaces unreindexed nodes** (added in the follow-up
  fix commit): at ASGI startup, the lifespan hook runs a best-effort Cypher
  query counting nodes whose `profile` property is `NULL`. When the count is
  > 0, `src.mcp.server` logs a `WARNING`-level message:
  > "*N* Neo4j nodes have no `profile` property — these are invisible to
  > profile-scoped MCP queries. Run a full reindex per ADR-0014 to backfill."
  The warning is wrapped in `try/except` so it cannot block startup. Ops
  teams should monitor server startup logs after any upgrade and schedule the
  reindex documented in `docs/deploy.md §Post-M8 reindex`.
- Each indexer run writes slightly more data to Neo4j (one extra list
  property per node). Measured overhead: negligible (< 5% write time on
  benchmark repos).

## References

- `migrations/0003_profile_hierarchy.sql` — DDL for the FK column + index.
- `src/db/repo_registry.py` — `_validate_parent`, `set_profile_parent`,
  `get_ancestor_profile_names`, `get_ancestor_repos`.
- `src/db/seed_master_data.py` — 4-tuple `_PROFILE_DEFS`, 2-pass
  `seed_profiles()`.
- `src/indexer/pipeline.py` — `ancestor_profiles` propagation in
  `index_profile` + `_index_repo`.
- `src/indexer/writer_neo4j.py` — `profile` property on all node MERGEs.
- `src/mcp/server.py` — `profile_name` filter on `resolve_model`,
  `resolve_field`, `resolve_method`, `resolve_view`, `impact_analysis`
  (all 5 sub-queries), `find_deprecated_usage`, `check_module_exists`,
  `find_examples` (Neo4j rerank only; pgvector path is version-scoped).
- ADR-0001: Schema Evolution Policy (Neo4j SET properties, idempotent MERGE).
- ADR-0007: Incremental Indexer (head_sha tracking; full reindex flag).
