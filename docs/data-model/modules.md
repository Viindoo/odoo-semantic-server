---
status: confirmed
confirmed_date: 2026-04-22
scope: data-model/modules
reads-with:


  - ../decisions/0004-multi-tenant-model.md
  - models.md
---

# Table: `modules`

One row per Odoo addon present in any of the configured addon paths.

This table (and every other data-model table) exists **per schema** — once in `public` for shared Odoo CE, once in each tenant schema for that tenant's private addons. See [`../decisions/0004-multi-tenant-model.md`](../decisions/0004-multi-tenant-model.md).

## Purpose

- Capture manifest metadata as-is from `__manifest__.py`
- Hold dependency graph used for load-order simulation
- Anchor every model/field/method/view back to its defining module

## Schema (draft)

| Column | Type | Nullable | Description |
| ------ | ---- | -------- | ----------- |
| `id` | bigserial | no | Primary key |
| `name` | text | no | Technical name = directory name (e.g. `sale_management`) |
| `manifest_path` | text | no | Absolute path to `__manifest__.py` at index time |
| `version` | text | yes | Version string from manifest |
| `depends` | text[] | no | Module names from manifest `depends` list |
| `auto_install` | bool | no | Manifest flag |
| `installable` | bool | no | Manifest flag |
| `source_repo` | text | yes | Logical repo name (e.g. `odoo/odoo`, `viindoo/tvtmaaddons`) |
| `tenant` | text | no | Tenant this row belongs to. Auto-filled via DEFAULT current_schema(). Redundant with schema name but enables cross-schema UNION queries to tag row origin without hard-coded literals |
| `load_order` | int | yes | Simulated load order rank (null until simulation runs) |
| `content_hash` | text | no | Hash of the `__manifest__.py` content |
| `indexed_at_sha` | text | no | Git SHA at the time this row was indexed |

## Invariants

- `name` is unique within `(tenant, source_repo)`. Same name can appear in `public` and in a tenant schema (that is exactly how tenant overrides a shared module)
- `depends` items resolve against `public.modules` union current-tenant modules; missing → indexer warning, not error
- `load_order` is monotonic within `(tenant-scope union public)`. `public` modules load first, tenant modules load after, so tenant wins on overrides
- Every row's `tenant` matches the schema the row lives in (belt + braces)

## Example

```text
id=42
name='sale_subscription'
manifest_path='/.../enterprise/sale_subscription/__manifest__.py'
version='17.0.1.0.0'
depends={'sale_management', 'product'}
source_repo='odoo/enterprise'
tenant='public'
load_order=184
indexed_at_sha='f8a1b2c'
```

## Notes

- We do not track install state — this is a static view of the code, not a live database
- We do not parse `data:` lists here; those are consumed by the view indexer
- Cycle detection in `depends` → indexer hard-fails with a clear error pointing at the cycle
- License not tracked in P1 — add via ADR when DPA/legal needs it.
