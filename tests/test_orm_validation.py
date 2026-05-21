# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_orm_validation.py
"""M10.5 P2 — integration tests for the 4 ORM-validation MCP tools.

Seeds a small ORM graph (sale.order → res.partner → res.country, a mail.thread
mixin via INHERITS, a compute method with @api.depends) and exercises
resolve_orm_chain / validate_domain / validate_depends / validate_relation
against it. Requires Neo4j (testcontainers).
"""
import os

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture(scope="module")
def seeded_orm_graph(neo4j_driver):
    """Seed a small but realistic ORM graph at TEST_VERSION."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)

    base = ModuleInfo("base", TEST_VERSION, "odoo_test", "/tmp", [], "")
    mail = ModuleInfo("mail", TEST_VERSION, "odoo_test", "/tmp", [], "")
    sale = ModuleInfo("sale", TEST_VERSION, "odoo_test", "/tmp", ["base", "mail"], "")

    res_country = ModelInfo(
        name="res.country", module="base", odoo_version=TEST_VERSION,
        fields=[FieldInfo("code", "char"), FieldInfo("name", "char")],
    )
    res_users = ModelInfo(
        name="res.users", module="base", odoo_version=TEST_VERSION,
        fields=[FieldInfo("login", "char")],
    )
    res_partner = ModelInfo(
        name="res.partner", module="base", odoo_version=TEST_VERSION,
        fields=[
            FieldInfo("name", "char"),
            FieldInfo("country_id", "many2one", comodel_name="res.country"),
        ],
    )
    mail_message = ModelInfo(
        name="mail.message", module="mail", odoo_version=TEST_VERSION,
        fields=[FieldInfo("body", "char")],
    )
    mail_thread = ModelInfo(
        name="mail.thread", module="mail", odoo_version=TEST_VERSION,
        fields=[FieldInfo("message_ids", "one2many", comodel_name="mail.message")],
    )
    sale_order_line = ModelInfo(
        name="sale.order.line", module="sale", odoo_version=TEST_VERSION,
        fields=[
            FieldInfo("price_subtotal", "float"),
            FieldInfo("product_id", "many2one", comodel_name="product.product"),
        ],
    )
    sale_order = ModelInfo(
        name="sale.order", module="sale", odoo_version=TEST_VERSION,
        inherit=["mail.thread"],
        fields=[
            FieldInfo("partner_id", "many2one", comodel_name="res.partner"),
            FieldInfo("order_line", "one2many", comodel_name="sale.order.line"),
            FieldInfo("amount_total", "float", compute="_compute_amount", stored=True),
            FieldInfo("state", "selection"),
        ],
        methods=[
            MethodInfo("_compute_amount", decorators=["api.depends"],
                       depends=["partner_id.country_id", "order_line.price_subtotal"]),
            MethodInfo("_compute_bad", decorators=["api.depends"],
                       depends=["partner_id.nonexistent", "id"]),
            MethodInfo("action_confirm"),
        ],
    )

    writer.write_results([
        ParseResult(module=base, models=[res_country, res_users, res_partner]),
        ParseResult(module=mail, models=[mail_message, mail_thread]),
        ParseResult(module=sale, models=[sale_order_line, sale_order]),
    ])
    writer.close()
    yield
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)


@pytest.fixture
def orm_tools(seeded_orm_graph):
    """Re-import server with test Neo4j env so _get_driver() hits the test DB."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import (
        _resolve_orm_chain,
        _validate_depends,
        _validate_domain,
        _validate_relation,
    )
    return _resolve_orm_chain, _validate_domain, _validate_depends, _validate_relation


# --- resolve_orm_chain ---------------------------------------------------

def test_orm_chain_full_path_ok(orm_tools):
    resolve_orm_chain, *_ = orm_tools
    out = resolve_orm_chain("sale.order", "partner_id.country_id.code", TEST_VERSION)
    assert out.startswith(f"sale.order.partner_id.country_id.code (Odoo {TEST_VERSION})")
    assert "sale.order.partner_id : many2one -> res.partner" in out
    assert "res.partner.country_id : many2one -> res.country" in out
    assert "res.country.code : char (terminal)" in out
    assert "└─ Next:" in out
    assert "BROKEN" not in out


def test_orm_chain_broken_missing(orm_tools):
    resolve_orm_chain, *_ = orm_tools
    out = resolve_orm_chain("sale.order", "partner_id.country_id.nope", TEST_VERSION)
    assert "BROKEN at step 3/3" in out
    assert "field 'nope' not found on res.country" in out


def test_orm_chain_broken_not_relational(orm_tools):
    resolve_orm_chain, *_ = orm_tools
    out = resolve_orm_chain("sale.order", "amount_total.foo", TEST_VERSION)
    assert "BROKEN at step 1/2" in out
    assert "not relational" in out


