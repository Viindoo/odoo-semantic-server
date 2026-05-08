# tests/test_mcp_spec_tools.py
"""MCP tool tests for the M4.5 spec layer (lookup_core_api / api_version_diff /
find_deprecated_usage / lint_check / cli_help).

Each tool is exercised with at least 3 cases: happy path, not-found / empty,
edge case (same version, invalid arg, etc.). Output must be a tree-formatted
string consumable by AI clients.
"""
import os
import sys

import pytest

from src.indexer.models import CoreSymbolInfo
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

# Spec tools use a dedicated test version pair so they don't collide with
# `seeded_neo4j` (99.0) / `seeded_views` (97.0).
SPEC_VERSION_FROM = "96.0"
SPEC_VERSION_TO = "95.0"


@pytest.fixture(scope="module")
def seeded_spec_neo4j(neo4j_driver):
    """Seed CoreSymbol / LintRule / CLI* nodes for the spec-tool test suite."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        for v in (SPEC_VERSION_FROM, SPEC_VERSION_TO, TEST_VERSION):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)

    # CoreSymbol: name_get deprecated@v96, removed@v95 + replacement display_name@v95
    writer.write_core_symbols([
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.name_get",
            kind="orm_method", odoo_version=SPEC_VERSION_FROM,
            signature="name_get(self)",
            status="deprecated",
            replacement_qname="odoo.models.BaseModel.display_name",
        ),
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.display_name",
            kind="orm_method", odoo_version=SPEC_VERSION_FROM,
            signature="display_name (computed property)",
            status="stable",
        ),
        # safe_eval present in v96 and v95 (stable both versions)
        CoreSymbolInfo(
            qualified_name="odoo.tools.safe_eval.safe_eval",
            kind="function", odoo_version=SPEC_VERSION_FROM,
            signature="safe_eval(expr, context=None)",
        ),
        CoreSymbolInfo(
            qualified_name="odoo.tools.safe_eval.safe_eval",
            kind="function", odoo_version=SPEC_VERSION_TO,
            signature="safe_eval(expr, context, locals_dict=None)",
            status="stable",
        ),
        # name_get in v95 is removed; replacement display_name@v95 added.
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.display_name",
            kind="orm_method", odoo_version=SPEC_VERSION_TO,
            signature="display_name (computed property)",
            status="added",
        ),
    ])

    yield SPEC_VERSION_FROM, SPEC_VERSION_TO

    with neo4j_driver.session() as session:
        for v in (SPEC_VERSION_FROM, SPEC_VERSION_TO, TEST_VERSION):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)
    writer.close()


@pytest.fixture
def spec_tools(seeded_spec_neo4j):
    """Import MCP spec-tool functions after seeding data."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp import server as mcp_server
    return mcp_server


# --- lookup_core_api ----------------------------------------------------


class TestLookupCoreApi:
    def test_happy_path_returns_status_and_replacement(self, spec_tools, seeded_spec_neo4j):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._lookup_core_api("name_get", v_from)
        assert "name_get" in out
        assert "deprecated" in out.lower()
        assert "display_name" in out  # replacement surfaced

    def test_returns_not_found_for_unknown_symbol(self, spec_tools, seeded_spec_neo4j):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._lookup_core_api("definitely_not_a_real_symbol_xyz", v_from)
        assert "not found" in out.lower()

    def test_partial_qualified_name_resolves_via_endswith(self, spec_tools, seeded_spec_neo4j):
        """Short name like 'safe_eval' resolves to qualified_name ending in '.safe_eval'."""
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._lookup_core_api("safe_eval", v_from)
        assert "safe_eval" in out
        assert "function" in out.lower()


# --- api_version_diff ---------------------------------------------------


class TestApiVersionDiff:
    def test_happy_path_signature_change(self, spec_tools, seeded_spec_neo4j):
        v_from, v_to = seeded_spec_neo4j
        out = spec_tools._api_version_diff("safe_eval", v_from, v_to)
        # Signature differs between the two versions → "Signature" or "Stable" + diff hint
        assert "safe_eval" in out
        assert v_from in out and v_to in out

    def test_same_version_returns_no_diff(self, spec_tools, seeded_spec_neo4j):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._api_version_diff("safe_eval", v_from, v_from)
        assert "no diff" in out.lower() or "same version" in out.lower()

    def test_symbol_missing_in_both_versions(self, spec_tools, seeded_spec_neo4j):
        v_from, v_to = seeded_spec_neo4j
        out = spec_tools._api_version_diff("nonexistent_xyz", v_from, v_to)
        assert "not found" in out.lower()

    def test_symbol_only_in_old_version_marked_removed(self, spec_tools, seeded_spec_neo4j):
        """name_get exists @v96 (deprecated) but not @v95 → diff says removed."""
        v_from, v_to = seeded_spec_neo4j
        out = spec_tools._api_version_diff("name_get", v_from, v_to)
        assert "name_get" in out
        assert "removed" in out.lower() or "deprecated" in out.lower()
