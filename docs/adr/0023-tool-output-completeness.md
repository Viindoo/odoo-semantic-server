# ADR-0023 — OSM Tool Output Completeness: Tree Grammar, Language Policy, Truncation, Next-Step Hints

**Status:** Proposed (parked — will be finalized as 0023 on land)
**Date:** 2026-05-16
**Milestone:** M9 W-OSM (Wave 1)

---

## Context

The 14 MCP tools shipped in M1–M5 (`resolve_model`, `resolve_field`, `resolve_method`, `resolve_view`, `find_examples`, `impact_analysis`, `lookup_core_api`, `api_version_diff`, `find_deprecated_usage`, `lint_check`, `cli_help`, `suggest_pattern`, `check_module_exists`, `find_override_point`) each grew its own tree-text formatter ad-hoc. By the end of M9 four concrete gaps surfaced:

1. **No way to enumerate** the 72 fields of `sale.order` after `resolve_model` reported the count. There is no `list_*` family.
2. **No architecture overview at the module level** — `check_module_exists` only answers YES/NO, leaving "what does `viin_sale` contain" unanswered in one round-trip.
3. **No next-step hint** in tool output → AI client stalls when the natural follow-up needs a different tool.
4. **Unbounded lists** in `_resolve_model` (`Extended by`), `_resolve_view` (`Extended by`), and `_find_deprecated_usage` (hits) blow up context with monorepo profiles.

Phase-1 evidence also shows two indent styles already drift in existing tools:

- `_resolve_model` (`src/mcp/server.py:265-281`) uses `│   ` (pipe + 3 spaces) for the `Extended by` sublist.
- `_resolve_field` (`src/mcp/server.py:320-333`) uses `    ` (4 spaces flat) for the `Declared in:` sublist.
- `_resolve_view` (`src/mcp/server.py:430-457`) mixes both styles depending on branch position and emits `└─ No extensions` instead of skipping silently.

Wave 1 ships 7 new tools (`describe_module`, `list_fields`, `list_methods`, `list_views`, `list_owl_components`, `list_qweb_templates`, `list_js_patches`) AND retrofits all 14 existing tools. Adding 7 more tools with their own grammar — without first codifying the contract — would lock in the drift permanently. Per "Boil the Lake" in `CLAUDE.md`, the grammar must be codified once and enforced by tests before the new family lands.

This ADR is the source of truth for OSM tool output grammar.

---

## Decision

### §1 Tree-text grammar contract

#### 1.1 Header

The first line is the header. Format:

```
{entity} (Odoo {version})
```

- `{entity}` is the canonical identifier: model name (`sale.order`), `model.field` (`sale.order.amount_total`), `model.method()` (`sale.order.action_confirm()`), view xmlid (`sale.view_order_form`), module name (`viin_sale`).
- `{version}` is always rendered with the major+minor (`17.0`, not `17`).
- No trailing punctuation, no decoration.

#### 1.2 Connectors

- `├─` for every middle child of a parent.
- `└─` for the last child of a parent.
- No other connector glyphs are allowed (no `├`, `└`, `─` alone; no `+--`, `\--`).

#### 1.3 Sublist indent

A sublist is rendered under a parent branch. The indent rule depends on whether the parent itself is the last child of its own parent:

- **Non-last parent** (parent uses `├─`): sublist indent is `│   ` (pipe + 3 spaces = 4 chars). The pipe maintains the vertical line back to the grandparent.
- **Last parent** (parent uses `└─`): sublist indent is `    ` (4 spaces). The vertical line ends at the parent's `└─`, so no pipe.

Example (canonical from `_resolve_model` post-retrofit):

```
sale.order (Odoo 17.0)
├─ Defined in:     [odoo_17.0] sale
├─ Inherits from:  mail.thread, portal.mixin
├─ Extended by:
│   ├─ [odoo_17.0] sale_stock
│   ├─ [odoo_17.0] sale_management
│   └─ [acme_enterprise_17.0] viin_sale
├─ Fields:         72
└─ Methods:        58
```

