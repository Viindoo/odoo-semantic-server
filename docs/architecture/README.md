---
status: draft
scope: architecture
reads-with:
  - overview.md
  - deployment.md
---

# Architecture

Public architecture documentation — system context and deployment topology. When real code diverges from what is written here, update the docs — do not leave them stale.

## How to read

Start with [`overview.md`](overview.md) (C4 L1+L2). For deployment topology, see [`deployment.md`](deployment.md). For database schema, see [`../data-model/`](../data-model/).

Component-level design documents (indexer, graph-store, vector-store, mcp-server, tenancy) are internal design artefacts kept in `project-docs/odoo-semantic-mcp/architecture/`.

## Index

| File | Component | Status |
| ---- | --------- | ------ |
| [`overview.md`](overview.md) | C4 Context + Container views, request flow | draft |
| [`deployment.md`](deployment.md) | Dev (Tailscale) / self-hosted (Docker Compose) / Hosted (Hetzner) topologies | draft |
| [`diagrams/`](diagrams/) | `.mmd` / `.puml` source files for diagrams referenced above | — |

## Principles

1. **Single database.** PostgreSQL 16 + `pgvector` holds relational + graph (recursive CTE) + vectors. Avoid separate Neo4j / Qdrant unless a decision in `decisions/` says otherwise.
2. **Idempotent indexer.** Running on unchanged files produces no DB writes. Enforced by content hashes.
3. **Stateless MCP.** Every tool call is self-contained. Horizontal scaling = add replicas.
4. **SHA in every response.** Every MCP response carries `indexed_at_sha` so callers can detect index/code skew.
5. **Graph is authoritative; vector is supportive.** When results conflict, graph wins. Vector narrows the search; graph verifies.

## Relationship with ADRs

- **This folder:** what exists *now* (public-facing topology and overview).
- **[`../decisions/`](../decisions/):** why it exists this way.

When an ADR is accepted, update the relevant file here the same day. Do not leave docs describing the pre-ADR state.
