# tests/test_mcp_server.py
import os

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture(scope="module")
def seeded_neo4j(neo4j_driver):
    """Seed Neo4j với test data cho MCP server tests."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)

    base_mod = ModuleInfo("account", TEST_VERSION, "odoo_test", "/tmp", [], "")
    base_model = ModelInfo(
        name="account.move", module="account", odoo_version=TEST_VERSION,
        fields=[FieldInfo("name", "char", required=True),
                FieldInfo("amount_total", "float", compute="_compute_amount", stored=True)],
        methods=[MethodInfo("action_post", has_super_call=False)],
    )

    ext_mod = ModuleInfo("viin_account", TEST_VERSION, "acme_addons_test", "/tmp",
                          ["account"], "")
    ext_model = ModelInfo(
        name="account.move", module="viin_account", odoo_version=TEST_VERSION,
        inherit=["account.move"],
        fields=[FieldInfo("x_approval_state", "selection")],
        methods=[MethodInfo("action_post", has_super_call=True)],
    )

    writer.write_results([
        ParseResult(module=base_mod, models=[base_model]),
        ParseResult(module=ext_mod, models=[ext_model]),
    ])
    writer.close()
    yield
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)


@pytest.fixture
def mcp_tools(seeded_neo4j):
    """Import MCP business logic functions sau khi đã seed data."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_field, _resolve_method, _resolve_model
    return _resolve_model, _resolve_field, _resolve_method


def test_resolve_model_found(mcp_tools):
    resolve_model, _, _ = mcp_tools
    result = resolve_model("account.move", TEST_VERSION)
    assert "account.move" in result
    assert TEST_VERSION in result


def test_resolve_model_shows_module(mcp_tools):
    resolve_model, _, _ = mcp_tools
    result = resolve_model("account.move", TEST_VERSION)
    assert "account" in result


def test_resolve_model_not_found(mcp_tools):
    resolve_model, _, _ = mcp_tools
    result = resolve_model("nonexistent.model", TEST_VERSION)
    assert "Không tìm thấy" in result


def test_resolve_field_found(mcp_tools):
    _, resolve_field, _ = mcp_tools
    result = resolve_field("account.move", "amount_total", TEST_VERSION)
    assert "amount_total" in result
    assert "float" in result.lower()


def test_resolve_field_shows_compute(mcp_tools):
    _, resolve_field, _ = mcp_tools
    result = resolve_field("account.move", "amount_total", TEST_VERSION)
    assert "_compute_amount" in result


def test_resolve_field_not_found(mcp_tools):
    _, resolve_field, _ = mcp_tools
    result = resolve_field("account.move", "nonexistent_field", TEST_VERSION)
    assert "Không tìm thấy" in result


def test_resolve_method_found(mcp_tools):
    _, _, resolve_method = mcp_tools
    result = resolve_method("account.move", "action_post", TEST_VERSION)
    assert "action_post" in result


def test_resolve_method_not_found(mcp_tools):
    _, _, resolve_method = mcp_tools
    result = resolve_method("account.move", "nonexistent_method", TEST_VERSION)
    assert "Không tìm thấy" in result


def test_resolve_model_excludes_unresolved_parents(neo4j_driver):
    """Unresolved parent (placeholder) phải bị filter khỏi 'Kế thừa từ' output."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Dọn data cũ
    UNRESOLVED_VERSION = "98.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=UNRESOLVED_VERSION)

    # Seed: sale.order inherit từ ghost.mixin (chưa index) → tạo unresolved edge
    mod = ModuleInfo("sale", UNRESOLVED_VERSION, "odoo_test", "/tmp", [], "")
    model = ModelInfo(
        name="sale.order", module="sale", odoo_version=UNRESOLVED_VERSION,
        inherit=["ghost.mixin"],  # intentionally NOT seeded
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_model

    result = _resolve_model("sale.order", UNRESOLVED_VERSION)

    assert "sale.order" in result
    assert "ghost.mixin" not in result  # unresolved parent bị filter

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=UNRESOLVED_VERSION)
