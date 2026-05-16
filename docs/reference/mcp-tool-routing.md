# MCP Tool × Persona × Adapter Routing Matrix

> **Status (2026-05-16):** Canonical source for tool routing logic. Adapter files (cursor/gemini/openai/odoo-router) duplicate this content manually. Generator script deferred to M9+ — see [ADR-0012](../adr/0012-persona-skill-architecture.md). 21-tool surface area as of M9 W-OSM Wave 1 (7 enumeration / module overview / UI-layer tools added — see [ADR-0023](../adr/proposed/0023-tool-output-completeness.md)).

## Purpose

Single-source documentation answering:
- Which MCP tool maps to which persona?
- Which trigger phrases route a user prompt to which tool?
- Where does each adapter (Cursor, Gemini Gem, Custom GPT, Claude plugin, Haiku router) duplicate this routing logic?
- How are skill keyword conflicts resolved?

When adding a new MCP tool or persona, update **this file first**, then propagate to adapter files manually (paths in §3).

---

## 1. Tool × Persona Matrix

| MCP Tool              | CEO | Dev | Consultant | Marketer | Sales |
|-----------------------|:---:|:---:|:---:|:---:|:---:|
| resolve_model         |     | ●  | ○ | ○ | ○ |
| resolve_field         |     | ●  |   |   |   |
| resolve_method        |     | ●  |   |   |   |
| resolve_view          |     | ●  |   |   |   |
| find_examples         |     | ○  | ● | ● | ● |
| impact_analysis       | ●  | ○  |   |   |   |
| lookup_core_api       |     | ●  | ○ |   |   |
| api_version_diff      |     | ●  |   | ● | ○ |
| find_deprecated_usage | ●  | ●  |   |   |   |
| lint_check            |     | ●  |   |   |   |
| cli_help              |     | ●  |   |   |   |
| suggest_pattern       |     | ●  | ○ |   |   |
| check_module_exists   | ●  | ○  | ● | ● | ● |
| find_override_point   |     | ●  |   |   |   |
| describe_module       | ○  | ●  | ● | ○ | ○ |
| list_fields           |     | ●  | ○ |   |   |
| list_methods          |     | ●  |   |   |   |
| list_views            |     | ●  |   |   |   |
| list_owl_components   |     | ●  |   |   |   |
| list_qweb_templates   |     | ●  |   |   |   |
| list_js_patches       |     | ●  |   |   |   |

**Legend:** ● = primary (default first choice), ○ = secondary (related context)

---

## 2. Tool Trigger Phrases

### resolve_model

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "show me sale.order", "inheritance chain of res.partner", "what modules extend X", "full structure of model X", "which modules override this model" |
| **Primary VI** | "liệt kê inheritance của sale.order", "model X có module nào extend", "cho tôi xem cấu trúc model X", "module nào override model này" |
| **Args** | `model_name` (required), `odoo_version` (optional, default auto) |
| **Prefer when** | Any question about a model's overall structure, fields list, inheritance chain, or which modules extend it |
| **Skip when** | Question is about a specific field (→ resolve_field) or method (→ resolve_method) or view (→ resolve_view) |

### resolve_field

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "what type is amount_total field", "is this field computed or stored", "where is field X defined", "is partner_id required" |
| **Primary VI** | "field X kiểu dữ liệu gì", "field này computed hay stored", "field X được khai báo ở đâu", "field này bắt buộc không" |
| **Args** | `model_name` (required), `field_name` (required), `odoo_version` (optional, default auto) |
| **Prefer when** | Question about one specific field's type, compute method, related path, required flag, or which modules declare it |
| **Skip when** | Question is about entire model (→ resolve_model) or method (→ resolve_method) |

### resolve_method

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "show override chain of action_confirm", "which modules override write()", "where is method X defined", "does module Y call super() on write" |
| **Primary VI** | "method nào super() lên model kia", "ai override method X", "module Y có gọi super() không", "chuỗi override của method này" |
| **Args** | `model_name` (required), `method_name` (required), `odoo_version` (optional, default auto) |
| **Prefer when** | Question about a method's override chain, super() linkage, decorators, or which modules override it in what order |
| **Skip when** | Question is about field (→ resolve_field) or view structure (→ resolve_view) |

