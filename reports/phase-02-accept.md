---
status: draft
scope: reports/phase-02-accept
phase: P2
reads-with:
  - ../tests/accept/questions.md
  - ../tests/accept/top50_views.json
---

# Phase 2 accept-test results — top-50 views

Iterations per view (latency loop): **100**
Tenant schema: `public`
Coverage (views with live-Odoo golden): **50/50** (threshold: 40)

Diff formula: ``len(unified_diff_lines) / max(len(golden_lines), len(handler_lines)) * 100`` — ``--`` / ``++`` / ``@@`` header lines excluded.

## Per-view results

| xmlid | status | chain | diff% | handler tok | raw tok | reduction | P50 ms | P99 ms |
|-------|--------|-------|-------|-------------|---------|-----------|--------|--------|
| account.view_move_form | ok | 56 | 200.00% | 19316 | 56529 | 65.8% | 6.05 | 6.93 |
| base.view_partner_form | ok | 79 | 200.00% | 9982 | 40273 | 75.2% | 5.10 | 7.11 |
| sale.view_order_form | ok | 27 | 200.00% | 9591 | 24287 | 60.5% | 3.07 | 3.83 |
| account.view_tax_form | ok | 25 | 200.00% | 1862 | 8178 | 77.2% | 0.80 | 1.02 |
| base.view_company_form | ok | 27 | 125.00% | 2445 | 9673 | 74.7% | 1.08 | 1.35 |
| payment.payment_provider_form | ok | 24 | 120.00% | 4635 | 9972 | 53.5% | 1.35 | 1.61 |
| base.res_config_settings_view_form | ok | 124 | 125.00% | 46169 | 73501 | 37.2% | 15.92 | 22.68 |
| hr.view_employee_form | ok | 18 | 200.00% | 4227 | 38183 | 88.9% | 1.29 | 1.68 |
| product.product_template_form_view | ok | 29 | 200.00% | 4651 | 47469 | 90.2% | 1.70 | 2.30 |
| account.view_account_invoice_filter | ok | 16 | 200.00% | 1992 | 40704 | 95.1% | 0.65 | 0.99 |
| account.view_account_journal_form | ok | 19 | 200.00% | 3590 | 8247 | 56.5% | 0.98 | 1.38 |
| stock.view_picking_form | ok | 13 | 200.00% | 6281 | 23212 | 72.9% | 1.21 | 1.56 |
| base.view_users_form | ok | 15 | 133.33% | 2338 | 21224 | 89.0% | 0.72 | 1.27 |
| base.view_partner_bank_form | ok | 10 | 200.00% | 529 | 3153 | 83.2% | 0.36 | 0.58 |
| point_of_sale.pos_payment_method_view_form | ok | 11 | 200.00% | 1693 | 5214 | 67.5% | 0.56 | 1.20 |
| product.product_template_only_form_view | ok | 11 | 133.33% | 22 | 23178 | 99.9% | 0.27 | 0.39 |
| purchase.purchase_order_form | ok | 11 | 133.33% | 5387 | 16910 | 68.1% | 1.48 | 1.72 |
| account.view_invoice_tree | ok | 9 | 200.00% | 1379 | 33763 | 95.9% | 0.44 | 0.65 |
| digest.digest_digest_view_form | ok | 9 | 200.00% | 762 | 2997 | 74.6% | 0.41 | 0.55 |
| account.view_account_payment_form | ok | 9 | 200.00% | 3448 | 10273 | 66.4% | 0.79 | 1.18 |
| base.res_partner_kanban_view | ok | 8 | 200.00% | 961 | 14565 | 93.4% | 0.49 | 0.77 |
| base.view_res_partner_filter | ok | 9 | 200.00% | 728 | 17097 | 95.7% | 0.38 | 0.70 |
| event.view_event_form | ok | 13 | 200.00% | 2362 | 8119 | 70.9% | 0.88 | 1.24 |
| point_of_sale.view_pos_pos_form | ok | 8 | 200.00% | 2421 | 8034 | 69.9% | 0.61 | 0.81 |
| product.product_normal_form_view | ok | 9 | 200.00% | 26 | 25590 | 99.9% | 0.27 | 0.46 |
| uom.product_uom_form_view | ok | 8 | 200.00% | 400 | 3326 | 88.0% | 0.32 | 0.48 |
| crm.crm_lead_view_form | ok | 8 | 200.00% | 5019 | 22303 | 77.5% | 0.85 | 1.64 |
| hr.hr_department_view_kanban | ok | 7 | 200.00% | 587 | 6528 | 91.0% | 0.44 | 0.69 |
| hr.view_employee_filter | ok | 7 | 200.00% | 780 | 14269 | 94.5% | 0.38 | 0.67 |
| hr.view_employee_tree | ok | 7 | 200.00% | 473 | 27082 | 98.3% | 0.34 | 0.47 |
| stock.view_picking_internal_search | ok | 7 | 200.00% | 1232 | 17664 | 93.0% | 0.39 | 0.53 |
| account.account_journal_dashboard_kanban_view | ok | 6 | 200.00% | 4308 | 6515 | 33.9% | 0.78 | 0.97 |
| account.view_out_invoice_tree | ok | 7 | 200.00% | 26 | 31104 | 99.9% | 0.25 | 0.57 |
| base.view_users_form_simple_modif | ok | 8 | 66.67% | 1378 | 12337 | 88.8% | 0.60 | 0.77 |
| delivery.view_delivery_carrier_form | ok | 6 | 200.00% | 1371 | 4858 | 71.8% | 0.53 | 0.78 |
| hr.hr_employee_public_view_form | ok | 6 | 200.00% | 1281 | 11145 | 88.5% | 0.52 | 0.93 |
| payment.payment_transaction_form | ok | 6 | 200.00% | 890 | 2649 | 66.4% | 0.48 | 0.66 |
| product.product_template_search_view | ok | 7 | 200.00% | 755 | 31548 | 97.6% | 0.45 | 0.79 |
| uom.product_uom_categ_form_view | ok | 6 | 200.00% | 299 | 5099 | 94.1% | 0.36 | 0.58 |
| utm.utm_campaign_view_form | ok | 6 | 200.00% | 1114 | 5482 | 79.7% | 0.57 | 0.79 |
| utm.utm_campaign_view_kanban | ok | 6 | 200.00% | 713 | 5482 | 87.0% | 0.53 | 0.71 |
| website.website_visitor_view_form | ok | 7 | 200.00% | 854 | 9297 | 90.8% | 0.52 | 0.64 |
| website.website_visitor_view_tree | ok | 7 | 200.00% | 350 | 9297 | 96.2% | 0.38 | 0.61 |
| account.view_account_move_filter | ok | 5 | 200.00% | 810 | 28170 | 97.1% | 0.40 | 0.66 |
| account.view_out_credit_note_tree | ok | 6 | 200.00% | 24 | 30249 | 99.9% | 0.30 | 0.51 |
| account.view_tax_tree | ok | 5 | 200.00% | 224 | 4278 | 94.8% | 0.34 | 0.47 |
| analytic.view_account_analytic_account_form | ok | 5 | 200.00% | 627 | 2732 | 77.0% | 0.36 | 0.54 |
| base.view_partner_tree | ok | 6 | 200.00% | 464 | 13577 | 96.6% | 0.36 | 0.61 |
| event.view_event_tree | ok | 5 | 200.00% | 392 | 6358 | 93.8% | 0.34 | 0.54 |
| product.product_template_tree_view | ok | 6 | 200.00% | 632 | 17310 | 96.3% | 0.44 | 0.66 |

## Aggregate

- Mean diff%: **188.73%** (target <5%)
- Overall token reduction: **82.0%** (target ≥70%)
- Median P50: **0.52 ms** (target <100ms)
- Max P99: **22.68 ms**

## Notes

- (no per-view notes)