Example (last-parent sublist, from `_resolve_field` post-retrofit):

```
sale.order.amount_total (Odoo 17.0)
├─ Type:     monetary
├─ Computed: Yes (_compute_amounts)
├─ Stored:   Yes
├─ Required: No
├─ Related:  —
└─ Declared in:
    ├─ [odoo_17.0] sale
    └─ [acme_enterprise_17.0] viin_sale
```

The `Declared in:` branch uses `└─` (last child), so its sublist indents with 4 spaces, not `│   `.

#### 1.4 Per-row format inside `list_*` tools

Inside list-tool subtrees, each row is:

```
{name} : {type} [<module_tag>]
```

- `{name}` is the field/method/view local name (no model prefix — model is in the header).
- `{type}` is the entity type (`monetary`, `many2one`, `char`; for methods: `compute`, `onchange`, `crud`, `api.depends`, etc.; for views: `form`, `tree`, `search`, `kanban`).
- `[<module_tag>]` is an optional bracketed tag when grouping is collapsed; when grouping by module via a subtree, the tag is omitted from rows.
- Methods have an override marker: `name(*) : kind` — the trailing `(*)` (asterisk in parentheses) marks an override (the method exists in 2+ modules for the same model). Definitions get bare `name`.
- Views render xmlid: `module.xmlid_local : type`.
- OWL components: `component_name : bound_model` (or `(unbound)` when `bound_model` is null).
- QWeb templates: `xmlid : t-inherit=<parent>` (or `(root)` when no parent).
- JS patches: `target.method : era=<era>` where era ∈ `era1` (Widget extend), `era2` (hybrid include), `era3` (OWL patch).

#### 1.5 Grouping by module inside `list_*` tools

When a list tool returns N entities spanning M modules, the tree groups rows by module. Each module becomes a subtree whose own header is the module name; its rows hang under it. Modules within a profile are ordered alphabetically. Cross-profile lists order modules by edition rank (community → enterprise → viindoo → oca → custom; same rank from ADR-0013) then alphabetical.

```
Fields of sale.order (Odoo 17.0)
├─ [odoo_17.0] sale
│   ├─ name : char
│   ├─ amount_total : monetary
│   └─ state : selection
├─ [odoo_17.0] sale_stock
│   └─ picking_ids : one2many
└─ Next: list_methods(model='sale.order', odoo_version='17.0') for behavior
```

#### 1.6 Empty section policy

Default: **skip silently**. If a sublist would have zero items, the entire parent branch is omitted from the tree. Example: `sale.order` with no `Extended by` modules renders without an `Extended by:` line at all.

Exception: when the question is explicitly enumerating (i.e., the user invoked a `list_*` tool, or invoked `find_examples`/`find_deprecated_usage` where empty IS the answer), the tool emits a single line `(none)` under the relevant header so the AI client can distinguish "no results" from "tool errored":

```
Fields of sale.order (Odoo 99.0)
└─ (none)
```

The literal string is `(none)` — lowercase, parentheses, no period. This replaces the current `_resolve_view` "└─ No extensions" string (`src/mcp/server.py:457`); during retrofit that branch is removed entirely (silent skip), since `resolve_view` is overview intent, not enumeration intent.

#### 1.7 `check_module_exists` vs `describe_module` demarcation

These two tools share the module name space but serve different intents. The grammar contract demarcates them:

| Aspect | `check_module_exists(name, version)` | `describe_module(name, version)` |
|---|---|---|
| Intent | Fast YES/NO existence check + EE guard | Full module architecture overview |
| Output size | 1–3 lines | 10–15 lines (tree) |
| Header | `{name} : {found|missing} (Odoo {version})` | `{name} (Odoo {version})` |
| Body | Edition badge + EE-confusion warning when applicable | Manifest fields, model counts, view counts, JS counts |
| Next hint | `Next: describe_module(...) for full overview` | `Next: list_fields(model=X, module=Y) for field list` |