### resolve_view

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "show xpath overrides for sale.order form", "which modules modify view X", "what does merged XML look like", "view inheritance chain" |
| **Primary VI** | "view bị override bởi module nào", "XPath chain của view X", "file view này bị patch bởi ai", "merged XML skeleton của view" |
| **Args** | `xmlid` (required, e.g., 'sale.view_order_form'), `odoo_version` (optional, default auto) |
| **Prefer when** | Question about view inheritance chain, XPath modifications, or which modules extend a specific view |
| **Skip when** | Question is about model/field/method logic (→ resolve_*) |

### find_examples

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "show me examples of wizard usage", "how is mail.thread used in codebase", "give me code example for X pattern", "real examples of computing field with dependencies" |
| **Primary VI** | "ví dụ code dùng X trong codebase", "cách dùng X trong thực tế", "code example cho pattern Y", "mẫu code implement wizard" |
| **Args** | `query` (required, natural language), `odoo_version` (optional, default auto), `limit` (optional, default 5), `chunk_types` (optional, filter by type) |
| **Prefer when** | User asks for real code examples from the indexed codebase, not LLM-generated patterns |
| **Skip when** | User wants pattern guidance with anti-patterns (→ suggest_pattern) or wants to check if module exists (→ check_module_exists) |

### impact_analysis

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "what breaks if I change amount_total", "impact of modifying field X", "dependencies of method Y", "blast radius of removing field Z" |
| **Primary VI** | "thay đổi field X ảnh hưởng đến gì", "rủi ro khi sửa method Y", "nếu xóa field này thì gây ra gì", "dependencies của field này là gì" |
| **Args** | `entity_type` (required: 'field'/'method'/'model'), `entity_name` (required), `odoo_version` (optional, default auto) |
| **Prefer when** | CEO/Manager needs to understand business risk of a change; Dev needs to see all side effects before refactoring |
| **Skip when** | Question is about just one entity's structure (→ resolve_*) |

### lookup_core_api

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "what does @api.depends do", "signature of fields.Many2one", "how to use Environment.ref()", "is name_get still valid in Odoo 18" |
| **Primary VI** | "api.model decorator dùng thế nào", "giải thích BaseModel._inherit", "signature của fields.Char là gì", "function X còn hợp lệ không" |
| **Args** | `name` (required, full or short qualified name), `odoo_version` (optional, default auto) |
| **Prefer when** | Dev wants to know exact signature, status (stable/deprecated/removed), or replacement of an Odoo core symbol |
| **Skip when** | Question is about comparing versions (→ api_version_diff) or scanning for deprecated usage (→ find_deprecated_usage) |

### api_version_diff

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "what changed in Odoo 17 vs 16 API", "new decorators in version 17", "breaking changes between versions", "is name_get removed in 18" |
| **Primary VI** | "API thay đổi gì từ v16 sang v17", "tính năng mới trong Odoo 17", "breaking changes từ 17 sang 18", "name_get bị xóa từ v17 sang v18" |
| **Args** | `symbol` (required), `from_version` (required), `to_version` (required) |
| **Prefer when** | Dev is upgrading and needs to understand what changed in core API between two versions |
| **Skip when** | Question is about single-version API (→ lookup_core_api) or scanning codebase for deprecated usage (→ find_deprecated_usage) |

### find_deprecated_usage

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "find deprecated API usage in my codebase", "which modules use old-style _columns", "upgrade risk scan", "what needs to change before upgrading" |
| **Primary VI** | "code nào dùng API cũ sắp bị xóa", "kiểm tra deprecated usage trước khi upgrade", "module nào dùng pattern lỗi thời", "chuẩn bị gì trước khi upgrade Odoo 18" |
| **Args** | `odoo_version` (required), `kind` (optional, filter by symbol kind) |
| **Prefer when** | Dev/CEO scanning entire codebase for deprecated usage before upgrade; CEO needs business risk report |
| **Skip when** | Question is about one symbol (→ lookup_core_api) or version comparison (→ api_version_diff) |

