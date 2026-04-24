---
status: draft
scope: security
reads-with:
  - ../architecture/deployment.md
---

# Security

Everything a security reviewer, auditor, or enterprise-sales counterpart needs to understand the security posture. The Hosted BYOC tier stores customers' private source code — an incident here is catastrophic.

## Principles

1. **Least privilege** — each tool sees only the customer it was called for
2. **Schema isolation** — one PostgreSQL schema per customer, never shared tables
3. **Encryption at rest** — filesystem-level (LUKS) + PostgreSQL-level for sensitive columns
4. **No plaintext secrets in repo** — env + secret manager only
5. **Audit everything that touches customer code** — index runs, MCP queries, admin access
6. **Off-ramp** — self-host is a first-class path, not a fallback

## Index

| File | Purpose | Status |
| ---- | ------- | ------ |
| [`access-control.md`](access-control.md) | RBAC + per-customer schema isolation + admin access procedures | placeholder |
| [`encryption.md`](encryption.md) | At-rest + in-transit posture; key management | placeholder |
| [`dpa-template.md`](dpa-template.md) | Data Processing Agreement template for Hosted-tier customers | placeholder |

The STRIDE threat model is an internal design artefact kept in `project-docs/odoo-semantic-mcp/security/threat-model.md`.

## Explicitly out of scope (for now)

- SOC 2 / ISO 27001 certification — revisit when Hosted tier reaches 50+ paying customers
- On-device encryption of customer laptops — their responsibility
- Hardening the customer's own Odoo instance — we index; we do not run it

## Review process

Any PR that changes data flow, adds a customer boundary crossing, or modifies auth must go through:

1. Author self-check against the threat model (see `project-docs/odoo-semantic-mcp/security/threat-model.md`)
2. Review with the `review.md` context
3. Security-reviewer sub-agent pass before merge

A PR that fails any of these cannot land, even with product-side approval.
