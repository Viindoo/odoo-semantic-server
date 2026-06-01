# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the model_inspect / entity_lookup superset tools (M12 v0.6 — shims removed).

In v0.5 the 10 deprecated shims (resolve_model, resolve_field, resolve_method,
resolve_view, list_fields, list_methods, list_views, list_owl_components,
list_qweb_templates, list_js_patches) exposed a dual-mode target= dispatch and
carried legacy-kwarg DeprecationWarnings.  All 10 were removed in v0.6 (M12
W-S1).  This file was updated (M12 W-S2) to remove the now-stale AC-C3 shim
contract tests and replace them with equivalent coverage against the superset
tools.

Remaining coverage:
  C3-2b: model_inspect('summary') returns the same content as the underlying
          _resolve_model implementation.
  C3-2c: model_inspect('field', ...) returns the same content as _resolve_field.
  C3-3b: model_inspect / entity_lookup with an invalid discriminator returns a
          friendly error string (no exception raised).
  Signature: model_inspect / entity_lookup have the correct parameters.

DB version: TEST_VERSION = "91.0" (unchanged — same Neo4j namespace as before).
"""
import asyncio
import importlib
import inspect
import os

import pytest

# ---------------------------------------------------------------------------
# DB version (must not collide with other test fixtures)
# ---------------------------------------------------------------------------
TEST_VERSION = "91.0"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dual_db(neo4j_driver, monkeypatch_module):
    """Seed minimal Neo4j data for dual-mode round-trip tests."""
    from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Wipe any leftover data at this version.
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)

    mod = ModuleInfo(
        name="c3_sale",
        odoo_version=TEST_VERSION,
        repo="odoo_test",
        path="/tmp/c3_sale",
        depends=["base"],
        edition="community",
    )
    model = ModelInfo(
        name="c3.order",
        module="c3_sale",
        odoo_version=TEST_VERSION,
        fields=[
            FieldInfo("amount_total", "monetary", compute="_compute_total", stored=True),
        ],
        methods=[
            MethodInfo("action_confirm", has_super_call=False),
        ],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    # Patch env vars so server.py can connect to the test Neo4j.
    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    )
    monkeypatch_module.setenv(
        "NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j")
    )
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password")
    )

    import sys
    sys.modules.pop("src.mcp.server", None)

    yield

    # Teardown
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)


# ---------------------------------------------------------------------------
# C3-2b: model_inspect('summary') matches _resolve_model
# ---------------------------------------------------------------------------


@pytest.mark.neo4j
def test_model_inspect_summary_matches_resolve_model_impl(dual_db):
    """model_inspect(model, 'summary') returns the same text as _resolve_model.

    Verifies that the superset tool routes to the same underlying implementation
    as the now-removed resolve_model shim did.
    """
    server = importlib.import_module("src.mcp.server")

    direct = server._resolve_model("c3.order", TEST_VERSION)
    via_superset = asyncio.run(server.model_inspect.fn(
        model="c3.order", method="summary", odoo_version=TEST_VERSION
    ))

    superset_text = via_superset.content[0].text
    assert "c3.order" in superset_text, f"Expected model name in output: {superset_text!r}"
    assert superset_text == direct, (
        f"model_inspect(summary) must match _resolve_model.\n"
        f"superset: {superset_text[:200]!r}\n"
        f"direct:   {direct[:200]!r}"
    )


# ---------------------------------------------------------------------------
# C3-2c: model_inspect('field', ...) matches _resolve_field
# ---------------------------------------------------------------------------


@pytest.mark.neo4j
def test_model_inspect_field_matches_resolve_field_impl(dual_db):
    """model_inspect(model, 'field', field='amount_total') returns same as _resolve_field.

    Verifies that the superset tool routes to the correct field resolver.
    """
    server = importlib.import_module("src.mcp.server")

    direct = server._resolve_field("c3.order", "amount_total", TEST_VERSION)
    via_superset = asyncio.run(server.model_inspect.fn(
        model="c3.order",
        method="field",
        odoo_version=TEST_VERSION,
        field="amount_total",
    ))

    superset_text = via_superset.content[0].text
    assert "amount_total" in superset_text, (
        f"Expected 'amount_total' in model_inspect(field) output: {superset_text!r}"
    )
    assert superset_text == direct, (
        f"model_inspect(field) must match _resolve_field.\n"
        f"superset: {superset_text[:200]!r}\n"
        f"direct:   {direct[:200]!r}"
    )


# ---------------------------------------------------------------------------
# C3-3b: Invalid discriminator → friendly error string (no exception)
# ---------------------------------------------------------------------------


def test_model_inspect_invalid_method_returns_error_string():
    """model_inspect with an unrecognised method= returns a friendly error, not an exception.

    Covers the invalid-discriminator guard in _model_inspect (src/mcp/inspect.py).
    No DB required — the guard fires before any Neo4j query.
    """
    server = importlib.import_module("src.mcp.server")

    result = asyncio.run(server.model_inspect.fn(
        model="c3.order", method="nonexistent_method", odoo_version=TEST_VERSION
    ))

    text = result.content[0].text
    assert "Error" in text, f"Expected 'Error' in output: {text!r}"
    assert "nonexistent_method" in text, f"Expected bad method name in error: {text!r}"


def test_entity_lookup_invalid_kind_returns_error_string():
    """entity_lookup with an unrecognised kind= returns a friendly error, not an exception.

    No DB required — the guard fires before any resolver call.
    """
    server = importlib.import_module("src.mcp.server")

    # entity_lookup is async (#227 — offloads blocking body off the event loop).
    result = asyncio.run(server.entity_lookup.fn(
        kind="nonexistent_kind", odoo_version=TEST_VERSION
    ))

    text = result.content[0].text
    assert "Error" in text, f"Expected 'Error' in output: {text!r}"
    assert "nonexistent_kind" in text, (
        f"Expected bad kind name in error message: {text!r}"
    )


# ---------------------------------------------------------------------------
# Signature contract: superset tools have the correct parameters
# ---------------------------------------------------------------------------


def test_model_inspect_signature():
    """model_inspect.fn has model, method, odoo_version, field, method_name params."""
    server = importlib.import_module("src.mcp.server")
    sig = inspect.signature(server.model_inspect.fn)
    params = set(sig.parameters.keys())
    assert "model" in params, f"Expected 'model' param, got: {params}"
    assert "method" in params, f"Expected 'method' param, got: {params}"
    assert "odoo_version" in params, f"Expected 'odoo_version' param, got: {params}"
    assert "field" in params, f"Expected 'field' param, got: {params}"
    assert "method_name" in params, f"Expected 'method_name' param, got: {params}"


def test_module_inspect_signature():
    """module_inspect.fn has name, method, odoo_version params."""
    server = importlib.import_module("src.mcp.server")
    sig = inspect.signature(server.module_inspect.fn)
    params = set(sig.parameters.keys())
    assert "name" in params, f"Expected 'name' param, got: {params}"
    assert "method" in params, f"Expected 'method' param, got: {params}"
    assert "odoo_version" in params, f"Expected 'odoo_version' param, got: {params}"


def test_entity_lookup_signature():
    """entity_lookup.fn has kind, odoo_version, model, field, method_name, xmlid, name params."""
    server = importlib.import_module("src.mcp.server")
    sig = inspect.signature(server.entity_lookup.fn)
    params = set(sig.parameters.keys())
    assert "kind" in params, f"Expected 'kind' param, got: {params}"
    assert "odoo_version" in params, f"Expected 'odoo_version' param, got: {params}"
    assert "model" in params, f"Expected 'model' param, got: {params}"
    assert "field" in params, f"Expected 'field' param, got: {params}"
    assert "method_name" in params, f"Expected 'method_name' param, got: {params}"
    assert "xmlid" in params, f"Expected 'xmlid' param, got: {params}"
    assert "name" in params, f"Expected 'name' param, got: {params}"
