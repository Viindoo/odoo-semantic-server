# Test Fixtures

Ground-truth corpus for `resolve_model`, `resolve_field`, and `resolve_method` tools (Phase 1).

All golden files use the pragma: 10 fully-labelled entries + skeleton TODOs for the rest.
TODO entries will be completed after WP-5 ships resolver output (plan §4 Wave 4).

---

## `odoo_ce_subset/`

Frozen subset of Odoo CE 17.0 — models directory only (no views, data, static, tests).
10 modules: `base`, `web`, `bus`, `mail`, `product`, `sale`, `account`, `stock`, `sale_management`, `contacts`.

Each module contains:
- `__manifest__.py` — trimmed deps to subset-only (no `sales_team`, `uom`, etc.)
- `__init__.py` — `from . import models`
- `models/__init__.py` — imports curated model files only
- `models/*.py` — real Odoo CE source, curated files only

| Module | Models covered | Source files |
|---|---|---|
| `base` | `res.partner`, `res.company`, `res.users` | `res_partner.py`, `res_company.py`, `res_users.py` |
| `web` | (dep only, no curated models) | — |
| `bus` | (dep only) | — |
| `mail` | `mail.thread`, `mail.activity.mixin` | `mail_thread.py`, `mail_activity_mixin.py` |
| `product` | `product.template`, `product.product` | `product_template.py`, `product_product.py` |
| `sale` | `sale.order`, `sale.order.line` | `sale_order.py`, `sale_order_line.py` |
| `account` | `account.move`, `account.move.line` | `account_move.py`, `account_move_line.py` |
| `stock` | `stock.picking`, `stock.move` | `stock_picking.py`, `stock_move.py` |
| `sale_management` | `sale.order` extension, `sale.order.line` extension | `sale_order.py`, `sale_order_line.py` |
| `contacts` | `res.users` extension | `res_users.py` |

---

## `custom_addons/`

10 hand-written Viindoo-flavored modules (each ≤50 LOC). One module per edge case.

### `viin_fixture_multi_inherit/`
- Covers: multi-inherit field stack (plan §2 WP-5 test case 2)
- Spec: `resolve_model.md` §5b multi-inherit; `resolve_field.md` §5b
- Deps: `sale`, `mail`
- Model: `sale.order` with `_inherit = ['sale.order', 'mail.thread', 'mail.activity.mixin']`
- Expected in golden: `resolve_model(sale.order)` chain includes this module at load_order 19

### `viin_fixture_inherits_delegation/`
- Covers: `_inherits` delegation (plan §2 WP-5 test case _inherits)
- Spec: `resolve_field.md` §5b `_inherits delegation` paragraph; R1 risk
- Deps: `product`
- Model: `y.custom` with `_inherits = {'product.template': 'tmpl_id'}`
- Expected in golden: `list_price` on `y.custom` resolves as `inherited_via_delegation`

### `viin_fixture_field_override_compute/`
- Covers: field override with new compute method
- Spec: `resolve_field.md` §5b last-loaded-wins rule
- Deps: `sale`
- Model: `sale.order` — `amount_total` overridden with `_viin_amount_all` compute
- Expected in golden: `resolve_field(sale.order, amount_total)` chain ends here

### `viin_fixture_field_override_no_compute/`
- Covers: field override changing only `readonly=True`, no compute change
- Spec: `resolve_field.md` §5b effective merge
- Deps: `sale`
- Model: `sale.order` — `partner_id` overridden with `readonly=True` only
- Expected in golden: effective `readonly=True`, `compute=None` preserved from root

### `viin_fixture_method_override_super/`
- Covers: method override that calls `super()` — chain not broken
- Spec: `resolve_method.md` §5b; `chain_is_broken=false`
- Deps: `sale`
- Model: `sale.order` — `action_confirm` calls `super().action_confirm()`
- Expected in golden: `calls_super=True`; `chain_is_broken=False`

### `viin_fixture_method_override_break_super/`
- Covers: method override breaking super chain (spec warns: super-break flag)
- Spec: `resolve_method.md` §3 `chain_is_broken`
- Deps: `sale`
- Model: `sale.order` — `action_confirm` does NOT call super, returns custom value
- Expected in golden: `calls_super=False`; `chain_is_broken=True`

### `viin_fixture_conditional_optional_dep/`
- Covers: `try/except ImportError` guard in `models/__init__.py` — spec §5c case 1
- Spec: `resolve_model.md` §5c case 1; `resolve_field.md` §5c case 1
- Deps: `sale`
- `models/__init__.py` has `try: from . import optional_model\nexcept ImportError: pass`
- Expected in golden: `optional_model.py` classes flagged `conditional_import=True`; warning `resolution: conditional`

### `viin_fixture_register_false/`
- Covers: `_register = False` model — spec §5c case 2
- Spec: `resolve_model.md` §5c case 2; `resolve_field.md` §5c case 2
- Deps: `base`
- Model: `viin.abstract.base` with `_register = False`
- Expected in golden: `indexer_notes.register_false_chain=True`; warning emitted

### `viin_fixture_depends_added/`
- Covers: `@api.depends` added to existing field compute in an override
- Spec: `resolve_field.md` §5b depends union
- Deps: `sale`
- Model: `sale.order` — `amount_total` re-declared with additional `viin_discount_extra` in `@api.depends`
- Expected in golden: effective `depends` is union of root + override depends sets

### `viin_fixture_order_override/`
- Covers: `_order` override on an existing model
- Spec: `data-model/models.md` `order` column
- Deps: `base`
- Model: `res.partner` with `_order = 'name desc'`
- Expected in golden: `resolve_model(res.partner)` chain shows this module with `order='name desc'`

---

## `golden/`

Hand-labelled expected outputs. Used by WP-8 handler golden tests.

| File | Fully labelled | TODO skeletons | Total |
|---|---|---|---|
| `resolve_model.json` | 10 | 0 | 10 |
| `resolve_field.json` | 10 | 40 | 50 |
| `resolve_method.json` | 5 | 15 | 20 |
| `load_order_ce_subset.json` | 3 (WP-3) | — | 3 |

**TODO skeletons** will be completed in Wave 4 after WP-5 outputs stabilise.
Tests in `test_fixtures_load.py` skip entries with a `"TODO"` key using
`pytest.mark.skip(reason="golden pending")` logic.

---

## `addons/` (WP-3 fixtures)

Simple synthetic addons for manifest scanner and load-order simulator unit tests.
Do not modify unless updating WP-3 tests.

## `odoo_ce_subset_manifests/` (WP-3 fixtures)

Frozen `__manifest__.py` copies used by the load-order golden test.
The actual full-models subset is in `odoo_ce_subset/` (this WP).
