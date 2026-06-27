# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/inspect.py
"""Discriminator-router layer for model_inspect, module_inspect, entity_lookup.

All three functions use late imports of src.mcp.server._X to avoid circular
deps (server.py will import from inspect.py via WI-D3).

See docs/adr/0028-discriminator-consolidation.md.
"""

# ---------------------------------------------------------------------------
# Discriminator constants
# ---------------------------------------------------------------------------

_MODEL_METHODS = frozenset({
    "summary", "fields", "methods", "views", "field", "method", "extenders",
})
_MODULE_METHODS = frozenset({"summary", "fields", "methods", "views", "owl", "qweb", "js",
                              "dependencies", "tests"})
_ENTITY_KINDS = frozenset({"model", "field", "method", "view", "module", "pattern", "report"})
_PROFILE_METHODS = frozenset({"summary", "repos", "modules"})

# H1 (#260): hard server-side cap for profile_inspect(method='modules').
# The docstring discloses "default 50, max 50"; the cap MUST be enforced so a
# caller-supplied limit cannot exceed it (ADR-0023 §3 — "caps never raised").
# Mirrors the min(limit, cap) clamp every _list_* path in server.py applies.
_PROFILE_MODULES_CAP = 50

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
        # OrmQueryTimeout is intentionally NOT caught here — it propagates to the
        # @offload_neo4j-decorated module_inspect handler, which records the
        # nonorm_query_timeout_total metric and returns the clean degraded string.
        # Only genuine driver/unexpected errors fall back to rows=[] (graceful
        # degradation so a one-off DB hiccup doesn't abort the whole tool output).
        try:
            rows = srv._data_bounded(
                session,
                f"""
                MATCH (tc:TestClass {{module: $module, odoo_version: $v}})
                WHERE {srv._scope_pred("tc")}
                RETURN
                    tc.name          AS name,
                    tc.file_path     AS file_path,
                    tc.test_type     AS test_type,
                    tc.is_helper     AS is_helper,
                    tc.commit_allowed AS commit_allowed,
                    size([x IN tc.profile WHERE x = x]) AS profile_count
                ORDER BY tc.file_path ASC, tc.name ASC
                LIMIT 200
                """,
                f"module_inspect tests ({module})",
                module=module,
                v=v,
                **srv._scope(profile_name),
            )
        except OrmQueryTimeout:
            raise
        except Exception:
            rows = []

    header = f"module_inspect(name='{module}', method='tests', odoo_version='{v}')"
    lines = [header]

    if is_test_integration:
        lines.append("├─ is_test_integration_module: True (module name starts with test_)")

    if not rows:
        lines.append(f"├─ No test classes indexed for [{module}] at Odoo {v}.")
    else:
        lines.append(f"├─ Test classes: {len(rows)}")
        for i, r in enumerate(rows[:10]):
            conn = "└─" if i == len(rows) - 1 else "├─"
            cls_name = r.get("name") or "?"
            fp = r.get("file_path") or ""
            tt = r.get("test_type") or "?"
            helper_tag = " [helper]" if r.get("is_helper") else ""
            lines.append(f"│  {conn} {cls_name}{helper_tag}  [{tt}]  {fp}")

        if len(rows) > 10:
            lines.append(f"│  ... +{len(rows) - 10} more test classes")

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
        Required for ``method='summary'``. Optional for ``method='repos'``
        and ``method='modules'`` (``None`` = all caller-visible profiles).
    method:
        ``summary`` | ``repos`` | ``modules``.
    odoo_version:
        Odoo version string. ``'auto'`` resolves to the session pin.
    repo:
        Filter modules/repos by repo URL substring. Applied only for
        ``method='modules'`` and ``method='repos'``.
    api_key_id:
        Tenant key for RBAC (default: ``'anonymous'``).
    start_index:
        Pagination cursor for ``method='modules'`` (zero-based SKIP).
    limit:
        Max rows per page for ``method='modules'`` (default 50).

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

    # Unreachable
    return _invalid_method_error("profile_inspect", method, _PROFILE_METHODS)  # pragma: no cover


# ---------------------------------------------------------------------------
# profile_inspect helpers
# ---------------------------------------------------------------------------

