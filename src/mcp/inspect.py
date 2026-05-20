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

_MODEL_METHODS = frozenset({"summary", "fields", "methods", "views", "field", "method"})
_MODULE_METHODS = frozenset({"summary", "fields", "methods", "views", "owl", "qweb", "js"})
_ENTITY_KINDS = frozenset({"model", "field", "method", "view", "module", "pattern"})

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
) -> str:
    """Route to a model-scoped tool by discriminator.

    Parameters
    ----------
    model:
        Dotted model name, e.g. ``sale.order``.
    method:
        One of ``summary``, ``fields``, ``methods``, ``views``, ``field``,
        ``method``.
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
        return srv._resolve_model(model, odoo_version, profile_name)

    if method == "fields":
        return srv._list_fields(
            model=model,
            odoo_version=odoo_version,
            profile_name=profile_name,
            api_key_id=api_key_id,
        )

    if method == "methods":
        return srv._list_methods(
            model=model,
            odoo_version=odoo_version,
            profile_name=profile_name,
            api_key_id=api_key_id,
        )

    if method == "views":
        return srv._list_views(
            model=model,
            odoo_version=odoo_version,
            profile_name=profile_name,
            api_key_id=api_key_id,
        )

    if method == "field":
        if not field:
            return (
                "Error: model_inspect(method='field') requires field='<field_name>'."
            )
        return srv._resolve_field(model, field, odoo_version, profile_name)

    if method == "method":
        if not method_name:
            return (
                "Error: model_inspect(method='method') requires"
                " method_name='<method_name>'."
            )
        return srv._resolve_method(model, method_name, odoo_version, profile_name)

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
            "use list_fields(model=<model>, odoo_version=...) for model-scoped fields, "
            "or describe_module(name='{name}') for counts."
        ).format(name=name)

    if method == "methods":
        # Same limitation as 'fields' — _list_methods requires a model arg.
        return (
            f"module_inspect(name='{name}', method='methods') — "
            "use list_methods(model=<model>, odoo_version=...) for model-scoped methods, "
            "or describe_module(name='{name}') for counts."
        ).format(name=name)

    if method == "views":
        return srv._list_views_by_module(
            module=name,
            odoo_version=odoo_version,
            profile_name=profile_name,
            api_key_id=api_key_id,
        )

    if method == "owl":
        return srv._list_owl_components(
            module=name,
            odoo_version=odoo_version,
            profile_name=profile_name,
            api_key_id=api_key_id,
        )

    if method == "qweb":
        return srv._list_qweb_templates(
            module=name,
            odoo_version=odoo_version,
            profile_name=profile_name,
            api_key_id=api_key_id,
        )

    if method == "js":
        return srv._list_js_patches(
            odoo_version=odoo_version,
            module=name,
            profile_name=profile_name,
            api_key_id=api_key_id,
        )

    # Unreachable — guard for exhaustiveness
    return _invalid_method_error("module_inspect", method, _MODULE_METHODS)  # pragma: no cover


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
) -> str:
    """Unified entity lookup by kind discriminator.

    Parameters
    ----------
    kind:
        Entity type: ``model``, ``field``, ``method``, ``view``, ``module``,
        ``pattern``.
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
        Required for ``kind='view'``. View XML ID.
    name:
        Required for ``kind`` in ``{'module', 'pattern'}``. Technical module
        name or pattern intent string.
    api_key_id:
        Tenant key for ref minting (default: ``'anonymous'``).

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
        return srv._resolve_model(model, odoo_version, profile_name)

    if kind == "field":
        if not model:
            return "Error: entity_lookup(kind='field') requires model='<model_name>'."
        if not field:
            return "Error: entity_lookup(kind='field') requires field='<field_name>'."
        return srv._resolve_field(model, field, odoo_version, profile_name)

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
        return srv._suggest_pattern(name, odoo_version)

    # Unreachable — guard for exhaustiveness
    return _invalid_kind_error(kind)  # pragma: no cover
