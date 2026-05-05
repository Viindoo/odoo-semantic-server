# tests/test_writer_neo4j.py
import pytest

from src.indexer.models import (
    FieldInfo,
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    ParseResult,
    QWebInfo,
    ViewInfo,
    ViewParseResult,
    XPathInfo,
)
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

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
                  -[:INHERITS]->
                  (base:Model {name: 'sale.order', module: 'base_mod', odoo_version: $v})
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


def test_write_delegates_to_unresolved_logs_warning(writer, neo4j_driver, caplog):
    import logging
    hr_module = ModuleInfo(
        name="hr", odoo_version=TEST_VERSION,
        repo="hr_repo", path="/tmp", depends=[], version_raw="",
    )
    hr_model = ModelInfo(
        name="hr.employee", module="hr", odoo_version=TEST_VERSION,
        inherits={"res.users": "user_id"},  # res.users intentionally NOT seeded
    )

    with caplog.at_level(logging.WARNING, logger="src.indexer.writer_neo4j"):
        writer.write_results([ParseResult(module=hr_module, models=[hr_model])])

    assert "unresolved DELEGATES_TO" in caplog.text
    assert "hr.employee" in caplog.text
    assert "res.users" in caplog.text

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (:Model {name: 'hr.employee', odoo_version: $v})
                  -[r:DELEGATES_TO]->(:Model {name: 'res.users',
                                              module: '__unresolved__', odoo_version: $v})
            RETURN r.unresolved AS unresolved, r.via_field AS via_field
        """, v=TEST_VERSION).single()
    assert rec is not None
    assert rec["unresolved"] is True
    assert rec["via_field"] == "user_id"


def test_write_inherits_unresolved_logs_warning(writer, neo4j_driver, caplog):
    import logging
    ext_module = ModuleInfo(
        name="viin_mail", odoo_version=TEST_VERSION,
        repo="viin_repo", path="/tmp", depends=[], version_raw="",
    )
    ext_model = ModelInfo(
        name="sale.order", module="viin_mail", odoo_version=TEST_VERSION,
        inherit=["mail.thread"],  # mail.thread intentionally NOT seeded
    )

    with caplog.at_level(logging.WARNING, logger="src.indexer.writer_neo4j"):
        writer.write_results([ParseResult(module=ext_module, models=[ext_model])])

    assert "unresolved INHERITS" in caplog.text
    assert "sale.order" in caplog.text
    assert "mail.thread" in caplog.text

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (:Model {name: 'sale.order', module: 'viin_mail', odoo_version: $v})
                  -[r:INHERITS]->(:Model {name: 'mail.thread',
                                          module: '__unresolved__', odoo_version: $v})
            RETURN r.unresolved AS unresolved
        """, v=TEST_VERSION).single()
    assert rec is not None
    assert rec["unresolved"] is True


# --- View/QWeb writer tests ---


def make_view_parse_result(
    module_name: str,
    views: list | None = None,
    qweb: list | None = None,
) -> ViewParseResult:
    module = ModuleInfo(
        name=module_name, odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo", path="/tmp",
        depends=[], version_raw="",
    )
    return ViewParseResult(module=module, views=views or [], qweb=qweb or [])


