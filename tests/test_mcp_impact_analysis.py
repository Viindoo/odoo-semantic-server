# tests/test_mcp_impact_analysis.py
"""
Tests for impact_analysis MCP tool.
Covers: field / method / model entity types, invalid inputs, risk thresholds.
"""
import os
import sys

import pytest

from tests.conftest import TEST_VERSION  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_tools(monkeypatch=None):
    """Import _impact_analysis and _compute_risk, optionally using monkeypatch for env isolation."""
    if monkeypatch is not None:
        monkeypatch.setenv("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
        monkeypatch.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
        monkeypatch.setenv("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))
    else:
        # Fallback for pure-logic tests that don't need Neo4j (no side-effects matter)
        os.environ.setdefault("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
        os.environ.setdefault("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
        os.environ.setdefault("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _compute_risk, _impact_analysis
    return _impact_analysis, _compute_risk


# ---------------------------------------------------------------------------
# Fixtures — seed Neo4j with WI6 test data (TEST_VERSION = "99.0")
# ---------------------------------------------------------------------------

@pytest.fixture()
def seeded_impact(clean_neo4j, monkeypatch):
    """Seed all node/edge types needed for impact_analysis tests."""
    from src.indexer.models import (
        FieldInfo,
        JSGraphResult,
        JSPatchInfo,
        MethodInfo,
        ModelInfo,
        ModuleInfo,
        OWLCompInfo,
        ParseResult,
        ViewInfo,
        ViewParseResult,
    )
    from src.indexer.writer_neo4j import Neo4jWriter

    uri = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_TEST_USER", "neo4j")
    password = os.getenv("NEO4J_TEST_PASSWORD", "password")

    # Isolate env vars for server module via monkeypatch
    monkeypatch.setenv("NEO4J_URI", uri)
    monkeypatch.setenv("NEO4J_USER", user)
    monkeypatch.setenv("NEO4J_PASSWORD", password)

    writer = Neo4jWriter(uri=uri, user=user, password=password)
    writer.setup_indexes()

    v = TEST_VERSION

    # ---------- Modules ----------
    base_mod = ModuleInfo("test_base", v, "test_repo", "/tmp", [], "")
    ext_mod = ModuleInfo("test_ext", v, "test_repo", "/tmp", ["test_base"], "")

    # ---------- Models ----------
    base_model = ModelInfo(
        name="test.model", module="test_base", odoo_version=v,
        fields=[
            FieldInfo("test_field", "float", compute="_compute_test", stored=True),
        ],
        methods=[
            MethodInfo("action_do", has_super_call=True),
            MethodInfo("compute_only", has_super_call=False),
        ],
    )
    ext_model = ModelInfo(
        name="test.model", module="test_ext", odoo_version=v,
        inherit=["test.model"],
        fields=[FieldInfo("x_extra", "char")],
        methods=[MethodInfo("action_do", has_super_call=True)],
    )

    writer.write_results([
        ParseResult(module=base_mod, models=[base_model]),
        ParseResult(module=ext_mod, models=[ext_model]),
    ])

    # ---------- Views targeting test.model ----------
    base_view_mod = ModuleInfo("test_base", v, "test_repo", "/tmp", [], "")
    ext_view_mod = ModuleInfo("test_ext", v, "test_repo", "/tmp", ["test_base"], "")

    base_view = ViewInfo(
        xmlid="test_base.view_test_form",
        name="test form",
        model="test.model",
        module="test_base",
        odoo_version=v,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    ext_view = ViewInfo(
        xmlid="test_ext.view_test_form_inherit",
        name="test form inherit",
        model="test.model",
        module="test_ext",
        odoo_version=v,
        view_type="form",
        mode="extension",
        inherit_xmlid="test_base.view_test_form",
    )

    writer.write_view_results([
        ViewParseResult(module=base_view_mod, views=[base_view]),
        ViewParseResult(module=ext_view_mod, views=[ext_view]),
    ])

    # ---------- OWLComp + JSPatch bound to test.model ----------
    owl_comp = OWLCompInfo(
        name="TestModelForm",
        module="test_base",
        odoo_version=v,
        bound_model="test.model",
    )
    js_patch = JSPatchInfo(
        target="TestModelForm",
        patch_name="TestModelFormPatch",
        module="test_ext",
        odoo_version=v,
        era="patch",
        file_path="/tmp/test.js",
    )
    writer.write_js_graph_results([
        JSGraphResult(module=ext_mod, patches=[js_patch], components=[owl_comp]),
    ])

    writer.close()
    yield v


# ---------------------------------------------------------------------------
# Pure-logic tests (no Neo4j)
# ---------------------------------------------------------------------------

def test_compute_risk_thresholds():
    """Risk threshold: HIGH >= 10, MEDIUM 4-9, LOW < 4."""
    _, _compute_risk = _import_tools()
    assert _compute_risk(5, 4, 1) == "HIGH"    # total=10 → HIGH
    assert _compute_risk(9, 0, 0) == "MEDIUM"  # total=9 → MEDIUM
    assert _compute_risk(3, 3, 3) == "MEDIUM"  # total=9 → MEDIUM
    assert _compute_risk(2, 2, 0) == "MEDIUM"  # total=4 → MEDIUM (boundary)
    assert _compute_risk(2, 1, 0) == "LOW"     # total=3 → LOW
    assert _compute_risk(1, 1, 1) == "LOW"     # total=3 → LOW
    assert _compute_risk(0, 0, 0) == "LOW"     # total=0 → LOW


def test_impact_analysis_invalid_entity_type(monkeypatch):
    """Invalid entity_type returns friendly error."""
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("garbage", "test.model.test_field", TEST_VERSION)
    assert "Invalid entity_type" in result
    assert "garbage" in result


def test_impact_analysis_unparseable_field(monkeypatch):
    """Field entity_name without dot returns friendly error."""
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("field", "nodot", TEST_VERSION)
    assert "not found" in result.lower() or "invalid" in result.lower() or "error" in result.lower()


def test_impact_analysis_unparseable_method(monkeypatch):
    """Method entity_name without dot returns friendly error."""
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("method", "nodot", TEST_VERSION)
    assert "not found" in result.lower() or "invalid" in result.lower() or "error" in result.lower()


# ---------------------------------------------------------------------------
# Neo4j tests
# ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_impact_analysis_entity_not_found(clean_neo4j, monkeypatch):
    """Entity not in DB returns friendly 'not found' message."""
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("model", "nonexistent.model.xyz", TEST_VERSION)
    assert "not found" in result.lower()


@pytest.mark.neo4j
def test_impact_analysis_field_returns_tree(seeded_impact, monkeypatch):
    """Field impact analysis returns tree with all sections."""
    v = seeded_impact
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("field", "test.model.test_field", v)

    assert "Risk:" in result
    assert "Views" in result
    assert "Methods" in result
    assert "JS patches" in result
    assert "Dependent modules" in result


@pytest.mark.neo4j
def test_impact_analysis_field_shows_view(seeded_impact, monkeypatch):
    """Field analysis lists views that target the model."""
    v = seeded_impact
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("field", "test.model.test_field", v)

    # At least one of the seeded views should appear
    assert "view_test_form" in result or "test_base" in result


@pytest.mark.neo4j
def test_impact_analysis_field_not_found(seeded_impact, monkeypatch):
    """Field that does not exist in DB returns not found."""
    v = seeded_impact
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("field", "test.model.nonexistent_field", v)
    assert "not found" in result.lower()


@pytest.mark.neo4j
def test_impact_analysis_method_basic(seeded_impact, monkeypatch):
    """Method impact analysis returns tree with Risk and sections."""
    v = seeded_impact
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("method", "test.model.action_do", v)

    assert "Risk:" in result
    assert "Views" in result
    assert "Dependent modules" in result


@pytest.mark.neo4j
def test_impact_analysis_method_shows_override_chain(seeded_impact, monkeypatch):
    """Method analysis lists override chain entries."""
    v = seeded_impact
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("method", "test.model.action_do", v)
    # Both test_base and test_ext define action_do
    assert "test_base" in result or "test_ext" in result


@pytest.mark.neo4j
def test_impact_analysis_method_not_found(seeded_impact, monkeypatch):
    """Method that does not exist in DB returns not found."""
    v = seeded_impact
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("method", "test.model.nonexistent_method", v)
    assert "not found" in result.lower()


@pytest.mark.neo4j
def test_impact_analysis_model_lists_extensions(seeded_impact, monkeypatch):
    """Model impact lists all extension modules."""
    v = seeded_impact
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("model", "test.model", v)

    assert "Risk:" in result
    # Both modules defining test.model should appear
    assert "test_base" in result
    assert "test_ext" in result


@pytest.mark.neo4j
def test_impact_analysis_model_lists_views(seeded_impact, monkeypatch):
    """Model impact shows views section."""
    v = seeded_impact
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("model", "test.model", v)
    assert "Views" in result


@pytest.mark.neo4j
def test_impact_analysis_model_not_found(seeded_impact, monkeypatch):
    """Model that does not exist returns not found."""
    v = seeded_impact
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("model", "nonexistent.xyz", v)
    assert "not found" in result.lower()


@pytest.mark.neo4j
def test_impact_analysis_rejects_unresolved_placeholder_model(clean_neo4j, monkeypatch):
    """__unresolved__ placeholder Model must be treated as 'not found'."""
    from neo4j import GraphDatabase

    # Seed a placeholder Model node directly
    uri = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_TEST_USER", "neo4j")
    password = os.getenv("NEO4J_TEST_PASSWORD", "password")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session() as session:
        session.run(
            "MERGE (m:Model {name: $name, module: $mod, odoo_version: $v}) "
            "SET m.unresolved = true",
            name="mail.thread", mod="__unresolved__", v=TEST_VERSION,
        )
    driver.close()

    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("model", "mail.thread", TEST_VERSION)

    # Should be treated as not found — placeholder must not pass existence check
    assert "not found" in result.lower(), (
        f"__unresolved__ placeholder should be rejected as 'not found', got: {result!r}"
    )
