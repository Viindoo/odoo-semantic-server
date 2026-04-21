---
status: draft
scope: research
reads-with:
  - ../product_brief.md
  - ../contexts/research.md
---

# Research

Evidence that feeds specs and ADRs. One topic per file. Every file has a date — research rots.

## Index

| File | Question | Status | Last updated |
| ---- | -------- | ------ | ------------ |
| [`odoo-internals.md`](odoo-internals.md) | How does Odoo actually resolve `_inherit`, `_inherits`, view XPath, manifest load order? | placeholder | — |
| [`competitors.md`](competitors.md) | Existing MCP servers / code-indexing tools and gaps | placeholder | — |
| [`embedding-benchmarks.md`](embedding-benchmarks.md) | Voyage vs `bge-code-v1` on Viindoo corpus | placeholder | — |
| [`mcp-ecosystem.md`](mcp-ecosystem.md) | Which MCP clients exist and what tool shapes they expect | placeholder | — |

## Writing rules

- One topic per file
- Every claim has a source link or is flagged **assumption** in bold
- Dates at top of file; mark stale files explicitly
- End with an **implications** section pointing at affected specs / ADRs

## What not to put here

- Implementation details → `specs/`
- Decisions → `decisions/`
- Terminology → `glossary.md`
- Daily status → `tasks/todo.md`
