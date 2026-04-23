---
status: draft
scope: specs/impact_analysis
phase: P4
reads-with:
  - ../product_brief.md
  - ../data-model/models.md
  - ../data-model/fields.md
  - ../data-model/methods.md
  - ../data-model/views.md
---

# Spec: `impact_analysis`

## 1. Purpose

Given an entity (model, field, method, or view), return every other place in the codebase affected by changing it. Used by AI and humans during refactors to understand blast radius before editing.

## 2. Input schema

```json
{
  "entity": {
    "kind": "field",
    "model_name": "sale.order",
    "name": "margin"
  },
  "depth": 2,
  "include_tests": true
}
```

- `entity.kind` — one of `model | field | method | view`
- `name` + identifiers per kind
- `depth` (int, default `2`) — how many hops to traverse
- `include_tests` (bool, default `true`)

## 3. Output schema

```json
{
  "result": {
    "entity": { "kind": "field", "model_name": "sale.order", "name": "margin" },
    "direct": {
      "fields": [{ "module": "sale_margin", "field": "margin_percent", "reason": "related='margin'" }],
      "methods": [{ "module": "sale_margin", "method": "_compute_margin_percent", "reason": "reads margin" }],
      "views": [{ "xmlid": "sale_margin.view_order_form_margin", "reason": "renders field" }],
      "tests": [{ "file": "...", "reason": "asserts on margin" }]
    },
    "indirect": [
      { "hop": 2, "entity": { "kind": "method", "model_name": "sale.report", "name": "_get_margin" } }
    ]
  },
  "indexed_at_sha": "abc1234",
  "warnings": []
}
```

## 4. Algorithm

1. Validate entity
2. Resolve entity to concrete row in graph
3. Direct hop — query reverse edges:
   - For field: other fields with `related_model+related_field` pointing here; methods with body referencing the field (requires vector store cross-check for accuracy); views rendering the field
   - For method: methods calling it via `super()` chain; vector search for bodies mentioning the method name
   - For model: fields, methods, views targeting this model
   - For view: views with `inherit_id` = this view
4. If `depth > 1`: recurse on each direct hit, mark as indirect
5. If `include_tests`: join `models/methods/fields` against files in `tests/` directories
6. Return envelope

## 5. Data accessed

- All graph tables
- Vector store (for body-text cross-check)
- Filesystem (for test file path detection)

## 6. Out of scope

- Runtime call graph (require booting Odoo)
- Downstream systems (webhooks, external callers outside the codebase)
- UI-driven behaviour outside explicit views (JS event handlers attached dynamically)

## 7. Acceptance criteria

### Correctness (necessary condition)

- [ ] Covers >80% of actually-affected files vs manual review on 5 historical refactor tickets
- [ ] False positive rate <20% (measured against manual review)
- [ ] Blast radius stays within `public + <tenant>`; never crosses to other tenants
- [ ] Clearly distinguishes graph-proven vs vector-guessed hits via a `confidence` score

### Token / context reduction (primary value)

- [ ] For "impact of renaming `sale.order.margin`", response ≤5k tokens vs ≥100k if AI had to grep the entire codebase
- [ ] Target: ≥95% token reduction vs raw-grep-and-read baseline

### Performance

- [ ] `depth=1` P50 <100ms
- [ ] `depth=2` P50 <500ms

## 8. Open questions

- Should vector-only hits be returned by default or gated behind a flag? Leaning: gated, because false positives cost trust
- What's the right default `depth`? Leaning: 2 for interactive use, 1 for CI guardrails

## 9. References

- All data-model files (this tool touches everything)
- Related: `../specs/find_examples.md` (shared vector infrastructure)