Both tools cross-reference each other in their docstring `SKIP` clauses:

- `check_module_exists` docstring SKIP: *"Use `describe_module` instead when caller needs module contents (models, views, JS), not just existence."*
- `describe_module` docstring SKIP: *"Use `check_module_exists` instead for fast YES/NO + edition badge — `describe_module` runs 5 Cypher queries; `check_module_exists` runs 1."*

This prevents the router from inflating cheap existence checks into 5-query overviews.

---

### §2 Language Policy — English-only output (HARD RULE)

All tool return strings are English. This is a HARD rule, enforced by CI test.

**Scope of "tool return strings":**

- Tree headers and labels (`Defined in:`, `Inherits from:`, `Extended by:`, `Fields`, `Methods`, `Views`, `JS patches`, `Manifest:`, `Defines models`, `Extends models`, `Next:`).
- Error messages (`Field '<name>' not found on model '<model>' in Odoo <version>.`, `No module named '<name>' indexed for Odoo <version>.`).
- Next-step hints (`Next: list_fields(...) for full field list`).
- "More" disclosure suffixes (`... and 12 more (use list_fields(...))`).
- Empty section placeholder (`(none)`).
- Warning footers (e.g., `Warning: bound_model resolution is heuristic — may miss components using dynamic this.props.resModel`).

**Rationale.** Tool output is an API contract for the LLM client. Modern LLMs already mirror language back to the user when composing the final reply — if the user asks in Vietnamese, the assistant replies in Vietnamese regardless of tool output language. Multi-language tool output would:

- Explode the test surface (each label × N languages).
- Bloat the MCP schema if exposed as a parameter.
- Confuse the persona/skill router (`docs/adr/0012`), which trigger-matches on label substrings.
- Break grep-based AI agent scripts (e.g., parsing `Next:` to chain calls).

**Exception — Trigger patterns in docstrings.** Tool docstrings register TRIGGER phrases for the persona-skill router (see ADR-0012). These keep EN + VI variants — they are semantic match patterns, NOT user-facing output. Example from `resolve_model` (`src/mcp/server.py` near line 608):

```python
"""
...
TRIGGER:
- "which modules extend res.partner"
- "module nào extend model Y"
- "inheritance chain of sale.order"
- "chuỗi kế thừa của sale.order"
...
"""
```

The router matches user intent against these phrases; the more language variants, the better the routing accuracy. Docstrings are exempt from the language-policy regex.

**Enforcement — narrowed to static template strings.** The CI test (`tests/test_grammar_consistency.py::test_language_policy_static_strings`) regex-checks `[À-ỹ]` (the Latin Extended-A/B range covering Vietnamese diacritics) only against the **static template parts** of f-strings and string literals inside tool implementations — NOT against interpolated `{value}` expressions.

Concrete consequence: an f-string like `f"Module '{name}' in profile '{profile_name}'"` is OK even when `profile_name='viindoo_việt_17'` flows through at runtime, because `việt` is in the interpolated value, not the template. The test parses each tool's function body via `ast`, extracts only `ast.Constant(value=str)` nodes inside `ast.JoinedStr` and bare string literals, then runs the regex on those. Docstrings (`ast.get_docstring(...)`) are filtered out before checking. This pattern matches the implementation outline in the Wave 1 plan and avoids false positives on user-provided data.

---

### §3 Truncation + total disclosure pattern

All list rendering inside MCP tools goes through one shared helper:

```python
def _render_capped(
    items: list,
    formatter: Callable[[Any], str],
    cap: int = LIST_PREVIEW_MAX_ITEMS,
    total: int | None = None,
    more_hint: str | None = None,
) -> list[str]:
    """Render `items` to formatted lines, truncated to `cap`, with total disclosure.

    Always emits '... and {total-cap} more (use {more_hint})' when `total > cap`.

    Parameters:
        items: list of entities to render (already sliced or full).
        formatter: callable mapping one item → one formatted line (no prefix indent).
        cap: max items to render (default LIST_PREVIEW_MAX_ITEMS).
        total: total count for disclosure. Defaults to len(items); explicit when
               caller pre-filtered or fetched LIMIT+1 to detect overflow.
        more_hint: suggested follow-up tool call to retrieve the full list.
                   Required when total > cap; otherwise ignored.

    Returns:
        List of formatted lines (no tree connector — caller adds connectors).
    """
```

**Behavior:**

- If `total ≤ cap`: render all items, no disclosure suffix.
- If `total > cap`: render the first `cap` items, append one disclosure line:
  ```
  ... and {total - cap} more (use {more_hint})
  ```
- The disclosure line is the LAST line in the returned list. The caller decides what connector to attach.
- If `more_hint is None` and `total > cap`: raise `ValueError("more_hint required when total > cap")`. This is a programmer error — every truncating call site MUST suggest a follow-up.

**Defaults — per-tool caps.** Single source of truth in `src/constants.py`:

```python
LIST_PREVIEW_MAX_ITEMS    = 20   # default for resolve_model.Extended by,
                                  # resolve_view.Extended by, find_deprecated_usage,
                                  # describe_module.Defines/Extends models,
                                  # list_methods, list_views,
                                  # list_owl_components, list_qweb_templates
LIST_PREVIEW_FIELDS_MAX   = 50   # list_fields override — account.move has ~150 fields,
                                  # 20 is too aggressive for the dominant use case
LIST_PREVIEW_PATCHES_MAX  = 10   # list_js_patches override — patch lines are
                                  # verbose (target.method + module + era + file path),
                                  # 10 is enough to spot patterns
```

`total` defaulting to `len(items)` covers the common case where the caller passes the full result list. The explicit `total` parameter exists for the rarer case where the caller fetched `LIMIT cap+1` (overflow detection) or already filtered the list — then `total` is the pre-filter count from a separate `COUNT(...)` query.

**Example more_hint values:**

- `"list_fields(model='sale.order', odoo_version='17.0') for full list"`
- `"list_fields(model='sale.order', module='sale', odoo_version='17.0') to scope by module"`
- `"find_deprecated_usage(pattern='X', odoo_version='17.0', limit=200) to widen"`

---

### §4 Next-step hint mapping

#### 4.1 Position and format

The hint is the **last branch of the tree**, rendered as:

```
└─ Next: tool_a(...) for X | tool_b(...) for Y
```

- Always `└─` connector (the hint is always the last child of the root header).
- Maximum 2 hints, pipe-separated (` | `, with single spaces).
- Each hint is `tool_name(<key args>) for <intent>` — `<key args>` shows only the parameters the caller would change, not every parameter.
- Immediately precedes the trailing newline; nothing after the hint line.

When a tool emits content under a `└─` last-data branch AND a `Next:` line, the data branch must be promoted to `├─` so the `Next:` line is the new last child. This keeps the tree well-formed.

#### 4.2 Alignment rule (no loops)

A hint MUST NOT violate the calling tool's own docstring `SKIP` clause. Examples:

- `resolve_model` SKIP says "do not loop into `list_*` when caller asked overview" — but `resolve_model` MAY suggest `list_fields` as Next because that is the natural drill-down, not a loop. Loop = self-reference; drill-down = different tool. OK.
- `check_module_exists` MAY suggest `describe_module` (drill-down).
- `describe_module` MUST NOT suggest `check_module_exists` (regression).
- `list_fields(model=X)` MUST NOT suggest `list_fields(model=X)` (self-reference).
- `list_fields(model=X)` MAY suggest `list_fields(model=X, module=Y)` (refinement, different params) — but prefer suggesting a downstream tool instead.

The CI test (`test_next_step_no_loop`) asserts the suggested tool name in `Next:` is never the calling tool's own name.

#### 4.3 MUST emit footer — 18 drill-down tools

