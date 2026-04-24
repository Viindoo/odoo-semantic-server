---
status: draft
scope: data-model
reads-with:
  - ../decisions/0004-multi-tenant-model.md
  - ../decisions/0001-postgres-vs-neo4j.md
---

# Data model

One file per table. Each file owns its schema, invariants, and example rows. Cross-table relationships are described in `project-docs/odoo-semantic-mcp/architecture/graph-store.md` (internal design doc).

## Tenancy (applies to every table in this folder)

Every table below exists **per schema**:

- `public.<table>` — shared Odoo CE index, indexed once by Viindoo, readable by every tenant
- `<tenant>.<table>` — per-tenant private addons (`viindoo.*`, `cust_<id>.*`)

Handlers query `public.<table> UNION ALL <tenant>.<table>` and resolve overrides with tenant rows winning. See [`../decisions/0004-multi-tenant-model.md`](../decisions/0004-multi-tenant-model.md).

Columns defined per-table apply to both schemas. The `tenant` column on each row is a belt-and-braces copy of the schema the row lives in — it makes audit logs and cross-table debugging unambiguous.

## Index

| File | Table | Role |
| ---- | ----- | ---- |
| [`modules.md`](modules.md) | `modules` | Odoo addon metadata and dependency graph |
| [`models.md`](models.md) | `models` | ORM classes (`_name`) with inheritance + delegation edges |
| [`fields.md`](fields.md) | `fields` | Per-model fields with override chain |
| [`methods.md`](methods.md) | `methods` | Per-model methods with override + super chain |
| [`views.md`](views.md) | `views` | XML views with `inherit_id` + xpath patches |

## Editing rules

- Every schema change to a table must be reflected in its file **in the same commit** as the code change
- Every new column needs: purpose, default, nullability, invariant (if any)
- Breaking changes require an ADR
- Example rows must be real — use fixtures from CE where possible, not invented ones
