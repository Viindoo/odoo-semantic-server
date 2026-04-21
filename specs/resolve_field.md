---
status: confirmed
scope: specs/resolve_field
phase: P1
date: 2026-04-21
confirmed_date: 2026-04-22
reads-with:
  - ../product_brief.md
  - ../architecture/mcp-server.md
  - ../data-model/fields.md
  - ../research/odoo-internals.md
---

# Spec: `resolve_field`

## 1. Purpose

Given `(model_name, field_name)`, return the full override chain for that field — root declaration, each override in module-load order, and the final effective definition. Includes computed/related metadata.

## 2. Input schema

```json
{
  "model_name": "sale.order",
  "field_name": "amount_total",
  "include_source_snippets": false
}
```

- `model_name` (string, required)
- `field_name` (string, required)
- `include_source_snippets` (bool, optional, default `false`) — when true, each chain element carries its source text. Expensive.

## 3. Output schema

```json
{
  "result": {
    "model_name": "sale.order",
    "field_name": "amount_total",
    "chain": [
      {
        "module": "sale",
        "field_type": "Monetary",
        "compute": "_amount_all",
        "store": true,
        "file": "addons/sale/models/sale_order.py",
        "line_range": [412, 418],
        "is_override": false
      },
      {
        "module": "sale_margin",
        "field_type": "Monetary",
        "compute": "_amount_all_with_margin",
        "store": true,
        "is_override": true
      }
    ],
    "effective": {
      "field_type": "Monetary",
      "compute": "_amount_all_with_margin",
      "store": true,
      "depends": ["order_line.price_total", "margin"]
    }
  },
  "indexed_at_sha": "abc1234",
  "warnings": []
}
```

## 4. Algorithm

1. Validate inputs
2. Resolve the `models` rows for `model_name` (all extensions, load-order sorted)
3. Join `fields` where `model_id IN (...) AND field_name = :field_name`
4. Walk `override_of` to produce the chain
5. Compose `effective` by merging non-null attributes from root → last override
6. For computed fields, union `depends` across the chain
7. If any row is flagged dynamic or missing, add warning
8. Return envelope

## 5. Data accessed

- [`../data-model/fields.md`](../data-model/fields.md) — primary
- [`../data-model/models.md`](../data-model/models.md) — model resolution
- [`../data-model/modules.md`](../data-model/modules.md) — load order

## 5b. Resolution rules — fields

**Rule: last-loaded definition in `_base_fields` stack wins.**

`_setup_base` (`odoo/models.py:3326-3409`) walks `cls.mro()` **in reverse** (earliest ancestor first) and collects all definitions for each field name. When more than one class defines a field with the same name, the framework produces a merged field:

```python
_base_fields = tuple(definitions)   # index 0 = earliest, index -1 = latest
```

The latest entry (`_base_fields[-1]`) has the highest priority and determines the effective field definition (`odoo/models.py:3349-3371`). This is the **inverse** of Python method MRO: for fields, the last-loaded module overrides earlier ones.

**For the algorithm (Step 5 — compose `effective`):**

Walk the chain in load order (earliest module first). For each non-null attribute in a later override, the later value replaces the earlier one. The final entry in the chain is authoritative for any attribute it explicitly sets.

**`_inherits` delegation fields** follow a separate path: `_add_inherited_fields` (`odoo/models.py:3256-3284`) injects shadow `related` fields for each parent field not already present on the child. If the child later defines the same field locally, `_add_inherited_fields` skips the shadow and the local definition wins (`odoo/models.py:3374`).

**Multi-inherit `_inherit = ['a', 'b']`:** fields from both `a` and `b` enter the same `_base_fields` merge. Extensions loaded after both parents still override via the same stack — module load order remains the tiebreaker, not the position in the `_inherit` list. (Method resolution is the opposite — see `resolve_method` §5b.)

Source: `../research/odoo-internals.md` §2, specifically `odoo/models.py:3326-3409`.

## 5c. Edge cases — when to return `resolution: unknown`

Three specific situations; no others warrant `resolution: unknown`:

1. **Conditional import guard.** The class or the module file that introduces this field override is imported inside a `try/except ImportError` block. Emit `resolution: conditional`.
2. **`_register = False` ancestor.** The indexer cannot confirm the class is registered without resolving the full subclass tree. Example: `odoo/addons/base/models/ir_qweb.py:2702`.
3. **DB-origin manual field.** `ir.model.fields` row with `state='manual'` — injected at runtime via `ir.model.fields._add_manual_fields` → `_setup_base` at `odoo/models.py:3374`. Invisible to static AST. Flag in `warnings` array; live-DB introspection may resolve it in a future L2 layer.

All other cases (multi-`_inherit`, extension modules from third parties in the addons path, etc.) are fully deterministic from AST and module load order.

## 6. Out of scope

- Cross-field dependency graph (which other fields depend on this one) — that's `impact_analysis`
- Runtime value of the field on an instance

## 7. Acceptance criteria

### Correctness (necessary condition)

- [ ] 95% accuracy on a hand-labelled test set of 50 field override chains
- [ ] Correct `effective` merge for `compute`, `store`, `required`, `readonly`, `related`
- [ ] Shared (`public`) + tenant override chain correctly ordered
- [ ] Returns clear error for non-existent model or field

### Token / context reduction (primary value)

- [ ] For "what is the final definition of `sale.order.amount_total`", response is ≤1k tokens vs ≥15k tokens if AI had to read the override chain from raw files
- [ ] Target: ≥90% token reduction vs raw-source baseline

### Performance

- [ ] P50 <20ms

## 8. Open questions

- How to represent "field removed in an override" (`False` marker)? Leaning: emit chain entry with `removed: true`
- For `related='a.b.c'`, should we recursively resolve the path? Leaning: no for MVP, caller composes with `resolve_model` calls.

## 9. References

- Data: `../data-model/fields.md`
- Companion: `../specs/resolve_model.md` (overview of method vs field resolution split)
- Research (authoritative evidence): `../research/odoo-internals.md` §2 (inherit algorithm), §3 (_inherits delegation), §5 (dynamic inherit)
