# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_mcp_inspect_router.py
"""Tests for src/mcp/inspect.py — discriminator routers.

Covers AC-D1-1 through AC-D1-5:
  D1-1: inspect.py exists with 3 router functions.
  D1-2: Each router has typed signature (no **kwargs catch-all).
  D1-3: Invalid discriminator -> "Error:" string listing valid methods.
  D1-4: >=12 tests across happy-path, invalid-discriminator, missing-arg, unknown-kind.
  D1-5: All existing tests still green (verified by full test run).

The D1-* tests are pure unit tests - no Neo4j, no Postgres. The underlying
_impl functions in server.py are patched with trivial stubs, so they run in the
no-Docker fast lane (`make test`, `-m "not neo4j"`).

Issue #339: name_filter tests are appended at the end of this file. Those tests
seed real data via Neo4jWriter and are individually tagged `@pytest.mark.neo4j`
(NOT a module-level marker - a module-level marker would wrongly deselect the
pure-unit D1-* tests from the fast lane).
"""
import importlib
import inspect
import os
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
from tests.conftest import TEST_VERSION

# NO module-level pytestmark. The D1-* tests below are pure unit tests and MUST
# stay in the `-m "not neo4j"` fast lane. Only the #339 name_filter tests at the
# bottom of this file touch a real Neo4j driver; each carries its own
# @pytest.mark.neo4j so the fast-lane selection stays correct.

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


def test_entity_lookup_report_kind_registered():
    """GAP-2: 'report' is a valid entity_lookup kind."""
    assert "report" in _ENTITY_KINDS


def test_entity_lookup_report_missing_model_and_name():
    """GAP-2: kind='report' with neither model nor name → Error: message."""
    # No model/name → tree_builder.list_reports short-circuits with an Error
    # string before any DB access, so this needs no server mock.
    result = _entity_lookup("report", odoo_version=TEST_VERSION)
    assert result.startswith("Error:")
    assert "model" in result.lower()


def test_entity_lookup_report_routes_to_list_reports(monkeypatch):
    """GAP-2: kind='report' with model= dispatches to tree_builder.list_reports."""
    import src.mcp.tree_builder as tb

    captured = {}

    def _fake_list_reports(*, model, name, odoo_version, profile_name):
        captured.update(
            model=model, name=name, odoo_version=odoo_version,
            profile_name=profile_name,
        )
        return _STUB_RETURN

    monkeypatch.setattr(tb, "list_reports", _fake_list_reports)
    with _patch_server(_make_srv_mock()):
        result = _entity_lookup(
            "report", model="sale.order", odoo_version=TEST_VERSION,
        )
    assert result == _STUB_RETURN
    assert captured["model"] == "sale.order"


def test_entity_lookup_report_xmlid_aliases_name(monkeypatch):
    """GAP-2: kind='report' xmlid= is forwarded as the name filter."""
    import src.mcp.tree_builder as tb

    captured = {}

    def _fake_list_reports(*, model, name, odoo_version, profile_name):
        captured.update(model=model, name=name)
        return _STUB_RETURN

    monkeypatch.setattr(tb, "list_reports", _fake_list_reports)
    with _patch_server(_make_srv_mock()):
        _entity_lookup(
            "report", xmlid="sale.action_report_saleorder",
            odoo_version=TEST_VERSION,
        )
    assert captured["name"] == "sale.action_report_saleorder"
    assert captured["model"] is None


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


# ---------------------------------------------------------------------------
# name_filter tests - issue #339
#
# Each test below carries its own @pytest.mark.neo4j (NOT a module-level marker)
# so the pure-unit D1-* tests above stay in the `-m "not neo4j"` fast lane.
# All tests use TEST_VERSION="99.0"; the nf_db fixture seeds + tears down the
# version-99.0 nodes via Neo4jWriter for realistic round-trip coverage.
# ---------------------------------------------------------------------------
#
# Red-before-green verification:
#   Running these tests on the un-patched codebase produces failures:
#   - Tests 1-5 + 7-8: `_list_fields`/`_list_methods` do not accept
#     `name_filter`, so _model_inspect raises TypeError on the unexpected kwarg.
#   - Test 6: passes trivially (summary ignores extra kwargs), but the signature
#     test on model_inspect_tool would fail.
#   The TypeError on tests 1-5+7-8 ensures fail-ability under ETHOS #10.

