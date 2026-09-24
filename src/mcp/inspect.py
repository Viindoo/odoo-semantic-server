# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/inspect.py
"""Discriminator-router layer for model_inspect, module_inspect, entity_lookup.

All three functions use late imports of src.mcp.server._X to avoid circular
deps (server.py will import from inspect.py via WI-D3).

See docs/adr/0028-discriminator-consolidation.md.
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Discriminator constants
# ---------------------------------------------------------------------------

_MODEL_METHODS = frozenset({
    "summary", "fields", "methods", "views", "field", "method", "extenders",
})
_MODULE_METHODS = frozenset({"summary", "fields", "methods", "views", "owl", "qweb", "js",
                              "dependencies", "tests"})
_ENTITY_KINDS = frozenset({"model", "field", "method", "view", "module", "pattern", "report"})
_PROFILE_METHODS = frozenset({"summary", "repos", "modules", "coverage"})

# H1 (#260): hard server-side cap for profile_inspect(method='modules') AND
# method='coverage' (issue #121 - the coverage category page reuses this cap).
# The docstring discloses "default 50, max 50"; the cap MUST be enforced so a
# caller-supplied limit cannot exceed it (ADR-0023 §3 - "caps never raised").
# Mirrors the min(limit, cap) clamp every _list_* path in server.py applies.
_PROFILE_MODULES_CAP = 50

# module_inspect(method='tests') preview: rows listed before "... and N more".
_MODULE_TESTS_PREVIEW_CAP = 10

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ANONYMOUS_API_KEY_ID = "anonymous"


def _invalid_method_error(router_name: str, method: str, valid: frozenset[str]) -> str:
    valid_csv = ", ".join(sorted(valid))
    return f"Error: unknown method '{method}'. Valid for {router_name}: {valid_csv}."


def _invalid_kind_error(kind: str) -> str:
    valid_csv = ", ".join(sorted(_ENTITY_KINDS))
    return f"Error: unknown kind '{kind}'. Valid: {valid_csv}."


# ---------------------------------------------------------------------------
# model_inspect
# ---------------------------------------------------------------------------

def _model_inspect(
    model: str,
    method: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    *,
    field: str | None = None,
    method_name: str | None = None,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
    start_index: int = 0,
    limit: int = 200,
    from_module: str | None = None,
    kind: str | None = None,
    view_type: str | None = None,
    name_filter: str | None = None,
) -> str:
    """Route to a model-scoped tool by discriminator.

    Parameters
    ----------
    model:
        Dotted model name, e.g. ``sale.order``.
    method:
        One of ``summary``, ``fields``, ``methods``, ``views``, ``field``,
        ``method``, ``extenders``.
    odoo_version:
        Odoo version string, e.g. ``17.0``. ``"auto"`` resolves to the latest
        indexed version.
    profile_name:
        Optional profile filter.
    field:
        Required when ``method='field'``. The field name to resolve.
    method_name:
        Required when ``method='method'``. The method name to resolve (distinct
        from the ``method`` discriminator to avoid clashing with the Python
        keyword).
    api_key_id:
        Tenant key for ref minting (default: ``'anonymous'``).
    start_index:
        Pagination cursor for fields/methods/views (zero-based SKIP).
    limit:
        Max rows per page for fields/methods/views (default 200).
    from_module:
        When set, filter results to rows declared in this module only.
        Passed through to ``_resolve_model`` (method='summary'),
        ``_list_fields`` (method='fields', as ``module=``) and
        ``_resolve_field`` (method='field').  Default ``None``.
    kind:
        Filter fields by ``Field.ttype``, e.g. ``'many2one'``.
        Only applied when ``method='fields'``.  Default ``None``.
    view_type:
        Filter views by type, e.g. ``'form'`` or ``'tree'``.
        Only applied when ``method='views'``.  Default ``None``.
    name_filter:
        Case-insensitive substring match on field/method names (e.g.
        ``'invoice'`` returns all fields whose names contain ``'invoice'``).
        Only applied when ``method='fields'`` or ``method='methods'``.
        Silently ignored for all other methods.  Default ``None``.

    Returns
    -------
    str
        Same shape as the routed ``_impl`` function. On invalid discriminator,
        returns ``"Error: ..."`` listing valid methods.
    """
    if method not in _MODEL_METHODS:
        return _invalid_method_error("model_inspect", method, _MODEL_METHODS)

    # Late import — server.py will import inspect.py; circular if eager.
    from src.mcp import server as srv

    if method == "summary":
        return srv._resolve_model(model, odoo_version, profile_name, from_module)

    if method == "fields":
        return srv._list_fields(
            model=model,
            odoo_version=odoo_version,
            module=from_module,
            kind=kind,
            profile_name=profile_name,
            api_key_id=api_key_id,
            limit=limit,
            start_index=start_index,
            name_filter=name_filter,
        )

    if method == "methods":
        return srv._list_methods(
            model=model,
            odoo_version=odoo_version,
            profile_name=profile_name,
            api_key_id=api_key_id,
            limit=limit,
            start_index=start_index,
            name_filter=name_filter,
        )

    if method == "views":
        return srv._list_views(
            model=model,
            odoo_version=odoo_version,
            view_type=view_type,
            profile_name=profile_name,
            api_key_id=api_key_id,
            limit=limit,
            start_index=start_index,
        )

    if method == "field":
        if not field:
            return (
                "Error: model_inspect(method='field') requires field='<field_name>'."
            )
        return srv._resolve_field(model, field, odoo_version, profile_name, from_module)

    if method == "method":
        if not method_name:
            return (
                "Error: model_inspect(method='method') requires"
                " method_name='<method_name>'."
            )
        return srv._resolve_method(model, method_name, odoo_version, profile_name)

    if method == "extenders":
        return srv._list_extenders(
            model=model,
            odoo_version=odoo_version,
            profile_name=profile_name,
            api_key_id=api_key_id,
            limit=limit,
            start_index=start_index,
        )

    # Unreachable — guard for exhaustiveness
    return _invalid_method_error("model_inspect", method, _MODEL_METHODS)  # pragma: no cover


# ---------------------------------------------------------------------------
# module_inspect
# ---------------------------------------------------------------------------

def _module_inspect(
    name: str,
    method: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    *,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
    start_index: int = 0,
    limit: int = 200,
    view_type: str | None = None,
    bound_model: str | None = None,
    era: str | None = None,
    target: str | None = None,
) -> str:
    """Route to a module-scoped tool by discriminator.

    Parameters
    ----------
    name:
        Technical module name, e.g. ``sale``.
    method:
        One of ``summary``, ``fields``, ``methods``, ``views``, ``owl``,
        ``qweb``, ``js``.
    odoo_version:
        Odoo version string. ``"auto"`` resolves to latest indexed.
    profile_name:
        Optional profile filter.
    api_key_id:
        Tenant key for ref minting (default: ``'anonymous'``).
    start_index:
        Pagination cursor for views/owl/qweb/js (zero-based SKIP).
    limit:
        Max rows per page for views/owl/qweb/js (default 200).
    view_type:
        Filter views by type, e.g. ``'form'`` or ``'tree'``.
        Only applied when ``method='views'``.  Default ``None``.
    bound_model:
        Filter OWL components bound to this model.
        Only applied when ``method='owl'``.  Default ``None``.
    era:
        Filter JS patches by era: ``'era1'``, ``'era2'``, or ``'era3'``.
        Only applied when ``method='js'``.  Default ``None``.
    target:
        Filter JS patches by patched target (class/widget name).
        Only applied when ``method='js'``.  Default ``None``.

    Returns
    -------
    str
        Same shape as the routed ``_impl`` function. On invalid discriminator,
        returns ``"Error: ..."`` listing valid methods.
    """
    if method not in _MODULE_METHODS:
        return _invalid_method_error("module_inspect", method, _MODULE_METHODS)

    # Late import — avoids circular dep with server.py.
    from src.mcp import server as srv

    if method == "summary":
        return srv._describe_module(name, odoo_version, profile_name)

    if method == "fields":
        # _list_fields is model-scoped; module filter narrows to this module.
        # We pass model=None is not supported, so we scan all fields in module
        # via the 'module' keyword filter with a wildcard model.
        # Closest available: _list_fields accepts module= as a filter on model fields.
        # For module-scoped field listing we need a model wildcard — not supported
        # in _list_fields (model is required). Return an informative stub.
        return (
            f"module_inspect(name='{name}', method='fields') — "
            "use model_inspect(model=<model>, method='fields', odoo_version=...) "
            "for model-scoped fields, "
            "or describe_module(name='{name}') for counts."
        ).format(name=name)

    if method == "methods":
        # Same limitation as 'fields' — _list_methods requires a model arg.
        return (
            f"module_inspect(name='{name}', method='methods') — "
            "use model_inspect(model=<model>, method='methods', odoo_version=...) "
            "for model-scoped methods, "
            "or describe_module(name='{name}') for counts."
        ).format(name=name)

    if method == "views":
        return srv._list_views_by_module(
            module=name,
            odoo_version=odoo_version,
            view_type=view_type,
            profile_name=profile_name,
            api_key_id=api_key_id,
            limit=limit,
            start_index=start_index,
        )

    if method == "owl":
        return srv._list_owl_components(
            module=name,
            odoo_version=odoo_version,
            bound_model=bound_model,
            profile_name=profile_name,
            api_key_id=api_key_id,
            limit=limit,
            start_index=start_index,
        )

    if method == "qweb":
        return srv._list_qweb_templates(
            module=name,
            odoo_version=odoo_version,
            profile_name=profile_name,
            api_key_id=api_key_id,
            limit=limit,
            start_index=start_index,
        )

    if method == "js":
        return srv._list_js_patches(
            odoo_version=odoo_version,
            module=name,
            era=era,
            target=target,
            profile_name=profile_name,
            api_key_id=api_key_id,
            limit=limit,
            start_index=start_index,
        )

    if method == "dependencies":
        # B2: transitive DEPENDS_ON closure + load order (ADR-0028 consolidation).
        return srv._module_dep_closure(name, odoo_version, profile_name)

    if method == "tests":
        # WI-4: list TestClass nodes defined in this module + flag integration modules.
        return _list_test_classes_for_module(name, odoo_version, profile_name, api_key_id)

    # Unreachable — guard for exhaustiveness
    return _invalid_method_error("module_inspect", method, _MODULE_METHODS)  # pragma: no cover


def _list_test_classes_for_module(
    module: str,
    odoo_version: str,
    profile_name: str | None,
    api_key_id: str,
) -> str:
    """List TestClass nodes defined in a module (WI-4 module_inspect method='tests').

    Flags ``is_test_integration_module=True`` for addon-level ``test_*`` modules
    (those whose primary purpose is testing, not shipping production code).
    """
    from src.mcp import server as srv
    from src.mcp.hints import format_next_step
    from src.mcp.orm import OrmQueryTimeout

    # Detect if this is an integration test module (name starts with test_)
    is_test_integration = module.startswith("test_")

    # Open a single session covering both _resolve_version and the query — mirrors
    # the pattern at _profile_summary (~L620) and _profile_modules (~L808).
    # This ensures the session is always closed (no leaked pool connection).
    with srv._get_driver().session() as session:
        v = srv._resolve_version(odoo_version, session)
        # OrmQueryTimeout propagates to the @offload_neo4j module_inspect handler
        # (metric + clean degraded string). Any other failure renders an explicit
        # "unavailable" line, marked degraded - never "No test classes indexed".
        # The total is counted separately so the preview cap is disclosed exactly.
        failed = False
        total = 0
        rows: list = []
        try:
            recs = srv._data_bounded(
                session,
                f"""
                MATCH (tc:TestClass {{module: $module, odoo_version: $v}})
                WHERE {srv._scope_pred("tc")}
                WITH tc ORDER BY tc.file_path ASC, tc.name ASC
                WITH count(tc) AS total,
                     collect({{name: tc.name, file_path: tc.file_path,
                              test_type: tc.test_type, is_helper: tc.is_helper}}) AS all_rows
                RETURN total, all_rows[..$cap] AS rows
                """,
                f"module_inspect tests ({module})",
                module=module, v=v, cap=_MODULE_TESTS_PREVIEW_CAP,
                **srv._scope(profile_name),
            )
            if recs:
                total = recs[0].get("total") or 0
                rows = list(recs[0].get("rows") or [])
        except OrmQueryTimeout:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as an unavailable line
            logger.warning("module_inspect tests query failed for %r @ %s: %s", module, v, exc)
            from src.mcp.degraded import mark_degraded
            mark_degraded("module_inspect tests query failed")
            failed = True

    header = f"module_inspect(name='{module}', method='tests', odoo_version='{v}')"
    lines = [header]

    if is_test_integration:
        lines.append("├─ is_test_integration_module: True (module name starts with test_)")

    if failed:
        lines.append(
            f"├─ Test classes: unavailable (the graph query failed; retry, this is"
            f" not evidence that [{module}] has no tests at Odoo {v})"
        )
    elif not total:
        lines.append(f"├─ No test classes indexed for [{module}] at Odoo {v}.")
    else:
        lines.append(f"├─ Test classes: {total}")
        shown = rows
        hidden = total - len(shown)
        for i, r in enumerate(shown):
            conn = "└─" if i == len(shown) - 1 and not hidden else "├─"
            cls_name = r.get("name") or "?"
            fp = r.get("file_path") or ""
            tt = r.get("test_type") or "?"
            helper_tag = " [helper]" if r.get("is_helper") else ""
            lines.append(f"│   {conn} {cls_name}{helper_tag}  [{tt}]  {fp}")

        if hidden:
            lines.append(
                f"│   └─ ... and {hidden} more (not listed; use test_class_inspect("
                f"name='<ClassName>', module='{module}', odoo_version='{v}') for one)"
            )

    next_line = format_next_step([
        f"test_class_inspect(name='<ClassName>', odoo_version='{v}')"
        " to inspect one class",
        f"test_coverage_audit(module='{module}', odoo_version='{v}')"
        " for coverage gaps",
    ])
    lines.append(next_line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# entity_lookup
# ---------------------------------------------------------------------------

def _entity_lookup(
    kind: str,
    *,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    model: str | None = None,
    field: str | None = None,
    method_name: str | None = None,
    xmlid: str | None = None,
    name: str | None = None,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
    from_module: str | None = None,
    _embedder=None,
    _query_vec=None,
) -> str:
    """Unified entity lookup by kind discriminator.

    Parameters
    ----------
    kind:
        Entity type: ``model``, ``field``, ``method``, ``view``, ``module``,
        ``pattern``, ``report``.
    odoo_version:
        Odoo version string. ``"auto"`` resolves to latest indexed.
    profile_name:
        Optional profile filter.
    model:
        Required for ``kind`` in ``{'model', 'field', 'method'}``. Dotted
        model name.
    field:
        Required for ``kind='field'``. Field name.
    method_name:
        Required for ``kind='method'``. Method name (avoids Python keyword
        clash with ``method`` discriminator used in other routers).
    xmlid:
        Required for ``kind='view'``. View XML ID. Also accepted for
        ``kind='report'`` as an alias for ``name`` (a specific report xmlid).
    name:
        Required for ``kind`` in ``{'module', 'pattern'}``. Technical module
        name or pattern intent string. For ``kind='report'`` it is an optional
        xmlid/title substring filter (give ``model`` and/or ``name``).
    api_key_id:
        Tenant key for ref minting (default: ``'anonymous'``).
    from_module:
        When set, filter results to rows declared in this module only.
        Passed through for ``kind`` in ``{'model', 'field'}``.
        Default ``None``.

    Returns
    -------
    str
        Same shape as the routed ``_impl`` function. On invalid kind or
        missing required args, returns ``"Error: ..."`` message.
    """
    if kind not in _ENTITY_KINDS:
        return _invalid_kind_error(kind)

    # Late import — avoids circular dep with server.py.
    from src.mcp import server as srv

    if kind == "model":
        if not model:
            return "Error: entity_lookup(kind='model') requires model='<model_name>'."
        return srv._resolve_model(model, odoo_version, profile_name, from_module)

    if kind == "field":
        if not model:
            return "Error: entity_lookup(kind='field') requires model='<model_name>'."
        if not field:
            return "Error: entity_lookup(kind='field') requires field='<field_name>'."
        return srv._resolve_field(model, field, odoo_version, profile_name, from_module)

    if kind == "method":
        if not model:
            return "Error: entity_lookup(kind='method') requires model='<model_name>'."
        if not method_name:
            return (
                "Error: entity_lookup(kind='method') requires"
                " method_name='<method_name>'."
            )
        return srv._resolve_method(model, method_name, odoo_version, profile_name)

    if kind == "view":
        if not xmlid:
            return "Error: entity_lookup(kind='view') requires xmlid='<xml.id>'."
        return srv._resolve_view(xmlid, odoo_version, profile_name)

    if kind == "report":
        # GAP-2/GAP-5: ir.actions.report (+ v8-v13 <report> shorthand) listing.
        # Accepts model= (reports on a business model) and/or name= (report
        # xmlid/title substring). Rendered in tree_builder (server.py is at its
        # god-file ceiling). xmlid is also accepted as an alias for name so the
        # caller can pass a specific report xmlid via the familiar `xmlid=` arg.
        from src.mcp.tree_builder import list_reports
        return list_reports(
            model=model,
            name=name or xmlid,
            odoo_version=odoo_version,
            profile_name=profile_name,
        )

    if kind == "module":
        if not name:
            return "Error: entity_lookup(kind='module') requires name='<module_name>'."
        return srv._describe_module(name, odoo_version, profile_name)

    if kind == "pattern":
        if not name:
            return (
                "Error: entity_lookup(kind='pattern') requires"
                " name='<pattern_intent_string>'."
            )
        # #227: forward a pre-embedded (semaphore-bounded, short-timeout) query
        # vector when the async wrapper supplied one; falls back to a sync embed
        # inside _suggest_pattern when called from a sync context (_query_vec=None).
        return srv._suggest_pattern(
            name, odoo_version, _embedder=_embedder, _query_vec=_query_vec,
        )

    # Unreachable — guard for exhaustiveness
    return _invalid_kind_error(kind)  # pragma: no cover


# ---------------------------------------------------------------------------
# profile_inspect (WI-4, #260, #259 chain-exposure) — ADR-0028 discriminator
# ---------------------------------------------------------------------------

def _profile_inspect(
    name: str | None,
    method: str,
    odoo_version: str = "auto",
    repo: str | None = None,
    *,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
    start_index: int = 0,
    limit: int = 50,
) -> str:
    """Route to a profile-scoped introspection view by discriminator.

    Parameters
    ----------
    name:
        Profile name to inspect (e.g. ``'viindoo_internal_17'``).
        Required for ``method='summary'`` and ``method='coverage'``. Optional for
        ``method='repos'`` and ``method='modules'`` (``None`` = all caller-visible
        profiles).
    method:
        ``summary`` | ``repos`` | ``modules`` | ``coverage``.
    odoo_version:
        Odoo version string. ``'auto'`` resolves to the session pin.
    repo:
        Filter modules/repos by repo URL substring. Applied only for
        ``method='modules'`` and ``method='repos'``.
    api_key_id:
        Tenant key for RBAC (default: ``'anonymous'``).
    start_index:
        Pagination cursor (zero-based SKIP) for ``method='modules'`` (over
        modules) and ``method='coverage'`` (over categories).
    limit:
        Max rows per page for ``method='modules'`` and ``method='coverage'``
        (default 50, capped at 50).

    Returns
    -------
    str
        Tree-formatted output (ADR-0023 §1). On invalid discriminator
        returns ``'Error: ...'``.
    """
    if method not in _PROFILE_METHODS:
        return _invalid_method_error("profile_inspect", method, _PROFILE_METHODS)

    # Late import — avoids circular dep with server.py.
    from src.mcp import server as srv

    if method == "summary":
        if not name:
            return (
                "Error: profile_inspect(method='summary') requires name='<profile_name>'."
            )
        return _profile_summary(name, odoo_version, srv)

    if method == "repos":
        return _profile_repos(name, odoo_version, repo, srv)

    if method == "modules":
        return _profile_modules(
            name, odoo_version, repo,
            start_index=start_index, limit=limit, srv=srv,
        )

    if method == "coverage":
        return _profile_coverage(
            name, odoo_version, srv,
            start_index=start_index, limit=limit,
        )

    # Unreachable
    return _invalid_method_error("profile_inspect", method, _PROFILE_METHODS)  # pragma: no cover


# ---------------------------------------------------------------------------
# profile_inspect helpers
# ---------------------------------------------------------------------------

def _caller_boundary(srv) -> dict:
    """The caller's full ADR-0034 boundary (``own``, ``shared``), without the session pin.

    For reads about an EXPLICITLY named profile: a session pin (set_active_profile)
    only defaults the profile when none is given, so it must not narrow - or zero -
    another profile's counts. The tenant boundary still applies unchanged
    (admin: unrestricted).
    """
    return srv._scope(None, pin=False)


def _ancestor_chain_or_self(name: str) -> list[str]:
    """*name* then its ancestors (root last); ``[name]`` when the registry is down.

    A registry failure is marked degraded (never cached) and logged: the
    with-ancestors numbers then equal the own numbers.
    """
    from src.db.pg import repo_store
    try:
        return repo_store().get_ancestor_profile_names(name) or [name]
    except Exception as exc:  # noqa: BLE001 - registry down degrades, never fails the tool
        logger.warning("ancestor chain of profile %r unavailable: %s", name, exc)
        from src.mcp.degraded import mark_degraded
        mark_degraded("profile ancestor chain unavailable")
        return [name]


def _visible_profile_names(srv) -> set[str] | None:
    """Profile names this key may see (own + shared, pin ignored); None = admin (all).

    ADR-0034 fail-closed: profile_inspect renders another profile's NAME (ancestor
    chain, children, "inherited from") or its repos only when it is in this set.
    """
    allowed = srv._session.resolve_allowed_profiles(srv._get_tenant_id())
    return None if allowed is None else set(allowed)


def _split_visible(names: list[str], visible: set[str] | None) -> tuple[list[str], int]:
    """(names the key may see, in order; how many were withheld)."""
    if visible is None:
        return list(names), 0
    shown = [n for n in names if n in visible]
    return shown, len(names) - len(shown)


def _hidden_suffix(hidden: int, noun: str) -> str:
    if not hidden:
        return ""
    return f" (+{hidden} {noun}{'' if hidden == 1 else 's'} not visible to this key)"


def _chain_label(ancestors: list[str], visible: set[str] | None) -> str:
    shown, hidden = _split_visible(ancestors, visible)
    return " -> ".join(shown) + _hidden_suffix(hidden, "ancestor profile")


def _profile_not_visible(name: str, method: str) -> str:
    return (
        f"profile_inspect(name={name!r}, method={method!r})\n"
        f"└─ Not found or not authorized: profile '{name}' is not visible to this key."
        " Use list_available_profiles() to see accessible profiles."
    )


def _profile_summary(name: str, odoo_version: str, srv) -> str:
    """Render profile summary: ancestor chain, children, repos, module_count."""
    from src.db.pg import repo_store

    # RBAC: check caller can see this profile at all.
    allowed = srv._effective_allowed(name)
    if allowed is not None and name not in allowed:
        return _profile_not_visible(name, 'summary')

    # Ancestors (self first, root last).
    ancestors = repo_store().get_ancestor_profile_names(name)
    if not ancestors:
        return (
            f"profile_inspect(name={name!r}, method='summary')\n"
            f"└─ Not found: profile '{name}' does not exist."
        )

    # Children (one level down).
    children = repo_store().get_children_profiles(name)

    # Repos for the full ancestor chain (depth-ordered: own repos first).
    repos = repo_store().get_ancestor_repos(name)

    # Deduplicate repos by (url, branch) — keep the shallowest (own = first seen).
    seen_repo_keys: set[tuple[str, str]] = set()
    unique_repos: list[dict] = []
    for r in repos:
        key = (r["url"], r["branch"])
        if key not in seen_repo_keys:
            seen_repo_keys.add(key)
            unique_repos.append(r)

    # Module count via Neo4j (needs #259 writer fix + backfill to be non-zero).
    try:
        with srv._get_driver().session() as neo_session:
            odoo_version = srv._resolve_version(odoo_version, neo_session)
            # The caller's tenant boundary WITHOUT the session pin
            # (_caller_boundary): name is explicit, so a pin to another profile
            # must not zero this count. Profile membership is filtered
            # separately; the caller-can-see-this-profile check is done above
            # via _effective_allowed(name).
            # Routed through srv._single_bounded so a tx-timeout becomes
            # OrmQueryTimeout (clean English, no Cypher leaked). The surrounding
            # `except Exception` below catches it too — the count degrades to
            # `unavailable` rather than failing the whole summary; the owning
            # profile_inspect handler (@offload_neo4j) never sees this timeout
            # because the summary swallows it here (graceful per-substep degrade).
            rec = srv._single_bounded(
                neo_session,
                f"""
                MATCH (m:Module)
                WHERE m.odoo_version = $v
                  AND {srv._scope_pred('m')}
                  AND $profile_name IN m.profile
                RETURN count(m) AS cnt
                """,
                f"module count for profile '{name}' (Odoo {odoo_version})",
                v=odoo_version,
                profile_name=name,
                **_caller_boundary(srv),
            )
            module_count = rec["cnt"] if rec else 0
            chain_rec = srv._single_bounded(
                neo_session,
                f"""
                MATCH (m:Module)
                WHERE m.odoo_version = $v
                  AND {srv._scope_pred('m')}
                  AND any(__a IN m.profile WHERE __a IN $chain)
                RETURN count(m) AS cnt
                """,
                f"module count for profile '{name}' incl. ancestors (Odoo {odoo_version})",
                v=odoo_version,
                chain=ancestors,
                **_caller_boundary(srv),
            )
            chain_count = chain_rec["cnt"] if chain_rec else 0
    except Exception:
        module_count = None  # graceful degradation if Neo4j unavailable
        chain_count = None
        # L1 fix: if odoo_version was not resolved before the exception
        # (e.g. driver down before line 536), normalize the sentinel so the
        # header never renders 'auto' literally.
        from src.mcp import session as _sess
        _normalized = _sess.normalize_version_arg(odoo_version)
        if _normalized is None:
            odoo_version = "(unresolved)"

    # Build tree output.
    lines = [f"profile_inspect(name={name!r}, method='summary', odoo_version={odoo_version!r})"]

    # ADR-0034: names and repos of profiles outside this key's scope (another
    # tenant's private ancestor or child) are withheld and disclosed as a count.
    visible = _visible_profile_names(srv)

    # Ancestor chain.
    if len(ancestors) == 1:
        lines.append(f"├─ Ancestor chain: {name} (root, no parent)")
    else:
        lines.append(f"├─ Ancestor chain: {_chain_label(ancestors, visible)}")

    # Children.
    shown_children, hidden_children = _split_visible(children, visible)
    if children:
        listed = ", ".join(shown_children) or "none visible"
        lines.append(
            f"├─ Children ({len(children)}): {listed}"
            f"{_hidden_suffix(hidden_children, 'child profile')}"
        )
    else:
        lines.append("├─ Children: none")

    # Repos.
    shown_repos = [
        r for r in unique_repos if visible is None or r["profile_name"] in visible
    ]
    hidden_repos = len(unique_repos) - len(shown_repos)
    lines.append(
        f"├─ Repos ({len(unique_repos)} unique across ancestor chain)"
        f"{_hidden_suffix(hidden_repos, 'repo')}:"
    )
    for i, r in enumerate(shown_repos):
        prefix = "│   └─" if i == len(shown_repos) - 1 else "│   ├─"
        depth_tag = " [own]" if r["depth"] == 0 else f" [inherited from {r['profile_name']}]"
        status = r.get("status", "unknown")
        lines.append(f"{prefix} {r['url']} @ {r['branch']}{depth_tag}  status:{status}")

    # Two counts so the reader knows where each part comes from. Nodes carry only
    # the profile that owns their repo (ADR-0034 single-owner), so "this profile"
    # is `$profile_name IN m.profile` and "with ancestors" is any profile of the
    # ancestor chain. Both pass the caller's tenant choke, so ancestor modules a
    # tenant may not see are not counted in its view.
    footer = srv.hints_for("profile_inspect", name=name, ver=odoo_version)
    # ADR-0023 §4.1: the Next footer is the root's last child, so the last data
    # branch stays ├─ whenever a footer follows.
    last = "├─" if footer else "└─"
    if module_count is not None:
        sub = "│   " if footer else "    "
        lines.append(f"{last} Module count (version {odoo_version}):")
        lines.append(f"{sub}├─ Owned by this profile: {module_count}")
        lines.append(
            f"{sub}└─ Including ancestor profiles ({_chain_label(ancestors, visible)}):"
            f" {chain_count}"
        )
    else:
        lines.append(f"{last} Module count: unavailable")
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def _profile_repos(
    name: str | None,
    odoo_version: str,
    repo_filter: str | None,
    srv,
) -> str:
    """Render distinct repos for a profile (or all visible profiles when name=None)."""
    from src.db.pg import repo_store

    # RBAC: restrict to caller-visible profiles.
    allowed = srv._effective_allowed(name)

    if name:
        # Check access.
        if allowed is not None and name not in allowed:
            return _profile_not_visible(name, 'repos')
        visible = _visible_profile_names(srv)
        chain_repos = repo_store().get_ancestor_repos(name)
        repos_raw = [
            r for r in chain_repos if visible is None or r["profile_name"] in visible
        ]
        withheld_repos = len({(r["url"], r["branch"]) for r in chain_repos}) - len(
            {(r["url"], r["branch"]) for r in repos_raw})
    else:
        withheld_repos = 0
        # All profiles visible to this caller.
        if allowed is None:
            # Admin: all repos.
            from src.db.pg import get_pool
            with get_pool().checkout() as conn:
                repos_raw = get_pool().fetch_all(conn, """
                    SELECT r.*, p.name AS profile_name, 0 AS depth, p.odoo_version
                    FROM repos r JOIN profiles p ON r.profile_id = p.id
                    ORDER BY r.url, r.branch, r.id
                """)
        elif not allowed:
            repos_raw = []
        else:
            from src.db.pg import get_pool
            with get_pool().checkout() as conn:
                repos_raw = get_pool().fetch_all(conn, """
                    SELECT r.*, p.name AS profile_name, 0 AS depth, p.odoo_version
                    FROM repos r JOIN profiles p ON r.profile_id = p.id
                    WHERE p.name = ANY(%s)
                    ORDER BY r.url, r.branch, r.id
                """, (allowed,))

    # Deduplicate by (url, branch) - keep first occurrence.
    seen: set[tuple[str, str]] = set()
    unique_repos: list[dict] = []
    for r in repos_raw:
        key = (r["url"], r["branch"])
        if key not in seen:
            if repo_filter is None or repo_filter in r["url"]:
                seen.add(key)
                unique_repos.append(r)

    scope_label = f"name={name!r}" if name else "all visible"
    lines = [f"profile_inspect({scope_label}, method='repos')"]
    withheld = _hidden_suffix(max(withheld_repos, 0), "ancestor repo")
    if not unique_repos:
        lines.append(f"└─ No repos found{withheld}.")
        return "\n".join(lines)

    lines.append(f"├─ Repos ({len(unique_repos)} unique){withheld}:")
    for i, r in enumerate(unique_repos):
        prefix = "│   └─" if i == len(unique_repos) - 1 else "│   ├─"
        status = r.get("status", "unknown")
        clone = r.get("clone_status", "manual")
        profile_tag = f"  [profile: {r['profile_name']}]" if not name else ""
        lines.append(
            f"{prefix} {r['url']} @ {r['branch']}"
            f"{profile_tag}  status:{status}  clone:{clone}"
        )
    footer = srv.hints_for("profile_inspect", name=name or "", ver=odoo_version)
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def _profile_modules(
    name: str | None,
    odoo_version: str,
    repo_filter: str | None,
    *,
    start_index: int,
    limit: int,
    srv,
) -> str:
    """Render paginated module list for a profile, optionally filtered by repo URL."""
    # RBAC: Neo4j choke via _scope.
    allowed = srv._effective_allowed(name)
    if name and allowed is not None and name not in allowed:
        return _profile_not_visible(name, 'modules')

    # H1 (#260): enforce the disclosed cap — a large caller limit must not
    # return more than _PROFILE_MODULES_CAP rows (ADR-0023 §3).
    effective_limit = min(limit, _PROFILE_MODULES_CAP)

    with srv._get_driver().session() as neo_session:
        odoo_version = srv._resolve_version(odoo_version, neo_session)

        # Build the WHERE clause for optional profile + repo filters.
        profile_clause = "AND $profile_name IN m.profile" if name else ""
        repo_clause = "AND m.repo_url CONTAINS $repo_filter" if repo_filter else ""

        # name=None: _scope(None) - the tenant boundary narrowed by any session
        # pin (ADR-0029 #251), so a pinned session lists only the pinned profile.
        # Explicit name: the boundary WITHOUT the pin (_caller_boundary) - the pin
        # only defaults a missing profile and must not empty another one's list.
        # Profile-specific filtering is applied separately via profile_clause
        # ($profile_name IN m.profile): the modules OWNED by that profile (nodes
        # carry their single owning profile, ADR-0034), parent profiles excluded.
        # The caller-can-see-this-profile check is already done via
        # _effective_allowed(name) above.
        scope_params = _caller_boundary(srv) if name else srv._scope(None)

        # Routed through srv._single_bounded / srv._data_bounded so a tx-timeout
        # becomes OrmQueryTimeout (clean English, no Cypher leaked). _profile_modules
        # has no internal catch, so the raise propagates to the owning
        # profile_inspect handler (now @offload_neo4j) which records the metric +
        # returns the clean string.
        total_rec = srv._single_bounded(
            neo_session,
            f"""
            MATCH (m:Module)
            WHERE m.odoo_version = $v
              AND {srv._scope_pred('m')}
              {profile_clause}
              {repo_clause}
            RETURN count(m) AS total
            """,
            f"module count for profile '{name or 'all visible'}' (Odoo {odoo_version})",
            v=odoo_version,
            profile_name=name,
            repo_filter=repo_filter or "",
            **scope_params,
        )
        total = total_rec["total"] if total_rec else 0

        if total == 0:
            scope_label = f"name={name!r}" if name else "all visible"
            return (
                f"profile_inspect({scope_label}, method='modules',"
                f" odoo_version={odoo_version!r})\n"
                "└─ No modules found. Verify the profile name, or call "
                "list_available_profiles to see indexed scope."
            )

        rows = srv._data_bounded(
            neo_session,
            f"""
            MATCH (m:Module)
            WHERE m.odoo_version = $v
              AND {srv._scope_pred('m')}
              {profile_clause}
              {repo_clause}
            RETURN m.name AS name, m.edition AS edition, m.repo AS repo,
                   m.repo_url AS repo_url
            ORDER BY m.name ASC
            SKIP $skip LIMIT $lim
            """,
            f"module list for profile '{name or 'all visible'}' (Odoo {odoo_version})",
            v=odoo_version,
            profile_name=name,
            repo_filter=repo_filter or "",
            skip=start_index,
            lim=effective_limit,
            **scope_params,
        )

    scope_label = f"name={name!r}" if name else "all visible"
    page_end = start_index + len(rows)
    lines = [
        f"profile_inspect({scope_label}, method='modules',"
        f" odoo_version={odoo_version!r})",
        f"├─ Showing rows {start_index + 1}-{page_end} of {total}:",
    ]
    for i, r in enumerate(rows):
        prefix = "│   └─" if i == len(rows) - 1 else "│   ├─"
        edition = r.get("edition") or "community"
        repo_tag = f"  [{r['repo']}]" if r.get("repo") else ""
        lines.append(f"{prefix} {r['name']}  ({edition}){repo_tag}")

    footer = srv.hints_for("profile_inspect", name=name or "", ver=odoo_version)
    last = "├─" if footer else "└─"
    if page_end < total:
        next_start = start_index + effective_limit
        more_hint = (
            f"profile_inspect(name={name!r}, method='modules',"
            f" odoo_version={odoo_version!r}, start_index={next_start})"
        )
        lines.append(f"{last} ... and {total - page_end} more (use {more_hint})")
    else:
        lines.append(f"{last} End of list ({total} total).")
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def _profile_coverage(
    name: str | None,
    odoo_version: str,
    srv,
    *,
    start_index: int,
    limit: int,
) -> str:
    """Render indexed module coverage by category, with a superset-diff (#121 P1).

    Rec.4 ("absence-in-index != absence-in-product"): a category breakdown alone
    only shows what IS indexed. To hint at what may be MISSING from this profile
    we add a data-driven (no curated SSOT) superset-diff: for each category we
    report ``own`` (modules stamped with THIS profile) and ``with_ancestors`` (any
    profile of its ancestor chain), and compare the latter against the count of
    modules of that category visible to the caller across the whole index
    (``indexed_elsewhere`` = in-scope total minus with_ancestors). A non-zero
    ``indexed_elsewhere`` is a "may be incomplete" signal - a real one derived
    purely from Neo4j, never from a hand-maintained brand/domain table.

    Choke-point (M4, ADR-0034): both aggregations use the caller's tenant
    boundary without the session pin (``_caller_boundary``) + ``profile_name=name``
    exactly like ``_profile_summary``. Profile membership is
    applied separately: ``$profile_name IN m.profile`` for ``own`` (single-owner
    stamping, ADR-0034) and ``any(profile IN chain)`` for ``with_ancestors``; the
    tenant choke still filters every node, so ancestor modules a tenant cannot
    see are not counted. The caller-can-see check is done up front via
    ``_effective_allowed(name)``. Both queries are flat
    aggregations (ADR-0048 no-VLP) bounded by ``_data_bounded`` (ADR-0050).
    """
    if not name:
        return (
            "Error: profile_inspect(method='coverage') requires name='<profile_name>'."
        )

    # RBAC: caller must be allowed to see this profile at all.
    allowed = srv._effective_allowed(name)
    if allowed is not None and name not in allowed:
        return _profile_not_visible(name, 'coverage')

    effective_limit = min(limit, _PROFILE_MODULES_CAP)
    ancestors = _ancestor_chain_or_self(name)

    with srv._get_driver().session() as neo_session:
        odoo_version = srv._resolve_version(odoo_version, neo_session)

        # (1) per-category count WITHIN this profile.
        in_profile_rows = srv._data_bounded(
            neo_session,
            f"""
            MATCH (m:Module)
            WHERE m.odoo_version = $v
              AND {srv._scope_pred('m')}
              AND $profile_name IN m.profile
            RETURN coalesce(m.category, '(uncategorized)') AS category,
                   count(m) AS cnt
            ORDER BY cnt DESC, category ASC
            """,
            f"coverage (in-profile) for '{name}' (Odoo {odoo_version})",
            v=odoo_version,
            profile_name=name,
            **_caller_boundary(srv),
        )

        # (1b) per-category count over this profile AND its ancestor chain.
        chain_rows = srv._data_bounded(
            neo_session,
            f"""
            MATCH (m:Module)
            WHERE m.odoo_version = $v
              AND {srv._scope_pred('m')}
              AND any(__a IN m.profile WHERE __a IN $chain)
            RETURN coalesce(m.category, '(uncategorized)') AS category,
                   count(m) AS cnt
            ORDER BY cnt DESC, category ASC
            """,
            f"coverage (with ancestors) for '{name}' (Odoo {odoo_version})",
            v=odoo_version,
            chain=ancestors,
            **_caller_boundary(srv),
        )

        # (2) per-category count across the WHOLE in-scope index (any profile).
        scope_rows = srv._data_bounded(
            neo_session,
            f"""
            MATCH (m:Module)
            WHERE m.odoo_version = $v
              AND {srv._scope_pred('m')}
            RETURN coalesce(m.category, '(uncategorized)') AS category,
                   count(m) AS cnt
            ORDER BY cnt DESC, category ASC
            """,
            f"coverage (in-scope total) for '{name}' (Odoo {odoo_version})",
            v=odoo_version,
            **_caller_boundary(srv),
        )

    in_profile = {r["category"]: r["cnt"] for r in in_profile_rows}
    with_chain = {r["category"]: r["cnt"] for r in chain_rows}
    in_scope = {r["category"]: r["cnt"] for r in scope_rows}

    if not with_chain:
        return (
            f"profile_inspect(name={name!r}, method='coverage',"
            f" odoo_version={odoo_version!r})\n"
            "└─ No modules indexed in this profile or its ancestor profiles. Verify the"
            " profile name, or call list_available_profiles to see indexed scope."
        )

    # indexed_elsewhere = in-scope total minus the with-ancestors count: modules of
    # that category visible to the caller that neither this profile nor any of its
    # ancestors carries - clamped at 0 defensively.
    categories = sorted(set(with_chain) | set(in_scope))
    merged: list[tuple[str, int, int, int]] = []
    for cat in categories:
        own = in_profile.get(cat, 0)
        chain = with_chain.get(cat, 0)
        total = in_scope.get(cat, chain)
        merged.append((cat, own, chain, max(total - chain, 0)))

    # Surface the "may be incomplete" signal first: highest indexed_elsewhere,
    # then largest with-ancestors presence, then alphabetical (deterministic).
    merged.sort(key=lambda t: (-t[3], -t[2], t[0]))

    total_categories = len(merged)
    page = merged[start_index:start_index + effective_limit]
    page_end = start_index + len(page)

    lines = [
        f"profile_inspect(name={name!r}, method='coverage',"
        f" odoo_version={odoo_version!r})",
        "├─ Legend: own = modules owned by this profile; with_ancestors = own plus"
        f" modules of its ancestor profiles"
        f" ({_chain_label(ancestors, _visible_profile_names(srv))});"
        " indexed_elsewhere = modules of that category visible to you in neither"
        " (a 'may be missing' signal).",
        f"├─ Indexed module coverage by category (version {odoo_version}):",
    ]
    last_idx = len(page) - 1
    for i, (cat, own, chain, elsewhere) in enumerate(page):
        conn = "│   └─" if (i == last_idx and page_end >= total_categories) else "│   ├─"
        flag = "  [may be incomplete]" if elsewhere > 0 else ""
        lines.append(
            f"{conn} {cat}: own={own}, with_ancestors={chain},"
            f" indexed_elsewhere={elsewhere}{flag}"
        )
    if page_end < total_categories:
        next_start = start_index + effective_limit
        lines.append(
            f"│   └─ ... and {total_categories - page_end} more categories"
            f" (use start_index={next_start} to page)"
        )

    # Caveat (Rec.4 + M1): route explicitly to live-verify. ASCII '!=' (M2), NOT
    # the Unicode not-equal U+2260. This is the load-bearing "absence" message.
    lines.append(
        "├─ NOTE: this reflects what is INDEXED in this profile, not what the"
        " product ships. Absence from this list != absence from the product. To"
        " CONFIRM a domain is absent, cross-check live ir.module.module - the"
        " static index cannot prove product absence."
    )

    footer = srv.hints_for("profile_inspect", name=name, ver=odoo_version)
    if footer:
        lines.append(footer)
    else:
        lines.append(
            f"└─ Next: profile_inspect(name={name!r}, method='modules',"
            f" odoo_version={odoo_version!r}) for the full module list"
        )
    return "\n".join(lines)
