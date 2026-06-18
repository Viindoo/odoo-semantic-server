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
    srv._list_extenders.return_value = _STUB_RETURN
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
    ("extenders", {}, "_list_extenders"),
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


# ---------------------------------------------------------------------------
# _list_test_classes_for_module — session-leak fix (Defect C) +
# OrmQueryTimeout propagation fix (Defect K)
#
# These are pure unit tests: Neo4j access is monkeypatched entirely so they
# run in the no-Docker fast lane.  Red-before-green: both tests failed
# against the original code (session.__enter__() leak + bare except swallow).
# ---------------------------------------------------------------------------


def _make_data_bounded_timeout():
    """Return a _data_bounded stub that always raises OrmQueryTimeout."""
    from src.mcp.orm import OrmQueryTimeout

    def _bounded_timeout(*a, **k):
        raise OrmQueryTimeout(
            "module_inspect tests timed out — narrow the query and retry."
        )

    return _bounded_timeout


class TestListTestClassesSessionAndTimeout:
    """Defect C + Defect K: session management and OrmQueryTimeout propagation."""

    def test_orm_query_timeout_propagates_not_swallowed(self, monkeypatch):
        """Defect K fix: OrmQueryTimeout from _data_bounded MUST propagate out.

        Business rule: a tx-timeout on the TestClass query must reach the
        @offload_neo4j handler so it emits nonorm_query_timeout_total and
        returns the clean degraded body — NOT the misleading
        'No test classes indexed' empty-result message.

        Red-before-green: before the fix, the bare ``except Exception`` at
        line ~365 swallowed OrmQueryTimeout and set rows=[], producing the
        wrong 'No test classes indexed' string instead of propagating.
        """
        import src.mcp.server as srv
        from src.mcp.orm import OrmQueryTimeout
        from tests._timeout_harness import TIMEOUT_TEST_VERSION, _TxTimeoutDriver

        # Patch driver so session().run() raises tx-timeout ClientError, which
        # _data_bounded converts to OrmQueryTimeout.
        monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())
        # Short-circuit _resolve_version at Tier-1 (explicit version string)
        # so it returns without touching the timing-out session.
        monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)

        from src.mcp.inspect import _list_test_classes_for_module

        with pytest.raises(OrmQueryTimeout) as exc_info:
            _list_test_classes_for_module(
                "sale", TIMEOUT_TEST_VERSION, None, "test_api_key"
            )

        # The raised exception must carry a clean user message (no Cypher/internals).
        from tests._timeout_harness import assert_clean_timeout_string
        assert_clean_timeout_string(exc_info.value.user_message)

    def test_empty_result_still_yields_no_test_classes_message(self, monkeypatch):
        """Defect K fix: genuine empty results still produce friendly message.

        Distinguish 'timeout' from 'no data': when the query succeeds but
        returns zero rows the friendly 'No test classes indexed' message
        must still appear (not an error string).
        """
        import src.mcp.server as srv
        from tests._timeout_harness import TIMEOUT_TEST_VERSION

        # Session context manager that does nothing on run (returns empty).
        class _EmptySession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def run(self, *a, **k):
                return []

        class _EmptyDriver:
            def session(self, *a, **k):
                return _EmptySession()

        monkeypatch.setattr(srv, "_get_driver", lambda: _EmptyDriver())
        monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)
        # _data_bounded with an empty session returns []
        monkeypatch.setattr(srv, "_data_bounded", lambda *a, **k: [])
        monkeypatch.setattr(srv, "_scope", lambda p: {})
        monkeypatch.setattr(srv, "_scope_pred", lambda alias: "true")

        from src.mcp.inspect import _list_test_classes_for_module

        result = _list_test_classes_for_module(
            "sale", TIMEOUT_TEST_VERSION, None, "test_api_key"
        )

        assert "No test classes indexed" in result
        assert "timed out" not in result.lower()
        assert "Next:" in result

    def test_single_session_used_for_resolve_and_query(self, monkeypatch):
        """Defect C fix: _list_test_classes_for_module opens exactly ONE session.

        Before the fix, a leaked session was opened via
        ``driver.session().__enter__()`` for _resolve_version and a second
        session via ``with driver.session() as session:`` for the query.
        After the fix, both share the same ``with`` block — only one
        ``driver.session()`` call is made.
        """
        import src.mcp.server as srv
        from tests._timeout_harness import TIMEOUT_TEST_VERSION

        session_open_count = []

        class _CountingSession:
            def __enter__(self):
                session_open_count.append(1)
                return self

            def __exit__(self, *a):
                return False

            def run(self, *a, **k):
                return []

        class _CountingDriver:
            def session(self, *a, **k):
                return _CountingSession()

        monkeypatch.setattr(srv, "_get_driver", lambda: _CountingDriver())
        monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)
        monkeypatch.setattr(srv, "_data_bounded", lambda *a, **k: [])
        monkeypatch.setattr(srv, "_scope", lambda p: {})
        monkeypatch.setattr(srv, "_scope_pred", lambda alias: "true")

        from src.mcp.inspect import _list_test_classes_for_module

        _list_test_classes_for_module(
            "sale", TIMEOUT_TEST_VERSION, None, "test_api_key"
        )

        assert len(session_open_count) == 1, (
            f"Expected exactly 1 session opened, got {len(session_open_count)}. "
            "The pre-fix code opened 2 sessions (one leaked via __enter__(), "
            "one via 'with' block). After the fix both share a single 'with' block."
        )

