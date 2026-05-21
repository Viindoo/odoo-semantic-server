# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_mcp_inspect_router.py
"""Tests for src/mcp/inspect.py — discriminator routers.

Covers AC-D1-1 through AC-D1-5:
  D1-1: inspect.py exists with 3 router functions.
  D1-2: Each router has typed signature (no **kwargs catch-all).
  D1-3: Invalid discriminator → "Error:" string listing valid methods.
  D1-4: ≥12 tests across happy-path, invalid-discriminator, missing-arg, unknown-kind.
  D1-5: All existing tests still green (verified by full test run).

These tests are pure unit tests — no Neo4j, no Postgres.
The underlying _impl functions in server.py are patched with trivial stubs.
"""
import inspect
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from src.mcp.inspect import (
    _ENTITY_KINDS,
    _MODEL_METHODS,
    _MODULE_METHODS,
    _entity_lookup,
    _invalid_kind_error,
    _invalid_method_error,
    _model_inspect,
    _module_inspect,
)

# ---------------------------------------------------------------------------
# Helpers — stub server module
# ---------------------------------------------------------------------------

_STUB_RETURN = "stub-output"


def _make_srv_mock() -> MagicMock:
    """Create a MagicMock whose callable attrs all return _STUB_RETURN."""
    srv = MagicMock()
    srv._resolve_model.return_value = _STUB_RETURN
    srv._resolve_field.return_value = _STUB_RETURN
    srv._resolve_method.return_value = _STUB_RETURN
    srv._resolve_view.return_value = _STUB_RETURN
    srv._list_fields.return_value = _STUB_RETURN
    srv._list_methods.return_value = _STUB_RETURN
    srv._list_views.return_value = _STUB_RETURN
    srv._list_owl_components.return_value = _STUB_RETURN
    srv._list_qweb_templates.return_value = _STUB_RETURN
    srv._list_js_patches.return_value = _STUB_RETURN
    srv._describe_module.return_value = _STUB_RETURN
    srv._suggest_pattern.return_value = _STUB_RETURN
    srv._list_views_by_module.return_value = _STUB_RETURN
    return srv


@contextmanager
def _patch_server(srv_mock: MagicMock):
    """Robustly patch src.mcp.server for late-import isolation.

    `from src.mcp import server as srv` inside inspect.py resolves via two
    paths depending on whether server.py is already in sys.modules:

    1. Not yet imported: Python checks sys.modules['src.mcp.server'] → our mock.
    2. Already imported: Python finds src.mcp.__dict__['server'] (package attr)
       and returns that directly, bypassing sys.modules entirely.

    We patch both to be safe: sys.modules entry + package attribute.
    """
    import src.mcp as mcp_pkg

    # Ensure we have a stable reference to the real server (if imported).
    real_server = sys.modules.get("src.mcp.server")
    real_pkg_attr = getattr(mcp_pkg, "server", None)

    # Inject mock into both locations.
    sys.modules["src.mcp.server"] = srv_mock
    mcp_pkg.server = srv_mock
    try:
        yield srv_mock
    finally:
        # Restore sys.modules.
        if real_server is None:
            sys.modules.pop("src.mcp.server", None)
        else:
            sys.modules["src.mcp.server"] = real_server
        # Restore package attribute.
        if real_pkg_attr is None:
            try:
                del mcp_pkg.server
            except AttributeError:
                pass
        else:
            mcp_pkg.server = real_pkg_attr


# ---------------------------------------------------------------------------
# AC-D1-1 / AC-D1-2: module exists + typed signatures
# ---------------------------------------------------------------------------


