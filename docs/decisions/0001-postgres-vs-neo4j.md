---
status: accepted
scope: decisions/0001
date: 2026-04-21
accepted_date: 2026-04-22
deciders:
  - David Tran
  - Tran Truong Son
---

# ADR-0001: Single PostgreSQL database vs dedicated graph + vector services

## Context

The product brief positions the system around three stores: graph, vector, cache. Brief lists PostgreSQL and mentions "Qdrant OR pgvector" for vectors. Graph DB choice is open. We need one decision that covers graph + vector storage.

## Drivers

- Minimise ops surface — this is a small team, every extra daemon is cost
- Consistency — a chunk's graph row and its embedding should be queryable in one transaction
- Performance — recursive CTE on inheritance depth ≤10 is fast on Postgres
- Future optionality — if we hit a ceiling, switching one store at a time should be possible

## Considered options

### Option A — Single PostgreSQL 16 + `pgvector`

- **Pros**: one daemon, one backup, atomic writes across graph + vector, simple Docker Compose
- **Cons**: `pgvector` HNSW is good but not Qdrant-good at very high cardinality; graph queries are recursive CTEs, not native graph

### Option B — PostgreSQL + Qdrant (separate)

- **Pros**: best-in-class ANN, purpose-built
- **Cons**: two daemons, two backup paths, embeddings can drift from graph on partial failures

### Option C — Neo4j + Qdrant

- **Pros**: native graph semantics, best-in-class ANN
- **Cons**: three stateful services in the deployment, licensing complexity (Neo4j Community limits)

## Decision

**Option A** — Single PostgreSQL 16 + `pgvector`.

Rationale: Odoo inheritance depth is shallow, recursive CTE is more than fast enough, and we do not want three stateful services in the Docker Compose. Atomic consistency between graph rows and embeddings is a non-obvious win during partial re-indexing.

## Consequences

- **Positive**: one-command setup, single backup, atomic consistency
- **Negative**: if customer scale pushes recall/throughput past `pgvector`, we have to split. Accept that and design the vector layer with a swappable backend interface from day one
- **Follow-ups**:
  - Benchmark `pgvector` HNSW on ~5M chunk dataset before end of P3
  - Keep the vector-store interface narrow so Qdrant can slot in later without touching specs

## Kill criteria

Revisit this ADR if any of:
- `find_examples` P50 exceeds 400ms on a real Hosted-tier customer and the bottleneck is ANN
- Recall@10 drops below 70% on production workloads
- Team ships by end of Q3 2026 without needing the split → decision stays

## References

- Brief: `project-docs/odoo-semantic-mcp/product_brief.md` ("Qdrant OR pgvector")
- Architecture (internal): `project-docs/odoo-semantic-mcp/architecture/graph-store.md`, `project-docs/odoo-semantic-mcp/architecture/vector-store.md`
