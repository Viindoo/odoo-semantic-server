# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_writer_report.py
"""GAP-2/GAP-5 — :Report graph writer + entity_lookup(kind='report'). neo4j-MARKED (CI-only).

Behaviour contract:
  (i)   write_view_results creates :Report nodes (composite key xmlid+odoo_version)
        from ViewParseResult.reports, with DEFINED_IN (Report -> Module).
  (ii)  REPORTS_ON (Report -> Model) resolves to the single business-model node
        deterministically (is_definition first, then field_count, then module);
        NO multi-row .single() pattern.
  (iii) USES_TEMPLATE (Report -> QWebTmpl) resolves the report_name template xmlid.
  (iv)  entity_lookup(kind='report', model=...) returns the report.

Do NOT run locally (dev box has live neo4j; clean_neo4j DROPs data) — CI only.
"""
import os

import pytest

from src.indexer.models import (
    ModelInfo,
    ModuleInfo,
    ParseResult,
    QWebInfo,
    ReportInfo,
    ViewParseResult,
)
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _mod(name: str) -> ModuleInfo:
    return ModuleInfo(
        name=name, odoo_version=TEST_VERSION, repo="test_repo",
        path="/tmp", depends=[], version_raw="",
    )


def _model_result(module: str, model_name: str) -> ParseResult:
    return ParseResult(module=_mod(module), models=[
        ModelInfo(name=model_name, module=module, odoo_version=TEST_VERSION),
    ])


def _report(xmlid: str, model: str, module: str, report_name: str | None) -> ReportInfo:
    return ReportInfo(
        xmlid=xmlid, name="Quotation / Order", model=model,
        report_type="qweb-pdf", module=module, odoo_version=TEST_VERSION,
        report_name=report_name,
    )


def _count(driver, cypher: str, **params) -> int:
    with driver.session() as s:
        row = s.run(cypher, v=TEST_VERSION, **params).single()
    return row["n"] if row else 0


def test_report_node_and_defined_in_created(writer, clean_neo4j):
    """Report node + DEFINED_IN edge are written from ViewParseResult.reports."""
    driver = clean_neo4j
    res = ViewParseResult(module=_mod("sale"), reports=[
        _report("sale.action_report_saleorder", "sale.order", "sale", None),
    ])
    writer.write_view_results([res], profiles=["test_repo"])

    assert _count(
        driver,
        "MATCH (rp:Report {xmlid:'sale.action_report_saleorder', odoo_version:$v}) "
        "RETURN count(rp) AS n",
    ) == 1
    assert _count(
        driver,
        "MATCH (:Report {xmlid:'sale.action_report_saleorder', odoo_version:$v})"
        "-[:DEFINED_IN]->(:Module {name:'sale', odoo_version:$v}) RETURN count(*) AS n",
    ) == 1


def test_reports_on_edge_to_model(writer, clean_neo4j):
    """REPORTS_ON (Report -> Model) resolves the business model after models exist."""
    driver = clean_neo4j
    # Model must exist first (write order in pipeline: write_results before views).
    writer.write_results([_model_result("sale", "sale.order")], profiles=["test_repo"])
    res = ViewParseResult(module=_mod("sale"), reports=[
        _report("sale.action_report_saleorder", "sale.order", "sale", None),
    ])
    writer.write_view_results([res], profiles=["test_repo"])

    assert _count(
        driver,
        "MATCH (:Report {xmlid:'sale.action_report_saleorder', odoo_version:$v})"
        "-[:REPORTS_ON]->(m:Model {name:'sale.order', odoo_version:$v}) "
        "RETURN count(m) AS n",
    ) == 1


def test_uses_template_edge(writer, clean_neo4j):
    """USES_TEMPLATE (Report -> QWebTmpl) resolves report_name to a template."""
    driver = clean_neo4j
    # Template must be indexed first (qweb pass runs before the report loop).
    tmpl_res = ViewParseResult(module=_mod("sale"), qweb=[
        QWebInfo(
            xmlid="sale.report_saleorder", module="sale",
            odoo_version=TEST_VERSION,
        ),
    ])
    writer.write_view_results([tmpl_res], profiles=["test_repo"])
    report_res = ViewParseResult(module=_mod("sale"), reports=[
        _report(
            "sale.action_report_saleorder", "sale.order", "sale",
            "sale.report_saleorder",
        ),
    ])
    writer.write_view_results([report_res], profiles=["test_repo"])

    assert _count(
        driver,
        "MATCH (:Report {xmlid:'sale.action_report_saleorder', odoo_version:$v})"
        "-[:USES_TEMPLATE]->(t:QWebTmpl {xmlid:'sale.report_saleorder', odoo_version:$v}) "
        "RETURN count(t) AS n",
    ) == 1


def test_report_removed_by_module_scoped_delete(writer, clean_neo4j):
    """integration MED-2: Report was added to the delete_modules_scoped child
    cascade (writer_neo4j.py). A re-index/repo-delete of the owning module must
    remove its Report node — otherwise a stale Report orphans on --full reindex.
    Red-before-green: drop 'Report' from the cascade label list and this fails.
    """
    driver = clean_neo4j
    # Write the owning module FIRST via write_results so the Module node carries
    # repo='test_repo' (write_view_results does not set Module.repo, and
    # delete_modules_scoped collects victims by Module {repo, version}).
    writer.write_results([_model_result("sale", "sale.order")], profiles=["test_repo"])
    res = ViewParseResult(module=_mod("sale"), reports=[
        _report("sale.action_report_saleorder", "sale.order", "sale", None),
    ])
    writer.write_view_results([res], profiles=["test_repo"])

    # Sanity: the Report exists before the delete.
    assert _count(
        driver,
        "MATCH (rp:Report {xmlid:'sale.action_report_saleorder', odoo_version:$v}) "
        "RETURN count(rp) AS n",
    ) == 1

    # The owning module's repo basename is 'test_repo' (see _mod), version TEST_VERSION.
    writer.delete_modules_scoped("test_repo", TEST_VERSION)

    # The Report node must be gone (it carries module='sale', so it is matched by
    # the module-scoped cascade now that 'Report' is in the child-label list).
    assert _count(
        driver,
        "MATCH (rp:Report {xmlid:'sale.action_report_saleorder', odoo_version:$v}) "
        "RETURN count(rp) AS n",
    ) == 0


def test_entity_lookup_report_returns_it(writer, clean_neo4j):
    """entity_lookup(kind='report', model=...) surfaces the indexed report."""
    from src.mcp.inspect import _entity_lookup

    writer.write_results([_model_result("sale", "sale.order")], profiles=["test_repo"])
    res = ViewParseResult(module=_mod("sale"), reports=[
        _report(
            "sale.action_report_saleorder", "sale.order", "sale",
            "sale.report_saleorder",
        ),
    ])
    writer.write_view_results([res], profiles=["test_repo"])

    out = _entity_lookup("report", model="sale.order", odoo_version=TEST_VERSION)
    assert "sale.action_report_saleorder" in out
    assert "qweb-pdf" in out
    assert "sale.report_saleorder" in out