### lint_check

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "lint check this module", "OCA style violations in module X", "check coding standards", "does this code follow Odoo guidelines" |
| **Primary VI** | "module X có vi phạm coding convention không", "kiểm tra code quality", "code này có vi phạm Odoo style không", "ruff/pylint check cho Odoo" |
| **Args** | `code` (required, source code chunk), `odoo_version` (optional, default auto), `language` (optional: 'python'/'javascript'/'xml', default 'python') |
| **Prefer when** | Dev wants to check code against Odoo-specific lint rules before committing |
| **Skip when** | Question is about deprecated API (→ find_deprecated_usage) or module existence (→ check_module_exists) |

### cli_help

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "how to run odoo-bin scaffold", "what CLI options does odoo-bin have", "is --longpolling-port still valid", "odoo-bin command for database update" |
| **Primary VI** | "cách dùng odoo-bin shell", "tham số nào để cài module mới", "flag nào để start server", "deprecated CLI option này là gì" |
| **Args** | `command` (optional: 'server'/'shell'/'scaffold'), `flag` (optional: '--http-port'), `odoo_version` (optional, default auto) |
| **Prefer when** | Dev needs version-specific Odoo CLI help, including deprecated flag replacements |
| **Skip when** | Question is about core API (→ lookup_core_api) or module existence (→ check_module_exists) |

### suggest_pattern

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "best pattern for wizard in Odoo", "how to implement multi-company", "pattern for override without breaking upstream", "right way to add computed field" |
| **Primary VI** | "cách tốt nhất implement X", "design pattern cho Odoo module", "pattern nào tránh breaking upstream", "làm thế nào để add field mà không break" |
| **Args** | `intent` (required, natural language), `odoo_version` (optional, default auto), `language` (optional: 'python'/'xml'/'js'/'all', default 'python'), `limit` (optional, default 5) |
| **Prefer when** | Dev wants curated patterns with gotchas from catalogue, not LLM-generated patterns |
| **Skip when** | Question is about existing code examples (→ find_examples) or method override chain (→ find_override_point) |

### check_module_exists

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "does module sale_management exist in Odoo 17", "is helpdesk an EE module", "check if feature X is in standard Odoo", "is this module in CE or EE" |
| **Primary VI** | "module X có trong OCA không", "Odoo 17 có tính năng X chưa", "feature này chỉ có trong Enterprise không", "module nào thay thế feature Y" |
| **Args** | `name` (required, module technical name), `odoo_version` (optional, default auto) |
| **Prefer when** | Consultant/Marketer/Sales verifying module existence across CE/EE/Viindoo editions; CEO checking if feature is standard |
| **Skip when** | Question is about feature comparison table (→ odoo-addon-diff skill) or requirement scoping (→ odoo-feature-check skill) |

### find_override_point

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "where should I override action_confirm in sale.order", "best override point for partner creation", "how to extend method X without breaking OCA", "safe place to inject custom logic" |
| **Primary VI** | "override field X ở đâu là đúng", "điểm override phù hợp cho method Y", "cách extend method mà không break upstream", "nơi nào an toàn để thêm logic" |
| **Args** | `model` (required, e.g., 'sale.order'), `method` (required), `odoo_version` (optional, default auto), `to_version` (optional, for cross-version diff) |
| **Prefer when** | Dev deciding where to inject custom behavior; needs convention guidance + super() safety + anti-patterns |
| **Skip when** | Question is about entire override chain (→ resolve_method) or code examples (→ find_examples) |

### describe_module

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "what does module viin_sale do", "describe sale_management module", "overview of website_sale", "show me the manifest and counts for module Z", "what's inside this module" |
| **Primary VI** | "module X làm gì", "tóm tắt module Y", "manifest của module Z", "module này có gì bên trong" |
| **Args** | `name` (required, module technical name), `odoo_version` (optional, default auto), `profile_name` (optional) |
| **Prefer when** | Caller needs module contents (models, views, JS) and counts in one round-trip — module-level architecture overview |
| **Skip when** | Caller only needs YES/NO + edition badge (→ check_module_exists, 1 Cypher vs 5) or wants enumerated entities (→ list_fields / list_views / list_methods) |

### list_fields

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "list all fields of sale.order", "show fields on account.move", "what fields does res.partner have", "all monetary fields on account.move", "fields added by viin_sale to sale.order" |
| **Primary VI** | "liệt kê field của model X", "tất cả field trên sale.order", "field nào thuộc về module Y" |
| **Args** | `model` (required), `odoo_version` (optional, default auto), `module` (optional filter), `kind` (optional ttype filter), `profile_name` (optional), `limit` (optional, default 200) |
| **Prefer when** | Caller needs the enumerated field list grouped by module — `resolve_model` only returns the count |
| **Skip when** | Caller wants one field's detail (→ resolve_field) or only "how many fields" (→ resolve_model is cheaper) |

