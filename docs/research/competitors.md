---
status: placeholder
scope: research/competitors
date: 2026-04-21
implications_for:
  - ../product_brief.md
  - ../decisions/0003-brand-name.md
---

# Competitors and adjacent tools

**Status**: placeholder. Fill in before committing to P5 positioning.

## Scope

Look at three tiers of competition:

1. **Odoo-specific AI tooling** — do any exist that understand `_inherit`?
2. **Generic code-indexing MCP servers** — `@code-mcp`, Sourcegraph-adjacent tooling, etc.
3. **IDE plugins with code understanding** — Cursor, Continue, JetBrains AI

## What to capture per tool

- Name, vendor, license
- What it indexes (languages, frameworks)
- Whether it understands dynamic inheritance
- Pricing model
- Distribution: OSS / SaaS / both
- Notable users or testimonials
- Gaps we can fill

## Questions

- Is there any existing tool that specifically understands Odoo `_inherit` + `inherit_id` chains? (Brief says no — verify)
- What's the pricing anchor in the MCP server space? ($10 / $29 / $99 / free + enterprise?)
- Is Sourcegraph-for-Odoo a thing anyone has built?

## Implications

This research informs:

- Differentiation claims in `product_brief.md`
- Pricing strategy (targeted by ADR later)
- Brand positioning (see `decisions/0003-brand-name.md`)