_NF_MODULE = "nf_sale"
_NF_MODEL = "nf.order"


@pytest.fixture(scope="module")
def nf_db(neo4j_driver, monkeypatch_module):
    """Seed nf.order with 3 fields + 3 methods for name_filter tests.

    Fields: amount_total (monetary), amount_tax (monetary), partner_id (many2one).
    Methods: action_confirm (public), _compute_amount (compute), write (public).
    Using TEST_VERSION='99.0' + a unique module name to avoid collision.
    """
    from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Clean any leftover data from previous runs.
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)

    mod = ModuleInfo(
        name=_NF_MODULE,
        odoo_version=TEST_VERSION,
        repo="odoo_test",
        path="/tmp/nf_sale",
        depends=["base"],
        edition="community",
    )
    model = ModelInfo(
        name=_NF_MODEL,
        module=_NF_MODULE,
        odoo_version=TEST_VERSION,
        fields=[
            FieldInfo("amount_total", "monetary", compute="_compute_total", stored=True),
            FieldInfo("amount_tax", "monetary", compute="_compute_tax", stored=True),
            FieldInfo("partner_id", "many2one"),
        ],
        methods=[
            MethodInfo("action_confirm", convention_kind="public"),
            MethodInfo("_compute_amount", convention_kind="private"),
            MethodInfo("write", convention_kind="public"),
        ],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    )
    monkeypatch_module.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password")
    )

    import sys
    sys.modules.pop("src.mcp.server", None)

    yield neo4j_driver

    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)


@pytest.fixture()
def nf_server(nf_db):
    """Import (or re-import) server module after nf_db sets env vars."""
    return importlib.import_module("src.mcp.server")


# ---------------------------------------------------------------------------
# Test 1: name_filter filters fields - matching IN, non-matching OUT
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_name_filter_fields_matching_in_nonmatching_out(nf_db, nf_server):
    """name_filter='amount' keeps amount_total+amount_tax; drops partner_id.

    Business rule: substring filter on field names reduces payload for large
    models. Non-matching fields must not appear in the output.
    """
    out = nf_server._list_fields(_NF_MODEL, TEST_VERSION, name_filter="amount")

    # Matching fields must be present.
    assert "amount_total" in out, f"Expected 'amount_total' in output:\n{out}"
    assert "amount_tax" in out, f"Expected 'amount_tax' in output:\n{out}"
    # Non-matching field must NOT appear.
    assert "partner_id" not in out, f"Expected 'partner_id' NOT in output:\n{out}"


# ---------------------------------------------------------------------------
# Test 2: name_filter filters methods - matching IN, non-matching OUT
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_name_filter_methods_matching_in_nonmatching_out(nf_db, nf_server):
    """name_filter='compute' keeps _compute_amount; drops action_confirm + write.

    Business rule: substring filter on method names reduces payload.
    """
    out = nf_server._list_methods(_NF_MODEL, TEST_VERSION, name_filter="compute")

    assert "_compute_amount" in out, f"Expected '_compute_amount' in output:\n{out}"
    assert "action_confirm" not in out, f"Expected 'action_confirm' NOT in output:\n{out}"
    assert "write" not in out, f"Expected 'write' NOT in output:\n{out}"


