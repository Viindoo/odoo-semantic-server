---
status: draft
scope: specs
reads-with:
  - ../product_brief.md
---

# Specs

One spec per MCP tool. The canonical list of tools is in [`../product_brief.md`](../product_brief.md) — do not add tools here that aren't in the brief.

## Index

| File | Tool | Phase | Status |
| ---- | ---- | ----- | ------ |
| [`_template.md`](_template.md) | — (template) | — | — |
| [`resolve_model.md`](resolve_model.md) | `resolve_model` | P1 | draft |
| [`resolve_field.md`](resolve_field.md) | `resolve_field` | P1 | draft |
| [`resolve_method.md`](resolve_method.md) | `resolve_method` | P1 | draft |
| [`resolve_view.md`](resolve_view.md) | `resolve_view` | P2 | draft |
| [`find_examples.md`](find_examples.md) | `find_examples` | P3 | draft |
| [`impact_analysis.md`](impact_analysis.md) | `impact_analysis` | P4 | draft |

## Writing rules

- One tool = one file
- Status lifecycle: `draft` → `review` → `confirmed` → `implemented`
- Never implement against `draft`
- When real behaviour diverges from the spec, update the spec first, then fix the code
- If a spec grows past ~200 lines, it is probably two features → split

## Every spec answers

1. Who calls this tool (AI client, which mode)
2. Exact input / output schema
3. Which tables it queries (link to `../data-model/`)
4. What counts as "correct" (acceptance criteria)
5. What it does NOT do (out of scope)

## Conventions for every tool

**Tenancy** — every tool is tenant-scoped. The current tenant is derived from the auth token, not passed by the caller. Handlers query `public.<table>` unioned with `<tenant>.<table>` and resolve overrides with tenant modules winning. See [`../architecture/tenancy.md`](../architecture/tenancy.md) and [`../decisions/0004-multi-tenant-model.md`](../decisions/0004-multi-tenant-model.md). Specs do **not** repeat this — they reference it.

**Primary value: token / context reduction.** The tool exists so an AI client can answer a question about Odoo code **without** reading the raw source files into context. Every spec must include, in its acceptance criteria, a comparison: "for task X, response size is ≤Y tokens vs ≥Z tokens if the AI had to read raw source". Correctness is a necessary condition; token savings is the product value.

**Standard response envelope** — see [`../architecture/mcp-server.md`](../architecture/mcp-server.md). Every response includes `indexed_at_sha` and a `warnings` array.
