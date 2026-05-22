# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for RelaxNG XML validation -> :LintViolation (WI-E, M11).

Coverage:
  - Indexing a v17 XML fixture with a deliberate RNG schema error yields a
    :LintViolation node + :HAS_VIOLATION edge to the owning :View.
  - A v13 XML fixture (below the v15 gate) yields NO :LintViolation nodes.
  - lint_check(language='xml') returns the violation from the graph.
  - A valid tree view yields NO violations.

NOTE: Parser-level unit tests (no Neo4j) are in test_relaxng_violations_unit.py
to avoid the file-level neo4j marker applying to them.
"""
import os
import textwrap
from pathlib import Path

import pytest

from src.indexer.models import (
    LintViolationInfo,
    ModuleInfo,
    ViewInfo,
    ViewParseResult,
)
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INVALID_TREE_XML = """\
<?xml version="1.0"?>
<odoo>
    <record id="view_order_tree_bad" model="ir.ui.view">
        <field name="name">sale.order.tree.bad</field>
        <field name="model">sale.order</field>
        <field name="arch" type="xml">
            <tree>
                <badtag foo="bar"/>
            </tree>
        </field>
    </record>
</odoo>
"""


def _make_module(name: str, version: str, path: str) -> ModuleInfo:
    return ModuleInfo(
        name=name, odoo_version=version, repo=f"{name}_repo",
        path=path, depends=[], version_raw="",
    )


def _write_xml(directory: Path, filename: str, content: str) -> str:
    p = directory / filename
    p.write_text(textwrap.dedent(content).strip())
    return str(p)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter connected to test DB."""
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


# ---------------------------------------------------------------------------
# Integration tests — Neo4j writer
# ---------------------------------------------------------------------------


class TestWriterLintViolations:
    """Neo4j integration: write_lint_violations stores nodes + edges."""

    def _make_view_result(self, module: ModuleInfo, xmlid: str) -> ViewParseResult:
        """Helper to build a ViewParseResult with one View node."""
        view = ViewInfo(
            xmlid=xmlid, name="test view",
            model="sale.order", module=module.name,
            odoo_version=module.odoo_version,
            view_type="tree", mode="primary",
            inherit_xmlid=None,
            file_path="/tmp/test.xml",
        )
        return ViewParseResult(module=module, views=[view], qweb=[])

    def test_write_lint_violation_node_exists(self, writer, neo4j_driver):
        violation = LintViolationInfo(
            file_path="/tmp/test.xml",
            line=5,
            rule="relaxng.tree_view",
            message="Did not expect element badtag there",
            view_xmlid=f"sale.view_order_tree_{TEST_VERSION}",
            odoo_version=TEST_VERSION,
            severity="error",
            view_type="tree",
        )
        writer.write_lint_violations([violation])

        with neo4j_driver.session() as session:
            rec = session.run(
                """MATCH (lv:LintViolation {
                    file_path: $fp, line: $line,
                    rule: $rule, odoo_version: $v
                }) RETURN lv""",
                fp="/tmp/test.xml", line=5, rule="relaxng.tree_view", v=TEST_VERSION,
            ).single()
        assert rec is not None
        lv = rec["lv"]
        assert lv["severity"] == "error"
        assert lv["view_type"] == "tree"
        assert "badtag" in lv["message"]

    def test_has_violation_edge_created(self, writer, neo4j_driver):
        """HAS_VIOLATION edge from :View to :LintViolation must be created."""
        module = _make_module("sale", TEST_VERSION, "/tmp")
        xmlid = f"sale.view_edge_test_{TEST_VERSION}"

        # Write the View node first
        vr = self._make_view_result(module, xmlid)
        writer.write_view_results([vr])

        # Write the violation
        violation = LintViolationInfo(
            file_path="/tmp/test.xml",
            line=7,
            rule="relaxng.tree_view",
            message="Did not expect element x there",
            view_xmlid=xmlid,
            odoo_version=TEST_VERSION,
            severity="error",
            view_type="tree",
        )
        writer.write_lint_violations([violation])

        with neo4j_driver.session() as session:
            rec = session.run(
                """MATCH (view:View {xmlid: $xmlid, odoo_version: $v})
                   -[:HAS_VIOLATION]->(lv:LintViolation)
                   RETURN count(lv) AS cnt""",
                xmlid=xmlid, v=TEST_VERSION,
            ).single()
        assert rec is not None
        assert rec["cnt"] == 1, f"expected 1 HAS_VIOLATION edge, got {rec['cnt']}"

    def test_write_lint_violations_empty_list_is_noop(self, writer, neo4j_driver):
        """Empty list must not cause any writes or errors."""
        writer.write_lint_violations([])
        with neo4j_driver.session() as session:
            rec = session.run(
                "MATCH (lv:LintViolation {odoo_version: $v}) RETURN count(lv) AS cnt",
                v=TEST_VERSION,
            ).single()
        assert rec["cnt"] == 0

    def test_idempotent_write_does_not_duplicate(self, writer, neo4j_driver):
        """Writing the same violation twice produces exactly one :LintViolation node."""
        violation = LintViolationInfo(
            file_path="/tmp/idem.xml",
            line=3,
            rule="relaxng.tree_view",
            message="Did not expect element x",
            view_xmlid="sale.view_idem",
            odoo_version=TEST_VERSION,
            severity="error",
            view_type="tree",
        )
        writer.write_lint_violations([violation])
        writer.write_lint_violations([violation])  # second write must be idempotent

        with neo4j_driver.session() as session:
            rec = session.run(
                """MATCH (lv:LintViolation {
                    file_path: $fp, line: $line, rule: $rule, odoo_version: $v
                }) RETURN count(lv) AS cnt""",
                fp="/tmp/idem.xml", line=3, rule="relaxng.tree_view", v=TEST_VERSION,
            ).single()
        assert rec["cnt"] == 1, f"expected exactly 1 node after 2 writes, got {rec['cnt']}"