class TestSignatures:
    """AC-D1-1 + AC-D1-2 — exported functions exist with proper typed sigs."""

    def test_model_inspect_exists_and_callable(self):
        assert callable(_model_inspect)

    def test_module_inspect_exists_and_callable(self):
        assert callable(_module_inspect)

    def test_entity_lookup_exists_and_callable(self):
        assert callable(_entity_lookup)

    def test_model_inspect_no_var_kwargs(self):
        """AC-D1-2: _model_inspect must not use **kwargs."""
        sig = inspect.signature(_model_inspect)
        for param in sig.parameters.values():
            assert param.kind != inspect.Parameter.VAR_KEYWORD, (
                "_model_inspect must not use **kwargs; use explicit named params."
            )

    def test_module_inspect_no_var_kwargs(self):
        """AC-D1-2: _module_inspect must not use **kwargs."""
        sig = inspect.signature(_module_inspect)
        for param in sig.parameters.values():
            assert param.kind != inspect.Parameter.VAR_KEYWORD, (
                "_module_inspect must not use **kwargs; use explicit named params."
            )

    def test_entity_lookup_no_var_kwargs(self):
        """AC-D1-2: _entity_lookup must not use **kwargs."""
        sig = inspect.signature(_entity_lookup)
        for param in sig.parameters.values():
            assert param.kind != inspect.Parameter.VAR_KEYWORD, (
                "_entity_lookup must not use **kwargs; use explicit named params."
            )

    def test_method_name_avoids_python_keyword_clash(self):
        """Plan §5 D5 — discriminator param is 'method', per-entity name is 'method_name'."""
        sig_model = inspect.signature(_model_inspect)
        assert "method" in sig_model.parameters
        assert "method_name" in sig_model.parameters
        sig_entity = inspect.signature(_entity_lookup)
        assert "method_name" in sig_entity.parameters


# ---------------------------------------------------------------------------
# AC-D1-3: Invalid discriminator returns "Error:" string
# ---------------------------------------------------------------------------


class TestInvalidDiscriminator:
    """AC-D1-3 — each router with bad method/kind returns Error: text."""

    def test_model_inspect_invalid_method_returns_error(self):
        result = _model_inspect("sale.order", method="garbage")
        assert result.startswith("Error:"), repr(result)
        assert "garbage" in result

    def test_model_inspect_error_lists_all_valid_methods(self):
        result = _model_inspect("sale.order", method="fnoo")
        for valid in _MODEL_METHODS:
            assert valid in result, f"Expected '{valid}' in error: {result!r}"

    def test_module_inspect_invalid_method_returns_error(self):
        result = _module_inspect("sale", method="garbage")
        assert result.startswith("Error:"), repr(result)
        assert "garbage" in result

    def test_module_inspect_error_lists_all_valid_methods(self):
        result = _module_inspect("sale", method="xyz")
        for valid in _MODULE_METHODS:
            assert valid in result, f"Expected '{valid}' in error: {result!r}"

    def test_entity_lookup_invalid_kind_returns_error(self):
        result = _entity_lookup("widget")
        assert result.startswith("Error:"), repr(result)
        assert "widget" in result

    def test_entity_lookup_error_lists_all_valid_kinds(self):
        result = _entity_lookup("bogus")
        for valid in _ENTITY_KINDS:
            assert valid in result, f"Expected '{valid}' in error: {result!r}"


# ---------------------------------------------------------------------------
# AC-D1-4: Happy-path parametrized — model_inspect
# ---------------------------------------------------------------------------


_MODEL_HAPPY_CASES = [
    ("summary", {}, "_resolve_model"),
    ("fields", {}, "_list_fields"),
    ("methods", {}, "_list_methods"),
    ("views", {}, "_list_views"),
    ("field", {"field": "amount_total"}, "_resolve_field"),
    ("method", {"method_name": "action_confirm"}, "_resolve_method"),
]


@pytest.mark.parametrize("method,extra_kwargs,impl_name", _MODEL_HAPPY_CASES)
def test_model_inspect_happy_path(method, extra_kwargs, impl_name):
    """AC-D1-4 happy-path: each method routes to correct impl and returns str."""
    with _patch_server(_make_srv_mock()):
        result = _model_inspect("sale.order", method=method, **extra_kwargs)

    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert not result.startswith("Error:"), f"Unexpected error: {result!r}"


# ---------------------------------------------------------------------------
# AC-D1-4: Happy-path parametrized — module_inspect
# (D2 landed: 'views' now routes to _list_views_by_module)
# ---------------------------------------------------------------------------


