# Test Fixtures

Ground-truth corpus for the `resolve_model`, `resolve_field`, and `resolve_method` tools.

All golden files use the same pragma: a small number of fully-labelled entries
plus skeleton TODOs that get filled in as resolver coverage grows.

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

## Views

The 10 CE modules now include a frozen `views/*.xml` tree in addition to
`models/`. Files are 1:1 copies from Odoo CE 17.0. Per-module file counts:

| Module | views/*.xml count | Notes |
|---|---|---|
| `base` | 29 | skipped: `base_menus.xml`, `ir_qweb_widget_templates.xml`, `report_paperformat_views.xml` (menus-only / QWeb templates) |
| `web` | 2 | skipped: `report_templates.xml`, `speedscope_template.xml`, `webclient_templates.xml`, `neutralize_views.xml` (menus-only / QWeb templates) |
| `mail` | 31 | skipped: `mail_menus.xml`, `discuss_public_templates.xml`, `mail_templates_public.xml` |
| `product` | 14 | — |
| `sale` | 12 | skipped: `sale_menus.xml`, `sale_portal_templates.xml` |
| `account` | 30 | skipped: `account_menuitem.xml`, `account_portal_templates.xml`, `bill_preview_template.xml`, `report_invoice.xml`, `report_payment_receipt_templates.xml`, `report_statement.xml`, `terms_template.xml` |
| `stock` | 19 | skipped: `stock_menu_views.xml`, `report_stock_traceability.xml`, `stock_template.xml` |
| `sale_management` | 4 | skipped: `sale_management_menus.xml`, `sale_portal_templates.xml` |
| `contacts` | 1 | — |
| `bus` | 0 | CE bus module has no `views/` directory |

Exclusion rules applied:
- QWeb report templates, portal templates, client-side web templates, menus-only
  files (parser is view-record-oriented; templates are out of scope for now).
- Files >200 KB or >3000 lines (sanity cap — we need breadth, not every byte).
  No single retained file exceeds either threshold.

Each module's `__manifest__.py` `data` key is updated to reference exactly the
files present in the frozen copy — data/security/wizard/demo entries are
dropped because they are not mirrored into the fixture.

---

## View-focused custom addons

Eight modules exercise `xml_parser` and `view_resolver` edge cases. Each is
≤50 lines of XML with a minimal manifest (`depends` lists the CE subset
parent when needed).

| Module | Purpose |
|---|---|
| `cv_basic_form` | Primary form view on `res.partner`, zero extensions — sanity baseline |
| `cv_simple_ext` | One extension, `position="after"` adding a field |
| `cv_replace_and_sibling` | Extension A replaces node N; extension B targets a sibling of N (sibling survives) |
| `cv_replace_orphan` | Extension A replaces node N; extension B targets a descendant of original N (resolver flags `replaced_ancestor`) |
| `cv_multi_ext_same_target` | Three extensions on the same primary, ordered by priority |
| `cv_xpath_no_match` | Extension with an XPath matching nothing (resolver flags `xpath_no_match`) |
| `cv_priority_tie` | Two extensions with identical priority — load_order tiebreak |
| `cv_attributes_op` | Extension using `position="attributes"` |

---

## `custom_addons/` (Python fixtures)

10 hand-written modules (each ≤50 LOC). One module per edge case.

### `viin_fixture_multi_inherit/`
- Covers: multi-inherit field stack
- Deps: `sale`, `mail`
- Model: `sale.order` with `_inherit = ['sale.order', 'mail.thread', 'mail.activity.mixin']`
- Expected in golden: `resolve_model(sale.order)` chain includes this module

### `viin_fixture_inherits_delegation/`
- Covers: `_inherits` delegation
- Deps: `product`
- Model: `y.custom` with `_inherits = {'product.template': 'tmpl_id'}`
- Expected in golden: `list_price` on `y.custom` resolves as `inherited_via_delegation`

### `viin_fixture_field_override_compute/`
- Covers: field override with new compute method
- Deps: `sale`
- Model: `sale.order` — `amount_total` overridden with `_viin_amount_all` compute
- Expected in golden: `resolve_field(sale.order, amount_total)` chain ends here

### `viin_fixture_field_override_no_compute/`
- Covers: field override changing only `readonly=True`, no compute change
- Deps: `sale`
- Model: `sale.order` — `partner_id` overridden with `readonly=True` only
- Expected in golden: effective `readonly=True`, `compute=None` preserved from root

### `viin_fixture_method_override_super/`
- Covers: method override that calls `super()` — chain not broken
- Deps: `sale`
- Model: `sale.order` — `action_confirm` calls `super().action_confirm()`
- Expected in golden: `calls_super=True`; `chain_is_broken=False`

### `viin_fixture_method_override_break_super/`
- Covers: method override breaking the super chain
- Deps: `sale`
- Model: `sale.order` — `action_confirm` does NOT call super, returns a custom value
- Expected in golden: `calls_super=False`; `chain_is_broken=True`

### `viin_fixture_conditional_optional_dep/`
- Covers: `try/except ImportError` guard in `models/__init__.py`
- Deps: `sale`
- `models/__init__.py` has `try: from . import optional_model\nexcept ImportError: pass`
- Expected in golden: `optional_model.py` classes flagged `conditional_import=True`; warning `resolution: conditional`

### `viin_fixture_register_false/`
- Covers: `_register = False` model
- Deps: `base`
- Model: `viin.abstract.base` with `_register = False`
- Expected in golden: `indexer_notes.register_false_chain=True`; warning emitted

### `viin_fixture_depends_added/`
- Covers: `@api.depends` added to existing field compute in an override
- Deps: `sale`
- Model: `sale.order` — `amount_total` re-declared with an additional dependency in `@api.depends`
- Expected in golden: effective `depends` is union of root + override depends sets

### `viin_fixture_order_override/`
- Covers: `_order` override on an existing model
- Deps: `base`
- Model: `res.partner` with `_order = 'name desc'`
- Expected in golden: `resolve_model(res.partner)` chain shows this module with `order='name desc'`

---

## `golden/`

Hand-labelled expected outputs. Used by the handler golden tests.

| File | Fully labelled | TODO skeletons | Total |
|---|---|---|---|
| `resolve_model.json` | 10 | 0 | 10 |
| `resolve_field.json` | 10 | 40 | 50 |
| `resolve_method.json` | 5 | 15 | 20 |
| `load_order_ce_subset.json` | 3 | — | 3 |

TODO skeletons get filled in as resolver coverage grows. Tests in
`test_fixtures_load.py` skip entries with a `"TODO"` key.

---

## `addons/`

Simple synthetic addons for manifest scanner and load-order simulator unit tests.

## `odoo_ce_subset_manifests/`

Frozen `__manifest__.py` copies used by the load-order golden test.
The actual full-models subset is in `odoo_ce_subset/`.