These tools always end with `└─ Next: ...`. The Wave 1 retrofit/implementation adds the footer:

| Tool | Recommended Next-step hint |
|---|---|
| `resolve_model` | `list_fields(model=X, odoo_version=V) for full field list \| list_methods(model=X, odoo_version=V) for behavior` |
| `resolve_field` | `find_examples(query='X usage', odoo_version=V) for real-world patterns \| impact_analysis(field=model.X, odoo_version=V) for blast radius` |
| `resolve_method` | `find_override_point(method=X, model=M, odoo_version=V) for safe extension spot \| find_examples(query='X override', odoo_version=V) for prior art` |
| `resolve_view` | `list_views(model=M, odoo_version=V) for sibling views \| find_examples(query='X xpath', odoo_version=V) for inheritance patterns` |
| `describe_module` | `list_fields(model=X, module=Y, odoo_version=V) for declared fields \| list_views(model=X, odoo_version=V) for module views` |
| `list_fields` | `resolve_field(model=X, field=Y, odoo_version=V) for one field's full chain \| list_methods(model=X, odoo_version=V) for behavior` |
| `list_methods` | `resolve_method(model=X, method=Y, odoo_version=V) for override chain \| find_override_point(method=Y, model=X, odoo_version=V) for hook spot` |
| `list_views` | `resolve_view(xmlid=X, odoo_version=V) for full xpath chain \| list_qweb_templates(module=Y, odoo_version=V) for QWeb siblings` |
| `list_owl_components` | `find_examples(query='OWL X', odoo_version=V) for component patterns \| list_js_patches(target=X, odoo_version=V) for related patches` |
| `list_qweb_templates` | `find_examples(query='QWeb X', odoo_version=V) for template patterns \| resolve_view(xmlid=X, odoo_version=V) when the template IS a view` |
| `list_js_patches` | `find_examples(query='JS X', odoo_version=V) for patch patterns \| list_owl_components(module=Y, odoo_version=V) for v15+ components` |
| `check_module_exists` | `describe_module(name=X, odoo_version=V) for full overview` (single hint) |
| `find_override_point` | `find_examples(query='X override', odoo_version=V) for prior art \| resolve_method(model=M, method=X, odoo_version=V) for chain` |
| `impact_analysis` | `find_deprecated_usage(pattern=X, odoo_version=V) to widen search \| find_examples(query='X migration', odoo_version=V) for refactor prior art` |
| `find_examples` | `suggest_pattern(query=X, odoo_version=V) for curated patterns \| resolve_method(model=M, method=X, odoo_version=V) for the canonical implementation` |
| `find_deprecated_usage` | `impact_analysis(pattern=X, odoo_version=V) for blast radius \| api_version_diff(from=V_old, to=V_new) for migration delta` |
| `lookup_core_api` | `find_examples(query='X usage', odoo_version=V) for in-the-wild patterns \| suggest_pattern(query=X, odoo_version=V) for curated examples` |
| `suggest_pattern` | `find_examples(query=X, odoo_version=V) for real-world variants \| resolve_method(model=M, method=X, odoo_version=V) when pattern targets a method` |

#### 4.4 MAY skip footer — 3 terminal tools

These tools have no natural drill-down and MUST NOT emit `Next:`:

| Tool | Reason |
|---|---|
| `lint_check` | Output is a violations list; the next step is to fix code, not call another tool. |
| `cli_help` | Output is curated CLI flag documentation; no graph drill-down exists. |
| `api_version_diff` | Output is the cross-version diff itself — the terminal artifact. Caller may chain on their own, but no single Next is canonical. |

For these three, the tree ends at the last data branch (`└─ ...`), and the language-policy test allows no `Next:` substring in their output.

---

### §5 List-tool tree grammar

This section codifies the shape of the 7 `list_*` and `describe_module` outputs. Drill-down tools are already covered by §1–§4.

#### 5.1 Header line

```
{entity_plural} of {parent} (Odoo {version})
```

