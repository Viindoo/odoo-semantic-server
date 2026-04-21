---
status: placeholder
scope: security/dpa-template
date: 2026-04-21
reads-with:
  - threat-model.md
  - access-control.md
  - encryption.md
---

# Data Processing Agreement — template

**Status**: placeholder. Legal review required before use with any paying Hosted customer.

## Purpose

Hosted BYOC customers hand us their source code, which may include personal data or trade secrets. The DPA sets out how Viindoo processes that data.

## Sections the final DPA must cover

1. **Parties** — Viindoo (processor) and customer (controller)
2. **Subject matter and duration** of processing
3. **Nature and purpose** of processing (indexing, embedding, serving queries)
4. **Type of data** processed (source code, which may contain personal data, secrets, etc.)
5. **Categories of data subjects** (developers named in commits, customers referenced in code comments, etc.)
6. **Sub-processors**
   - Embedding provider (Voyage, if API mode)
   - Hosting provider (Hetzner)
   - Customer must be notified before new sub-processor is added
7. **International transfers** — where data is processed; SCCs if applicable
8. **Security measures** — reference `access-control.md`, `encryption.md`
9. **Data subject rights** — how Viindoo supports controller in fulfilling them
10. **Data breach notification** — timeline (target: 24h from detection), content of notification
11. **Audit rights** — customer's right to audit Viindoo's security posture (scope, frequency)
12. **Return / deletion** on termination — secure erasure with cryptographic-erasure evidence
13. **Liability** — caps, carve-outs

## Sources

- EU Standard Contractual Clauses (current version)
- Template DPAs from reputable SaaS (e.g. Vercel, Supabase) as structural references — not content copy

## Workflow

1. Draft legal agreement using standard EU SCC as base
2. Tech review (we — do our operations match what the DPA claims)
3. Legal review (Viindoo legal counsel or external)
4. Customer review during onboarding
5. Signed version filed per customer

## Open questions

- Do we need per-country variations from day 1? Leaning: no, EU SCC baseline covers majority; add as demand appears
- Do we support BAA (Business Associate Agreement) for US healthcare customers? Post-MVP
