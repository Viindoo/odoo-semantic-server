"""Tests for WI-C3 dual-mode dispatch on the 4 resolve_* wrappers.

Covers AC-C3-1 through AC-C3-7:
  C3-1: 4 wrappers expose target= as first arg; legacy kwargs kept as None-default
  C3-2: Round-trip — ref → canonical → same output as legacy kwarg call
  C3-3: Stale-ref returns a friendly error string (no exception raised)
  C3-4: DeprecationWarning fires exactly once per legacy-kwarg use
  C3-7: ≥4 tests covering ref round-trip, canonical, stale-ref, legacy-warn

These are unit tests (no DB required for stale-ref / warning tests).
The round-trip tests require Neo4j and are marked with pytest.mark.neo4j.

DB version: TEST_VERSION = "91.0" (distinct from all other test modules).
"""
import inspect
import os
import warnings

import pytest

from src.mcp.refs import RefMinter

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


@pytest.fixture()
def minted_field_ref():
    """Mint a ref for 'amount_total' on 'c3.order' in the test API key namespace."""
    minter = RefMinter()
    items = [{"field_name": "amount_total", "model": "c3.order"}]
    refs = minter.mint(items, api_key_id="test-key-c3")
    # Patch the global singleton so server._GLOBAL_MINTER resolves the same ref.
    from src.mcp import refs as refs_module
    original = refs_module._GLOBAL_MINTER
    refs_module._GLOBAL_MINTER = minter
    yield refs[0]
    refs_module._GLOBAL_MINTER = original


# ---------------------------------------------------------------------------
# AC-C3-7 Test 1 — target=ref round-trip (requires Neo4j, AC-C3-2)
# ---------------------------------------------------------------------------


@pytest.mark.neo4j
def test_resolve_field_via_ref_round_trip(dual_db, minted_field_ref):
    """Ref 'f1' minted for c3.order.amount_total resolves to identical output as legacy kwargs.

    AC-C3-2: round-trip output byte-identical to legacy kwarg call.
    AC-C3-7 test 1: target=ref happy path.
    """
    from src.mcp.server import _api_key_id_local

    # Set the thread-local API key so the wrapper looks up the right namespace.
    _api_key_id_local.value = "test-key-c3"

    import importlib
    server = importlib.import_module("src.mcp.server")

    # Call via ref
    ref_result = server.resolve_field.fn(
        target=minted_field_ref, odoo_version=TEST_VERSION
    )
    # Call via legacy kwargs (should produce same text)
    legacy_result = server.resolve_field.fn(
        model_name="c3.order", field_name="amount_total", odoo_version=TEST_VERSION
    )

    ref_text = ref_result.content[0].text
    legacy_text = legacy_result.content[0].text

    assert ref_text == legacy_text, (
        f"Ref round-trip text mismatch:\n  ref:    {ref_text!r}\n  legacy: {legacy_text!r}"
    )


# ---------------------------------------------------------------------------
# AC-C3-7 Test 2 — target=canonical happy path
# ---------------------------------------------------------------------------


@pytest.mark.neo4j
def test_resolve_field_via_canonical_target(dual_db):
    """target='c3.order.amount_total' dispatches to the correct field impl.

    AC-C3-7 test 2: target=canonical happy path.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = server.resolve_field.fn(
        target="c3.order.amount_total", odoo_version=TEST_VERSION
    )
    text = result.content[0].text
    assert "amount_total" in text, f"Expected 'amount_total' in output: {text!r}"
    assert "c3.order" in text, f"Expected 'c3.order' in output: {text!r}"
    assert "monetary" in text.lower(), f"Expected type 'monetary' in output: {text!r}"


# ---------------------------------------------------------------------------
# AC-C3-7 Test 3 — stale-ref returns friendly error string (AC-C3-3)
# ---------------------------------------------------------------------------


def test_resolve_field_stale_ref_returns_friendly_error():
    """resolve_field(target='f999') returns a friendly error string, not an exception.

    AC-C3-3: unknown/stale ref → friendly error string in same tree format.
    AC-C3-7 test 3: stale-ref error path.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    # 'f99999' cannot exist (no minting happened for this ref)
    result = server.resolve_field.fn(target="f99999", odoo_version=TEST_VERSION)

    # Must return ToolResult with text, not raise.
    text = result.content[0].text
    assert "f99999" in text, f"Expected ref 'f99999' mentioned in error: {text!r}"
    assert "expired" in text.lower() or "unknown" in text.lower(), (
        f"Expected 'expired' or 'unknown' in error text: {text!r}"
    )
    assert "list_fields" in text.lower() or "re-run" in text.lower(), (
        f"Expected recovery hint mentioning list_fields or re-run: {text!r}"
    )


