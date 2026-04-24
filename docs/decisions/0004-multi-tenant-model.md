---
status: accepted
accepted_date: 2026-04-22
scope: decisions/0004
date: 2026-04-21
deciders:
  - David Tran
  - Tran Truong Son
---

# ADR-0004: Multi-tenant overlay model

## Context

Every consumer queries a combination of **shared** Odoo modules (CE, and optionally Enterprise) plus a **private** set of custom modules that is specific to that consumer — Viindoo's own addons, or a paying customer's private repo.

This mirrors how Odoo itself loads modules at runtime: the "same" model (`sale.order`) is materially different depending on which set of addons is installed.

The product must:

1. Let a customer query their own private code alongside the shared Odoo code, without seeing anyone else's private code
2. Avoid re-indexing the same CE code once per tenant (cost + storage)
3. Preserve Odoo's actual override semantics: tenant-private modules load **after** shared modules → win on any override

## Drivers

- **Cost** — CE is ~20M LOC after Enterprise is added. Re-embedding it per customer is prohibitive
- **Correctness** — override chain between CE and customer modules must match runtime
- **Isolation** — no cross-customer leakage; threat model is explicit (see `project-docs/odoo-semantic-mcp/security/threat-model.md`)
- **Simplicity** — one query shape at the handler level, regardless of tenant

## Considered options

### Option A — Single schema, row-level security (RLS) on a `tenant_id` column

- **Pros**: one schema, simplest to reason about, indexes shared naturally
- **Cons**: RLS bugs leak data silently; per-customer cryptographic erasure is hard (can't just drop a schema); operational accidents (a missing `tenant_id` filter) become incidents

### Option B — Schema per tenant, `public` schema for shared

- Shared CE index lives in `public.*`
- Each tenant has their own schema: `viindoo.*`, `cust_acme.*`, ...
- Handler binds to `public.<table>` and `<tenant>.<table>` at query time, unions them, resolves overrides
- **Pros**: isolation is enforced by the database, not by application code; per-customer erase = `DROP SCHEMA`; shared data indexed once
- **Cons**: query code has to know to union; cross-tenant admin reports need a different path

### Option C — Database per tenant

- Each tenant gets its own Postgres DB; shared CE is either copied (expensive) or reached via foreign data wrapper (slow)
- **Pros**: maximum isolation
- **Cons**: storage explosion OR FDW latency; operational overhead per new customer

## Decision

**Option B** — Schema per tenant, with a `public` schema holding the shared CE (and optionally Enterprise) index.

Tenants we plan to support from day one:

| `tenant_id` | Contents | Visibility |
| ----------- | -------- | ---------- |
| `public` (implicit) | Odoo CE index (and Enterprise once we support it) | Readable by every tenant |
| `viindoo` | Viindoo's private addons: `tvtmaaddons`, `erponline-enterprise`, `branding` | Only readable with a Viindoo-scoped token |
| `cust_<id>` | A paying customer's private addons | Only readable with that customer's token |

Every MCP tool call carries an implicit `tenant` from the auth token. Handlers union `public.*` with `<tenant>.*` and resolve overrides with tenant modules loading after shared.

## Consequences

- **Positive**:
  - CE indexed once; storage and re-embedding cost bounded
  - Customer erasure = `DROP SCHEMA CASCADE`, cryptographically complete when combined with per-tenant data-key erasure (per `security/encryption.md`)
  - Isolation enforced by Postgres, not by application filter — much fewer ways to leak
  - Override semantics match runtime Odoo behaviour
- **Negative**:
  - Every query path must be tenant-aware; add lint rule + CI test to forbid cross-schema access outside the union layer
  - Admin / reporting queries cross tenants; those use a separate `viindoo_ops` role path with explicit audit
  - Migrations now fan out to N schemas; use a deploy script that iterates
- **Follow-ups**:
  - Enterprise index — `public.*` or `viindoo.*`? (Enterprise is licensed; not every customer has it.) Decide via follow-up ADR when first paying customer has Enterprise
  - Odoo version dimension — do we have `public.v17`, `public.v18`? Possibly schema per `(odoo_version)` for shared content. Decide when we support 2 versions simultaneously

## Kill criteria

Revisit this ADR if:

- Cross-schema union queries become the dominant cost in query plans (rare at our expected scale)
- A customer requires "their CE override of CE itself" — at that point they need a dedicated database (Option C), not an overlay
- Enterprise-tier customers demand complete physical isolation → migrate them to Option C per-tenant without moving the rest

## References

- Brief: `project-docs/odoo-semantic-mcp/product_brief.md`
- Architecture (internal): `project-docs/odoo-semantic-mcp/architecture/tenancy.md`, `project-docs/odoo-semantic-mcp/architecture/graph-store.md`
- Security: `../security/access-control.md` — threat model at `project-docs/odoo-semantic-mcp/security/threat-model.md`
- Every spec: tenant is now a first-class input
