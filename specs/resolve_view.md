---
status: confirmed
confirmed_date: 2026-04-22
scope: specs/resolve_view
phase: P2
reads-with:
  - ../product_brief.md
  - ../data-model/views.md
---

# Spec: `resolve_view`

## 1. Purpose

Given a view xmlid, return the full inheritance chain and the final merged XML. This is the first tool that lets AI answer "what does the UI actually look like after N modules patched this form?".

## 2. Input schema

```json
{
  "xmlid": "sale.view_order_form",
  "include_final_xml": true,
  "include_patch_log": true
}
```

- `xmlid` (string, required) — fully-qualified, e.g. `sale.view_order_form`
- `include_final_xml` (bool, default `true`) — omit when caller only wants the chain metadata
- `include_patch_log` (bool, default `true`) — per-patch attribution

## 3. Output schema

```json
{
  "result": {
    "xmlid": "sale.view_order_form",
    "model": "sale.order",
    "view_type": "form",
    "chain": [
      { "xmlid": "sale.view_order_form", "module": "sale", "priority": 16, "mode": "primary" },
      { "xmlid": "sale_margin.view_order_form_margin", "module": "sale_margin", "priority": 16, "mode": "extension" }
    ],
    "patch_log": [
      {
        "from_xmlid": "sale_margin.view_order_form_margin",
        "ordinal": 1,
        "expr": "//field[@name='amount_total']",
        "position": "after",
        "applied": true
      }
    ],
    "final_xml": "<form> ... </form>"
  },
  "indexed_at_sha": "abc1234",
  "warnings": []
}
```

## 4. Algorithm

1. Look up `views` row by `xmlid`
2. Walk forward: find all views with `inherit_id` pointing into the chain; order by `(priority, load_order)`
3. Start with primary's `<arch>` as the mutable DOM
4. For each extension, apply its `view_patches` rows in ordinal order
5. If any XPath fails to match, record in `patch_log` with `applied: false` and warning; do NOT abort
6. Serialize the DOM back to XML
7. Return envelope

## 5. Data accessed

- [`../data-model/views.md`](../data-model/views.md) — primary
- [`../data-model/modules.md`](../data-model/modules.md) — load order

## 6. Out of scope

- Studio views (DB-stored) — brief is explicit
- Resolving dynamic `arch` built from Python code
- QWeb templates — that is a separate tool (P4)

## 7. Acceptance criteria

### Correctness (necessary condition)

- [ ] Final XML diff <5% vs live Odoo on the top-50 most-inherited views in CE
- [ ] Correct `patch_log` with per-patch attribution
- [ ] Shared + tenant view chains correctly unioned
- [ ] Non-matching XPath produces warning, not error

### Token / context reduction (primary value)

- [ ] For "what does the final `res.partner` form view look like in our tenant", response with merged XML ≤6k tokens vs ≥20k tokens if AI had to read + merge N inheriting files
- [ ] Target: ≥70% token reduction vs raw-source baseline

### Performance

- [ ] P50 <100ms for deep-chain views (`res.partner`, `sale.order`)

## 8. Resolved semantics

### 8a. `position="replace"` with further extensions (closed 2026-04-22)

We follow Odoo core exactly (`odoo/addons/base/models/ir_ui_view.py::apply_inheritance_specs`):

- When an extension targets a node N with `position="replace"`, N is removed from the parent DOM and replaced with the patch content.
- Subsequent extensions whose XPath targets a **sibling** of N (resolved against the updated DOM) still apply — siblings after replace are re-applied.
- Subsequent extensions whose XPath targets a **descendant of the original N** fail to match. Record in `patch_log` with `applied: false` + warning `replaced_ancestor` (non-fatal).
- No reordering: extensions are applied in `(priority ASC, load_order ASC)` even when an earlier one nukes the target.

Resolver emits one `patch_log` row per attempted xpath op; `applied: false` entries carry a `reason` (`replaced_ancestor`, `xpath_no_match`, `malformed_expr`).

## 9. Deferred questions

- **Cached final XML vs recompute** — deferred. P2 ships with plain recompute on every call; profile against `resolve_view` P50 <100ms target in WP-17 accept bench. If breached, file ADR for materialized view keyed on chain SHA in P3 window.

## 10. References

- Data: `../data-model/views.md`
