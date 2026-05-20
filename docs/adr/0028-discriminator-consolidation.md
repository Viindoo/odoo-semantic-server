# ADR-0028 — Method-Discriminator Consolidation: model_inspect / module_inspect / entity_lookup

**Date:** 2026-05-19
**Milestone:** M11 Wave D

---

## Status

Accepted

---

## Context

The odoo-semantic MCP server shipped 21 tools across M1–M9. Ten of these share a nearly identical parameter signature `(model_or_module, odoo_version, profile_name)` and differ only in which Neo4j subgraph they walk:

| Tool (flat) | Scope axis | Entity type |
|---|---|---|
| `resolve_model` | model | single-entity overview |
| `resolve_field` | model.field | single-entity |
| `resolve_method` | model.method | single-entity |
| `resolve_view` | view xmlid | single-entity |
| `list_fields` | model | enumeration |
| `list_methods` | model | enumeration |
| `list_views` | model or module | enumeration |
| `list_owl_components` | module | enumeration |
| `list_qweb_templates` | module | enumeration |
| `list_js_patches` | module or model | enumeration |

With 21 registered tools the LLM's `tools/list` response consumes a significant portion of the context window before any user message is processed. Research across 12 production MCP servers (internal design notes, Pattern 7) shows that GitHub's MCP server collapses four `issue_read` behaviours into a single tool with a `method=get|get_comments|get_sub_issues|get_labels` discriminator enum; Postgres MCP uses a `health_type` discriminator across eight analysis modes. The pattern halves the tool count without reducing capability, and the JSON Schema enum on the discriminator self-documents the available choices to the LLM at schema-read time.

odoo-semantic's 10 tools divide naturally along two scope axes — model-scoped versus module-scoped — and one entity axis — single-entity lookup versus enumeration. A naive single mega-tool would require every parameter to be optional and nullable, forcing the LLM to reason about which combinations are valid. Instead, three superset tools with well-named discriminators map cleanly onto the two scope axes:

- **`model_inspect`** — model-scoped reads (fields, methods, views, OWL components bound to a model).
- **`module_inspect`** — module-scoped reads (QWeb templates, JS patches, views declared in a module).
- **`entity_lookup`** — single-record deep-dive for any kind of entity (model, field, method, view).

