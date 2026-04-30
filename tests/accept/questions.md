---
status: draft
scope: tests/accept/questions
phase: P1
date: 2026-04-22
---

# Accept test — 10 sample questions

Each question a developer might ask an AI assistant, mapped to the exact
MCP tool call the assistant should make and the golden answer-shape to
expect. `runner.py` drives the list programmatically; this doc is the
human-readable source of truth.

Token-reduction baseline for each question is the set of files an AI
would otherwise have to read into context before answering. Listed
explicitly below so the computation is reproducible.

---

## Q1 — Model chain on a pure-CE model

**Question:** "What modules contribute to `account.move` in this install?"

- **Tool**: `resolve_model("account.move")`
- **Baseline files** (AI would read without us):
  - `tests/fixtures/odoo_ce_subset/account/models/account_move.py`
- **Exit target**: model-tool ≥90% token reduction.

## Q2 — Model chain extended by a custom module

**Question:** "Which modules have touched `res.partner`, and in what order?"

- **Tool**: `resolve_model("res.partner")`
- **Baseline**:
  - `tests/fixtures/odoo_ce_subset/base/models/res_partner.py`
  - `tests/fixtures/custom_addons/viin_fixture_order_override/models/res_partner.py`

## Q3 — Deep extension chain — the real stress test

**Question:** "`sale.order` after `sale_management` + 7 viin\_\* fixtures: what's the final module chain?"

- **Tool**: `resolve_model("sale.order")`
- **Baseline**: `sale_order.py` in `sale`, `sale_management`, and 7 viin\_\* custom fixtures (all files list `sale.order` in their chain).
- **Notes**: primary value demo — raw-source read is >1500 LOC.

## Q4 — Field override, one level deep

**Question:** "What's the final definition of `sale.order.partner_id` after overrides?"

- **Tool**: `resolve_field("sale.order", "partner_id")`
- **Baseline**:
  - `tests/fixtures/odoo_ce_subset/sale/models/sale_order.py`
  - `tests/fixtures/custom_addons/viin_fixture_field_override_no_compute/models/sale_order.py`

## Q5 — Computed field chain

**Question:** "Which compute function actually runs for `sale.order.amount_total`, and what does it depend on?"

- **Tool**: `resolve_field("sale.order", "amount_total")`
- **Baseline**:
  - `sale/models/sale_order.py`
  - `viin_fixture_depends_added/models/sale_order.py`
  - `viin_fixture_field_override_compute/models/sale_order.py`
- **Notes**: tests the `depends` union + `compute` last-wins logic.

## Q6 — Method override chain — safe super() flow

**Question:** "Walk me through `sale.order._amount_all` across modules."

- **Tool**: `resolve_method("sale.order", "_amount_all")`
- **Baseline**:
  - `sale/models/sale_order.py`
  - `viin_fixture_depends_added/models/sale_order.py`

## Q7 — Method override chain — broken super (bug detector)

**Question:** "After sale_management + viin_fixture_method_override_super + viin_fixture_method_override_break_super, does `sale.order.action_confirm` call super all the way down?"

- **Tool**: `resolve_method("sale.order", "action_confirm")`
- **Baseline**:
  - `sale/models/sale_order.py`
  - `sale_management/models/sale_order.py`
  - `viin_fixture_method_override_super/models/sale_order.py`
  - `viin_fixture_method_override_break_super/models/sale_order.py`
- **Notes**: response must set `chain_is_broken=true` — primary bug-detection demo.

## Q8 — Abstract-model field ownership

**Question:** "What does `mail.thread` look like? Is it abstract?"

- **Tool**: `resolve_model("mail.thread")`
- **Baseline**: `tests/fixtures/odoo_ce_subset/mail/models/mail_thread.py`

## Q9 — `_inherits` delegation

**Question:** "`res.users` delegates to what, via which FK?"

- **Tool**: `resolve_model("res.users")`
- **Baseline**:
  - `tests/fixtures/odoo_ce_subset/base/models/res_users.py`
  - `tests/fixtures/odoo_ce_subset/contacts/models/res_users.py`
