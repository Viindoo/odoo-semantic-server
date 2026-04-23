---
status: draft
scope: architecture
reads-with:
  - ../product_brief.md
---

# Architecture

Living technical picture of the system. When real code diverges from what is written here, update the docs — do not leave them stale.

## How to read

Start with [`overview.md`](overview.md) (C4 L1+L2). Then drill into the component you are changing. For database schema, see [`../data-model/`](../data-model/).

## Index

| File | Component | Status |
| ---- | --------- | ------ |
| [`overview.md`](overview.md) | C4 Context + Container views, request flow | draft |
| [`indexer.md`](indexer.md) | Parsing pipeline (Python, XML, QWeb, JS) + manifest resolver | draft |
| [`graph-store.md`](graph-store.md) | PostgreSQL schema for models/fields/methods/views + inheritance edges | draft |
| [`vector-store.md`](vector-store.md) | Embedding pipeline + pgvector / Qdrant choice | draft |
| [`mcp-server.md`](mcp-server.md) | FastMCP process exposing 6 tools | draft |
| [`tenancy.md`](tenancy.md) | Multi-tenant overlay: shared `public` CE schema + per-tenant private schema | draft |
| [`deployment.md`](deployment.md) | Dev (Tailscale) / self-hosted (Docker Compose) / Hosted (Hetzner) topologies | draft |
| [`diagrams/`](diagrams/) | `.mmd` / `.puml` source files for diagrams referenced above | — |

## Principles

1. **Single database.** PostgreSQL 16 + `pgvector` holds relational + graph (recursive CTE) + vectors. Avoid separate Neo4j / Qdrant unless a decision in `decisions/` says otherwise.
2. **Idempotent indexer.** Running on unchanged files produces no DB writes. Enforced by content hashes.
3. **Stateless MCP.** Every tool call is self-contained. Horizontal scaling = add replicas.
4. **SHA in every response.** Every MCP response carries `indexed_at_sha` so callers can detect index/code skew.
5. **Graph is authoritative; vector is supportive.** When results conflict, graph wins. Vector narrows the search; graph verifies.

## Relationship with ADRs

- **This folder:** what exists *now*.
- **[`../decisions/`](../decisions/):** why it exists this way.

When an ADR is accepted, update the relevant file here the same day. Do not leave docs describing the pre-ADR state.