Examples:

- `Fields of sale.order (Odoo 17.0)` — `list_fields`
- `Methods of sale.order (Odoo 17.0)` — `list_methods`
- `Views of sale.order (Odoo 17.0)` — `list_views`
- `OWL components of sale_management (Odoo 17.0)` — `list_owl_components`
- `QWeb templates of website_sale (Odoo 17.0)` — `list_qweb_templates`
- `JS patches on hr.employee (Odoo 13.0)` — `list_js_patches` uses `on` (verb fit: patches are applied **on** targets, not **of** them)
- `viin_sale (Odoo 17.0)` — `describe_module` uses bare module name (no plural — single module)

#### 5.2 Subtree per module

Within a `list_*` body, group rows under a per-module subtree. Each module is one `├─`/`└─` branch under the header, named `[<repo>] <module_name>`. Rows hang under the module branch with the §1.3 indent rules.

```
Fields of sale.order (Odoo 17.0)
├─ [odoo_17.0] sale
│   ├─ name : char
│   ├─ partner_id : many2one
│   └─ amount_total : monetary
├─ [odoo_17.0] sale_stock
│   ├─ picking_ids : one2many
│   └─ warehouse_id : many2one
└─ Next: resolve_field(model='sale.order', field='amount_total', odoo_version='17.0') for one field's full chain
```

#### 5.3 Per-row formats

| Tool | Row format | Notes |
|---|---|---|
| `list_fields` | `{name} : {ttype}` | `ttype` is the Field node property (`char`, `monetary`, `many2one`, ...) |
| `list_methods` | `{name}{('(*)' if override else '')} : {kind}` | `kind` ∈ `crud`, `compute`, `onchange`, `api.depends`, `action`. `(*)` marks methods appearing in ≥2 modules. |
| `list_views` | `{xmlid} : {type}` | `type` ∈ `form`, `tree`, `kanban`, `search`, `pivot`, `graph`, `calendar`, `activity`. |
| `list_owl_components` | `{component_name} : {bound_model or "(unbound)"}` | Emits `Warning: bound_model resolution is heuristic — may miss components using dynamic this.props.resModel` footer when `bound_model` filter was applied (per `parser_js.py:415` heuristic). |
| `list_qweb_templates` | `{xmlid} : t-inherit={parent or "(root)"}` | `(root)` for templates with no `t-inherit`. |
| `list_js_patches` | `{target}.{method} : era={era}` | `era` ∈ `era1` (v8–v13 Widget extend), `era2` (v14–v16 hybrid include), `era3` (v15+ OWL patch). |

#### 5.4 describe_module body

`describe_module` is structurally a list-tool family member (it summarizes a module) but its row format is the manifest schema, not a homogeneous entity stream. The canonical layout:

```
viin_sale (Odoo 17.0)
├─ Manifest:
│   ├─ Depends: sale, account, viin_base
│   ├─ Author: Viindoo
│   └─ Version: 17.0.1.2.3
├─ Defines models: 2 (sale.report.custom, viin.sale.config)
├─ Extends models: 5 (sale.order, sale.order.line, ...)
├─ Views: 12 (8 form, 3 tree, 1 search)
├─ JS patches: 3
└─ Next: list_fields(model='sale.order', module='viin_sale', odoo_version='17.0') for declared fields
```

- `Defines models` and `Extends models` lists are capped via `_render_capped` (default `LIST_PREVIEW_MAX_ITEMS=20`); the parenthesized inline list shows up to the cap with the `... and K more` suffix promoted into the same line: `... 5 (sale.order, sale.order.line, sale.order.template, ... and 2 more)`.
- `Views: N (X form, Y tree, ...)` aggregates by view `type` via Cypher `count(...) GROUP BY type`.
- All manifest fields (`Depends`, `Author`, `Version`, `License`, `Category`) read from the `Module` node properties already populated by `writer_neo4j.py`.

#### 5.5 Truncation in list-tools