def test_orm_chain_inherited_field_via_mixin(orm_tools):
    """message_ids lives on mail.thread; reachable from sale.order via INHERITS."""
    resolve_orm_chain, *_ = orm_tools
    out = resolve_orm_chain("sale.order", "message_ids", TEST_VERSION)
    assert "sale.order.message_ids : one2many -> mail.message (terminal)" in out
    assert "BROKEN" not in out


def test_orm_chain_magic_field_comodel(orm_tools):
    """create_uid is a magic many2one -> res.users; login resolves on res.users."""
    resolve_orm_chain, *_ = orm_tools
    out = resolve_orm_chain("sale.order", "create_uid.login", TEST_VERSION)
    assert "sale.order.create_uid : many2one -> res.users" in out
    assert "res.users.login : char (terminal)" in out


def test_orm_chain_empty_path(orm_tools):
    resolve_orm_chain, *_ = orm_tools
    out = resolve_orm_chain("sale.order", "", TEST_VERSION)
    assert out.startswith("Error:")


# --- validate_domain -----------------------------------------------------

def test_validate_domain_all_ok(orm_tools):
    _, validate_domain, *_ = orm_tools
    out = validate_domain(
        "sale.order",
        "[('partner_id.country_id', '=', 1), ('amount_total', '>', 100)]",
        TEST_VERSION,
    )
    assert "OK" in out.splitlines()[0]
    assert "ERROR" not in out


def test_validate_domain_bad_field(orm_tools):
    _, validate_domain, *_ = orm_tools
    out = validate_domain("sale.order", "[('partner_id.nope', '=', 1)]", TEST_VERSION)
    assert "ERROR" in out
    assert "field 'nope' not found" in out


def test_validate_domain_bad_operator(orm_tools):
    _, validate_domain, *_ = orm_tools
    out = validate_domain("sale.order", "[('amount_total', 'badop', 1)]", TEST_VERSION)
    assert "operator 'badop' not valid" in out


def test_validate_domain_skips_logical_connectors(orm_tools):
    _, validate_domain, *_ = orm_tools
    out = validate_domain(
        "sale.order",
        "['|', ('partner_id', '=', 1), ('amount_total', '>', 0)]",
        TEST_VERSION,
    )
    assert "logical operator '|' : skipped" in out
    assert "ERROR" not in out


def test_validate_domain_malformed(orm_tools):
    _, validate_domain, *_ = orm_tools
    out = validate_domain("sale.order", "not [ a valid (((", TEST_VERSION)
    assert out.startswith("Error:")


# --- validate_depends ----------------------------------------------------

def test_validate_depends_all_ok(orm_tools):
    *_, validate_depends, _ = orm_tools
    out = validate_depends("sale.order", "_compute_amount", TEST_VERSION)
    assert "all dependencies valid" in out
    assert "'partner_id.country_id' : OK" in out
    assert "'order_line.price_subtotal' : OK" in out


def test_validate_depends_id_and_missing(orm_tools):
    *_, validate_depends, _ = orm_tools
    out = validate_depends("sale.order", "_compute_bad", TEST_VERSION)
    assert "cannot depend on 'id'" in out
    assert "field 'nonexistent' not found on res.partner" in out


def test_validate_depends_no_depends(orm_tools):
    *_, validate_depends, _ = orm_tools
    out = validate_depends("sale.order", "action_confirm", TEST_VERSION)
    assert "no @api.depends found" in out


def test_validate_depends_method_not_found(orm_tools):
    *_, validate_depends, _ = orm_tools
    out = validate_depends("sale.order", "_compute_ghost", TEST_VERSION)
    assert "not found" in out


# --- validate_relation ---------------------------------------------------

def test_validate_relation_ok(orm_tools):
    *_, validate_relation = orm_tools
    out = validate_relation("sale.order", "partner_id", "res.partner", TEST_VERSION)
    assert "OK" in out
    assert "partner_id is many2one -> res.partner" in out


def test_validate_relation_mismatch(orm_tools):
    *_, validate_relation = orm_tools
    out = validate_relation("sale.order", "partner_id", "res.users", TEST_VERSION)
    assert "MISMATCH" in out
    assert "res.partner" in out  # reports the actual comodel


def test_validate_relation_not_relational(orm_tools):
    *_, validate_relation = orm_tools
    out = validate_relation("sale.order", "amount_total", "res.partner", TEST_VERSION)
    assert "not a relational field" in out


def test_validate_relation_field_typo_suggestion(orm_tools):
    *_, validate_relation = orm_tools
    out = validate_relation("sale.order", "parner_id", "res.partner", TEST_VERSION)
    assert "not found" in out
    assert "did you mean 'partner_id'" in out