- **Expected output**: `inherits = {"res.partner": "partner_id"}`.

## Q10 — 404 — nonexistent model

**Question:** "Is `sale.fancyMadeUpModel` a real model here?"

- **Tool**: `resolve_model("sale.fancyMadeUpModel")`
- **Expected**: `NotFoundError` (HTTP 404 equivalent).
- **Baseline**: none (baseline is zero; handler correctly short-circuits).
- **Notes**: validates the error path. Token reduction not applicable.

---

## Q11 — Deep tenant-overlay view chain

**Question:** "What does the final `res.partner` form view look like in our tenant after all installed modules?"

- **Tool**: `resolve_view("base.view_res_partner_form")`
- **Baseline**: concatenation of every XML file contributing to the `res.partner` form chain (base, mail, contacts, sale, account, + tenant extensions).
- **Notes**: deepest chain in a typical Odoo install. Exercises tenant-overlay interleaving with the public (CE) primary.

## Q12 — Patch attribution via patch_log

**Question:** "What fields has `sale_margin` added to `sale.view_order_form`?"

- **Tool**: `resolve_view("sale.view_order_form", include_patch_log=True)`
- **Baseline**: `sale_margin/views/sale_margin_views.xml` + the chain up to it.
- **Notes**: answer is derived from `patch_log[*]` entries whose `from_xmlid` starts with `sale_margin.` and whose `position` inserts fields. Tests that the patch log is attributable, not just diff-able.

## Q13 — Chain metadata without XML payload

**Question:** "Show the final form view for `sale.order`, omitting raw XML — just the chain metadata."

- **Tool**: `resolve_view("sale.view_order_form", include_final_xml=False)`
- **Baseline**: not applicable — measuring envelope-shape correctness rather than token reduction.
- **Notes**: verifies the `include_final_xml=False` path returns `chain` + `patch_log` without `final_xml`. Keeps context cost low when the caller only needs attribution.

## Q14 — 404 — nonexistent view

**Question:** "Resolve view `nonexistent.view_foo`."

- **Tool**: `resolve_view("nonexistent.view_foo")`
- **Expected**: `NotFoundError` (HTTP 404 equivalent).
- **Baseline**: none.
- **Notes**: mirrors Q10 for `resolve_view` error path.

## Q15 — Tenant-scoped patch filter

**Question:** "Show me the view for `account.view_move_form` but only patches from our tenant modules."

- **Tool**: `resolve_view("account.view_move_form")` — no direct filter flag in the P2 API. Caller post-filters `patch_log[*].from_xmlid` by matching the tenant's module namespace.
- **Baseline**: tenant-origin XML file(s) contributing to the chain.
- **Notes**: documents a caller-side pattern. If a tenant-only filter flag is added in P3, move this to the formal flag.

---

## Exit targets (ref: `roadmap.md` P1)

- Q1–Q3, Q8, Q9 → `resolve_model`: ≥90% token reduction.
- Q4, Q5 → `resolve_field`: ≥90% token reduction.
- Q6, Q7 → `resolve_method`: ≥70% token reduction.
- All questions → correctness 100% (each response matches the corresponding golden entry in `tests/fixtures/golden/*.json` where present, or the documented shape here).
- Q10 → handler returns `NotFoundError`; no baseline comparison.
- Latency: P50 <20ms for model/field, <50ms for method; P99 <500ms across all.

## Exit targets (ref: `roadmap.md` P2 — `resolve_view`)

- Q11–Q13, Q15 → `resolve_view`: overall token reduction ≥70%; mean diff% vs live-Odoo golden <5%.
- Q14 → handler returns `NotFoundError`; no baseline comparison.
- Latency: P50 <100ms, P99 <500ms on the top-50 benchmark in `runner_p2.py`.
- Top-50 coverage: ≥40 of 50 views must have a golden file from `dump_live_odoo_views.py`.
