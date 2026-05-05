# tests/test_writer_neo4j.py
import pytest
from tests.conftest import TEST_VERSION
from src.indexer.models import ModuleInfo, ModelInfo, FieldInfo, MethodInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter kết nối tới test DB, dùng version TEST_VERSION."""
    import os
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def make_parse_result(module_name: str, model_name: str) -> ParseResult:
    module = ModuleInfo(
        name=module_name, odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo", path="/tmp",
        depends=[], version_raw="",
    )
    model = ModelInfo(
        name=model_name, module=module_name, odoo_version=TEST_VERSION,
        fields=[
            FieldInfo(name="name", ttype="char", required=True),
            FieldInfo(name="amount", ttype="float", compute="_compute", stored=False),
        ],
        methods=[
            MethodInfo(name="action_confirm", has_super_call=True, decorators=[]),
        ],
    )
    return ParseResult(module=module, models=[model])


def test_write_module_node(writer, neo4j_driver):
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m",
            n="sale", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert rec["m"]["repo"] == "sale_repo"


def test_write_model_node(writer, neo4j_driver):
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Model {name: $n, odoo_version: $v}) RETURN m",
            n="sale.order", v=TEST_VERSION
        ).single()
    assert rec is not None


def test_write_field_nodes(writer, neo4j_driver):
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result])

    with neo4j_driver.session() as session:
        fields = session.run(
            "MATCH (f:Field {model: $m, odoo_version: $v}) RETURN f.name as name",
            m="sale.order", v=TEST_VERSION
        ).data()
    field_names = {r["name"] for r in fields}
    assert "name" in field_names
    assert "amount" in field_names


def test_write_method_node(writer, neo4j_driver):
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Method {name: $n, model: $model, odoo_version: $v}) RETURN m",
            n="action_confirm", model="sale.order", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert rec["m"]["has_super_call"] is True


def test_write_inherits_edge(writer, neo4j_driver):
    base_module = ModuleInfo(
        name="base_mod", odoo_version=TEST_VERSION,
        repo="base_repo", path="/tmp", depends=[], version_raw="",
    )
    base_model = ModelInfo(
        name="sale.order", module="base_mod", odoo_version=TEST_VERSION,
    )
    ext_module = ModuleInfo(
        name="ext_mod", odoo_version=TEST_VERSION,
        repo="ext_repo", path="/tmp", depends=["base_mod"], version_raw="",
    )
    ext_model = ModelInfo(
        name="sale.order", module="ext_mod", odoo_version=TEST_VERSION,
        inherit=["sale.order"],
    )
    writer.write_results([
        ParseResult(module=base_module, models=[base_model]),
        ParseResult(module=ext_module, models=[ext_model]),
    ])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (ext:Model {name: 'sale.order', module: 'ext_mod', odoo_version: $v})
                  -[:INHERITS]->(base:Model {name: 'sale.order', module: 'base_mod', odoo_version: $v})
            RETURN count(*) AS cnt
        """, v=TEST_VERSION).single()
    assert rec["cnt"] == 1


def test_write_delegates_to_edge(writer, neo4j_driver):
    # Seed res.users first — topo-sort guarantees base is indexed before hr
    base_module = ModuleInfo(
        name="base", odoo_version=TEST_VERSION,
        repo="base_repo", path="/tmp", depends=[], version_raw="",
    )
    base_model = ModelInfo(
        name="res.users", module="base", odoo_version=TEST_VERSION,
    )
    hr_module = ModuleInfo(
        name="hr", odoo_version=TEST_VERSION,
        repo="hr_repo", path="/tmp", depends=["base"], version_raw="",
    )
    hr_model = ModelInfo(
        name="hr.employee", module="hr", odoo_version=TEST_VERSION,
        inherits={"res.users": "user_id"},
    )
    writer.write_results([
        ParseResult(module=base_module, models=[base_model]),
        ParseResult(module=hr_module, models=[hr_model]),
    ])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (:Model {name: 'hr.employee', odoo_version: $v})
                  -[r:DELEGATES_TO]->(:Model {name: 'res.users', odoo_version: $v})
            RETURN r.via_field as via_field
        """, v=TEST_VERSION).single()
    assert rec is not None
    assert rec["via_field"] == "user_id"
