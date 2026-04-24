---
status: draft
scope: decisions
reads-with:
  - ../architecture/overview.md
---

# Architecture Decision Records

One file per significant decision, MADR format. Copy [`_template.md`](_template.md) to `NNNN-short-title.md` when proposing a new one.

## When to write an ADR

- Changes module boundary or storage layout
- Changes a public contract (MCP tool shape, API)
- Picks a vendor / license / brand
- Affects security posture
- Is hard to reverse

If it is just a naming preference or local refactor → do not write an ADR.

## Index

| ID | Title | Status |
| -- | ----- | ------ |
| [`0001-postgres-vs-neo4j.md`](0001-postgres-vs-neo4j.md) | Postgres (pgvector) vs separate graph DB | draft |
| [`0002-embedding-provider.md`](0002-embedding-provider.md) | Voyage API vs self-hosted `bge-code-v1` as default | draft |
| [`0003-brand-name.md`](0003-brand-name.md) | Product brand name (must not contain "Odoo") | draft |
| [`0004-multi-tenant-model.md`](0004-multi-tenant-model.md) | Schema-per-tenant with shared `public` schema for Odoo CE | draft |

## Status lifecycle

- `draft` — circulating
- `accepted` — in force; code should reflect it
- `deprecated` — no longer applies; reason + replacement in the file
- `superseded by ADR-NNNN` — link forward

## File naming

`NNNN-kebab-case-title.md`. Never reuse a number.
