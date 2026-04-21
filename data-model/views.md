---
status: confirmed
confirmed_date: 2026-04-22
scope: data-model/views
reads-with:
  - ../architecture/indexer.md
  - ../specs/resolve_view.md
---

# Table: `views`

One row per XML view declaration (`<record model="ir.ui.view" ...>`). Inheritance and XPath patches are captured in edges.

## Purpose

- Capture every view definition in source
- Record `inherit_id` chain + XPath operations
- Power `resolve_view` MCP tool

## Schema (draft)

| Column | Type | Nullable | Description |
| ------ | ---- | -------- | ----------- |
| `id` | bigserial | no | Primary key |
| `tenant` | text | no | Tenant this row belongs to. Auto-filled via DEFAULT current_schema(). Redundant with schema name but enables cross-schema UNION queries to tag row origin without hard-coded literals |
| `xmlid` | text | no | Fully-qualified xmlid, e.g. `sale.view_order_form` |
| `module_id` | bigint FK → modules | no | Declaring module |
| `model` | text | no | `model` field of the view |
| `view_type` | text | no | `form`, `tree`, `kanban`, `search`, ... |
| `inherit_id` | bigint FK → views | yes | Parent view (NULL for primary views) |
| `priority` | int | no | View priority (default 16) |
| `mode` | text | no | `primary` or `extension` |
| `arch_hash` | text | no | Hash of the `<arch>` content for change detection |
| `file_path` | text | no | Source XML file |
| `start_line` | int | no | First line of the `<record>` element |
| `end_line` | int | no | Last line of the `<record>` element |
| `indexed_at_sha` | text | no | Git SHA at index time |

## Related: `view_patches`

Each inheriting view contains one or more XPath operations. We flatten these into a sibling table:

| Column | Type | Description |
| ------ | ---- | ----------- |
| `id` | bigserial | Primary key |
| `tenant` | text | Tenant this row belongs to. Auto-filled via DEFAULT current_schema(). Redundant with schema name but enables cross-schema UNION queries to tag row origin without hard-coded literals |
| `view_id` | bigint FK → views | The view that contains this patch |
| `ordinal` | int | Order within the view |
| `expr` | text | XPath expression targeted at parent |
| `position` | text | `after`, `before`, `inside`, `replace`, `attributes` |
| `content` | text | Source text of the patch body |

## Invariants

- `inherit_id` chain has no cycles
- `mode = 'extension'` iff `inherit_id IS NOT NULL`
- `xmlid` is unique within `module_id`
- `view_patches.ordinal` is dense per `view_id` (no gaps)

## Example

```text
views row:
id=201
xmlid='sale.view_order_form'
module_id=182 (sale)
model='sale.order'
view_type='form'
inherit_id=NULL
mode='primary'
priority=16
```

```text
views row:
id=334
xmlid='sale_margin.view_order_form_margin'
module_id=248 (sale_margin)
model='sale.order'
view_type='form'
inherit_id=201
mode='extension'
priority=16

view_patches rows:
  (334, ordinal=1, expr="//field[@name='amount_total']", position='after', content='<field name="margin"/>')
```

## Notes

- `position='replace'` with nested inheritance is the highest-risk area for the resolver — see [`../specs/resolve_view.md`](../specs/resolve_view.md)
- Studio-generated views live in the DB and are explicitly out of scope
