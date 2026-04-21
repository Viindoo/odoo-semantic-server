---
status: placeholder
scope: security/access-control
date: 2026-04-21
reads-with:
  - ../architecture/deployment.md
  - threat-model.md
---

# Access control

**Status**: placeholder. Flesh out alongside Hosted tier implementation.

## Principles

- **Overlay model.** Every tenant reads `public.*` (shared Odoo CE) + their own `<tenant>.*`. No tenant can read any other tenant's schema. See [`../decisions/0004-multi-tenant-model.md`](../decisions/0004-multi-tenant-model.md) and [`../architecture/tenancy.md`](../architecture/tenancy.md).
- **One schema per tenant.** Cross-tenant queries are forbidden — enforced by Postgres role grants and a lint rule on handler code
- **Tokens scope to one tenant.** A customer with 3 tenants has 3 tokens; compromise of one does not reveal the others
- **Admin access uses a separate credential path**, never the customer token flow
- **Every read is logged.** Writes (indexer) produce a shorter summary log; reads (MCP tool calls) produce a per-call audit event

## Roles

| Role | Read scope | Write scope | Typical holder |
| ---- | ---------- | ----------- | -------------- |
| `tenant_token` | `public.*` + own `<tenant>.*` | — | Customer's CI / AI client |
| `tenant_admin` | `public.*` + own `<tenant>.*` | own `<tenant>.*` (trigger re-index) | Customer admin |
| `viindoo_indexer` | `public.*` | `public.*` | Viindoo CI running CE indexer |
| `viindoo_ops` | all schemas, audited | — | On-call engineer |
| `viindoo_root` | all schemas | limited emergency writes, 2-of-N approval, fully audited | Senior engineer (emergencies) |

## Boundary crossings

Every boundary crossing (token → schema, admin → all customers) is:

1. Checked in the handler
2. Logged to the audit trail with actor + target + action
3. Rate-limited

## Kill switch

Per-customer kill switch that:

- Immediately invalidates all active tokens
- Locks the schema from reads
- Preserves data for recovery (not deletion)

## Open questions

- Who holds the root credential? Hardware key? 2-of-N Shamir split?
- How do we handle a customer leaving? Schema export → secure delete with cryptographic erasure of the LUKS key
- Do we offer SSO for Hosted tier? Not MVP, revisit post-P5
