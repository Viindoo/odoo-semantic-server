---
status: draft
scope: decisions/0003
date: 2026-04-21
deciders:
  - David Tran
---

# ADR-0003: Product brand name

## Context

The repo is currently named `odoo-semantic-mcp` — fine as an internal project name, but the public-facing product cannot use "Odoo" in its brand (Odoo SA trademark). Before P5 (public distribution) we need a brand that we can legally promote, own a domain for, and build community around.

## Drivers

- Legal — Odoo SA enforces the "Odoo" trademark
- Recognisability — name should hint at the domain (code graph, Odoo ecosystem) without infringing
- Availability — `.com` or at minimum `.dev` domain + GitHub org available
- Short — easy to type in CLI and mention in tweets
- Non-generic — "codegraph" alone is too generic to trademark

## Considered options

### Option A — Orbit

- **Pros**: short, evokes structure around a center, easy to pronounce worldwide
- **Cons**: crowded name space; `.com` almost certainly taken

### Option B — Vortex

- **Pros**: visual, memorable, implies motion/graph
- **Cons**: negative connotation risk ("vortex of confusion"), crowded

### Option C — Canopy

- **Pros**: suggests structure over a codebase, natural metaphor, friendly
- **Cons**: generic when searched, forestry/climate domains already use it

### Option D — Loomix

- **Pros**: invented, unique, trademarkable
- **Cons**: no immediate meaning; requires brand-building

### Option E — Semaphore

- **Pros**: technical signaling term, known in dev community
- **Cons**: conflicts with Semaphore CI (major brand)

## Decision

**Pending.** This ADR is `draft` and blocks P5. Owner: David Tran.

Recommended path:
1. Shortlist 3 from above (leaning Canopy, Loomix, Orbit)
2. Check `.com` + `.dev` + GitHub org + trademark database
3. Pick within 4 weeks so P5 prep has runway

## Consequences

- Changing later is expensive — re-issue npm packages, GitHub redirects, SEO rebuild. Pick once.
- Must not contain "Odoo" anywhere in product name, tagline, or package name

## Kill criteria

N/A — this is a one-shot decision. Revisit only if legal forces a change.

## References

- Brief: `project-docs/odoo-semantic-mcp/product_brief.md` (product vision)
- Roadmap: `project-docs/odoo-semantic-mcp/roadmap.md` (P5 distribution — internal)