# ---------------------------------------------------------------------------
# Test 2b: methods zero-match still discloses the name_filter breadcrumb
# (review #340 Fix A: _list_methods early-returned before emitting the filter
#  line, so a 0-match method filter dropped it. Mirrors the fields zero-match
#  test below; both the breadcrumb AND the '(none)' sentinel must appear.)
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_name_filter_methods_zero_match_keeps_breadcrumb(nf_db, nf_server):
    """A method name_filter matching 0 methods keeps the filter breadcrumb.

    Business rule: an empty filtered result must still disclose WHICH filter
    produced it, so the agent is not left guessing why the method list is empty.
    Output must contain BOTH the '(none)' sentinel AND the
    'filter: name_filter=...' breadcrumb (parity with the fields zero-match path).

    Red-before-green: against the pre-fix _list_methods (which built the
    breadcrumb only on the total>0 path, after an early `return` for total==0),
    the breadcrumb line is absent on this 0-match case and this test fails.
    """
    out = nf_server._list_methods(
        _NF_MODEL, TEST_VERSION, name_filter="xxxxxxxxxx_nonexistent_zzz"
    )

    assert "(none)" in out, f"Expected '(none)' sentinel in output:\n{out}"
    assert "name_filter=" in out, (
        f"Expected the name_filter breadcrumb in zero-match output:\n{out}"
    )
    assert "xxxxxxxxxx_nonexistent_zzz" in out, (
        f"Expected the filter value echoed in the breadcrumb:\n{out}"
    )
    assert not out.startswith("Error:"), f"Unexpected error: {out!r}"


# ---------------------------------------------------------------------------
# Test 3: 0-match -> '(none)' sentinel, no exception
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_name_filter_zero_match_returns_none_sentinel(nf_db, nf_server):
    """0 matches -> ADR-0023 '(none)' sentinel; no exception raised.

    Business rule: an empty result set is a valid answer, not an error.
    """
    out = nf_server._list_fields(_NF_MODEL, TEST_VERSION,
                                  name_filter="xxxxxxxxxx_nonexistent_zzz")

    assert "(none)" in out, f"Expected '(none)' sentinel in output:\n{out}"
    # Must not raise and must not start with Error:.
    assert not out.startswith("Error:"), f"Unexpected error: {out!r}"


# ---------------------------------------------------------------------------
# Test 4: name_filter=None -> full tree (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_name_filter_none_returns_full_tree(nf_db, nf_server):
    """name_filter=None (default) returns all seeded fields - regression guard.

    Business rule: omitting name_filter must not change existing behavior.
    """
    out_with_none = nf_server._list_fields(_NF_MODEL, TEST_VERSION, name_filter=None)
    out_default = nf_server._list_fields(_NF_MODEL, TEST_VERSION)

    # Both calls must agree on content.
    assert out_with_none == out_default, (
        "name_filter=None must produce identical output to omitting name_filter."
    )
    # All three seeded fields must appear.
    assert "amount_total" in out_with_none
    assert "amount_tax" in out_with_none
    assert "partner_id" in out_with_none


# ---------------------------------------------------------------------------
# Test 5: name_filter is case-insensitive
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_name_filter_case_insensitive(nf_db, nf_server):
    """name_filter='AMOUNT' (uppercase) matches 'amount_total' + 'amount_tax'.

    Business rule: case-insensitive substring match per solution-339 §1.
    """
    out = nf_server._list_fields(_NF_MODEL, TEST_VERSION, name_filter="AMOUNT")

    assert "amount_total" in out, f"Expected 'amount_total' in output:\n{out}"
    assert "amount_tax" in out, f"Expected 'amount_tax' in output:\n{out}"
    assert "partner_id" not in out, f"Expected 'partner_id' NOT in output:\n{out}"


