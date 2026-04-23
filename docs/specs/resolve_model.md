---
status: confirmed
scope: specs/resolve_model
phase: P1
date: 2026-04-21
confirmed_date: 2026-04-22
reads-with:
  - ../product_brief.md
  - ../architecture/mcp-server.md
  - ../data-model/models.md
  - ../data-model/fields.md
  - ../data-model/methods.md
  - ../research/odoo-internals.md
---

# Spec: `resolve_model`

## 1. Purpose

Given a model name (e.g. `sale.order`), return its inheritance chain, delegated models, defining module, and a summary of fields and methods contributed by each module in the chain.

This is the anchor tool for Phase 1. Every other Phase-1 tool narrows from here.

## 2. Input schema

```json
{
  "model_name": "sale.order",
  "include_field_summary": true,
  "include_method_summary": true
}
```

- `model_name` (string, required) — exact `_name`
- `include_field_summary` (bool, optional, default `true`) — if false, skip the field summary block (cheaper call)
- `include_method_summary` (bool, optional, default `false`) — methods are verbose; off by default

## 3. Output schema

```json
{
  "result": {
    "name": "sale.order",
    "primary_module": "sale",
    "abstract": false,
    "transient": false,
    "inheritance_chain": [
      { "module": "sale", "file": "addons/sale/models/sale_order.py", "kind": "primary" },
      { "module": "sale_margin", "file": "...", "kind": "extension" }
    ],
    "delegates_to": [
      { "field": "partner_id", "model": "res.partner" }
    ],
    "fields_contributed": [
      { "module": "sale", "field_name": "amount_total", "is_override": false }
    ],
    "methods_contributed": [
      { "module": "sale_subscription", "method_name": "action_confirm", "is_override": true, "calls_super": true }
    ]
  },
  "indexed_at_sha": "abc1234",
  "warnings": []
}
```

## 4. Algorithm

1. Validate `model_name` is non-empty
2. Look up all rows in `models` where `name = :model_name`; order by `modules.load_order`
3. If no rows → return `404`
4. Chain is the ordered list; first `is_primary_declaration` row flagged as `primary`
5. If `include_field_summary`: join `fields` by `model_id` for every chain element
6. If `include_method_summary`: join `methods` by `model_id`
7. If any chain element has `indexer_notes.dynamic_inherit`, append a warning
8. Return envelope with latest common `indexed_at_sha` across joined rows

## 5. Data accessed

- [`../data-model/models.md`](../data-model/models.md) — chain walk
- [`../data-model/fields.md`](../data-model/fields.md) — optional field summary
- [`../data-model/methods.md`](../data-model/methods.md) — optional method summary
- [`../data-model/modules.md`](../data-model/modules.md) — load-order sort

## 5b. Resolution rules — overview

**Methods and fields use fundamentally different resolution rules.** Do not conflate them.

**Methods** resolve via Python C3 MRO. `_build_model` (`odoo/models.py:694-770`) builds `__base_classes` as a `LastOrderedSet` with the current class first, then parents in list order. After `_prepare_setup` assigns `cls.__bases__ = cls.__base_classes`, Python's C3 linearization runs. Two distinct cases — do **not** conflate them:

- **Multi-inherit** (`_inherit = ['a', 'b']` in one class): `a` enters `__base_classes` before `b`, so C3 puts `a` earlier in the MRO → **first-listed parent wins** on method conflicts.
- **Pure extension chain** (single-model `_inherit = 'sale.order'` across multiple modules): position in any list is irrelevant. Each module's class is prepended into the ancestor MRO, so the **latest-loaded module's class sits earliest** and its method wins.

See `resolve_method` for per-method detail.

**Fields** resolve via `_base_fields` override stack, not MRO. `_setup_base` (`odoo/models.py:3326-3409`) walks `cls.mro()` **in reverse** and collects all field definitions per name. When multiple classes define the same field, a merged field is produced with `_base_fields = tuple(definitions)` where **later entries have higher priority** (`odoo/models.py:3349-3371`). The last-loaded definition wins — the opposite of naive MRO intuition. See `resolve_field` for per-field detail.

**Practical consequence for the indexer:**

- When building `fields_contributed`, walk in load order and flag is_override; the last entry is authoritative.
- When building `methods_contributed`, use MRO order; the first class in the MRO chain that defines the method is the dispatch target.
- Both orderings derive from the same module load order (Section 1 of `../research/odoo-internals.md`).

Detailed rules: [`resolve_field.md`](resolve_field.md) §5b · [`resolve_method.md`](resolve_method.md) §5b.

## 5c. Edge cases — when to return `resolution: unknown`

The research grep across CE 17.0 found **zero** runtime mutations of `_inherit` after class creation (`../research/odoo-internals.md` §5). Treat AST `_inherit` as authoritative. `resolution: unknown` applies only to these three specific cases:

1. **Conditional import guard.** The class lives inside a `try/except ImportError` block in `models/__init__.py` (optional dependency not guaranteed to be installed). Emit `resolution: conditional`.
2. **`_register = False` chain.** The class or a statically-unresolvable ancestor has `_register = False` (e.g. `odoo/addons/base/models/ir_qweb.py:2702`). Cannot determine if the model is actually registered without following the full subclass tree.
3. **DB-origin manual fields.** Fields from `ir.model.fields` rows with `state='manual'` are injected at runtime via `ir.model.fields._add_manual_fields` called from `_setup_base` at `odoo/models.py:3374`. Invisible to static AST. Emit `resolution: unknown` and document in warnings; offer live-DB introspection as an opt-in.

Do not emit `resolution: unknown` for any other reason. In particular, do not treat multi-`_inherit` lists or extension modules as ambiguous — these are fully deterministic from static analysis.

## 6. Out of scope

- Runtime introspection against a live Odoo instance (P3+ feature)
- Returning full method bodies (use `resolve_method` for that)
- Cross-model impact (`impact_analysis` covers that)

## 7. Acceptance criteria

### Correctness (necessary condition)

- [ ] Returns correct chain for 10 curated models (`sale.order`, `account.move`, `res.partner`, `product.template`, `stock.move`, and 5 Viindoo-specific)
- [ ] Handles models with 10+ extensions without ordering drift
- [ ] Flags dynamic `_inherit` as a warning, does not silently drop
- [ ] Correctly merges shared `public` schema + current tenant schema; tenant overrides win

### Token / context reduction (primary value)

- [ ] For the task "list all fields on `sale.order` after installing `sale_margin` + `sale_subscription`", response is ≤2k tokens vs ≥40k tokens if the AI had to read raw source files
- [ ] Target: ≥90% token reduction vs raw-source baseline on the 10-model fixture

### Performance

- [ ] P50 <20ms on the test fixture
- [ ] P99 <100ms

## 8. Open questions

- Should we include inactive (uninstallable) modules in the chain? Leaning: no, but flag in warnings.
- Should `delegates_to` be recursive (follow the delegated model's own chain)? Leaning: no for MVP, caller composes.

## 9. References

- Phase: `project-docs/odoo-semantic-mcp/roadmap.md` (internal)
- Data: `../data-model/models.md`
- Architecture: `../architecture/graph-store.md`
- Research (authoritative evidence): `../research/odoo-internals.md` §2 (inherit algorithm), §5 (dynamic inherit)
