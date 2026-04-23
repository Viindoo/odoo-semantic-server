---
status: confirmed
confirmed_date: 2026-04-22
scope: data-model/methods
reads-with:
  - ../architecture/indexer.md
  - models.md
---

# Table: `methods`

One row per method definition in the source. Same method name across modules = multiple rows linked by `override_of`.

## Purpose

- Capture method signatures, decorators, and source ranges
- Record override relationships (`super()` chain)
- Power `resolve_method` MCP tool

## Schema (draft)

| Column | Type | Nullable | Description |
| ------ | ---- | -------- | ----------- |
| `id` | bigserial | no | Primary key |
| `tenant` | text | no | Tenant this row belongs to. Auto-filled via DEFAULT current_schema(). Redundant with schema name but enables cross-schema UNION queries to tag row origin without hard-coded literals |
| `model_id` | bigint FK → models | no | Declaring model row |
| `method_name` | text | no | E.g. `action_confirm` |
| `signature` | text | no | Extracted parameter list, as source text |
| `decorators` | text[] | no | E.g. `['api.model', 'api.depends']` |
| `calls_super` | bool | no | True if the body contains `super().method(...)` |
| `override_of` | bigint FK → methods | yes | Previous row in override chain |
| `file_path` | text | no | Source file path |
| `start_line` | int | no | First line |
| `end_line` | int | no | Last line |
| `content_hash` | text | no | Hash of the method body |
| `indexed_at_sha` | text | no | Git SHA at index time |

## Invariants

- `override_of` points at an older row in load order
- `override_of IS NOT NULL` implies a call to `super()` is expected but not guaranteed (some overrides do not call super — indexer notes this but does not fail)
- For a given `(model_id, method_name)` there is at most one row

## Example

```text
id=4011
model_id=317 (sale.order in 'sale')
method_name='action_confirm'
signature='(self)'
decorators={}
calls_super=false
override_of=NULL
```

```text
id=4012
model_id=512 (sale.order in 'sale_subscription')
method_name='action_confirm'
signature='(self)'
calls_super=true
override_of=4011
```

## Notes

- We do NOT capture method bodies in this table — `content_hash` covers change detection. The vector store holds the actual body text for semantic search
- Method resolution order at runtime depends on MRO; here we provide the **linear override chain per module load order**, which is the useful answer for static tooling
- Decorator flags derivable from `decorators[]` — use GIN index on `decorators` (defined in `architecture/graph-store.md`) for filter queries.