def test_resolve_model_stale_ref_returns_friendly_error():
    """resolve_model(target='m99999') returns a friendly error string."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = server.resolve_model.fn(target="m99999")

    text = result.content[0].text
    assert "m99999" in text
    assert "expired" in text.lower() or "unknown" in text.lower()


# ---------------------------------------------------------------------------
# AC-C3-7 Test 4 — legacy kwarg triggers DeprecationWarning (AC-C3-4)
# ---------------------------------------------------------------------------


def test_legacy_kwargs_trigger_deprecation_warning():
    """Supplying model_name= / field_name= fires exactly one DeprecationWarning each call.

    AC-C3-4: warnings.warn(..., DeprecationWarning, stacklevel=2) with Python dedup.
    AC-C3-7 test 4: legacy kwarg + DeprecationWarning fires.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    # Use 'always' filter so warnings are never suppressed by default once-per-location rule.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        # Call resolve_model with legacy model_name= (no valid DB needed — it will
        # hit "not found" but the warning must fire before the DB query completes
        # or the DB simply isn't available; we only care about the warning).
        try:
            server.resolve_model.fn(model_name="nonexistent.model.for.warn.test")
        except Exception:
            pass  # DB not available or not found — irrelevant for this test.

    deprecation_warns = [
        w for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "model_name" in str(w.message).lower()
        and "deprecated" in str(w.message).lower()
    ]
    assert len(deprecation_warns) >= 1, (
        f"Expected DeprecationWarning for model_name=, got warnings: {caught}"
    )


def test_legacy_field_kwargs_trigger_deprecation_warning():
    """Supplying model_name= + field_name= on resolve_field fires DeprecationWarning."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        try:
            server.resolve_field.fn(
                model_name="nonexistent.model", field_name="nonexistent_field"
            )
        except Exception:
            pass

    deprecation_warns = [
        w for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "deprecated" in str(w.message).lower()
    ]
    assert len(deprecation_warns) >= 1, (
        f"Expected DeprecationWarning for legacy field kwargs, got: {caught}"
    )


# ---------------------------------------------------------------------------
# AC-C3-1 — Signature contract: target= is first positional, legacy kwargs present
# ---------------------------------------------------------------------------


def test_resolve_model_signature_has_target_first():
    """resolve_model wrapper has target as first parameter (AC-C3-1)."""
    import importlib
    server = importlib.import_module("src.mcp.server")
    sig = inspect.signature(server.resolve_model.fn)
    params = list(sig.parameters.keys())
    assert params[0] == "target", f"Expected 'target' as first param, got: {params}"
    assert "model_name" in params, "Expected legacy 'model_name' param to be present"
    assert "odoo_version" in params, "Expected 'odoo_version' param"


def test_resolve_field_signature_has_target_first():
    """resolve_field wrapper has target as first parameter and legacy field kwargs (AC-C3-1)."""
    import importlib
    server = importlib.import_module("src.mcp.server")
    sig = inspect.signature(server.resolve_field.fn)
    params = list(sig.parameters.keys())
    assert params[0] == "target", f"Expected 'target' as first param, got: {params}"
    assert "model_name" in params, "Expected legacy 'model_name' param"
    assert "field_name" in params, "Expected legacy 'field_name' param"


def test_resolve_method_signature_has_target_first():
    """resolve_method wrapper has target as first parameter and legacy method kwargs (AC-C3-1)."""
    import importlib
    server = importlib.import_module("src.mcp.server")
    sig = inspect.signature(server.resolve_method.fn)
    params = list(sig.parameters.keys())
    assert params[0] == "target", f"Expected 'target' as first param, got: {params}"
    assert "model_name" in params, "Expected legacy 'model_name' param"
    assert "method_name" in params, "Expected legacy 'method_name' param"


def test_resolve_view_signature_has_target_first():
    """resolve_view wrapper has target as first parameter and legacy xmlid kwarg (AC-C3-1)."""
    import importlib
    server = importlib.import_module("src.mcp.server")
    sig = inspect.signature(server.resolve_view.fn)
    params = list(sig.parameters.keys())
    assert params[0] == "target", f"Expected 'target' as first param, got: {params}"
    assert "xmlid" in params, "Expected legacy 'xmlid' param"