Every list-tool body call goes through `_render_capped`. Specifically:

- `list_fields`: `cap=LIST_PREVIEW_FIELDS_MAX` (50).
- `list_js_patches`: `cap=LIST_PREVIEW_PATCHES_MAX` (10).
- All other list tools: `cap=LIST_PREVIEW_MAX_ITEMS` (20).
- `more_hint` MUST suggest the same tool with `limit=` raised (when `limit` is an existing parameter) OR a refinement parameter (`module=`, `kind=`, `view_type=`, `era=`).

---

## Consequences

**Positive:**

- One grammar contract enforced by CI test — no drift between the original 14 tools and the 7 new tools.
- AI clients can chain tools without round-trip parsing: every drill-down tool ends with a machine-readable `Next:` line.
- Truncation is consistent and always discloses the total — context-window pressure becomes a deterministic, scaling property, not a tool-by-tool surprise.
- English-only output policy removes a multiplication factor from the test surface (no per-language label assertions) while LLM clients retain natural-language reply quality via their own mirroring behavior.
- `check_module_exists` vs `describe_module` demarcation prevents the persona router from inflating cheap existence checks into 5-query overviews.

**Negative:**

- Wave 1 retrofit touches all 14 existing tools — each existing test's expected output must be re-verified against the new indent/connector rules. Mitigation: empirical evidence shows 99% of test fixtures have <20 entries, so the truncation rule does not change their output; only the indent-style fix (`_resolve_field`) and the empty-section policy change (`_resolve_view`) affect existing assertions.
- Language-policy enforcement requires AST parsing in the test; the test is non-trivial (~80 lines) but runs in <1s for the whole `src/mcp/server.py`.
- Per-tool cap overrides (`LIST_PREVIEW_FIELDS_MAX=50`) widen the default 20 — slight context bloat for `account.move` field listings, but inverse of the original gap (truncating at 20 of 150 was uselessly aggressive).

**Risk:**

- Future tool authors may copy/paste an old tool that predates the retrofit and re-introduce drift. Mitigation: the grammar test runs against `tools/list` enumeration, so any new `@mcp.tool()` is auto-checked at next CI run.
- The `(none)` placeholder is a magic string; if a future i18n decision reverses §2, this string would also need translation. The tradeoff is accepted — `(none)` is short, ASCII, and matches the rest of the English-only output contract.
- The `Next:` footer assumes the persona router will not re-trigger on the suggestion's words (e.g., the word `field` in `list_fields(...) for ...` causing a re-route). Mitigation: ADR-0012 trigger phrases are sentence-level, not word-level; the test asserts no infinite-loop self-reference.

---

## Follow-up (M10 / M10.5)

The following tools are planned and MUST adopt this ADR's grammar contract (§1 tree text, §2 English-only output, §3 truncation, §4 Next-step hints):

- **Stylesheet surface (M10A)** — `resolve_stylesheet(module, odoo_version)`, `find_style_override(selector_or_variable, odoo_version)`. From WI-A1 (`:Stylesheet` node landed via ADR-0025); tracked in `TASKS.md` Milestone 10 § M10A "Tool Surface Expansion".
- **ORM Intelligence (M10.5)** — `validate_domain`, `resolve_orm_chain`, `validate_depends`, `validate_relation`. From `peaceful-orbiting-dongarra.md` deferred list (WI-A7 absorption); tracked in `TASKS.md` Milestone 10.5.

When these tools land, the integrator MUST:

1. Update routing matrix `docs/reference/mcp-tool-routing.md` with TRIGGER phrases (EN + VI per §2 docstring exception) for each new tool.
2. Add Next-step hint rows for the new tools in §4.3 of this ADR (the "MUST emit footer — drill-down tools" table grows from 18 to 24 entries).
3. Update the §4.4 "MAY skip footer — terminal tools" table only if the new tool is genuinely terminal (none of the 6 planned tools qualify — all have natural drill-downs).
4. Re-run `tests/test_grammar_consistency.py` to ensure the new tools pass the language-policy + truncation-disclosure + no-self-loop tests by construction.