def test_write_view_node(writer, neo4j_driver):
    view = ViewInfo(
        xmlid="sale.view_sale_order_form",
        name="sale.order.form",
        model="sale.order",
        module="sale",
        odoo_version=TEST_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    result = make_view_parse_result("sale", views=[view])
    writer.write_view_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (v:View {xmlid: $x, odoo_version: $v}) RETURN v",
            x="sale.view_sale_order_form", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert rec["v"]["type"] == "form"
    assert rec["v"]["mode"] == "primary"
    assert rec["v"]["model"] == "sale.order"


def test_write_view_xpaths_stored(writer, neo4j_driver):
    view = ViewInfo(
        xmlid="viin_sale.view_sale_order_form_inherit",
        name="viin inherit",
        model="sale.order",
        module="viin_sale",
        odoo_version=TEST_VERSION,
        view_type="form",
        mode="extension",
        inherit_xmlid="sale.view_sale_order_form",
        xpaths=[
            XPathInfo(expr="//field[@name='partner_id']", position="after"),
            XPathInfo(expr="//button[@name='action_confirm']", position="attributes"),
        ],
    )
    result = make_view_parse_result("viin_sale", views=[view])
    writer.write_view_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (v:View {xmlid: $x, odoo_version: $v}) RETURN v",
            x="viin_sale.view_sale_order_form_inherit", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert list(rec["v"]["xpaths_exprs"]) == [
        "//field[@name='partner_id']",
        "//button[@name='action_confirm']",
    ]
    assert list(rec["v"]["xpaths_positions"]) == ["after", "attributes"]


def test_write_inherits_view_edge(writer, neo4j_driver):
    base_view = ViewInfo(
        xmlid="sale.view_sale_order_form",
        name="base", model="sale.order", module="sale",
        odoo_version=TEST_VERSION, view_type="form",
        mode="primary", inherit_xmlid=None,
    )
    ext_view = ViewInfo(
        xmlid="viin_sale.view_sale_order_form_inherit",
        name="ext", model="sale.order", module="viin_sale",
        odoo_version=TEST_VERSION, view_type="form",
        mode="extension", inherit_xmlid="sale.view_sale_order_form",
    )
    writer.write_view_results([
        make_view_parse_result("sale", views=[base_view]),
        make_view_parse_result("viin_sale", views=[ext_view]),
    ])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (ext:View {xmlid: $ext_xmlid, odoo_version: $v})
                  -[:INHERITS_VIEW]->
                  (base:View {xmlid: $base_xmlid, odoo_version: $v})
            RETURN count(*) AS cnt
        """, ext_xmlid="viin_sale.view_sale_order_form_inherit",
             base_xmlid="sale.view_sale_order_form", v=TEST_VERSION).single()
    assert rec["cnt"] == 1


def test_write_inherits_view_unresolved(writer, neo4j_driver, caplog):
    import logging
    ext_view = ViewInfo(
        xmlid="viin_sale.view_sale_order_form_inherit",
        name="ext", model="sale.order", module="viin_sale",
        odoo_version=TEST_VERSION, view_type="form",
        mode="extension", inherit_xmlid="sale.view_sale_order_form",  # NOT seeded
    )
    with caplog.at_level(logging.WARNING, logger="src.indexer.writer_neo4j"):
        writer.write_view_results([make_view_parse_result("viin_sale", views=[ext_view])])

    assert "unresolved INHERITS_VIEW" in caplog.text
    assert "viin_sale.view_sale_order_form_inherit" in caplog.text

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (ext:View {xmlid: $ext_xmlid, odoo_version: $v})
                  -[r:INHERITS_VIEW]->(:View {xmlid: $base_xmlid, module: '__unresolved__'})
            RETURN r.unresolved AS unresolved
        """, ext_xmlid="viin_sale.view_sale_order_form_inherit",
             base_xmlid="sale.view_sale_order_form", v=TEST_VERSION).single()
    assert rec is not None
    assert rec["unresolved"] is True


def test_write_qweb_node(writer, neo4j_driver):
    q = QWebInfo(
        xmlid="sale.sale_order_portal",
        module="sale",
        odoo_version=TEST_VERSION,
    )
    result = make_view_parse_result("sale", qweb=[q])
    writer.write_view_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (t:QWebTmpl {xmlid: $x, odoo_version: $v}) RETURN t",
            x="sale.sale_order_portal", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert rec["t"]["module"] == "sale"


def test_write_extends_tmpl_edge(writer, neo4j_driver):
    base_q = QWebInfo(xmlid="sale.portal_tmpl", module="sale", odoo_version=TEST_VERSION)
    ext_q = QWebInfo(
        xmlid="viin_sale.portal_tmpl_inherit", module="viin_sale",
        odoo_version=TEST_VERSION, inherit_xmlid="sale.portal_tmpl",
    )
    writer.write_view_results([
        make_view_parse_result("sale", qweb=[base_q]),
        make_view_parse_result("viin_sale", qweb=[ext_q]),
    ])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (ext:QWebTmpl {xmlid: $ext, odoo_version: $v})
                  -[:EXTENDS_TMPL]->
                  (base:QWebTmpl {xmlid: $base, odoo_version: $v})
            RETURN count(*) AS cnt
        """, ext="viin_sale.portal_tmpl_inherit",
             base="sale.portal_tmpl", v=TEST_VERSION).single()
    assert rec["cnt"] == 1


def test_view_xpaths_arrays_length_invariant(writer, neo4j_driver):
    """xpaths_exprs và xpaths_positions phải luôn cùng độ dài (parallel array invariant)."""
    view = ViewInfo(
        xmlid="sale.view_xpaths_invariant_test",
        name="invariant test", model="sale.order", module="sale",
        odoo_version=TEST_VERSION, view_type="form", mode="extension",
        inherit_xmlid="sale.base_view",
        xpaths=[
            XPathInfo(expr="//field[@name='a']", position="after"),
            XPathInfo(expr="//field[@name='b']", position="inside"),
            XPathInfo(expr="//button[@name='c']", position="attributes"),
        ],
    )
    writer.write_view_results([make_view_parse_result("sale", views=[view])])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (v:View {xmlid: $x, odoo_version: $ver})
            RETURN size(v.xpaths_exprs) AS exprs_count,
                   size(v.xpaths_positions) AS pos_count
        """, x="sale.view_xpaths_invariant_test", ver=TEST_VERSION).single()
    assert rec["exprs_count"] == rec["pos_count"] == 3


def test_write_view_indexes_created(writer, neo4j_driver):
    """Verify indexes for View and QWebTmpl exist after setup_indexes()."""
    with neo4j_driver.session() as session:
        indexes = session.run("SHOW INDEXES YIELD labelsOrTypes, properties").data()
    view_index = any(
        "View" in (r.get("labelsOrTypes") or [])
        for r in indexes
    )
    qweb_index = any(
        "QWebTmpl" in (r.get("labelsOrTypes") or [])
        for r in indexes
    )
    assert view_index, "Missing index on :View"
    assert qweb_index, "Missing index on :QWebTmpl"
