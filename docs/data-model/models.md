---
status: confirmed
confirmed_date: 2026-04-22
scope: data-model/models
reads-with:

  - modules.md
  - fields.md
  - methods.md
---

# Table: `models`

One row per ORM model declaration found by the indexer. A single `_name` can appear in many rows — one per module that declares or extends it.

## Purpose

- Represent the distribution of a model across modules
- Capture inheritance edges (`_inherit`, `_inherits`)
- Anchor fields and methods to a defining module

## Schema (draft)

| Column | Type | Nullable | Description |
| ------ | ---- | -------- | ----------- |
| `id` | bigserial | no | Primary key |
| `tenant` | text | no | Tenant this row belongs to. Auto-filled via DEFAULT current_schema(). Redundant with schema name but enables cross-schema UNION queries to tag row origin without hard-coded literals |
| `name` | text | no | `_name` of the model (e.g. `sale.order`) |
| `module_id` | bigint FK → modules | no | Module that declared this row |
| `is_primary_declaration` | bool | no | True for the first-loaded module that sets `_name` |
| `inherits_from` | text[] | no | `_inherit` list |
| `delegates_to` | jsonb | no | `_inherits = {'linked.model': 'field_id'}` map |
| `table` | text | yes | Explicit `_table` if set |
| `rec_name` | text | yes | `_rec_name` if set |
| `order` | text | yes | `_order` if set |
| `abstract` | bool | no | True for `models.AbstractModel` |
| `transient` | bool | no | True for `models.TransientModel` |
| `file_path` | text | no | Relative path to the source file |
| `start_line` | int | no | First line of the class declaration |
| `end_line` | int | no | Last line of the class declaration |
| `content_hash` | text | no | Hash of the class body |
| `indexed_at_sha` | text | no | Git SHA at index time |
| `indexer_notes` | jsonb | no | Diagnostic hints from indexer. Keys: `dynamic_inherit` (bool), `conditional_import` (bool), `register_false_chain` (bool). Default `'{}'::jsonb` |

## Invariants

- For a given `name`, exactly one row has `is_primary_declaration = true`
- Every item in `inherits_from` must resolve to some row with `name = that item` (or be flagged as missing)
- `abstract` and `transient` are mutually exclusive
- `module_id` + `name` is unique (a module cannot declare the same model twice)

## Example

```text
id=317
name='sale.order'
module_id=182 (sale)
is_primary_declaration=true
inherits_from={}
delegates_to={}
abstract=false
transient=false
file_path='addons/sale/models/sale_order.py'
start_line=18
end_line=1204
```

```text
id=512
name='sale.order'
module_id=248 (sale_margin)
is_primary_declaration=false
inherits_from={'sale.order'}
```

## Notes

- We do NOT attempt to compute a "merged view" of a model at the row level. Merging is the job of the resolver at query time
- Dynamic `_inherit` (variable-valued) → the row's `inherits_from` is empty and `indexer_notes` field captures `{"dynamic_inherit": true}`
