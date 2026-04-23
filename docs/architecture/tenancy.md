---
status: confirmed
confirmed_date: 2026-04-22
scope: architecture/tenancy
reads-with:
  - overview.md
  - graph-store.md
  - ../decisions/0004-multi-tenant-model.md
  - ../security/access-control.md
---

# Tenancy

Multi-tenant overlay model. Shared `public` schema holds Odoo CE; each tenant has their own schema for private addons. Queries union the two at runtime and resolve overrides with tenant modules winning.

> Decision: [`../decisions/0004-multi-tenant-model.md`](../decisions/0004-multi-tenant-model.md)

## Layer picture

```text
+----------------------------------------------------+
|                   Postgres                         |
|                                                    |
|  public schema                                     |
|  +----------------------------------------------+  |
|  | Odoo CE index                                |  |
|  | (shared, read-only for every tenant)         |  |
|  +----------------------------------------------+  |
|                                                    |
|  viindoo schema                                    |
|  +----------------------------------------------+  |
|  | Viindoo private addons                       |  |
|  | tvtmaaddons/, erponline-enterprise/, branding|  |
|  +----------------------------------------------+  |
|                                                    |
|  cust_acme schema                                  |
|  +----------------------------------------------+  |
|  | Acme's private addons                        |  |
|  +----------------------------------------------+  |
|                                                    |
|  cust_<id> schema  ... one per paying customer     |
+----------------------------------------------------+
```

## Query shape

Every handler call is tenant-scoped. The current tenant is derived from the auth token; the caller never passes it as a free-form parameter (that would be a trust hole).

```sql
-- Example: resolve a model across shared + current tenant
WITH m AS (
    SELECT *, 'public' AS source FROM public.models WHERE name = :model
    UNION ALL
    SELECT *, :tenant AS source FROM <tenant>.models WHERE name = :model
)
SELECT * FROM m
ORDER BY source_order(source), module_load_order;
```

Where `source_order('public')` < `source_order(<any tenant>)` — shared modules load first, tenant overrides win.

## Which tenant sees what

| Tenant | Reads | Writes |
| ------ | ----- | ------ |
| `public` | — (indexed by Viindoo) | — (Viindoo ops only) |
| `viindoo` | `public` + `viindoo` | `viindoo` (Viindoo dev) |
| `cust_<id>` | `public` + `cust_<id>` | `cust_<id>` (customer) |
| `viindoo_ops` (admin) | all (audited) | limited emergency writes |

## Indexing responsibility

- **`public`** — Viindoo re-indexes on Odoo CE release (manual trigger, per Odoo version)
- **`viindoo`** — Viindoo CI re-indexes on every push to `tvtmaaddons` / related repos
- **`cust_<id>`** — customer's own indexer, scheduled or webhook-driven from their git

## Storage and cost model

- Shared `public` schema indexed once → ~20M LOC (CE + Enterprise later) embedded once, stored once
- Each tenant pays to embed only their overlay — typically 50k–500k LOC
- Per-tenant cost dominates embedding cost; shared cost is amortised across all tenants

## Override semantics

Tenant modules load after shared modules. If both `public.sale` and `cust_acme.sale_acme` declare changes to `sale.order.amount_total`, the override chain is:

1. `public.sale` (root)
2. `cust_acme.sale_acme` (overlay override)

Effective field definition is the tenant's override, with a chain visible in the response.

## What this means per tool

- `resolve_model`, `resolve_field`, `resolve_method` — chain spans shared + tenant
- `resolve_view` — view inheritance chain spans shared + tenant
- `find_examples` — by default searches `public + <tenant>`, caller can restrict to tenant-only
- `impact_analysis` — blast radius is computed within `public + <tenant>`; never crosses to other tenants

## Failure modes

- **Missing tenant schema** — auth layer rejects before the handler runs
- **Tenant references shared module not present** (e.g. customer built on a module that was removed from CE) — indexer emits a warning, tool response flags it
- **Concurrent writes during query** — Postgres MVCC handles it; no locking needed at the read path

## Open design questions

- **Cross-schema foreign keys — CLOSED 2026-04-22.** Decision: **soft logical references only**. Cross-schema refs (e.g. tenant `fields.override_of` → `public.fields.id`) are stored as `bigint` with no `REFERENCES` constraint. Integrity is checked via `indexed_at_sha` staleness (resolver returns 409 if stale). Hard FK constraints still apply within a single schema. Rationale: independent re-index cycles between public and tenant; hard FK couples them, making partial re-index and per-tenant backup fragile. See `architecture/graph-store.md` "Tenancy — no cross-schema hard foreign keys".
- **Odoo version dimension.** If we support v17 and v18 simultaneously, `public` might need to split into `public_v17`, `public_v18`. Revisit when we onboard a v18 customer.
- **Enterprise tier.** Is Odoo Enterprise part of `public` (licensed content leaks to any tenant) or a separate `public_enterprise` readable only by tenants whose contract includes it? Follow-up ADR.

## Not handled here

- Per-customer encryption keys — see [`../security/encryption.md`](../security/encryption.md)
- Tenant onboarding flow — product concern, will be a spec when P5 approaches
- Cross-tenant analytics — explicit out-of-scope; requires a separate admin data path with its own audit