_MODULE_HAPPY_CASES = [
    ("summary", "_describe_module"),
    ("views", "_list_views_by_module"),
    ("owl", "_list_owl_components"),
    ("qweb", "_list_qweb_templates"),
    ("js", "_list_js_patches"),
]


@pytest.mark.parametrize("method,impl_name", _MODULE_HAPPY_CASES)
def test_module_inspect_happy_path(method, impl_name):
    """AC-D1-4 happy-path: each module method returns str (not Error:)."""
    with _patch_server(_make_srv_mock()):
        result = _module_inspect("sale", method=method)

    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert not result.startswith("Error:"), f"Unexpected error: {result!r}"


def test_module_inspect_views_routes_to_list_views_by_module():
    """module_inspect(method='views') routes to _list_views_by_module (D2 landed)."""
    with _patch_server(_make_srv_mock()) as srv_mock:
        result = _module_inspect("sale", method="views")
    assert result == _STUB_RETURN
    srv_mock._list_views_by_module.assert_called_once()


def test_module_inspect_fields_returns_stub_string():
    """module_inspect(method='fields') returns informative stub string (not Error:)."""
    result = _module_inspect("sale", method="fields")
    assert isinstance(result, str)
    assert not result.startswith("Error:")


def test_module_inspect_methods_returns_stub_string():
    """module_inspect(method='methods') returns informative stub string (not Error:)."""
    result = _module_inspect("sale", method="methods")
    assert isinstance(result, str)
    assert not result.startswith("Error:")


# ---------------------------------------------------------------------------
# AC-D1-4: entity_lookup — happy-path + missing-arg errors
# ---------------------------------------------------------------------------


_ENTITY_HAPPY_CASES = [
    ("model", {"model": "sale.order"}, "_resolve_model"),
    ("field", {"model": "sale.order", "field": "amount_total"}, "_resolve_field"),
    ("method", {"model": "sale.order", "method_name": "action_confirm"}, "_resolve_method"),
    ("view", {"xmlid": "sale.view_order_form"}, "_resolve_view"),
    ("module", {"name": "sale"}, "_describe_module"),
    ("pattern", {"name": "compute field pattern"}, "_suggest_pattern"),
]


@pytest.mark.parametrize("kind,kwargs,impl_name", _ENTITY_HAPPY_CASES)
def test_entity_lookup_happy_path(kind, kwargs, impl_name):
    """AC-D1-4 happy-path: each kind routes correctly and returns str."""
    with _patch_server(_make_srv_mock()):
        result = _entity_lookup(kind, **kwargs)

    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert not result.startswith("Error:"), f"Unexpected error: {result!r}"


def test_entity_lookup_field_missing_field_arg():
    """AC-D1-4: kind='field' without field= → Error: message."""
    result = _entity_lookup("field", model="sale.order")
    assert result.startswith("Error:")
    assert "field" in result.lower()


def test_entity_lookup_field_missing_model_arg():
    """AC-D1-4: kind='field' without model= → Error: message."""
    result = _entity_lookup("field", field="amount_total")
    assert result.startswith("Error:")
    assert "model" in result.lower()


def test_entity_lookup_method_missing_method_name_arg():
    """AC-D1-4: kind='method' without method_name= → Error: message."""
    result = _entity_lookup("method", model="sale.order")
    assert result.startswith("Error:")
    assert "method_name" in result.lower()


def test_entity_lookup_unknown_kind():
    """AC-D1-4: unknown kind returns Error: listing valid kinds."""
    result = _entity_lookup("unknown_kind_xyz")
    assert result.startswith("Error:")
    for valid in _ENTITY_KINDS:
        assert valid in result


def test_entity_lookup_view_missing_xmlid():
    """AC-D1-4: kind='view' without xmlid= → Error: message."""
    result = _entity_lookup("view")
    assert result.startswith("Error:")
    assert "xmlid" in result.lower()


def test_entity_lookup_module_missing_name():
    """AC-D1-4: kind='module' without name= → Error: message."""
    result = _entity_lookup("module")
    assert result.startswith("Error:")
    assert "name" in result.lower()


