---
status: placeholder
scope: security/threat-model
date: 2026-04-21
reads-with:
  - ../architecture/deployment.md
  - access-control.md
---

# Threat model (STRIDE)

**Status**: placeholder. Flesh out before onboarding first Hosted BYOC customer.

## Assets

| Asset | Classification | Where it lives |
| ----- | -------------- | -------------- |
| Customer source code | Confidential | Postgres + filesystem, encrypted at rest |
| Embedding vectors of customer code | Confidential (derivable to source) | `pgvector` tables |
| Customer identity + billing | Confidential | Reverse-proxy auth layer |
| Index SHA | Internal | Cache metadata table |
| Audit log | Confidential | Append-only log store |

## Actors

- **Paying customer** — authorised consumer of their own index
- **Malicious customer** — authorised but attempts to access neighbours' data
- **External attacker** — no credentials; attempts network-layer access
- **Insider (Viindoo engineer)** — has prod access for ops
- **Compromised dependency** — upstream package or model provider

## STRIDE (to be completed)

### Spoofing

- Tool call without auth token → reverse proxy rejects
- Compromised API key → rotation + rate-limit anomaly detection

### Tampering

- Indexer input manipulation (customer pushes malicious `__manifest__.py`) — bounded: we parse, we do not execute
- Vector index poisoning — out-of-band injection requires DB write access; mitigated by schema isolation

### Repudiation

- "I never ran that query" — audit log is append-only, signed batches

### Information disclosure

- **Primary risk.** Cross-customer data leak via shared Postgres. Mitigation: one schema per customer, every query in a handler must include schema in the SQL (lint rule + tests)
- Embedding inversion attacks — out-of-scope for threat-model v1, note as known research risk

### Denial of service

- Single customer runs unbounded indexing → quotas per project (LOC limit, embed token limit)
- Query flood → rate limit per token

### Elevation of privilege

- MCP handler calls out to filesystem — sandboxed working directory
- Admin endpoints separated from tool endpoints; admin auth is a separate credential path

## Next steps

- Fill each STRIDE section with concrete mitigations + tests
- Map each mitigation to an implementation artifact (code location, config)
- Run a tabletop exercise with David before first Hosted customer