def _profile_summary(name: str, odoo_version: str, srv) -> str:
    """Render profile summary: ancestor chain, children, repos, module_count."""
    from src.db.pg import repo_store

    # RBAC: check caller can see this profile at all.
    allowed = srv._effective_allowed(name)
    if allowed is not None and name not in allowed:
        return (
            f"profile_inspect(name={name!r}, method='summary')\n"
            f"└─ Not found or not authorized: profile '{name}' is not visible to this key.\n"
            "   Use list_available_profiles() to see accessible profiles."
        )

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
            # Use _scope(None) for the own+shared tenant boundary, FURTHER
            # narrowed by any active session pin (ADR-0029 #251): _scope(None)
            # injects the pinned profile via _resolve_profile(None) before the
            # ADR-0034 narrowing, so a pinned session sees only the pinned
            # profile here. Narrowing-only / fail-closed — the pin can never
            # widen beyond own∪shared. We then filter by profile_name IN
            # m.profile separately. _scope(name) would narrow own=[name] which
            # breaks for modules stamped with the full ancestor chain (e.g.
            # [child, parent_shared]). The caller-can-see-this-profile check is
            # already done above via _effective_allowed(name).
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
                **srv._scope(None),
            )
            module_count = rec["cnt"] if rec else 0
    except Exception:
        module_count = None  # graceful degradation if Neo4j unavailable
        # L1 fix: if odoo_version was not resolved before the exception
        # (e.g. driver down before line 536), normalize the sentinel so the
        # header never renders 'auto' literally.
        from src.mcp import session as _sess
        _normalized = _sess.normalize_version_arg(odoo_version)
        if _normalized is None:
            odoo_version = "(unresolved)"

    # Build tree output.
    lines = [f"profile_inspect(name={name!r}, method='summary', odoo_version={odoo_version!r})"]

    # Ancestor chain.
    if len(ancestors) == 1:
        lines.append(f"├─ Ancestor chain: {name} (root, no parent)")
    else:
        chain_str = " -> ".join(ancestors)
        lines.append(f"├─ Ancestor chain: {chain_str}")

    # Children.
    if children:
        lines.append(f"├─ Children ({len(children)}): {', '.join(children)}")
    else:
        lines.append("├─ Children: none")

    # Repos.
    lines.append(f"├─ Repos ({len(unique_repos)} unique across ancestor chain):")
    for i, r in enumerate(unique_repos):
        prefix = "│  └─" if i == len(unique_repos) - 1 else "│  ├─"
        depth_tag = " [own]" if r["depth"] == 0 else f" [inherited from {r['profile_name']}]"
        status = r.get("status", "unknown")
        lines.append(f"{prefix} {r['url']} @ {r['branch']}{depth_tag}  status:{status}")

    # Module count.
    # M3 (#259/#260): the count uses `$profile_name IN m.profile`. Module nodes
    # are stamped with the FULL ancestor chain (ADR-0016), so querying a parent
    # profile matches every descendant-profile module that inherits it. That is
    # the inheritance-RESOLVED semantics #259/#260 ask for (the same scope a
    # check_module_exists answer reflects), so the count is intentionally
    # inheritance-inclusive — the label says so to avoid the reader mistaking it
    # for an own-profile-only tally.
    if module_count is not None:
        lines.append(
            f"└─ Module count (version {odoo_version}, inheritance-inclusive):"
            f" {module_count}"
        )
    else:
        lines.append("└─ Module count: unavailable")

    footer = srv.hints_for("profile_inspect", name=name, ver=odoo_version)
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
            return (
                f"profile_inspect(name={name!r}, method='repos')\n"
                f"└─ Not found or not authorized: profile '{name}' is not visible to this key.\n"
                "   Use list_available_profiles() to see accessible profiles."
            )
        repos_raw = repo_store().get_ancestor_repos(name)
    else:
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
    if not unique_repos:
        lines.append("└─ No repos found.")
        return "\n".join(lines)

    lines.append(f"├─ Repos ({len(unique_repos)} unique):")
    for i, r in enumerate(unique_repos):
        prefix = "│  └─" if i == len(unique_repos) - 1 else "│  ├─"
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
        return (
            f"profile_inspect(name={name!r}, method='modules')\n"
            f"└─ Not found or not authorized: profile '{name}' is not visible to this key.\n"
            "   Use list_available_profiles() to see accessible profiles."
        )

    # H1 (#260): enforce the disclosed cap — a large caller limit must not
    # return more than _PROFILE_MODULES_CAP rows (ADR-0023 §3).
    effective_limit = min(limit, _PROFILE_MODULES_CAP)

    with srv._get_driver().session() as neo_session:
        odoo_version = srv._resolve_version(odoo_version, neo_session)

        # Build the WHERE clause for optional profile + repo filters.
        profile_clause = "AND $profile_name IN m.profile" if name else ""
        repo_clause = "AND m.repo_url CONTAINS $repo_filter" if repo_filter else ""

        # Use _scope(None) for the own+shared tenant boundary, FURTHER narrowed
        # by any active session pin (ADR-0029 #251): _scope(None) injects the
        # pinned profile via _resolve_profile(None) before the ADR-0034
        # narrowing, so a pinned session (name=None caller) sees only the pinned
        # profile here. Narrowing-only / fail-closed — the pin can never widen
        # beyond own∪shared, so this never leaks across tenants.
        # Profile-specific filtering is applied separately via profile_clause
        # ($profile_name IN m.profile). This avoids the all(...) predicate
        # mismatch when modules carry the full ancestor chain in their profile[]
        # (e.g. [child_profile, parent_shared]) — narrowing own=[name] would
        # cause the predicate to deny modules that have a parent profile not in own.
        # The caller-can-see-this-profile check is already done via
        # _effective_allowed(name) above.
        scope_params = srv._scope(None)

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
        prefix = "│  └─" if i == len(rows) - 1 else "│  ├─"
        edition = r.get("edition") or "community"
        repo_tag = f"  [{r['repo']}]" if r.get("repo") else ""
        lines.append(f"{prefix} {r['name']}  ({edition}){repo_tag}")

    if page_end < total:
        next_start = start_index + effective_limit
        more_hint = (
            f"profile_inspect(name={name!r}, method='modules',"
            f" odoo_version={odoo_version!r}, start_index={next_start})"
        )
        lines.append(f"└─ ... and {total - page_end} more (use {more_hint})")
    else:
        lines.append(f"└─ End of list ({total} total).")

    footer = srv.hints_for("profile_inspect", name=name or "", ver=odoo_version)
    if footer:
        lines.append(footer)
    return "\n".join(lines)