This split was chosen over a single `inventory(scope=model|module, ...)` mega-tool because it eliminates nullable-required-param confusion: a caller of `model_inspect` never needs to supply `module` and vice versa (Appendix B #6 of the Wave D plan).

A secondary design question was whether `entity_lookup` should parse dotted names (e.g., `"sale.order.amount_total"` → field `amount_total` on model `sale.order`). This was rejected because `sale.order.line.amount` is ambiguous — it could mean field `amount` on model `sale.order.line`, or field `line.amount` on model `sale.order`. Typed explicit args (`kind`, `model`, `field`, `method_name`, `xmlid`) eliminate the ambiguity without any parsing heuristic (Appendix B #7).

FastMCP 2.14.7 (the version in use at the time of this ADR) does not expose a `_meta.deprecated` field in the `tools/list` schema response. Marking old tools as deprecated therefore requires an in-output banner plus a docstring `SKIP` clause pointing to the superset tool. The in-output banner is the runtime contract; the `SKIP` clause gates the persona-skill router (see ADR-0012).

---

## Decision

### Naming convention

Three new superset tools are registered in `src/mcp/server.py` (implemented via routers in `src/mcp/inspect.py`):

1. **`model_inspect(model, method, odoo_version, profile_name, field, method_name)`**
   - `model`: dotted model name, e.g., `"sale.order"`.
   - `method`: discriminator enum — `"summary"` | `"fields"` | `"methods"` | `"views"` | `"field"` | `"method"`.
     - `"summary"` routes to `_resolve_model` (overview + field count).
     - `"fields"` / `"methods"` / `"views"` route to the corresponding `_list_*` enumeration function.
     - `"field"` requires `field=<field_name>`; routes to `_resolve_field`.
     - `"method"` requires `method_name=<method_name>`; routes to `_resolve_method`.
   - Scope: model-scoped reads. Never requires a `module` parameter.

2. **`module_inspect(name, method, odoo_version, profile_name)`**
   - `name`: Odoo module technical name, e.g., `"sale"`.
   - `method`: discriminator enum — `"summary"` | `"fields"` | `"methods"` | `"views"` | `"owl"` | `"qweb"` | `"js"`.
     - `"summary"` routes to `_describe_module`.
     - `"views"` routes to `_list_views_by_module`.
     - `"owl"` / `"qweb"` / `"js"` route to the corresponding `_list_*` enumeration function.
     - `"fields"` and `"methods"` return an informative stub (module-scoped field/method listing requires a model argument, which `module_inspect` does not accept).
   - Scope: module-scoped reads. Never requires a `model` parameter.

3. **`entity_lookup(kind, odoo_version, profile_name, model, field, method_name, xmlid, name)`**
   - `kind`: discriminator enum — `"model"` | `"field"` | `"method"` | `"view"` | `"module"` | `"pattern"`.
   - `model`, `field`, `method_name`, `xmlid`, `name`: typed explicit args; caller supplies only the args relevant to `kind`.
   - Routes to `_resolve_model` / `_resolve_field` / `_resolve_method` / `_resolve_view` / `_describe_module` / `_suggest_pattern`.
   - Does **not** accept a `target` (opaque ref) parameter — use `resolve_field(target=ref)` or the appropriate `resolve_*` tool for ref-based lookup.

### Deprecation of 10 flat tools

The 10 superseded tools become runtime shims in `src/mcp/server.py`. Each shim:

1. Prepends `DEPRECATED: use <new_tool>(...). Will be removed in v0.6.` as the first line of its output string, before calling the unchanged core function.
2. Updates its docstring `SKIP` clause to point to the superset tool name.
3. Retains its full implementation (no logic removed) so existing AI client configs continue to work through the v0.5.x series without breakage.

### Deprecation timeline

| Version | Action |
|---|---|
| v0.5.x | Three superset tools shipped (`model_inspect`, `module_inspect`, `entity_lookup`). Ten old tools become shims: output begins with `DEPRECATED:` banner; docstring `SKIP` clause updated. Existing client configs work unchanged. |
| v0.6 | Ten deprecated shims removed. Clients that have not migrated to the superset tools will receive `Tool not found` errors. Migration path: swap tool name + convert flat params to discriminator param. |

One major release between deprecation banner and removal gives integrators a full release cycle to update client configurations, which are typically embedded in AI tool config files (`claude_desktop_config.json`, `.cursor/mcp.json`, etc.) and are not always easy to update on short notice.

---

## Consequences

### Positive

- **Reduced context cost.** The `tools/list` schema shrinks from 21 tools to 24 (21 − 10 deprecated + 3 new) for the v0.5.x transition period, and to 14 after v0.6 cleanup. Each tool entry in the FastMCP schema is approximately 300–500 tokens; cutting 7 net tools saves ~2–3k tokens per session cold-start.
- **Self-documenting discriminators.** The JSON Schema `enum` on `method` and `kind` lists every valid value in the schema response, so the LLM can self-route without re-reading docstrings. GitHub's production evidence shows this reduces hallucinated parameter values significantly.
- **Single Cypher path per tool.** Each superset tool routes to exactly one core function per discriminator value; there is no conditional branching inside the Cypher queries. The fan-out is purely at the Python routing layer (`src/mcp/inspect.py`), which is trivially testable.
- **Backward compatibility through v0.5.x.** The shim pattern allows all existing AI client configs — Claude Code `~/.claude/mcp.json`, Cursor `.cursor/mcp.json`, Codex CLI `~/.codex/config.yaml` — to continue working without any user-side change until v0.6.
- **Typed args prevent ambiguity bugs.** Explicit `kind`, `model`, `field`, `method_name`, `xmlid` parameters eliminate the dotted-name parsing path that would silently misroute `sale.order.line.amount` (see Alternatives Considered below).

### Negative

- **Temporary tool count inflation.** During v0.5.x, the tool count is 24 (21 + 3 new, before the 10 deprecated shims are removed). The context-cost benefit is only fully realized after v0.6. Mitigation: the deprecation banners and docstring `SKIP` clauses actively discourage LLM selection of the old tools.
- **Breaking change at v0.6.** Any AI client config that was not updated during the v0.5.x window will break at v0.6. Mitigation: the deprecation banner in tool output is a machine-readable signal — AI agents that read their own tool output can detect the deprecation and surface the migration instruction to the user before v0.6.
- **Discriminator validation overhead.** Each superset tool must validate the `method`/`kind` enum at runtime and return an instructive error for unknown values. This is a small constant cost but is new code that must be tested (covered in `tests/test_mcp_inspect_router.py`).

---

## Alternatives Considered

### 1. Single mega `inventory(scope, ...)` tool — rejected

A single tool with `scope="model|module"` and a combined discriminator would require `model` and `name` (module name) to be simultaneously optional, creating a nullable-required-param anti-pattern. The LLM cannot reliably infer that `model` is irrelevant when `scope="module"`. Splitting into two tools with disjoint required parameters eliminates this ambiguity at the schema level, before any LLM inference is needed.

### 2. Dotted-name parsing in `entity_lookup` — rejected

A design where `entity_lookup(target="sale.order.amount_total")` auto-parses the dotted name into `(model="sale.order", field="amount_total")` was considered as a usability convenience. It was rejected because:

- `sale.order.line.amount` cannot be deterministically parsed: is this field `amount` on model `sale.order.line`, or field `line.amount` on model `sale.order`? Both are valid Odoo models.
- The disambiguation heuristic (try both, return the one that exists in the DB) adds a double Cypher round-trip on every call and produces silent mis-routing when both models have a field by that name.
- Explicit typed args (`kind="field", model="sale.order.line", field="amount"`) cost the caller one extra parameter but produce zero ambiguity. (See plan Appendix B item #7.)

### 3. Keep flat 21 tools indefinitely — rejected

Preserving the flat tool surface avoids any migration burden but does not address the LLM context cost, self-documentation gap, or the growing difficulty of adding new tool variants without inflating the schema further. Research shows that 3 of the 12 leading MCP servers have already adopted the discriminator pattern precisely because schema bloat compounds as new capabilities are added. At odoo-semantic's current rate (7 new tools per milestone), the flat surface would reach 35+ tools by M11 — a structural problem, not just an aesthetic one.

### 4. Immediate removal of 10 flat tools in v0.5 — rejected

Shipping the superset tools and simultaneously removing the flat tools in the same release would break every existing AI client config the moment v0.5 deployed. The deprecation shim approach costs one additional version cycle but eliminates a forced migration cliff that would require synchronized updates across Claude Code, Cursor, Codex CLI, Gemini CLI, and any custom client. Given that AI client configs are often managed by end users rather than administrators, the deprecation window is essential for a smooth rollout.

---

## Timeline

| Version | Action |
|---|---|
| v0.5.x | New superset tools shipped; 10 old tools become shims with `DEPRECATED:` banner |
| v0.6 | 10 deprecated shims removed (~1 major release later) |

---

## References

- Internal design notes §Pattern 7 — Method-discriminator consolidation (source rationale, GitHub/Postgres adoption evidence).
- `/home/tuan/.claude/plans/rippling-greeting-tulip.md` §5 Wave D — per-WI spec; Appendix B items #6 (split `model_inspect`/`module_inspect`) and #7 (typed args, not dotted-name parsing).
- `docs/adr/0012-persona-skill-architecture.md` — TRIGGER/PREFER/SKIP routing; docstring `SKIP` clauses on deprecated shims gate the persona router away from deprecated tool paths.
- `docs/adr/0023-tool-output-completeness.md` — tree grammar contract, English-only output, truncation; `entity_lookup` and superset tools must conform to this grammar.
- `docs/adr/0013-defined-in-ranking-heuristic.md` — 5-tier deterministic ranking used by `resolve_*` tools; `entity_lookup` inherits the same ranking via its `_resolve_*` delegate.
- `src/mcp/inspect.py` — Wave D WI-D1 implementation: `_model_inspect`, `_module_inspect`, `_entity_lookup` router functions.
- `src/mcp/server.py` — Wave D WI-D3: 3 new `@mcp.tool()` wrappers; WI-D4: 10 deprecation shim wrappers.
- `tests/test_mcp_inspect_router.py`, `tests/test_mcp_deprecation_shims.py` — Wave D WI-D5: router parametrization + shim banner + equivalence tests.