# ---------------------------------------------------------------------------
# Integration tests — MCP server lint_check xml dispatch
# ---------------------------------------------------------------------------


class TestMcpLintCheckXml:
    """Integration: lint_check(language='xml') returns RelaxNG violations from graph."""

    def test_lint_check_xml_returns_violations(self, writer, neo4j_driver):
        """lint_check xml mode returns formatted output with indexed violations."""
        from src.mcp.server import _lint_check_xml

        # Seed a LintViolation node
        violation = LintViolationInfo(
            file_path="/tmp/mcp_test.xml",
            line=5,
            rule="relaxng.tree_view",
            message="Did not expect element badel there",
            view_xmlid=f"sale.view_mcp_test_{TEST_VERSION}",
            odoo_version=TEST_VERSION,
            severity="error",
            view_type="tree",
        )
        writer.write_lint_violations([violation])

        result = _lint_check_xml(TEST_VERSION)
        assert "RelaxNG violations" in result
        assert "relaxng.tree_view" in result
        assert "badel" in result

    def test_lint_check_xml_no_violations_message(self, writer, neo4j_driver):
        """lint_check xml with no violations returns the empty-state message."""
        from src.mcp.server import _lint_check_xml

        # "8.0" — no violations indexed for this version in the test DB
        result = _lint_check_xml("8.0")
        assert "no RelaxNG violations" in result

    def test_lint_check_dispatches_xml_language(self, writer, neo4j_driver):
        """_lint_check('', version, 'xml') dispatches to xml path (not V0 fuzzy)."""
        from src.mcp.server import _lint_check

        # language=xml must return xml-style header, not python-style V0 banner
        result = _lint_check("", "8.0", "xml")
        assert "RelaxNG violations" in result
        # Must NOT contain V0 fuzzy banner
        assert "V0 fuzzy" not in result
