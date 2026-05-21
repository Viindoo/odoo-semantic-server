# SPDX-License-Identifier: AGPL-3.0-or-later
"""Next-step hint registry (single source of truth per ADR-0023 §4.3).

This module owns:

1. ``NEXT_STEP_HINTS`` — 11-entry registry mapping each drill-down tool to its
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

# Per ADR-0023 §4.3 — 11 drill-down tools (v0.6: 10 flat shims removed), up to 2 hints each.
# Templates use str.format keyword args (name, ver, module, field, method, xmlid).
# Callers pass relevant context kwargs to hints_for(); unused ones are ignored.
NEXT_STEP_HINTS: dict[str, list[str]] = {
    # Superset discriminator tools (ADR-0028, v0.6+) — 10 flat shims removed.
    "model_inspect": [
        "impact_analysis(entity_type='model', entity_name='{name}',"
        " odoo_version='{ver}') for change blast radius",
        "find_examples(query='{name}', odoo_version='{ver}') for real-world usage",
    ],
    "entity_lookup": [
        "find_examples(query='{name} xpath', odoo_version='{ver}') for inheritance patterns",
        "find_override_point(model='{model}', method='{name}', odoo_version='{ver}') for hook spot",
    ],
    "module_inspect": [
        "find_examples(query='OWL {name}', odoo_version='{ver}') for component patterns",
        "find_examples(query='JS {name}', odoo_version='{ver}') for patch patterns",
    ],
    "describe_module": [
        "model_inspect(model='{name}', method='fields', odoo_version='{ver}') for declared fields",
        "model_inspect(model='{name}', method='views', odoo_version='{ver}') for module views",
    ],
    "check_module_exists": [
        "describe_module(name='{name}', odoo_version='{ver}') for full overview",
    ],
    "find_override_point": [
        "find_examples(query='{name} override', odoo_version='{ver}') for prior art",
        "model_inspect(model='{model}', method='method', odoo_version='{ver}') for chain",
    ],
    "impact_analysis": [
        "find_deprecated_usage(pattern='{name}', odoo_version='{ver}') to widen search",
        "find_examples(query='{name} migration', odoo_version='{ver}') for refactor prior art",
    ],
    "find_examples": [
        "suggest_pattern(query='{name}', odoo_version='{ver}') for curated patterns",
        "model_inspect(model='{model}', method='method', odoo_version='{ver}')"
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
        "model_inspect(model='{model}', method='method', odoo_version='{ver}')"
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
