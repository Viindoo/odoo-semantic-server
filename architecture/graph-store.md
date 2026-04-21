---
status: confirmed
confirmed_date: 2026-04-22
scope: architecture/graph-store
reads-with:
  - overview.md
  - indexer.md
  - ../data-model/modules.md
  - ../data-model/models.md
  - ../data-model/fields.md
  - ../data-model/methods.md
  - ../data-model/views.md
  - ../data-model/cache_metadata.md
---

# Graph store

PostgreSQL 16 tables that hold modules, models, fields, methods, views, and the inheritance edges between them. Queried by MCP tools using recursive CTEs.

## Tenancy

The graph is **multi-tenant** — see [`tenancy.md`](tenancy.md) and [`../decisions/0004-multi-tenant-model.md`](../decisions/0004-multi-tenant-model.md). Every table described below is replicated in one schema per tenant, with a `public` schema holding the shared Odoo CE index. Query handlers union `public.<table>` with `<tenant>.<table>` and sort so tenant modules win on overrides.

## Why PostgreSQL over Neo4j

Odoo inheritance depth is shallow (typically ≤10). Recursive CTEs resolve in milliseconds. Running a second database is operational cost we do not want to pay yet. If depth or graph complexity grows, revisit via ADR.

> See: [`../decisions/0001-postgres-vs-neo4j.md`](../decisions/0001-postgres-vs-neo4j.md)

## Schema at a glance

The canonical per-table definitions live in [`../data-model/`](../data-model/). This file captures the relationships.

```text
      +----------+
      | modules  |
      +----------+
           ^
           | (defining_module)
           |
      +----------+
      |  models  |<------ inherits_model (self edge)
      +----------+<------ delegates_model (self edge)
        ^    ^    ^
        |    |    |
        |    |    +-----------+
        |    |                |
  +---------+  +---------+  +---------+
  | fields  |  | methods |  |  views  |
  +---------+  +---------+  +---------+
                                ^
                                | inherit_id (self edge)
```

## Query patterns

### P1 — override chain for a field

```sql
WITH RECURSIVE chain AS (
    SELECT * FROM fields WHERE model_id = :model AND field_name = :field
    UNION ALL
    SELECT f.* FROM fields f
    JOIN chain c ON f.override_of = c.id
)
SELECT * FROM chain ORDER BY module_load_order;
```

### P2 — final view

Resolve `inherit_id` chain, apply XPath patches from [`views`](../data-model/views.md) in load order.

## Indexing strategy

- `btree` on `(module_id, name)` for lookups
- `btree` on `override_of` for chain walks
- Partial indexes on recent commits for hot paths

## Tenancy — no cross-schema hard foreign keys

Hard FK constraints are scoped to a single schema. Cross-schema references (e.g. a tenant's `fields.override_of` pointing at a `public.fields.id`) are stored as `bigint` without `REFERENCES`. Reason: `public` and tenant schemas have independent re-index cycles — hard FKs would couple them and make partial re-index, backup, and tenant erase operationally fragile. Integrity is preserved via `indexed_at_sha` staleness checks in the resolver — a 409 response signals stale index and the caller triggers re-index (see `mcp-server.md` error model).

## Functional indexes and GIN

Method decorator filtering uses a GIN index on `methods.decorators text[]`:

```sql
CREATE INDEX methods_decorators_gin ON <schema>.methods USING GIN (decorators);
```

Query `WHERE 'api.depends' = ANY(decorators)` uses this index. This replaces earlier draft columns `is_api_model` / `is_api_depends` — single source of truth is the array.

## Consistency invariants

- Every `override_of` points at an older row in load-order
- Every inheritance edge's target row must exist
- `indexed_at_sha` is per-table (not global) so partial re-indexing is safe

## What is NOT here

- Vector data — lives in the vector store, linked by stable keys
- Embeddings metadata — lives in the vector store
- Indexer file-level cache — see [`../data-model/cache_metadata.md`](../data-model/cache_metadata.md)
