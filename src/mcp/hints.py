"""Next-step hint registry (single source of truth per ADR-0023 §4.3).

This module owns:

1. ``NEXT_STEP_HINTS`` — 18-entry registry mapping each drill-down tool to its
   recommended next-step templates. Templates contain ``{name}``, ``{ver}``,
   ``{module}``, ``{field}``, ``{method}``, ``{xmlid}`` placeholders rendered
   via ``str.format(**ctx)``.

2. ``TERMINAL_TOOLS`` — frozenset of three tools (``lint_check``, ``cli_help``,
   ``api_version_diff``) that MUST NOT emit a Next-step footer per ADR-0023
   §4.4.

3. ``format_next_step(hints)`` — relocated helper that renders the trailing
   ``└─ Next: a | b`` footer line per ADR-0023 §4 (accepts up to 2 hints,
   pipe-separated). Empty list returns ``""``.

4. ``hints_for(tool_name, **ctx)`` — convenience renderer: look up registry,
   render templates with ``ctx``, call ``format_next_step``. Returns ``""``
   if ``tool_name`` is in ``TERMINAL_TOOLS`` or not registered.

External callers (server.py) import from this module. There are zero
non-server callers today; ``_format_next_step`` (the old private name) has
been removed from ``server.py`` — the relocated helper is exported under
the public name ``format_next_step``.
"""

# Per ADR-0023 §4.3 table — 18 drill-down tools, each with up to 2 hints.
# Templates use str.format keyword args (name, ver, module, field, method, xmlid).
# Callers pass relevant context kwargs to hints_for(); unused ones are ignored.
NEXT_STEP_HINTS: dict[str, list[str]] = {
    "resolve_model": [
        "list_fields(model='{name}', odoo_version='{ver}') for full field list",
        "list_methods(model='{name}', odoo_version='{ver}') for behavior",
    ],
    "resolve_field": [
        "find_examples(query='{name} usage', odoo_version='{ver}') for real-world patterns",
        "impact_analysis(field='{name}', odoo_version='{ver}') for blast radius",
    ],
    "resolve_method": [
        "find_override_point(model='{model}', method='{name}', odoo_version='{ver}')"
        " for safe extension spot",
        "find_examples(query='{name} override', odoo_version='{ver}') for prior art",
    ],
    "resolve_view": [
        "list_views(model='{model}', odoo_version='{ver}') for sibling views",
        "find_examples(query='{name} xpath', odoo_version='{ver}') for inheritance patterns",
    ],
    "describe_module": [
        "list_fields(model='{name}', module='{module}', odoo_version='{ver}') for declared fields",
        "list_views(model='{name}', odoo_version='{ver}') for module views",
    ],
    "list_fields": [
        "resolve_field(model='{model}', field='{name}', odoo_version='{ver}')"
        " for one field's full chain",
        "list_methods(model='{model}', odoo_version='{ver}') for behavior",
    ],
    "list_methods": [
        "resolve_method(model='{model}', method='{name}', odoo_version='{ver}') for override chain",
        "find_override_point(model='{model}', method='{name}', odoo_version='{ver}') for hook spot",
    ],
    "list_views": [
        "resolve_view(xmlid='{name}', odoo_version='{ver}') for full xpath chain",
        "list_qweb_templates(module='{module}', odoo_version='{ver}') for QWeb siblings",
    ],
    "list_owl_components": [
        "find_examples(query='OWL {name}', odoo_version='{ver}') for component patterns",
        "list_js_patches(target='{name}', odoo_version='{ver}') for related patches",
    ],
    "list_qweb_templates": [
        "find_examples(query='QWeb {name}', odoo_version='{ver}') for template patterns",
        "resolve_view(xmlid='{name}', odoo_version='{ver}') when the template IS a view",
    ],
    "list_js_patches": [
        "find_examples(query='JS {name}', odoo_version='{ver}') for patch patterns",
        "list_owl_components(module='{module}', odoo_version='{ver}') for v15+ components",
    ],
    "check_module_exists": [
        "describe_module(name='{name}', odoo_version='{ver}') for full overview",
    ],
    "find_override_point": [
        "find_examples(query='{name} override', odoo_version='{ver}') for prior art",
        "resolve_method(model='{model}', method='{name}', odoo_version='{ver}') for chain",
    ],
    "impact_analysis": [
        "find_deprecated_usage(pattern='{name}', odoo_version='{ver}') to widen search",
        "find_examples(query='{name} migration', odoo_version='{ver}') for refactor prior art",
    ],
    "find_examples": [
        "suggest_pattern(query='{name}', odoo_version='{ver}') for curated patterns",
        "resolve_method(model='{model}', method='{name}', odoo_version='{ver}')"
        " for the canonical implementation",
    ],
    "find_deprecated_usage": [
        "impact_analysis(pattern='{name}', odoo_version='{ver}') for blast radius",
        "api_version_diff(symbol='{name}', from_version='{from_ver}', to_version='{to_ver}')"
        " for migration delta",
    ],
    "lookup_core_api": [
        "find_examples(query='{name} usage', odoo_version='{ver}') for in-the-wild patterns",
        "suggest_pattern(query='{name}', odoo_version='{ver}') for curated examples",
    ],
    "suggest_pattern": [
        "find_examples(query='{name}', odoo_version='{ver}') for real-world variants",
        "resolve_method(model='{model}', method='{name}', odoo_version='{ver}')"
        " when pattern targets a method",
    ],
}

# Per ADR-0023 §4.4 — these three tools terminate the chain (output IS the artifact).
TERMINAL_TOOLS: frozenset[str] = frozenset({"lint_check", "cli_help", "api_version_diff"})


def format_next_step(hints: list[str]) -> str:
    """Render the trailing ``└─ Next: ...`` footer per ADR-0023 §4.

    Accepts up to 2 hint strings; joins with `` | ``. Returns a single line
    ready to append as the last branch of a tree. Empty list returns ``""``.
    Caller is responsible for ADR-0023 §4 alignment rule (hints must not
    violate the calling tool's own SKIP clause — i.e., no self-reference).
    """
    if not hints:
        return ""
    if len(hints) > 2:
        hints = hints[:2]
    return f"└─ Next: {' | '.join(hints)}"


class _SafeDict(dict):  # type: ignore[type-arg]
    """str.format_map source that returns "" for missing placeholders.

    Prevents ``KeyError`` when ``hints_for(tool, name=...)`` is called without
    every placeholder a template uses (e.g. ``{ver}`` template but only
    ``name`` supplied). Tradeoff: missing-placeholder bugs become silent
    (rendered hint reads ``find_examples(query='X usage', odoo_version='')``
    — obviously wrong to a human but the call does not 500). Accepted
    because the alternative (KeyError → tool returns isError) is strictly
    worse UX for the LLM than a slightly degraded hint.
    """
    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return ""


def hints_for(tool_name: str, **ctx: object) -> str:
    """Look up ``tool_name`` in registry, render templates with ``ctx``, format footer.

    Returns ``""`` when ``tool_name`` is in ``TERMINAL_TOOLS`` (no footer per
    ADR-0023 §4.4) or not registered. Both EXTRA and MISSING ``ctx`` keys are
    silently tolerated via ``_SafeDict.__missing__`` — missing keys render as
    empty string in the template.
    """
    if tool_name in TERMINAL_TOOLS:
        return ""
    templates = NEXT_STEP_HINTS.get(tool_name, [])
    safe_ctx = _SafeDict(ctx)
    rendered = [tpl.format_map(safe_ctx) for tpl in templates]
    return format_next_step(rendered)