### list_methods

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "list methods of sale.order", "all methods on res.partner", "what behavior does account.move have", "what are the action_* methods on sale.order" |
| **Primary VI** | "method nào trên model X", "tất cả method của sale.order", "behavior của model này" |
| **Args** | `model` (required), `odoo_version` (optional, default auto), `module` (optional filter), `profile_name` (optional), `limit` (optional, default 200) |
| **Prefer when** | Caller needs the enumerated method list grouped by module; methods overridden in ≥2 modules are marked `(*)` |
| **Skip when** | Caller wants one method's override chain (→ resolve_method) or the best override point (→ find_override_point) |

### list_views

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "list views of sale.order", "what views are defined for res.partner", "all form views on account.move", "kanban views on hr.employee" |
| **Primary VI** | "view nào của model X", "tất cả form/tree view trên sale.order", "list view của model này" |
| **Args** | `model` (required), `odoo_version` (optional, default auto), `view_type` (optional: form/tree/kanban/search/...), `profile_name` (optional), `limit` (optional, default 200) |
| **Prefer when** | Caller needs the per-model view inventory grouped by module |
| **Skip when** | Caller wants one view's xpath chain (→ resolve_view) or QWeb portal templates (→ list_qweb_templates) |

### list_owl_components

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "list OWL components in sale_management", "what OWL components does website_sale define", "OWL components for sale.order" |
| **Primary VI** | "OWL component nào trong module X", "tất cả OWL component bound to res.partner" |
| **Args** | `module` (required), `odoo_version` (optional, default auto), `bound_model` (optional — triggers heuristic warning footer), `profile_name` (optional), `limit` (optional, default 200) |
| **Prefer when** | Caller needs the OWL component inventory of a module (Odoo v14+) |
| **Skip when** | Caller wants legacy Widget extensions (v8-v13) (→ list_js_patches with `era='era1'`) or QWeb templates (→ list_qweb_templates). Returns empty + warning for Odoo v8-v13 (no OWL). |

### list_qweb_templates

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "list QWeb templates in website_sale", "what QWeb templates does module X define", "all t-name templates in module Z", "show me QWeb inheritance for module W" |
| **Primary VI** | "QWeb template nào trong module Y", "template nào trong module này", "t-name nào trong module" |
| **Args** | `module` (required), `odoo_version` (optional, default auto), `profile_name` (optional), `limit` (optional, default 200) |
| **Prefer when** | Caller needs the QWeb template inventory of a module with `t-inherit` parent info |
| **Skip when** | Caller wants OWL components (v15+ JS classes) (→ list_owl_components) or the template IS an `ir.ui.view` (→ resolve_view) |

### list_js_patches

| Attribute | Value |
|-----------|-------|
| **Primary EN** | "list JS patches on hr.employee", "all OWL patches in Odoo 17", "Widget extends in v12", "legacy widget extensions in v11" |
| **Primary VI** | "JS patch nào trên model X", "tất cả patch() trong module Y", "widget extend nào trong v11" |
| **Args** | `odoo_version` (optional, default auto), `target` (optional, patched widget/component name), `module` (optional patching module), `era` (optional: 'era1' v8-v13 Widget extend / 'era2' v14-v16 mixin include / 'era3' v15+ OWL patch — also accepts extend/include/patch), `profile_name` (optional), `limit` (optional, default 200) |
| **Prefer when** | Caller needs the per-target JS patch inventory across all eras (era1/era2/era3) |
| **Skip when** | Caller wants OWL component declarations (not patches) (→ list_owl_components) or code-level usage patterns (→ find_examples) |

---

## 3. Adapter Sync Map

Khi update routing logic trong file này, propagate manual sang các adapter sau:

| Adapter | File path | Section to update | Format |
|---------|-----------|-------------------|--------|
| Cursor IDE rules | `dist/cursor-rules.md` | `## When to call Odoo Semantic tools` (~L11-68) | Markdown list + code snippets |
| Gemini Gem | `dist/gemini-gem-instructions.md` | `## Tool Routing Rules` (~L19-88) + `## Persona Modes` (~L91-117) | Instruction prose + tables |
| Custom GPT | `dist/openai-gpt-instructions.md` | `## TOOL ROUTING` (~L19-89) + `## PERSONA MODES` (~L62-75) | System instruction prose |
| Haiku router agent | `dist/odoo-semantic-plugin/agents/odoo-router.md` | Tool category list (~L8-24) | Markdown classification |
| Plugin skills (21) | `dist/odoo-semantic-plugin/skills/<name>/SKILL.md` | `description:` frontmatter TRIGGER line | YAML trigger keywords |

> **Drift surface:** Today 6 edit points per new tool. Future generator (deferred to M9+) will reduce to 1 edit in this file + `make generate-adapters`.

---

## 4. Manual Sync Workflow

### Adding a new MCP tool

1. Update §1 Tool × Persona Matrix (add row with ● or ○ markings).
2. Update §2 Tool Trigger Phrases (add 4-row table block with EN/VI triggers + args + when to use).
3. Open each adapter file in §3 table:
   - For **Cursor rules**: add 3-5 example prompts to tool list
   - For **Gemini Gem**: add trigger phrases + persona note to Tool Routing Rules section
   - For **Custom GPT**: add trigger phrases to TOOL ROUTING section
   - For **Haiku router**: add tool name + category to tool list (odoo-semantic:odoo-<name> format)
   - For **Plugin skills** (if applicable): create `dist/odoo-semantic-plugin/skills/odoo-<name>/SKILL.md` with TRIGGER frontmatter
4. Bump `version` in ADR-0012 §Decision matrix if structural change.
5. Run smoke test from each adapter (verify prompts in their respective IDE/CLI).

### Adding a new persona

1. Update §1 (add column with persona name).
2. Update §5 if new conflicts arise with existing skills.
3. Create `docs/personas/<name>.md` following template (see other persona files for reference).
4. Add persona mode block to:
   - **Gemini Gem** adapter (if not dev-only)
   - **Custom GPT** adapter (if not dev-only)
   - Skip **Cursor** (dev-only IDE)
5. Create corresponding plugin skill(s) under `dist/odoo-semantic-plugin/skills/` if persona has dedicated workflow.

---

## 5. Skill Conflict Resolution

Plugin skills can claim overlapping trigger keywords. Resolution policy:

### 5.1 `odoo-risk-overview` vs `odoo-deprecation-audit`

- **Overlap**: "upgrade risk", "is our code ready for v17", "what breaks in our system"
- **Resolution**: 
  - `odoo-risk-overview` → **CEO/Manager persona** (no code-level detail, business framing, LOW/MEDIUM/HIGH risk labels, executive summary)
  - `odoo-deprecation-audit` → **Developer persona** (file:line evidence, code-level fixes, detailed deprecation scan)
- **Heuristic**: User mentions "team", "budget", "timeline", "business risk" → `odoo-risk-overview`. User shows code or mentions specific module/file → `odoo-deprecation-audit`.
- **MCP tools involved**: Both use `find_deprecated_usage` + `impact_analysis`; skill adds persona-specific framing + fix suggestions.

### 5.2 `odoo-version-diff` vs `odoo-feature-highlights`

- **Overlap**: "tính năng mới Odoo 17", "what's new in v17", "feature comparison"
- **Resolution**:
  - `odoo-version-diff` → **Developer persona** (API changes, breaking changes, migration guide tone, technical detail)
  - `odoo-feature-highlights` → **Marketer persona** (sales-deck tone, customer-facing language, business value, announcement copy)
- **Heuristic**: "migration", "breaking", "API", "deprecation" → `odoo-version-diff`. "highlight", "sales deck", "blog post", "announcement" → `odoo-feature-highlights`.
- **MCP tools involved**: `api_version_diff` (developer), `find_examples` (marketer/sales).

### 5.3 `odoo-feature-check` vs `odoo-addon-diff`

- **Overlap**: "is module X in CE or EE", "do we need Enterprise for feature Y", "CE vs EE feature list"
- **Resolution**:
  - `odoo-feature-check` → **Consultant persona** (requirement scoping context, gap analysis, "does standard Odoo have this")
  - `odoo-addon-diff` → **Marketer/Sales persona** (edition comparison table for proposals, feature-parity matrix)
