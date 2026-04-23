---
status: draft
scope: architecture/overview
reads-with:
  - ../product_brief.md
  - indexer.md
  - graph-store.md
  - vector-store.md
  - mcp-server.md
---

# Overview (C4 L1 + L2)

## L1 — System Context

```text
[ AI coding client ]       [ Odoo source repos ]
   Claude Code / Cursor         Python / XML / JS
   Codex / Continue               __manifest__.py
         |                              |
         | MCP over stdio/http          | read-only clone
         v                              v
   +------------------------------------------+
   |         odoo-semantic-mcp                |
   |   (indexer + graph + vector + MCP)       |
   +------------------------------------------+
```

**External actors**

- **Developer's AI client** — consumes MCP tools
- **Odoo source repositories** — input to the indexer (CE + Enterprise + custom)
- **Git host** — triggers incremental re-indexing via commit SHA

## L2 — Containers

```text
+------------------------------------------------------+
|                  odoo-semantic-mcp                   |
|                                                      |
|  +-------------+    +------------------------+       |
|  |   Indexer   |--->|  PostgreSQL 16         |       |
|  |  (Python)   |    |  + pgvector            |       |
|  |             |    |  - graph schema        |       |
|  |  libcst     |    |  - vector tables       |       |
|  |  lxml       |    |  - cache metadata      |       |
|  |  resolver   |    +------------------------+       |
|  +-------------+                ^                    |
|        |                        |                    |
|        v                        |                    |
|  +-------------+                |                    |
|  |  Embedder   |----------------+                    |
|  |  (Voyage    |                                     |
|  |   API / bge |                                     |
|  |  self-host) |                                     |
|  +-------------+                                     |
|                                                      |
|  +-------------+                                     |
|  |   FastMCP   |<---- clients                        |
|  |   server    |      (Claude Code, etc.)            |
|  +-------------+                                     |
|        |                                             |
|        +------> reads from Postgres                  |
+------------------------------------------------------+
```

## Request flow — example: "what fields does `sale.order` expose?"

1. Client sends MCP tool call `resolve_model(model_name="sale.order")`
2. FastMCP server validates input, dispatches to handler
3. Handler queries Postgres using a recursive CTE over the inheritance edge table
4. Handler attaches `indexed_at_sha` from cache metadata
5. Response returns via MCP

Everything is synchronous. No background jobs on the read path.

## Indexing flow

1. Indexer watches a git repo (or is run manually)
2. For each file changed vs last indexed SHA: re-parse, diff structured output, upsert into graph tables
3. For newly-added or structurally-changed chunks: enqueue for embedding
4. Embedder batches requests, stores vectors
5. Cache metadata updated with new SHA

## What this file is NOT

- A detailed DB schema — see [`../data-model/`](../data-model/)
- A component-level design — see the per-component files in this folder
- A decision record — see [`../decisions/`](../decisions/)
