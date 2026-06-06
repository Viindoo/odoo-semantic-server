# Production Smoke Runbook — 15 MCP Tools + 7 Resources

> **Note:** The `24` in this file's name is legacy — the total MCP tool count is now **25** (25th = `profile_inspect`, ADR-0028 Wave 2 WI-4 #260). The filename is retained intentionally to preserve existing links; this runbook covers tools #11-25.

> **Operator-facing walkthrough for post-go-live smoke-test of 15 MCP tools (#11-25) and 7 Resources (R1-R7) that were code-complete + unit-tested but never smoke-tested end-to-end against the live production MCP endpoint. Estimated ~90-100 min/session.**
>
> Covers: `describe_module`, 3 superset discriminators (`model_inspect`/`module_inspect`/`entity_lookup`), 4 session tools (`set_active_version`/`set_active_profile`/`list_available_versions`/`list_available_profiles`), 2 stylesheet tools (`resolve_stylesheet`/`find_style_override`), 4 ORM-validation tools (`resolve_orm_chain`/`validate_domain`/`validate_depends`/`validate_relation`), 1 profile discriminator (`profile_inspect`), and 7 URI resources (`odoo://...`).
>
> **References:** ADR-0023 (Tool Output Completeness), ADR-0028 (Superset Discriminator Consolidation), ADR-0029 (Implicit Session Context), ADR-0030 (MCP Resources URI Scheme).

---

## Why This Runbook Exists

Code-complete + unit-tested ≠ end-to-end verified on production. This smoke session catches:
- **ORM-validation tools** returning "BROKEN" when underlying data (Field.comodel_name, Method.depends) not yet materialized in the graph
- **Silent empty results** if post-reindex steps (e.g., `seed-patterns`) were skipped
- **Stylesheet tools** returning 0 files if CSS/SCSS indexing was skipped or `--no-embed` was used
- **Era1 (v8/v9) quirks** for ORM tools when testing against legacy Odoo versions

Each smoke entry = natural-language prompt → AI client → MCP tool → result inspection for structure + non-empty content.

---

## Preconditions

- **MCP client configured:** Claude Code with MCP plugin, Codex CLI, Gemini CLI, or direct MCP JSON-RPC via `curl`
- **API key:** Valid smoke-test plan tier for `<PROD_BASE_URL>`
- **Post-reindex 2026-05-25:** ~591k embeddings indexed across Odoo v8.0 → v19.0
- **WI-A7 OPS actions complete:** Post-reindex enrichment + era1 (v8/v9) re-embed finished
- **Stopwatch (optional):** Note start/end time per tool if debugging slow responses

---

## Placeholder Reference

| Placeholder | Default | Notes |
|---|---|---|
| `<PROD_BASE_URL>` | `https://odoo-semantic.viindoo.com` | Replace for staging/self-hosted |
| `<OPERATOR_API_KEY>` | Your smoke-test API key | From `python -m src.manager list` |
| `<DRILL_PROFILE>` | `odoo_17` (or first from `list_available_profiles`) | A profile with full v17.0 data |

---

## General Acceptance Criteria

For **EVERY** tool/resource smoke, operator verifies:

1. **Non-empty response** — Tool returns non-empty text/markdown/CSS. An error like `"Error: ..."` or empty string is a **FAIL**.
2. **Tree structure** — Response includes `├─` or `└─` lines following ADR-0023 tree grammar, or raw CSS source (for stylesheets). Flat text without tree = **FAIL**.
3. **Footer hint** — Response ends with `Next:` footer or `_meta.summary` pointing to related tools (not hard-fail if missing for leaf entities).
4. **Truncation disclosure** — If result is capped (>200 rows), response includes `(showing N of M rows)` or similar. Silent truncation = **FAIL**.
5. **No Python traceback** — Response must NOT contain `Traceback`, `Exception`, `KeyError`, `TypeError`, or `AttributeError`. Any exception = **FAIL**.

---

## Smoke Session Structure

### Phase 1: Discovery (Dependencies First)

Start with read-only discovery tools to confirm data presence, then move to context-setting and entity enumeration.

#### Phase 1A: Tool #17 — `list_available_versions`

**Trigger prompts:**

- **EN:** "Using odoo-semantic, list all indexed Odoo versions available in this knowledge base."
- **VI:** "Dùng odoo-semantic, liệt kê tất cả các version Odoo đã được index trong knowledge base này."

**Expected behaviour:** Markdown tree listing v8.0 → v19.0 (or subset if partial indexing).

**Acceptance:**
- [ ] Response contains at least 10 version lines (v8 through v17 minimum)
- [ ] Each version line starts with `├─` or `└─`
- [ ] No error output or traceback
- [ ] Latest version identified (usually v19.0 if full indexing)

**Silent-fail indicator:** Empty list → indicates data missing entirely; proceed with caution.

---

#### Phase 1B: Tool #18 — `list_available_profiles`

**Trigger prompts:**

- **EN:** "Using odoo-semantic, list all profiles registered in this knowledge base."
- **VI:** "Dùng odoo-semantic, liệt kê tất cả các profile đã đăng ký trong knowledge base này."

**Expected behaviour:** Markdown tree listing available profiles (at least 1).

**Acceptance:**
- [ ] Response contains at least 1 profile line
- [ ] Each profile line has format `├─ <profile_name> (<version>)`
- [ ] No error output

**Note for operator:** Copy the first profile name `<profile_name>` — needed for tool #16.

---

### Phase 2: Session Context (ADR-0029)

Set sticky version + profile to eliminate repetitive parameters in subsequent calls.

#### Phase 2A: Tool #15 — `set_active_version` (MUTATING)

**Trigger prompts:**

- **EN:** "Set my active Odoo version to 17.0 so I don't have to specify it in subsequent calls."
- **VI:** "Đặt phiên bản Odoo active là 17.0 để không cần truyền odoo_version mỗi lần."

**Expected behaviour:** Confirmation message with TTL info.

**Acceptance:**
- [ ] Response contains `Active version set to 17.0`
- [ ] Response mentions `24h sliding TTL` or similar
- [ ] No traceback

**Verify-after:** Immediately follow with tool #17 `list_available_versions()` — confirm displayed active version now shows `17.0` or indicator.

---

#### Phase 2B: Tool #16 — `set_active_profile` (MUTATING)

**Trigger prompts:**

- **EN:** "Set my active profile to `<DRILL_PROFILE>` (replace with first name from `list_available_profiles`)."
- **VI:** "Đặt profile active là `<DRILL_PROFILE>`."

**Expected behaviour:** Confirmation with TTL info.

**Acceptance:**
- [ ] Response contains `Active profile set to <DRILL_PROFILE>`
- [ ] Response mentions TTL or per-key sticky binding
- [ ] No traceback

---

### Phase 3: Entity Enumeration (Core Tools #11-14)

#### Phase 3A: Tool #11 — `describe_module` (M9 Wave 1)

**Trigger prompts:**

- **EN:** "Using odoo-semantic, describe the `sale` module in Odoo 17.0. Show its manifest, what models it defines and extends, and its view/JS patch counts."
- **VI:** "Dùng odoo-semantic, mô tả module `sale` trên Odoo 17.0. Cho xem manifest, model nào được định nghĩa/mở rộng, và số lượng view/JS patch."

**Expected behaviour:** Markdown tree with module overview.

**Acceptance:**
- [ ] Response contains `Manifest:` section
- [ ] Response contains `Defines models:` or `Extends models:` lines
- [ ] Response contains `Views:` count line
- [ ] Each section has tree-line prefix (`├─`, `└─`)
- [ ] No traceback

**Tool-specific acceptance:**
- [ ] Module description present (≥1 sentence)
- [ ] Depends list non-empty
- [ ] Version-aware (explicitly says 17.0, not auto)

---

#### Phase 3B: Tool #12 — `model_inspect` (M11 Wave D)

**Smoke variant A: Summary mode**

**Trigger prompts:**

- **EN:** "Use odoo-semantic to inspect the model `sale.order` in Odoo 17.0 — show me a summary with inheritance chain and field/method counts."
- **VI:** "Dùng odoo-semantic xem tổng quan model `sale.order` trên Odoo 17.0 — inheritance chain và số lượng field/method."

**Expected behaviour:** Tree showing inheritance chain + field/method counts.

**Acceptance:**
- [ ] Response contains `Defined in:` section
- [ ] Response contains `Extended by:` section (may be empty if not extended)
- [ ] Response shows `Fields:` count line
- [ ] Response shows `Methods:` count line
- [ ] Tree structure with `├─` / `└─` lines

**Smoke variant B: Fields list (with filter)**

**Trigger prompts:**

- **EN:** "List all Many2one fields of `sale.order` in Odoo 17.0."
- **VI:** "Liệt kê các field Many2one của model `sale.order` trên Odoo 17.0."

**Expected behaviour:** Per-field rows with field name, type, module.

**Acceptance:**
- [ ] Response shows multiple rows with field names (e.g., `partner_id`, `company_id`)
- [ ] Each row shows field type (`Many2one`)
- [ ] Declaring module shown per row
- [ ] No traceback

**Smoke variant C: Single field detail**

**Trigger prompts:**

- **EN:** "Show me details of the field `amount_total` on `sale.order` in Odoo 17.0."
- **VI:** "Xem chi tiết field `amount_total` của model `sale.order` trên Odoo 17.0."

**Expected behaviour:** Detailed field metadata.

**Acceptance:**
- [ ] Response contains field type (e.g., `Monetary`)
- [ ] Response contains `Computed:` flag
- [ ] Response contains `Stored:` flag
- [ ] Response shows declaring modules
- [ ] No traceback

---

#### Phase 3C: Tool #13 — `module_inspect` (M11 Wave D)

**Smoke variant A: Views list**

**Trigger prompts:**

- **EN:** "Use odoo-semantic to list all form views in the `sale` module for Odoo 17.0."
- **VI:** "Dùng odoo-semantic liệt kê các form view trong module `sale` trên Odoo 17.0."

**Expected behaviour:** Per-view rows with xmlid + view_type.

**Acceptance:**
- [ ] Response shows multiple rows with xmlid (e.g., `sale.view_order_form`)
- [ ] Each row shows view_type (`form`)
- [ ] No traceback

**Smoke variant B: Transitive dependency closure**

**Trigger prompts:**

- **EN:** "Show the full dependency closure (transitive) of the `sale` module in Odoo 17.0."
- **VI:** "Xem toàn bộ cây phụ thuộc (transitive) của module `sale` trên Odoo 17.0."

**Expected behaviour:** Tree showing load-order + transitive closure.

**Acceptance:**
- [ ] Response contains numbered load-order lines (1, 2, 3, ...)
- [ ] Module dependencies listed with tree structure
- [ ] Core dependencies present (e.g., `base`, `account`, `web`)
- [ ] No traceback

**Smoke variant C: OWL components**

**Trigger prompts:**

- **EN:** "List all OWL components defined in the `web` module in Odoo 17.0."
- **VI:** "Liệt kê các OWL component trong module `web` trên Odoo 17.0."

**Expected behaviour:** Per-component rows with class name + file path.

**Acceptance:**
- [ ] Response shows multiple rows with component names (e.g., `odoo.web.AbstractAction`)
- [ ] Each row shows file path
- [ ] Count line present (e.g., `OWL Components: 15 defined`)
- [ ] No traceback

---

#### Phase 3D: Tool #14 — `entity_lookup` (M11 Wave D)

**Smoke variant A: Field lookup**

**Trigger prompts:**

- **EN:** "Use odoo-semantic entity_lookup to find all information about the field `partner_id` on model `sale.order` in Odoo 17.0."
- **VI:** "Dùng entity_lookup của odoo-semantic để tra cứu thông tin field `partner_id` của model `sale.order` trên Odoo 17.0."

**Expected behaviour:** Full field metadata (routes internally to `model_inspect(method='field')`).

**Acceptance:**
- [ ] Response contains field name `partner_id`
- [ ] Response contains field type (`Many2one`)
- [ ] Response contains comodel (`res.partner`)
- [ ] Discriminator field in `structuredContent` (verify via MCP JSON-RPC if available)
- [ ] No traceback

**Smoke variant B: Module lookup**

**Trigger prompts:**

- **EN:** "Use entity_lookup to look up the module `sale` in Odoo 17.0."
- **VI:** "Dùng entity_lookup để tra cứu module `sale` trên Odoo 17.0."

**Expected behaviour:** Module overview (routes internally to `describe_module`).

**Acceptance:**
- [ ] Response contains `Manifest:` section
- [ ] Response contains module description
- [ ] Module name `sale` mentioned
- [ ] No traceback

#### Phase 3E: Tool #25 — `profile_inspect` (ADR-0028 Wave 2 WI-4)

**Trigger prompts:**

- **EN:** "Use odoo-semantic profile_inspect to show the composition of profile `<PROFILE>` — its ancestor chain, repos, and module count — in Odoo 17.0."
- **VI:** "Dùng profile_inspect của odoo-semantic để xem thành phần của profile `<PROFILE>`: ancestor chain, repos và số module trên Odoo 17.0."

**Expected behaviour:** Profile-level discriminator. `method='summary'` returns the ancestor chain + child profiles + repos + module_count; `method='repos'` lists repos deduped across the ancestor chain; `method='modules'` returns a paginated module list scoped to the profile.

**Acceptance:**
- [ ] `summary` lists the profile's ancestor chain (or explicitly notes none)
- [ ] `summary` includes the repo list + `module_count`
- [ ] `method='repos'` returns deduped repos across the ancestor chain
- [ ] `method='modules'` returns a paginated module list scoped to the profile
- [ ] No traceback

---

### Phase 4: Stylesheet Tools (M10A, ADR-0025)

#### Phase 4A: Tool #19 — `resolve_stylesheet`

**Trigger prompts:**

- **EN:** "List all CSS/SCSS stylesheets in the `web` module for Odoo 17.0, including their import chains."
- **VI:** "Liệt kê tất cả file CSS/SCSS trong module `web` trên Odoo 17.0, kèm import chain."

**Expected behaviour:** Per-stylesheet stats + import chain.

**Acceptance:**
- [ ] Response contains `Stylesheets: N file(s)` header
- [ ] Each stylesheet line shows language (`css` or `scss`)
- [ ] Stats for each file (selectors, vars, mixins counts)
- [ ] Import chain shown with tree structure
- [ ] No traceback

**Silent-fail indicator:** `Stylesheets: 0 file(s)` → CSS/SCSS indexing may have been skipped; check WI-A7 OPS completion.

---

#### Phase 4B: Tool #20 — `find_style_override` (M10A, ADR-0025)

**Trigger prompts:**

- **EN:** "Which module last overrides the CSS selector `.o_form_view` in Odoo 17.0? Show the override chain."
- **VI:** "Module nào override CSS selector `.o_form_view` cuối cùng trên Odoo 17.0? Cho xem override chain."

**Expected behaviour:** ANN search hit with cosine score + override chain.

**Acceptance:**
- [ ] Response contains cosine similarity score (0.0–1.0)
- [ ] Response shows matched selector name (`.o_form_view`)
- [ ] Response shows file path (e.g., `addons/web/static/src/css/views.css`)
- [ ] Override chain shown with `:IMPORTS` edges
- [ ] No traceback

**Alternative prompt (SCSS variable):** `find_style_override("$primary", "17.0")` — search for SCSS variable instead.

**Silent-fail indicator:** Empty ANN results → CSS/SCSS pgvector chunks may not be indexed; check WI-A7 steps.

---

### Phase 5: ORM Validation Tools (M10.5 Phase 2, v0.8.0)

#### Phase 5A: Tool #21 — `resolve_orm_chain`

**Trigger prompts:**

- **EN:** "Trace the dotted field path `partner_id.country_id.code` on model `sale.order` in Odoo 17.0 — is it valid?"
- **VI:** "Trace đường dẫn field `partner_id.country_id.code` trên model `sale.order` trên Odoo 17.0 — có hợp lệ không?"

**Expected behaviour:** Per-hop resolution lines or `BROKEN` at failure point.

**Acceptance:**
- [ ] Response shows hop-by-hop lines: `partner_id : Many2one -> res.partner`
- [ ] Next hop: `country_id : Many2one -> res.country`
- [ ] Final hop: `code : Char` (terminal, tagged)
- [ ] OR `BROKEN` at first unresolved hop with reason
- [ ] No traceback

**Silent-fail risk:** May return `BROKEN` if `Field.comodel_name` not populated; distinguishable from tool error but indicates data gap.

---

#### Phase 5B: Tool #22 — `validate_domain`

**Trigger prompts:**

- **EN:** "Validate this search domain on `sale.order` in Odoo 17.0: `[('partner_id.country_id', '=', 'VN')]`"
- **VI:** "Kiểm tra domain tìm kiếm này trên model `sale.order` trên Odoo 17.0: `[('partner_id.country_id', '=', 'VN')]`"

**Expected behaviour:** Per-term validation + verdict header.

**Acceptance:**
- [ ] Response starts with verdict line (e.g., `VALID` or `INVALID`)
- [ ] Per-term lines show field-path validation: `partner_id.country_id` → resolved
- [ ] Operator validation: `=` → valid for char field
- [ ] No traceback

**Simpler test prompt:** `validate_domain("sale.order", "[('state','=','sale')]", "17.0")` — single-term domain.

---

#### Phase 5C: Tool #23 — `validate_depends` (Era1 v8/v9 Note)

**Trigger prompts:**

- **EN:** "Check if the `@api.depends` paths of the method `_compute_amount_total` on `sale.order` are correct in Odoo 17.0."
- **VI:** "Kiểm tra các đường dẫn `@api.depends` của method `_compute_amount_total` trên model `sale.order` có đúng không trên Odoo 17.0."

**Expected behaviour:** Per-dependency validation + note if no @api.depends.

**Acceptance:**
- [ ] Response lists each dependency: `order_line_ids.price_subtotal` → resolved
- [ ] Each line shows OK or ERROR
- [ ] Verdict line present (e.g., `All 3 dependencies OK`)
- [ ] No traceback

**Era1 note (v8/v9):** If testing against Odoo ≤v9, response returns `method has no @api.depends` — this is correct (era1 methods lack decorator). Use v12+ method for meaningful smoke.

---

#### Phase 5D: Tool #24 — `validate_relation`

**Trigger prompts:**

- **EN:** "Assert that the field `partner_id` on `sale.order` points to `res.partner` in Odoo 17.0."
- **VI:** "Xác nhận field `partner_id` trên `sale.order` trỏ đến model `res.partner` trên Odoo 17.0."

**Expected behaviour:** Comodel validation result.

**Acceptance:**
- [ ] Response shows `partner_id → res.partner` match
- [ ] Verdict line: `OK` or `MISMATCH (expected res.partner, actual <actual_comodel>)`
- [ ] No traceback

**Mismatch scenario:** If comodel mismatch exists, response shows actual comodel (e.g., `MISMATCH: expected res.partner, actual res.users`).

---

### Phase 6: MCP Resources (ADR-0030, M11 Wave F)

#### Resource R1 — `odoo://17.0/model/sale.order`

**Read method:** Ask AI client: "Read the MCP resource at `odoo://17.0/model/sale.order`"

**Expected MIME:** `text/markdown`

**Acceptance:**
- [ ] Body non-empty (>100 chars)
- [ ] Header line: `sale.order (Odoo 17.0)`
- [ ] `Defined in:` + `Extended by:` subtree
- [ ] `Fields:` count + `Methods:` count
- [ ] Cache metadata (LRU hit on second read)

---

#### Resource R2 — `odoo://17.0/field/sale.order/amount_total`

**Read method:** "Read the MCP resource at `odoo://17.0/field/sale.order/amount_total`"

**Expected MIME:** `text/markdown`

**Acceptance:**
- [ ] Header: `sale.order.amount_total (Odoo 17.0)`
- [ ] `Type:` line (e.g., `Monetary`)
- [ ] `Computed:` flag (e.g., `Yes`)
- [ ] `Stored:` flag (e.g., `Yes`)
- [ ] `Declared in:` section with module name

---

#### Resource R3 — `odoo://17.0/method/sale.order/action_confirm`

**Read method:** "Read the MCP resource at `odoo://17.0/method/sale.order/action_confirm`"

**Expected MIME:** `text/markdown`

**Acceptance:**
- [ ] Header: `sale.order.action_confirm() (Odoo 17.0)`
- [ ] Override chain per module (≥1 line)
- [ ] Module names shown for each override
- [ ] No traceback

---

#### Resource R4 — `odoo://17.0/view/sale.view_order_form`

**Read method:** "Read the MCP resource at `odoo://17.0/view/sale.view_order_form`"

**Expected MIME:** `text/markdown`

**Acceptance:**
- [ ] Header: `sale.view_order_form (Odoo 17.0)` or similar
- [ ] View type mentioned (e.g., `form`)
- [ ] Extension chain shown (≥1 XPath entry)
- [ ] No traceback

---

#### Resource R5 — `odoo://17.0/module/sale`

**Read method:** "Read the MCP resource at `odoo://17.0/module/sale`"

**Expected MIME:** `text/markdown`

**Acceptance:**
- [ ] Body matches `describe_module("sale", "17.0")` output
- [ ] Header: `sale (Odoo 17.0)`
- [ ] `Manifest:`, `Defines models:`, `Views:` sections
- [ ] Tree structure with `├─` / `└─`

---

#### Resource R6 — `odoo://17.0/pattern/<pattern_id>`

**Pre-step:** First, run tool #8 `suggest_pattern("computed field")` and extract a real `pattern_id` from response.

**Read method:** "Read the MCP resource at `odoo://17.0/pattern/<pattern_id>`" (substitute actual ID)

**Expected MIME:** `text/markdown`

**Acceptance:**
- [ ] Body non-empty
- [ ] Contains `Language:` section (e.g., `Python`)
- [ ] Contains `Keywords:` list
- [ ] Contains `Snippet:` (code example)
- [ ] Contains `Gotchas:` section

---

#### Resource R7 — `odoo://17.0/stylesheet/web/static/src/scss/primary_variables.scss`

**Read method:** "Read the MCP resource at `odoo://17.0/stylesheet/web/static/src/scss/primary_variables.scss`"

**Expected MIME:** `text/x-scss` (or `text/css` for .css files)

**Acceptance:**
- [ ] Body is raw SCSS/CSS source (NOT markdown header)
- [ ] First line is SCSS/CSS syntax (e.g., `$variable: value;` or `.selector { ... }`)
- [ ] Not a markdown tree
- [ ] MIME type correct for file extension

---

#### Resource Sentinel Test (Version Auto-Resolution)

**Read method:** "Read the MCP resource at `odoo://auto/model/sale.order`"

**Expected behaviour:** `auto` resolves to the API key's active version (set via tool #15).

**Acceptance:**
- [ ] Body header shows resolved version (e.g., `sale.order (Odoo 17.0)`)
- [ ] NOT `sale.order (Odoo auto)`
- [ ] If no active version set, resolves to latest indexed (usually v19.0)

---

## Re-Smoke: Partial Tools from Phase 1 (Close Gaps)

If tool #3 (`lookup_core_api`) was marked PARTIAL before reindex, verify now:

#### Tool #3 — `lookup_core_api` (re-verify post-reindex)

**Trigger prompts:**

- **EN:** "Look up the Odoo core API symbol `name_get` in Odoo 17.0 — what is its status?"
- **VI:** "Tra cứu biểu tượng Odoo core API `name_get` trên Odoo 17.0 — status của nó là gì?"

**Expected behaviour:** Status + signature + replacement info.

**Acceptance:**
- [ ] Response contains `name_get` symbol name
- [ ] Response contains `Status:` line
- [ ] Status should be `deprecated` (if M10C detection active) or `stable` (if not)
- [ ] Signature shown
- [ ] Replacement (if deprecated) shown or noted
- [ ] No traceback

---

If tool #8 (`suggest_pattern`) was marked PARTIAL, verify now:

#### Tool #8 — `suggest_pattern` (re-verify post-reindex)

**Trigger prompts:**

- **EN:** "Suggest a pattern for implementing a computed field across multiple models in Odoo 17.0."
- **VI:** "Gợi ý pattern để implement computed field qua nhiều model trên Odoo 17.0."

**Expected behaviour:** 3–5 pattern examples with snippets + gotchas.

**Acceptance:**
- [ ] Response contains ≥1 pattern example (NOT "no patterns indexed")
- [ ] Each pattern has `Language:`, `Keywords:`, `Snippet:`, `Gotchas:` sections
- [ ] Code snippet present
- [ ] No traceback

**Silent-fail risk:** If response is "no patterns indexed", seed-patterns step was skipped; operator should run `python -m src.indexer seed-patterns` and re-test.

---

## Post-Deploy: Verify /account/usage Page (M10B P0)

After deploying PR #200, verify the quota dashboard and plan gating are functional end-to-end.

### Operator Test — /account/usage Page

1. **Log in** to the web UI at `<PROD_BASE_URL>/login` (canonical; `/admin/login` 301-redirects here) with an admin account.
2. **Navigate** to `<PROD_BASE_URL>/account/usage`.
3. **Expected:**
   - Page loads without error (HTTP 200, no traceback visible).
   - Plan name displayed (e.g., "Free" or "Pro").
   - Monthly quota counter shown (e.g., "42 / 100 calls this month").
   - RPM limit shown (e.g., "30 requests/min").
   - Values sourced from `usage_counter` table (live, reflects last buffer flush).

4. **Verify quota headers** — make an MCP call and inspect response headers:
   ```bash
   curl -s -I -X POST <PROD_BASE_URL>/mcp \
     -H "X-API-Key: <OPERATOR_API_KEY>" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","method":"tools/list","params":{}}' | grep -i "x-quota\|x-ratelimit"
   ```
   Expected headers present:
   - `X-RateLimit-Limit: <rpm_from_plan>`
   - `X-RateLimit-Remaining: <remaining_rpm>`
   - `X-Quota-Limit: <monthly_quota_from_plan>`
   - `X-Quota-Remaining: <remaining_monthly>`

5. **Verify 429 differentiation** (optional, low-traffic window only):
   - Burst calls exceeding RPM → HTTP 429 with `reason: rpm_exceeded` in JSON.
   - A key with 0 remaining monthly quota → HTTP 429 with `reason: monthly_quota_exceeded`.

### Acceptance Criteria

- [ ] `/account/usage` page loads and shows plan + quota counters
- [ ] MCP responses include `X-RateLimit-*` and `X-Quota-*` headers
- [ ] No traceback or 500 errors on the usage page
- [ ] `usage_counter` table has rows after any MCP calls (verify via psql if needed):
  ```sql
  SELECT * FROM usage_counter ORDER BY period_yyyymm DESC LIMIT 5;
  ```

**Silent-fail indicator:** If plan shows `None` or quota shows `0/0`, m13_006 seed data may not have been applied; run `python -m src.db.migrate` and reload.

---

## Sign-Off Table

Operator fills in this table during the smoke session and attaches to session report.

| # | Tool/Resource | Type | Smoke Result | Time (min) | Notes |
|---|---|---|---|---|---|
| 11 | `describe_module` | Tool | [ ] PASS / [ ] FAIL | | |
| 12 | `model_inspect` | Tool | [ ] PASS / [ ] FAIL | | |
| 13 | `module_inspect` | Tool | [ ] PASS / [ ] FAIL | | |
| 14 | `entity_lookup` | Tool | [ ] PASS / [ ] FAIL | | |
| 15 | `set_active_version` | Tool (MUTATING) | [ ] PASS / [ ] FAIL | | |
| 16 | `set_active_profile` | Tool (MUTATING) | [ ] PASS / [ ] FAIL | | |
| 17 | `list_available_versions` | Tool | [ ] PASS / [ ] FAIL | | |
| 18 | `list_available_profiles` | Tool | [ ] PASS / [ ] FAIL | | |
| 19 | `resolve_stylesheet` | Tool | [ ] PASS / [ ] FAIL | | |
| 20 | `find_style_override` | Tool | [ ] PASS / [ ] FAIL | | |
| 21 | `resolve_orm_chain` | Tool | [ ] PASS / [ ] FAIL | | |
| 22 | `validate_domain` | Tool | [ ] PASS / [ ] FAIL | | |
| 23 | `validate_depends` | Tool | [ ] PASS / [ ] FAIL | | |
| 24 | `validate_relation` | Tool | [ ] PASS / [ ] FAIL | | |
| 25 | `profile_inspect` | Tool | [ ] PASS / [ ] FAIL | | |
| R1 | `odoo://.../model/<name>` | Resource | [ ] PASS / [ ] FAIL | | |
| R2 | `odoo://.../field/<m>/<f>` | Resource | [ ] PASS / [ ] FAIL | | |
| R3 | `odoo://.../method/<m>/<n>` | Resource | [ ] PASS / [ ] FAIL | | |
| R4 | `odoo://.../view/<xmlid>` | Resource | [ ] PASS / [ ] FAIL | | |
| R5 | `odoo://.../module/<name>` | Resource | [ ] PASS / [ ] FAIL | | |
| R6 | `odoo://.../pattern/<pid>` | Resource | [ ] PASS / [ ] FAIL | | |
| R7 | `odoo://.../stylesheet/...` | Resource | [ ] PASS / [ ] FAIL | | |
| **Re-smoke #3** | `lookup_core_api` (post-reindex) | Tool | [ ] PASS / [ ] PARTIAL | | |
| **Re-smoke #8** | `suggest_pattern` (post-reindex) | Tool | [ ] PASS / [ ] PARTIAL | | |

---

## Recommended Smoke Order (Dependency-Aware)

1. **Tool #17** — `list_available_versions()` — confirm data present
2. **Tool #18** — `list_available_profiles()` — discover profile names
3. **Tool #15** — `set_active_version("17.0")` — lock version context
4. **Tool #16** — `set_active_profile(<name>)` — lock profile context
5. **Tool #11** — `describe_module("sale")` — high-confidence basic test
6. **Tool #12** — `model_inspect("sale.order", "summary")` → then fields → then field detail
7. **Tool #13** — `module_inspect("sale", "summary")` → then dependencies → then views → then owl
8. **Tool #14** — `entity_lookup("field", model="sale.order", field="amount_total")` — test discriminator dispatch
9. **Tool #19** — `resolve_stylesheet("web")` — stylesheet index check
10. **Tool #20** — `find_style_override(".o_form_view")` — pgvector search check
11. **Tool #21** — `resolve_orm_chain("sale.order", "partner_id.country_id.code")` — comodel hop validation
12. **Tool #22** — `validate_domain("sale.order", "[('state','=','sale')]")` — simple domain first
13. **Tool #23** — `validate_depends("sale.order", "_compute_amount_total")` — v17+ method only
    > **Note**: Odoo 17 renamed `amount_total` compute method to `_amount_all` (from v15+); `_compute_amount_total` may not exist. If tool returns "method not found", replace method name in prompt. Operator verify with `model_inspect(model='sale.order', method='methods', odoo_version='17.0')` before smoke.
14. **Tool #24** — `validate_relation("sale.order", "partner_id", "res.partner")` — comodel assertion
15. **Tool #25** — `profile_inspect(<PROFILE>, "summary")` — profile composition (ancestor chain + repos + module_count)
16. **Resources R1–R7** — after tools confirm data exists
17. **Re-smoke #3** — `lookup_core_api("name_get", "17.0")` — post-reindex verification
18. **Re-smoke #8** — `suggest_pattern("computed field")` — post-seed-patterns verification

---

## Troubleshooting

| Symptom | Cause | Action |
|---------|-------|--------|
| All smokes return `HTTP 401` | Invalid/missing API key | Verify `<OPERATOR_API_KEY>` in `X-API-Key` header; key may be deactivated or expired |
| All smokes return `HTTP 429 quota_exhausted` | API rate limit hit | Pause 60 sec; use a fresh smoke-test API key; or contact admin for higher quota |
| Tool returns empty list (e.g., `Stylesheets: 0`) | Data indexing skipped | Check WI-A7 OPS completion: did `index-repo --all` finish? (default behavior embeds; pass `--no-embed` to skip) Did `seed-patterns` run? |
| Tool returns `BROKEN` (ORM validators) | Data gap in graph nodes | Field.comodel_name or Method.depends properties not populated; run full reindex with `--full` flag |
| Tool returns unstructured error text | Tool signature mismatch | Verify actual signature matches survey § D (e.g., `model_inspect` uses `model=`, `method=`, not `target=`, `kind=`) |
| Resource read returns `HTTP 404` | URI syntax error | Check URI template matches survey § E; verify `{version}` is concrete (e.g., `17.0`, not `auto`) |

---

## References

- **Phase 2D Survey:** `/tmp/osm-survey-2026-05-28/phase2d-prod-smoke-gap.md` — source prompts + drift analysis
- **ADR-0023:** Tool Output Completeness — tree grammar contract, truncation disclosure
- **ADR-0028:** Discriminator Consolidation — superset tool routing
- **ADR-0029:** Implicit Session Context — per-API-key sticky version/profile, 24h TTL
- **ADR-0030:** MCP Resources URI Scheme — `odoo://` URI grammar, LRU cache, version sentinel
- **Pre-Launch Checklist:** `docs/deploy/pre-launch-checklist.md` — master sign-off table, overrides this runbook for baseline tool #1-10 status
- **README.md §Trạng Thái Hiện Tại:** Current prod status, post-reindex (2026-05-25) — ~591k embeddings, full v8→v19 coverage

---

## Session Report Template

After completing the smoke session, document findings:

```
# Smoke Session Report — <DATE>

**Operator:** <name>
**Session duration:** <HH:mm>
**API key used:** osm_<last-6>
**Endpoint:** <PROD_BASE_URL>/mcp

## Sign-off Summary

**Total smoke items:** 25 tools + 7 resources = 32
**PASS:** <count>
**FAIL:** <count>
**PARTIAL:** <count>

### Failures (if any)

| Tool/Resource | Error | Root cause | Remediation |
|---|---|---|---|
| | | | |

### Post-Reindex Gaps

- [ ] `lookup_core_api` now correctly reflects post-reindex deprecation detection
- [ ] `suggest_pattern` now returns patterns (seed-patterns completed)
- [ ] Stylesheet tools return data (CSS/SCSS indexing confirmed)

## Approval

- [ ] Smoke complete — ready for next phase
- [ ] Gaps noted — operator defers final sign-off pending fixes
- [ ] Critical failure — escalate to dev team
```

---

**End of Runbook**