This ADR is **not invalidated** by the new tools — it is the contract they must conform to. The contract's "Boil the Lake" intent is precisely that the next 6 tools cost zero design rounds.

> **Tracking:** ORM Intelligence tools tracked at `TASKS.md` → M10.5 (Phase 1 comodel_name data layer + Phase 2 four MCP tools).

---

## Alternatives Considered

1. **Per-tool grammar (status quo)** — reject. Phase-1 evidence shows two indent styles already drift after 14 tools. Adding 7 more without a contract guarantees permanent drift.

2. **YAML or JSON tool output** — reject. The MCP tool surface is consumed by LLM clients that already mirror to natural language; structured output forces every consumer to re-render. Tree text is human-readable AND machine-greppable.

3. **Bilingual (EN+VI) labels in output** — reject. Doubles the test surface; conflicts with LLM mirroring. Trigger phrases keep VI in docstrings instead (where it improves router accuracy, not output legibility).

4. **Unbounded lists with client-side pagination** — reject. The MCP protocol has no native pagination cursor; tools would need to maintain server-side cursors per session. `_render_capped` + `more_hint` with raised `limit=` parameter is simpler and explicit.

5. **`Next:` as a separate response field, not embedded in tree** — reject. MCP tool responses are a single string; splitting into multiple fields would require client-side knowledge that not all clients have. Embedding `Next:` as the last tree branch keeps the response a single coherent string.

6. **Per-tool helper functions instead of `_render_capped`** — reject. Each tool would re-implement truncation slightly differently. One shared helper enforces the disclosure format by construction.

---

## References

- `docs/adr/0001-schema-evolution-policy.md` — Neo4j schema additions for new tool node types (OWLComp, QWebTmpl, JSPatch already exist; no schema change in Wave 1).
- `docs/adr/0012-persona-skill-architecture.md` — TRIGGER/PREFER/SKIP routing; this ADR's §2 exception clause for docstrings preserves the router's match surface.
- `docs/adr/0013-defined-in-ranking-heuristic.md` — 5-tier deterministic ranking used by `resolve_*` tools; this ADR's §1.5 grouping inherits the same edition-rank order.
- `src/mcp/server.py:265-281` — `_resolve_model` current `Extended by` rendering (template for retrofit).
- `src/mcp/server.py:320-333` — `_resolve_field` current `Declared in:` rendering (currently uses flat `    ` — fixed by §1.3 last-parent rule).
- `src/mcp/server.py:430-457` — `_resolve_view` current `Extended by` rendering + `No extensions` string (replaced by §1.6 silent-skip).
- `src/constants.py` — destination for `LIST_PREVIEW_MAX_ITEMS`, `LIST_PREVIEW_FIELDS_MAX`, `LIST_PREVIEW_PATCHES_MAX`.
- `src/indexer/writer_neo4j.py:263-366` — QWebTmpl/OWLComp/JSPatch node schema (read-only reference for the 3 UI list tools).
- `src/indexer/parser_js.py:415` — `bound_model` heuristic; basis for the `list_owl_components` warning footer.
- `CLAUDE.md` "Hai Nguyên Tắc Cốt Lõi" — Boil the Lake (codify grammar once) + Ship Wow Product (output must be readable without parsing).
- `.claude/plans/swift-coalescing-kurzweil.md` — Wave 1 plan that drives this ADR.

---

## Notes on this ADR's status

This file is parked in `docs/adr/proposed/` until Wave 1 lands. On merge of the Wave 1 PR, the main session moves it to `docs/adr/0023-tool-output-completeness.md`. Number 0023 is reserved because ADR-0022 was claimed by MFA TOTP (merged in PR #100 on 2026-05-15). If a concurrent ADR claims 0023 before Wave 1 lands, this file is renamed to the next free number with no content changes.