def test_entity_lookup_pattern_missing_name():
    """AC-D1-4: kind='pattern' without name= → Error: message."""
    result = _entity_lookup("pattern")
    assert result.startswith("Error:")
    assert "name" in result.lower()


# ---------------------------------------------------------------------------
# AC-D1-3 edge: _invalid_method_error and _invalid_kind_error helpers
# ---------------------------------------------------------------------------


def test_invalid_method_error_format():
    valid = frozenset({"a", "b", "c"})
    msg = _invalid_method_error("test_router", "bad", valid)
    assert msg.startswith("Error: unknown method 'bad'.")
    assert "a" in msg
    assert "b" in msg
    assert "c" in msg


def test_invalid_kind_error_format():
    msg = _invalid_kind_error("bad_kind")
    assert msg.startswith("Error: unknown kind 'bad_kind'.")
    for k in _ENTITY_KINDS:
        assert k in msg


def test_model_inspect_missing_field_arg():
    """model_inspect(method='field') without field= → Error: message."""
    result = _model_inspect("sale.order", method="field")
    assert result.startswith("Error:")
    assert "field" in result.lower()


def test_model_inspect_missing_method_name_arg():
    """model_inspect(method='method') without method_name= → Error: message."""
    result = _model_inspect("sale.order", method="method")
    assert result.startswith("Error:")
    assert "method_name" in result.lower()


# ---------------------------------------------------------------------------
# Filter-parity forwarding tests (v0.7.1)
# ---------------------------------------------------------------------------


def test_model_inspect_fields_forwards_kind():
    """model_inspect(method='fields', kind='many2one') forwards kind= to _list_fields."""
    with _patch_server(_make_srv_mock()) as srv_mock:
        result = _model_inspect("sale.order", method="fields", kind="many2one")
    assert result == _STUB_RETURN
    srv_mock._list_fields.assert_called_once()
    assert srv_mock._list_fields.call_args.kwargs.get("kind") == "many2one"


def test_model_inspect_views_forwards_view_type():
    """model_inspect(method='views', view_type='form') forwards view_type= to _list_views."""
    with _patch_server(_make_srv_mock()) as srv_mock:
        result = _model_inspect("sale.order", method="views", view_type="form")
    assert result == _STUB_RETURN
    srv_mock._list_views.assert_called_once()
    assert srv_mock._list_views.call_args.kwargs.get("view_type") == "form"


def test_module_inspect_views_forwards_view_type():
    """module_inspect views+view_type forwards view_type= to _list_views_by_module."""
    with _patch_server(_make_srv_mock()) as srv_mock:
        result = _module_inspect("sale", method="views", view_type="tree")
    assert result == _STUB_RETURN
    srv_mock._list_views_by_module.assert_called_once()
    assert srv_mock._list_views_by_module.call_args.kwargs.get("view_type") == "tree"


def test_module_inspect_owl_forwards_bound_model():
    """module_inspect owl+bound_model forwards bound_model= to _list_owl_components."""
    with _patch_server(_make_srv_mock()) as srv_mock:
        result = _module_inspect("sale", method="owl", bound_model="sale.order")
    assert result == _STUB_RETURN
    srv_mock._list_owl_components.assert_called_once()
    assert srv_mock._list_owl_components.call_args.kwargs.get("bound_model") == "sale.order"


def test_module_inspect_js_forwards_era():
    """module_inspect(method='js', era='era3') forwards era= to _list_js_patches."""
    with _patch_server(_make_srv_mock()) as srv_mock:
        result = _module_inspect("sale", method="js", era="era3")
    assert result == _STUB_RETURN
    srv_mock._list_js_patches.assert_called_once()
    assert srv_mock._list_js_patches.call_args.kwargs.get("era") == "era3"


def test_module_inspect_js_forwards_target():
    """module_inspect(method='js', target='ListController') forwards target= to _list_js_patches."""
    with _patch_server(_make_srv_mock()) as srv_mock:
        result = _module_inspect("sale", method="js", target="ListController")
    assert result == _STUB_RETURN
    srv_mock._list_js_patches.assert_called_once()
    assert srv_mock._list_js_patches.call_args.kwargs.get("target") == "ListController"
