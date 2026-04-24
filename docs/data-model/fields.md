---
status: confirmed
confirmed_date: 2026-04-22
scope: data-model/fields
reads-with:

  - models.md
---

# Table: `fields`

One row per field declaration in the source. Same field name appearing in multiple modules = multiple rows.

## Purpose

- Capture every `fields.X(...)` call the indexer sees
- Record override relationships so the resolver can walk the chain
- Power `resolve_field` MCP tool

## Schema (draft)

| Column | Type | Nullable | Description |
| ------ | ---- | -------- | ----------- |
| `id` | bigserial | no | Primary key |
| `tenant` | text | no | Tenant this row belongs to. Auto-filled via DEFAULT current_schema(). Redundant with schema name but enables cross-schema UNION queries to tag row origin without hard-coded literals |
| `model_id` | bigint FK → models | no | Row in `models` that declared this field |
| `field_name` | text | no | E.g. `amount_total` |
| `field_type` | text | no | E.g. `Char`, `Many2one`, `Monetary` |
| `related_model` | text | yes | For `Many2one`/`One2many`/`Many2many`: target model name |
| `related_field` | text | yes | For `One2many`: the inverse field name |
| `compute` | text | yes | Name of the compute method if set |
| `inverse` | text | yes | Name of the inverse method if set |
| `search` | text | yes | Name of the search method if set |
| `store` | bool | yes | Value of `store=` |
| `required` | bool | yes | Value of `required=` |
| `readonly` | bool | yes | Value of `readonly=` |
| `default` | text | yes | Source text of `default=` argument (e.g. `"'draft'"` or `"lambda self: ..."`), null if not declared. Resolver applies Odoo defaults when null |
| `related_path` | text | yes | Dotted path if `related='a.b.c'` |
| `depends` | text[] | yes | `@api.depends` for compute |
| `override_of` | bigint FK → fields | yes | The previous row in the override chain (null for first declaration) |
| `file_path` | text | no | Source file path |
| `start_line` | int | no | First line of the field declaration |
| `end_line` | int | no | Last line of the field declaration |
| `content_hash` | text | no | Hash of the declaration source |
| `indexed_at_sha` | text | no | Git SHA at index time |

## Invariants

- `override_of` points at an older row in module load order (see `modules.load_order`)
- The root of any override chain has `override_of = NULL`
- For a given `(model_id, field_name)` there is at most one row (a module cannot double-declare the same field)
- `related_model` is required iff `field_type` is relational

## Example

```text
id=9001
model_id=317 (sale.order in 'sale')
field_name='amount_total'
field_type='Monetary'
compute='_amount_all'
store=true
override_of=NULL
```

```text
id=9002
model_id=512 (sale.order in 'sale_margin')
field_name='margin'
field_type='Monetary'
compute='_compute_margin'
store=true
override_of=NULL
```

## Notes

- Override vs new: if a later module redeclares an existing field, `override_of` points back; if it declares a new field, `override_of` is NULL
- Computed-field dependency arrows are NOT stored as separate edges — use the `depends` array and join at query time
- `store`, `required`, `readonly` are nullable. `NULL` means the field declaration did not set the argument explicitly — resolver must apply Odoo default behaviour (`store=True` for stored relational/primitive fields, `required=False`, `readonly=False`).
