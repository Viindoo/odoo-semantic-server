# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_writer_neo4j_comodel.py
"""M10.5 P1 — integration tests: comodel_name persisted to Neo4j Field nodes."""
import os

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter connected to test DB, using TEST_VERSION."""
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _make_result(module_name: str, model_name: str, fields: list[FieldInfo]) -> ParseResult:
    module = ModuleInfo(
        name=module_name, odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo", path="/tmp",
        depends=[], version_raw="",
    )
    model = ModelInfo(
        name=model_name, module=module_name, odoo_version=TEST_VERSION,
        fields=fields,
        methods=[MethodInfo(name="action_confirm", has_super_call=False, decorators=[])],
    )
    return ParseResult(module=module, models=[model])


def test_comodel_name_persisted_for_relational_field(writer, neo4j_driver):
    """Many2one with comodel_name → field node has f.comodel_name set in Neo4j."""
    result = _make_result("sale", "sale.order", fields=[
        FieldInfo(name="partner_id", ttype="many2one", comodel_name="res.partner"),
        FieldInfo(name="line_ids", ttype="one2many", comodel_name="sale.order.line"),
        FieldInfo(name="tag_ids", ttype="many2many", comodel_name="crm.tag"),
    ])
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rows = session.run(
            "MATCH (f:Field {model: $m, odoo_version: $v}) "
            "RETURN f.name AS name, f.comodel_name AS comodel",
            m="sale.order", v=TEST_VERSION,
        ).data()

    field_map = {r["name"]: r["comodel"] for r in rows}
    assert field_map["partner_id"] == "res.partner"
    assert field_map["line_ids"] == "sale.order.line"
    assert field_map["tag_ids"] == "crm.tag"


def test_comodel_name_none_for_non_relational_field(writer, neo4j_driver):
    """Non-relational field → f.comodel_name is null in Neo4j."""
    result = _make_result("sale", "sale.product", fields=[
        FieldInfo(name="name", ttype="char", comodel_name=None),
        FieldInfo(name="price", ttype="float", comodel_name=None),
    ])
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rows = session.run(
            "MATCH (f:Field {model: $m, odoo_version: $v}) "
            "RETURN f.name AS name, f.comodel_name AS comodel",
            m="sale.product", v=TEST_VERSION,
        ).data()

    field_map = {r["name"]: r["comodel"] for r in rows}
    assert field_map["name"] is None
    assert field_map["price"] is None


def test_comodel_name_idempotent_reindex(writer, neo4j_driver):
    """Re-writing the same relational field is idempotent — comodel_name unchanged."""
    fields = [FieldInfo(name="partner_id", ttype="many2one", comodel_name="res.partner")]
    result = _make_result("crm", "crm.lead", fields=fields)

    writer.write_results([result])
    writer.write_results([result])  # second write — must not duplicate or corrupt

    with neo4j_driver.session() as session:
        rows = session.run(
            "MATCH (f:Field {name: 'partner_id', model: $m, odoo_version: $v}) "
            "RETURN f.comodel_name AS comodel",
            m="crm.lead", v=TEST_VERSION,
        ).data()

    assert len(rows) == 1, "Field node must be unique (MERGE idempotent)"
    assert rows[0]["comodel"] == "res.partner"