- **Heuristic**: Embedded in scoping workshop, RFP analysis, or gap analysis → `odoo-feature-check`. Standalone "which edition for feature X" question → `odoo-addon-diff`.
- **MCP tools involved**: Both use `check_module_exists`; skill adds persona-specific context (scope vs. sales).

### 5.4 `odoo-owl-coder` vs `odoo-js-coder` at Odoo v14

- **Overlap**: Odoo v14 JavaScript code (grey zone — pre-OWL but post-legacy peak)
- **Resolution**: Prefer `odoo-js-coder` for v14 (legacy widget system + jQuery/Backbone era still dominant). OWL appeared in v15 but v14 community remains on `web.Widget` patterns.
- **Heuristic**: 
  - `odoo-js-coder` if user mentions: `odoo.define()`, `web.Widget`, `field_registry`, `AbstractField`, `inherit`, require(), legacy widget lifecycle
  - `odoo-owl-coder` if user mentions: `useService`, `t-component`, `patch()`, `useState`, template syntax, reactive component
- **MCP tools involved**: None (both skills use code generation, not MCP queries).

---

## Cross-references

- [ADR-0012 Persona-Skill Architecture](../adr/0012-persona-skill-architecture.md) — Design rationale, alternatives considered, decision matrix.
- [docs/personas/](../personas/) — Per-persona quick-start guides (CEO, Dev, Consultant, Marketer, Sales).
- [README.md §Persona Guides](../../README.md#persona-guides) — Public entry point linking to persona guides + plugin install instructions.
- Plugin skills location: `dist/odoo-semantic-plugin/skills/<name>/SKILL.md` — Each skill has `description:` TRIGGER field listing keywords.

---

## Appendix: Tool × Adapter Quick Reference

| Tool | Cursor | Gemini | OpenAI | Router | Plugin Skill |
|------|:------:|:------:|:------:|:------:|:------:|
| resolve_model | ✓ | ✓ | ✓ | ✓ | odoo-coder |
| resolve_field | ✓ | ✓ | ✓ | ✓ | odoo-coder |
| resolve_method | ✓ | ✓ | ✓ | ✓ | odoo-override-finder |
| resolve_view | ✓ | ✓ | ✓ | ✓ | odoo-coder |
| find_examples | ✓ | ✓ | ✓ | ✓ | odoo-coder |
| impact_analysis | ✓ | ✓ | ✓ | ✓ | odoo-risk-overview |
| lookup_core_api | ✓ | ✓ | ✓ | ✓ | odoo-coder |
| api_version_diff | ✓ | ✓ | ✓ | ✓ | odoo-version-diff |
| find_deprecated_usage | ✓ | ✓ | ✓ | ✓ | odoo-deprecation-audit |
| lint_check | ✓ | ✓ | ✓ | ✓ | odoo-code-reviewer |
| cli_help | ✓ | ✓ | ✓ | ✓ | odoo-coder |
| suggest_pattern | ✓ | ✓ | ✓ | ✓ | odoo-override-finder |
| check_module_exists | ✓ | ✓ | ✓ | ✓ | odoo-addon-diff |
| find_override_point | ✓ | ✓ | ✓ | ✓ | odoo-override-finder |
| describe_module | ✓ | ✓ | ✓ | ✓ | odoo-customization-inventory |
| list_fields | ✓ | ✓ | ✓ | ✓ | odoo-coder |
| list_methods | ✓ | ✓ | ✓ | ✓ | odoo-coder |
| list_views | ✓ | ✓ | ✓ | ✓ | odoo-coder |
| list_owl_components | ✓ | ✓ | ✓ | ✓ | odoo-owl-coder |
| list_qweb_templates | ✓ | ✓ | ✓ | ✓ | odoo-coder |
| list_js_patches | ✓ | ✓ | ✓ | ✓ | odoo-js-coder |

> **Note:** Each adapter implements these tools via HTTP MCP protocol to `odoo-semantic-mcp` server; no duplication of logic, only routing/routing heuristics. Tool count: 14 (M1–M5) + 7 (M9 W-OSM Wave 1) = 21.
