---
status: confirmed
confirmed_date: 2026-04-22
scope: architecture/mcp-server
reads-with:
  - overview.md
  - graph-store.md
  - vector-store.md
  - ../specs/resolve_model.md
  - ../specs/resolve_field.md
  - ../specs/resolve_method.md
  - ../specs/resolve_view.md
  - ../specs/find_examples.md
  - ../specs/impact_analysis.md
---

# MCP server

FastMCP (Python) process exposing the 6 tools defined in `product_brief.md`. Stateless per request.

## Responsibilities

- Speak MCP protocol (stdio + http transports)
- Validate tool input against published schemas
- Dispatch to handler functions
- Return structured JSON with `indexed_at_sha`
- Emit audit log entries for every call touching customer data

## Non-responsibilities

- Running the indexer (separate process)
- Embedding (separate process)
- Authentication for Hosted tier — that is a reverse-proxy / gateway concern, not the MCP server itself

## Tools exposed

Each tool has a spec file with input/output schema and acceptance criteria.

| Tool | Spec | Phase |
| ---- | ---- | ----- |
| `resolve_model` | [`../specs/resolve_model.md`](../specs/resolve_model.md) | P1 |
| `resolve_field` | [`../specs/resolve_field.md`](../specs/resolve_field.md) | P1 |
| `resolve_method` | [`../specs/resolve_method.md`](../specs/resolve_method.md) | P1 |
| `resolve_view` | [`../specs/resolve_view.md`](../specs/resolve_view.md) | P2 |
| `find_examples` | [`../specs/find_examples.md`](../specs/find_examples.md) | P3 |
| `impact_analysis` | [`../specs/impact_analysis.md`](../specs/impact_analysis.md) | P4 |

## Response envelope (all tools)

```json
{
  "result": { "...": "..." },
  "indexed_at_sha": "abc1234",
  "warnings": ["..."]
}
```

`warnings` is populated when the indexer marked a chunk as `resolution: "unknown"` (dynamic `_inherit` etc.). AI callers should surface these to users rather than hide them.

## Error model

- `400` — invalid input (schema validation)
- `404` — entity not in index
- `409` — index stale for this path, caller should trigger re-index
- `500` — handler bug, include trace in audit log, return generic message to client

## Performance target

- P50 <20ms for single-entity resolvers
- P50 <100ms for `impact_analysis` on a mid-size module
- P99 <500ms across all tools
- Budget breached → open an issue; do not paper over with caching

## Scaling

Stateless → add replicas behind a simple load balancer. PostgreSQL is the bottleneck long before the server is.