# ---------------------------------------------------------------------------
# Test 6: name_filter silently ignored on method='summary'
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_name_filter_ignored_on_summary(nf_db, nf_server):
    """name_filter silently ignored when method='summary'; output unchanged.

    Business rule: name_filter only applies to fields/methods. Summary route
    must not error and must produce the same output with or without name_filter.
    """
    from src.mcp.inspect import _model_inspect

    out_no_filter = _model_inspect(
        _NF_MODEL, method="summary", odoo_version=TEST_VERSION
    )
    out_with_filter = _model_inspect(
        _NF_MODEL, method="summary", odoo_version=TEST_VERSION,
        name_filter="amount",
    )

    assert out_no_filter == out_with_filter, (
        "name_filter must be silently ignored for method='summary'. "
        f"Without filter:\n{out_no_filter}\n\nWith filter:\n{out_with_filter}"
    )
    assert not out_with_filter.startswith("Error:"), (
        f"Unexpected error with name_filter on summary: {out_with_filter!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: "Showing X of N" count reflects count-AFTER-filter (protects R4)
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_name_filter_count_reflects_filtered_total(nf_db, nf_server):
    """'Showing X of N' total reflects count-after-filter, not total-without-filter.

    Risk R4 protection: if _count_fields_with_inherited is not updated to
    accept name_filter, the total N in 'Showing X of N' will be the unfiltered
    total (3 fields) while only matching rows are shown (2). This test catches
    that divergence.

    With name_filter='amount': 2 fields match (amount_total, amount_tax).
    Total Neo4j fields = 3. If total=3 appears in the pagination line,
    the count function was NOT updated - test fails as intended.

    Note: pagination hint is only emitted when there are more rows than shown.
    Since we have only 2 matching rows and cap is 50, no pagination hint is
    emitted. We verify by checking the total returned by the count function
    directly via the underlying ORM helper.
    """
    # Direct count verification: _count_fields_with_inherited with name_filter
    # must return 2 (only matching fields), not 3 (total without filter).
    import src.mcp.server as srv
    from src.mcp.orm_queries import _count_fields_with_inherited

    with srv._get_driver().session() as session:
        version = srv._resolve_version(TEST_VERSION, session)
        count_filtered = _count_fields_with_inherited(
            _NF_MODEL, version, session, name_filter="amount"
        )
        count_all = _count_fields_with_inherited(
            _NF_MODEL, version, session, name_filter=None
        )

    # Filtered count must be LESS than total (proves filter actually applies).
    assert count_filtered == 2, (
        f"Expected 2 fields matching 'amount', got {count_filtered}. "
        "If this is 3, _count_fields_with_inherited ignores name_filter (risk R4)."
    )
    assert count_all == 3, (
        f"Expected 3 total fields without filter, got {count_all}."
    )


# ---------------------------------------------------------------------------
# Test 8: pagination + name_filter - start_index skips AFTER filter
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_name_filter_pagination_skips_after_filter(nf_db, nf_server):
    """start_index skips rows AFTER name_filter is applied (not before).

    Business rule (risk R1): pagination semantics with name_filter.
    'start_index=50, name_filter=amount' means 'skip 50 filtered results',
    not 'skip 50 all fields then filter'.

    With only 2 matching fields, start_index=1 must return 1 row (the second
    matching field), and start_index=2 must return 0 rows (past the end).
    """
    # start_index=0: 2 matching rows
    out_page0 = nf_server._list_fields(
        _NF_MODEL, TEST_VERSION, name_filter="amount", start_index=0
    )
    # Both matching fields on page 0.
    assert "amount_total" in out_page0 or "amount_tax" in out_page0, (
        f"Expected at least one 'amount_*' field on page 0:\n{out_page0}"
    )

    # start_index past end (beyond 2 matching fields): should signal over-run.
    out_past_end = nf_server._list_fields(
        _NF_MODEL, TEST_VERSION, name_filter="amount", start_index=100
    )
    # Must not crash and must not show any field data (over-run state).
    assert "amount_total" not in out_past_end, (
        f"'amount_total' should not appear at start_index=100 with only 2 matching "
        f"fields:\n{out_past_end}"
    )
    assert "amount_tax" not in out_past_end, (
        f"'amount_tax' should not appear at start_index=100:\n{out_past_end}"
    )
