---
status: confirmed
scope: specs/resolve_method
phase: P1
date: 2026-04-21
confirmed_date: 2026-04-22
reads-with:
  - ../data-model/methods.md
  - resolve_model.md
---

# Spec: `resolve_method`

## 1. Purpose

Given `(model_name, method_name)`, return the full override chain including `super()` usage per step. Enables AI to understand "what does `action_confirm` actually do after all the modules touch it".

## 2. Input schema

```json
{
  "model_name": "sale.order",
  "method_name": "action_confirm",
  "include_source_snippets": true
}
```

- `include_source_snippets` defaults **`true`** here (method bodies are the whole point)

## 3. Output schema

```json
{
  "result": {
    "model_name": "sale.order",
    "method_name": "action_confirm",
    "chain": [
      {
        "module": "sale",
        "decorators": [],
        "signature": "(self)",
        "calls_super": false,
        "file": "addons/sale/models/sale_order.py",
        "line_range": [812, 870],
        "snippet": "def action_confirm(self): ..."
      },
      {
        "module": "sale_subscription",
        "decorators": [],
        "signature": "(self)",
        "calls_super": true,
        "snippet": "def action_confirm(self): ... super().action_confirm() ..."
      }
    ],
    "chain_is_broken": false
  },
  "indexed_at_sha": "abc1234",
  "warnings": []
}
```

`chain_is_broken = true` when any intermediate override does not call super — the final behaviour drops earlier overrides. Flag this loudly.

## 4. Algorithm

1. Validate inputs
2. Gather all `models` rows for `model_name`
3. Join `methods` where `model_id IN (...) AND method_name = :method_name`
4. Walk `override_of` chain in load order
5. Detect `chain_is_broken`: any non-root row with `calls_super = false`
6. Attach source snippets if requested (read file + slice by line range)
7. Return envelope

## 5. Data accessed

- [`../data-model/methods.md`](../data-model/methods.md) — primary
- [`../data-model/models.md`](../data-model/models.md)
- [`../data-model/modules.md`](../data-model/modules.md) — load order
- Filesystem for source snippets

## 5b. Resolution rules — methods

**Rule: Python C3 MRO, first-listed parent wins.**

`_build_model` (`odoo/models.py:694-770`) builds `__base_classes` via a `LastOrderedSet` starting with the current class, then appending parents in `_inherit` list order. After `_prepare_setup` sets `cls.__bases__ = cls.__base_classes`, Python's C3 linearization runs. For a multi-parent case `_inherit = ['a', 'b']` the effective MRO is roughly `current_def → a_registry → b_registry → base`. A method defined in both `a` and `b` resolves to `a`'s version — **first-listed parent wins** (`odoo/models.py:737-753`).

**Latest-loaded module still overrides earlier modules** for pure extension chains (`_inherit = 'sale.order'` in multiple modules). This is consistent: the latest-loaded module's definition class sits earliest in `__base_classes`, so it is first in the C3 result and its method wins.

**Contrast with fields:** fields use `_base_fields` stack where the last-loaded definition wins, not the first in MRO. The two rules diverge specifically for multi-`_inherit` (`['a', 'b']`) scenarios — do not unify them.

**For the algorithm (Step 4 — walk chain in load order):** build the chain in module load order (earliest first), then flag `is_override = true` for all but the first occurrence. The effective dispatch target is the class earliest in the final MRO, which corresponds to the latest-loaded module in a pure extension chain.

Source: `project-docs/odoo-semantic-mcp/research/odoo-internals.md` §2, specifically `odoo/models.py:694-770` (`_build_model`, `LastOrderedSet`, `__base_classes`).

## 5c. Edge cases — when to return `resolution: unknown`

Three specific situations; no others warrant `resolution: unknown`:

1. **Conditional import guard.** The module file containing this method override is imported inside a `try/except ImportError` block in `models/__init__.py`. The method may or may not be present depending on whether the optional dependency is installed. Emit `resolution: conditional`.
2. **`_register = False` ancestor.** The class or a base it inherits from has `_register = False` (e.g. `odoo/addons/base/models/ir_qweb.py:2702`). Cannot confirm MRO without resolving the full subclass tree statically.
3. **DB-origin model.** The model itself is `state='manual'` (Studio-generated). Its class hierarchy is assembled at runtime from `ir.model` rows (`odoo/models.py:3374`). No static AST to walk. Emit `resolution: unknown` in warnings.

All other method resolution — including multi-`_inherit` lists, third-party addons on the path, or modules with complex dependency graphs — is fully deterministic from static AST plus module load order (Section 1 of `project-docs/odoo-semantic-mcp/research/odoo-internals.md`).

## 6. Out of scope

- Dynamic runtime resolution (monkey-patches at runtime, `_patch_method`)
- Cross-method call graph (who calls whom) — possibly a later tool
- Tests that exercise the method — `impact_analysis` covers that

## 7. Acceptance criteria

### Correctness (necessary condition)

- [ ] Correct chain for 20 hand-labelled method overrides (mix of `public`-only, tenant-only, and tenant-overrides-public)
- [ ] Correctly flags `chain_is_broken` when intermediate overrides skip super
- [ ] Snippets are byte-accurate (no whitespace drift)

### Token / context reduction (primary value)

- [ ] For "what does `sale.order.action_confirm` do after `sale_subscription` overrides it", response with snippets ≤4k tokens vs ≥30k tokens if AI had to read both files
- [ ] Target: ≥70% token reduction vs raw-source baseline (lower than other tools because snippets are the point)

### Performance

- [ ] P50 <50ms (snippet read is the tax)

## 8. Open questions

- How to handle `@api.model` vs `@api.multi` (legacy) differences in chain? Probably note in warnings.
- Private helpers (`_compute_...`) — same tool or a variant? Leaning: same tool, behaviour identical.

## 9. References

- Data: `../data-model/methods.md`
- Companion: `../specs/resolve_model.md` (overview of method vs field resolution split)
- Research (authoritative evidence): `project-docs/odoo-semantic-mcp/research/odoo-internals.md` §2 (inherit algorithm + `LastOrderedSet`), §5 (dynamic inherit)
