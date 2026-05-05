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


# --- resolve_view tests ---
from src.indexer.models import (  # noqa: E402,I001
    ViewInfo, ViewParseResult, XPathInfo,
)


@pytest.fixture(scope="module")
def seeded_views(neo4j_driver):
    """Seed Neo4j với view data cho resolve_view tests."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    VIEW_VERSION = "97.0"  # version riêng để tránh conflict với seeded_neo4j (99.0, 98.0)

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=VIEW_VERSION)

    base_mod = ModuleInfo("sale", VIEW_VERSION, "odoo_test", "/tmp", [], "")
    ext_mod = ModuleInfo("viin_sale", VIEW_VERSION, "tvtma_test", "/tmp", ["sale"], "")

    base_view = ViewInfo(
        xmlid="sale.view_sale_order_form",
        name="sale.order.form",
        model="sale.order",
        module="sale",
        odoo_version=VIEW_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    ext_view = ViewInfo(
        xmlid="viin_sale.view_sale_order_form_inherit",
        name="viin sale form inherit",
        model="sale.order",
        module="viin_sale",
        odoo_version=VIEW_VERSION,
        view_type="form",
        mode="extension",
        inherit_xmlid="sale.view_sale_order_form",
        xpaths=[
            XPathInfo(expr="//field[@name='partner_id']", position="after"),
            XPathInfo(expr="//button[@name='action_confirm']", position="attributes"),
        ],
    )

    writer.write_view_results([
        ViewParseResult(module=base_mod, views=[base_view]),
        ViewParseResult(module=ext_mod, views=[ext_view]),
    ])
    writer.close()

    yield VIEW_VERSION

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=VIEW_VERSION)


@pytest.fixture
def view_tools(seeded_views):
    """Import _resolve_view sau khi đã seed data."""
    view_version = seeded_views
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_view
    return _resolve_view, view_version


def test_resolve_view_found(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "sale.view_sale_order_form" in result
    assert version in result
    assert "form" in result


def test_resolve_view_shows_model(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "sale.order" in result


def test_resolve_view_shows_extensions(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "viin_sale.view_sale_order_form_inherit" in result


def test_resolve_view_shows_xpaths(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "//field[@name='partner_id']" in result
    assert "after" in result


def test_resolve_view_extension_shows_parent(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("viin_sale.view_sale_order_form_inherit", version)
    assert "sale.view_sale_order_form" in result
    assert "Kế thừa từ" in result


def test_resolve_view_extension_shows_own_xpaths(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("viin_sale.view_sale_order_form_inherit", version)
    assert "//field[@name='partner_id']" in result
    assert "//button[@name='action_confirm']" in result


def test_resolve_view_not_found(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("nonexistent.view", version)
    assert "Không tìm thấy" in result
