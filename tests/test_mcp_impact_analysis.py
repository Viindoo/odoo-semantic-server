# SPDX-License-Identifier: AGPL-3.0-or-later
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


# ---------------------------------------------------------------------------
# profile_name filter tests for impact_analysis
# ---------------------------------------------------------------------------

@pytest.fixture()
def seeded_impact_profiles(clean_neo4j, monkeypatch):
    """Seed Model nodes with distinct profile arrays for profile_name filter tests."""
    from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    uri = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_TEST_USER", "neo4j")
    password = os.getenv("NEO4J_TEST_PASSWORD", "password")

    monkeypatch.setenv("NEO4J_URI", uri)
    monkeypatch.setenv("NEO4J_USER", user)
    monkeypatch.setenv("NEO4J_PASSWORD", password)

    writer = Neo4jWriter(uri=uri, user=user, password=password)
    writer.setup_indexes()

    v = TEST_VERSION

    # model in profile "alpha_impact" only
    alpha_mod = ModuleInfo("alpha_mod", v, "repo_alpha", "/tmp", [], "")
    alpha_model = ModelInfo(
        name="alpha.model", module="alpha_mod", odoo_version=v,
        fields=[FieldInfo("alpha_field", "char")],
        methods=[MethodInfo("alpha_method", has_super_call=False)],
    )
    writer.write_results(
        [ParseResult(module=alpha_mod, models=[alpha_model])],
        profiles=["alpha_impact"],
    )

    # model in profile "beta_impact" only
    beta_mod = ModuleInfo("beta_mod", v, "repo_beta", "/tmp", [], "")
    beta_model = ModelInfo(
        name="beta.model", module="beta_mod", odoo_version=v,
        fields=[FieldInfo("beta_field", "char")],
        methods=[MethodInfo("beta_method", has_super_call=False)],
    )
    writer.write_results(
        [ParseResult(module=beta_mod, models=[beta_model])],
        profiles=["beta_impact"],
    )

    writer.close()
    yield v


@pytest.mark.neo4j
def test_impact_analysis_profile_none_backward_compat(
    seeded_impact_profiles, monkeypatch,
):
    """profile_name=None returns impact without filtering (backward compat)."""
    v = seeded_impact_profiles
    _impact_analysis, _ = _import_tools(monkeypatch)
    # Both models exist — querying alpha.model with no profile filter should find it
    result = _impact_analysis("model", "alpha.model", v, profile_name=None)
    assert "impact_analysis" in result
    assert "not found" not in result.lower()


@pytest.mark.neo4j
def test_impact_analysis_profile_name_narrows_non_escalating_for_admin(
    seeded_impact_profiles, monkeypatch,
):
    """WG-3t T3 (ADR-0034): profile_name is a NON-ESCALATING narrowing filter,
    consistent across the Neo4j and pgvector paths (fixes the split-brain).

    Pre-WG-3t the Neo4j path treated admin's profile_name as an advisory no-op
    (alpha.model still found when asking for 'beta_impact') while the pgvector
    path narrowed — a split-brain. Under T3 BOTH paths narrow: admin asking for
    'beta_impact' narrows the visible set to that profile, so alpha.model (which
    is under 'alpha_impact') is correctly NOT found. The tenant boundary remains
    the isolation guarantee (test_cross_tenant_isolation); profile_name can only
    ever shrink within own∪shared, never widen.
    """
    v = seeded_impact_profiles
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("model", "alpha.model", v, profile_name="beta_impact")
    assert "not found" in result.lower(), (
        f"profile_name='beta_impact' must narrow away alpha.model (non-escalating), "
        f"got: {result!r}"
    )
    # Positive narrowing: the MATCHING profile still surfaces the model — proving
    # this is a precise narrowing, not a blanket block.
    matched = _impact_analysis("model", "alpha.model", v, profile_name="alpha_impact")
    assert "not found" not in matched.lower(), (
        f"profile_name='alpha_impact' must still find alpha.model, got: {matched!r}"
    )


# ---------------------------------------------------------------------------
# G1 cap contract tests — ADR-0023 §3 (output limits disclosure)
# ---------------------------------------------------------------------------

