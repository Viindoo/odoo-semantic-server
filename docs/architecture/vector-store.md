---
status: draft
scope: architecture/vector-store
reads-with:
  - overview.md
  - indexer.md
  - graph-store.md
  - ../decisions/0002-embedding-provider.md
---

# Vector store

Holds embeddings of code chunks for semantic search (MCP tool `find_examples`). Supportive of the graph store — graph verifies correctness; vector narrows the search space.

## Backend choice

**Default**: `pgvector` in the same PostgreSQL database as the graph store.

Rationale: operational simplicity, atomic consistency between graph rows and their embeddings, sufficient recall at our scale. If throughput or recall becomes insufficient, revisit via ADR.

Alternative considered: **Qdrant**. Better specialised retrieval features, but adds a second stateful service. Keep in mind for post-MVP.

> See: [`../decisions/0001-postgres-vs-neo4j.md`](../decisions/0001-postgres-vs-neo4j.md) (same decision covers DB choice overall)

## Embedding model

Two supported configurations:

| Mode | Model | Use case |
| ---- | ----- | -------- |
| API (default) | `voyage-code-3` | Hosted tier, fast setup, predictable cost |
| Self-host | `bge-code-v1` | On-prem customers, offline dev, no API egress |

Decision: [`../decisions/0002-embedding-provider.md`](../decisions/0002-embedding-provider.md).

## Chunking strategy

- **Python**: one chunk per method body
- **XML view**: one chunk per top-level record
- **QWeb** (P4): one chunk per template
- **JS** (P4): one chunk per exported component / function

A chunk carries:

- Stable ID (model_id + method_name or view xmlid)
- Source range (file, start line, end line) for precise snippet return
- Content hash (for idempotent re-embedding)
- `indexed_at_sha`

## Re-embedding trigger

Only when chunk **content hash** changes. A rename without body change is not a re-embed. A whitespace-only diff is not a re-embed. This is how we keep cost bounded.

## Search shape

```text
find_examples(query) →
  1. Embed query
  2. Top-K ANN on vectors (HNSW index)
  3. For each candidate: graph-side validation (still exists, still in scope)
  4. Return ranked snippets with file:line + indexed_at_sha
```

## What is NOT here

- Graph data — lives in the graph store
- Query reranking logic (uses both stores) — part of MCP server handler, not storage