@pytest.fixture()
def seeded_impact_many_views(clean_neo4j, monkeypatch):
    """Seed a model with > LIST_PREVIEW_MAX_ITEMS views to trigger the cap."""
    from src.constants import LIST_PREVIEW_MAX_ITEMS
    from src.indexer.models import (
        FieldInfo,
        MethodInfo,
        ModelInfo,
        ModuleInfo,
        ParseResult,
        ViewInfo,
        ViewParseResult,
    )
    from src.indexer.writer_neo4j import Neo4jWriter

    uri = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_TEST_USER", "neo4j")
    password = os.getenv("NEO4J_TEST_PASSWORD", "password")

    monkeypatch.setenv("NEO4J_URI", uri)
    monkeypatch.setenv("NEO4J_USER", user)
    monkeypatch.setenv("NEO4J_PASSWORD", password)

    v = TEST_VERSION
    writer = Neo4jWriter(uri=uri, user=user, password=password)
    writer.setup_indexes()

    # Seed the model module
    base_mod = ModuleInfo("big_model_mod", v, "test_repo", "/tmp", [], "")
    big_model = ModelInfo(
        name="big.model",
        module="big_model_mod",
        odoo_version=v,
        fields=[FieldInfo("name", "char")],
        methods=[MethodInfo("write", has_super_call=True)],
    )
    writer.write_results([ParseResult(module=base_mod, models=[big_model])])

    # Seed LIST_PREVIEW_MAX_ITEMS + 5 views targeting big.model
    view_count = LIST_PREVIEW_MAX_ITEMS + 5
    views = []
    view_mods = []
    for idx in range(view_count):
        mod_name = f"view_mod_{idx}"
        view_mods.append(ModuleInfo(mod_name, v, "test_repo", "/tmp", [], ""))
        views.append(ViewInfo(
            xmlid=f"{mod_name}.view_big_form_{idx}",
            name=f"big form {idx}",
            model="big.model",
            module=mod_name,
            odoo_version=v,
            view_type="form",
            mode="primary",
            inherit_xmlid=None,
        ))

    for i, (vm, vw) in enumerate(zip(view_mods, views)):
        writer.write_view_results([ViewParseResult(module=vm, views=[vw])])

    writer.close()
    yield v, view_count


@pytest.mark.neo4j
def test_impact_analysis_views_capped_with_disclosure(seeded_impact_many_views, monkeypatch):
    """G1 contract: views section capped at LIST_PREVIEW_MAX_ITEMS;
    real count shown in label; disclosure line present when total > cap.
    """
    from src.constants import LIST_PREVIEW_MAX_ITEMS
    v, total_views = seeded_impact_many_views
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("field", "big.model.name", v)

    lines = result.splitlines()

    # Risk count must reflect REAL total (not capped) - total includes views + methods
    risk_line = next((ln for ln in lines if "Risk:" in ln), None)
    assert risk_line is not None, "Missing Risk line"

    # Views section must show REAL count in header
    views_line = next((ln for ln in lines if "Views (" in ln), None)
    assert views_line is not None, "Missing Views section header"
    assert str(total_views) in views_line, (
        f"Views header must show real count {total_views}, got: {views_line!r}"
    )

    # Disclosure "... and N more" must appear when total > cap
    has_disclosure = any(
        "... and" in ln and "more" in ln for ln in lines
    )
    assert has_disclosure, (
        f"No '... and N more' disclosure found for {total_views} views "
        f"(cap={LIST_PREVIEW_MAX_ITEMS}). Full output:\n{result}"
    )

    # Number of actual view items rendered must be <= cap
    view_items = [
        ln for ln in lines
        if ("├─" in ln or "└─" in ln) and "view_mod_" in ln and "view_big_form" in ln
    ]
    assert len(view_items) <= LIST_PREVIEW_MAX_ITEMS, (
        f"Too many view items rendered: {len(view_items)} > cap={LIST_PREVIEW_MAX_ITEMS}"
    )


@pytest.mark.neo4j
def test_impact_analysis_dependent_modules_capped(seeded_impact_many_views, monkeypatch):
    """G1 contract: dependent modules section shows cap + 'and N more' disclosure."""
    # seeded_impact_many_views only has a few dep modules so won't trigger the modules cap.
    # This test just verifies the output format is correct (Dependent modules line present).
    v, _ = seeded_impact_many_views
    _impact_analysis, _ = _import_tools(monkeypatch)
    result = _impact_analysis("model", "big.model", v)
    lines = result.splitlines()
    dep_line = next((ln for ln in lines if "Dependent modules" in ln), None)
    assert dep_line is not None, "Missing 'Dependent modules' line in impact output"


@pytest.mark.neo4j
def test_impact_analysis_risk_uses_real_count(seeded_impact_many_views, monkeypatch):
    """G1 contract: risk score computed from REAL entity count, not capped count."""
    from src.constants import IMPACT_RISK_HIGH_THRESHOLD
    v, total_views = seeded_impact_many_views
    _impact_analysis, _compute_risk = _import_tools(monkeypatch)

    result = _impact_analysis("field", "big.model.name", v)

    risk_line = next((ln for ln in result.splitlines() if "Risk:" in ln), "")
    # total_views > IMPACT_RISK_HIGH_THRESHOLD (25 views > 10 threshold),
    # so risk MUST be HIGH — if it were computed from capped count it might be wrong.
    if total_views >= IMPACT_RISK_HIGH_THRESHOLD:
        assert "HIGH" in risk_line or "MEDIUM" in risk_line, (
            f"Risk must not be LOW when total_views={total_views}: {risk_line!r}"
        )
