# tests/test_writer_neo4j.py
import pytest

from src.indexer.models import (
    FieldInfo,
    JSGraphResult,
    JSPatchInfo,
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    OWLCompInfo,
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


def test_write_extends_tmpl_unresolved(writer, neo4j_driver, caplog):
    """EXTENDS_TMPL tới base chưa index → placeholder + edge unresolved=true."""
    import logging

    ext_q = QWebInfo(
        xmlid="viin_sale.portal_tmpl_orphan", module="viin_sale",
        odoo_version=TEST_VERSION, inherit_xmlid="missing.portal_tmpl",
    )
    with caplog.at_level(logging.WARNING, logger="src.indexer.writer_neo4j"):
        writer.write_view_results([make_view_parse_result("viin_sale", qweb=[ext_q])])

    assert "unresolved EXTENDS_TMPL" in caplog.text

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (ext:QWebTmpl {xmlid: $ext, odoo_version: $v})
                  -[r:EXTENDS_TMPL {unresolved: true}]->
                  (ph:QWebTmpl {xmlid: $base, module: '__unresolved__', odoo_version: $v})
            RETURN ph.unresolved AS flag
        """, ext="viin_sale.portal_tmpl_orphan",
             base="missing.portal_tmpl", v=TEST_VERSION).single()
    assert rec is not None, "Placeholder node + unresolved edge must be created"
    assert rec["flag"] is True


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


def test_write_view_creates_targets_model_edge(writer, neo4j_driver):
    """View targeting model creates TARGETS_MODEL edge to all Model nodes with same name."""
    # Seed Model nodes first
    model_result = make_parse_result("sale", "sale.order")
    writer.write_results([model_result])

    # Now write View targeting that model
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

    # Assert TARGETS_MODEL edge exists
    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
                  -[:TARGETS_MODEL]->
                  (m:Model {name: $model_name, odoo_version: $ver})
            RETURN count(*) AS cnt
        """, xmlid="sale.view_sale_order_form",
             model_name="sale.order", ver=TEST_VERSION).single()
    assert rec["cnt"] >= 1, "TARGETS_MODEL edge should exist"


def test_write_view_targets_model_multiple_module_nodes(writer, neo4j_driver):
    """When same model exists in multiple modules, View.TARGETS_MODEL → all nodes."""
    # Seed base model in 'sale' module
    base_result = make_parse_result("sale", "sale.order")
    writer.write_results([base_result])

    # Seed extension model in 'viin_sale' module
    ext_module = ModuleInfo(
        name="viin_sale", odoo_version=TEST_VERSION,
        repo="viin_sale_repo", path="/tmp", depends=["sale"], version_raw="",
    )
    ext_model = ModelInfo(
        name="sale.order", module="viin_sale", odoo_version=TEST_VERSION,
        inherit=["sale.order"],
    )
    ext_result = ParseResult(module=ext_module, models=[ext_model])
    writer.write_results([ext_result])

    # Write View targeting sale.order
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

    # Assert TARGETS_MODEL edges exist to both module nodes
    with neo4j_driver.session() as session:
        count_rec = session.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
                  -[:TARGETS_MODEL]->
                  (m:Model {name: $model_name, odoo_version: $ver})
            RETURN count(*) AS cnt
        """, xmlid="sale.view_sale_order_form",
             model_name="sale.order", ver=TEST_VERSION).single()
        # Should have edges to both Model nodes (one per module)
        assert count_rec["cnt"] >= 2, f"Expected >=2 TARGETS_MODEL edges, got {count_rec['cnt']}"


def test_write_view_no_target_when_model_missing(writer, neo4j_driver):
    """View targeting missing model skips silently (no placeholder TARGETS_MODEL edge)."""
    # Write View with model that was never indexed
    view = ViewInfo(
        xmlid="custom.view_nonexistent_model",
        name="nonexistent.view",
        model="nonexistent.model",
        module="custom",
        odoo_version=TEST_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    result = make_view_parse_result("custom", views=[view])
    writer.write_view_results([result])

    # Assert no TARGETS_MODEL edge exists (skip silently)
    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
                  -[:TARGETS_MODEL]->
                  (m:Model)
            RETURN count(*) AS cnt
        """, xmlid="custom.view_nonexistent_model",
             ver=TEST_VERSION).single()
    assert rec["cnt"] == 0, "No TARGETS_MODEL edge should be created for missing model"


# --- JS Graph writer tests ---


def make_js_module(module_name: str) -> ModuleInfo:
    return ModuleInfo(
        name=module_name, odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo", path="/tmp",
        depends=[], version_raw="",
    )


def test_write_js_graph_creates_jspatch_node(writer, neo4j_driver):
    """JSPatch node is created with correct composite key and properties."""
    module = make_js_module("sale")
    patch = JSPatchInfo(
        target="SaleOrderWidget",
        patch_name="sale_patch",
        module="sale",
        odoo_version=TEST_VERSION,
        era="patch",
        file_path="/sale/static/src/js/sale.js",
    )
    result = JSGraphResult(module=module, patches=[patch])
    writer.write_js_graph_results([result])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (j:JSPatch {target: $target, patch_name: $pn,
                              module: $mod, odoo_version: $v})
            RETURN j
        """, target="SaleOrderWidget", pn="sale_patch",
             mod="sale", v=TEST_VERSION).single()
    assert rec is not None
    assert rec["j"]["era"] == "patch"
    assert rec["j"]["file_path"] == "/sale/static/src/js/sale.js"


def test_write_js_graph_creates_owlcomp_node(writer, neo4j_driver):
    """OWLComp node is created with correct composite key and properties."""
    module = make_js_module("sale")
    comp = OWLCompInfo(
        name="SaleOrderWidget",
        module="sale",
        odoo_version=TEST_VERSION,
        template="sale.SaleOrderWidget",
        extends="Component",
        bound_model=None,
        file_path="/sale/static/src/components/sale_widget.js",
    )
    result = JSGraphResult(module=module, components=[comp])
    writer.write_js_graph_results([result])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (c:OWLComp {name: $name, module: $mod, odoo_version: $v})
            RETURN c
        """, name="SaleOrderWidget", mod="sale", v=TEST_VERSION).single()
    assert rec is not None
    assert rec["c"]["template"] == "sale.SaleOrderWidget"
    assert rec["c"]["extends"] == "Component"
    assert rec["c"]["file_path"] == "/sale/static/src/components/sale_widget.js"


def test_write_js_graph_patches_edge_resolved(writer, neo4j_driver):
    """PATCHES edge is created without unresolved flag when OWLComp target exists."""
    module = make_js_module("viin_sale")
    comp = OWLCompInfo(
        name="MyComp",
        module="viin_sale",
        odoo_version=TEST_VERSION,
        file_path="/viin_sale/static/src/components/my_comp.js",
    )
    patch = JSPatchInfo(
        target="MyComp",
        patch_name="my_comp_patch",
        module="viin_sale",
        odoo_version=TEST_VERSION,
        era="patch",
        file_path="/viin_sale/static/src/js/patch.js",
    )
    result = JSGraphResult(module=module, patches=[patch], components=[comp])
    writer.write_js_graph_results([result])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (j:JSPatch {target: $target, patch_name: $pn,
                              module: $mod, odoo_version: $v})
                  -[r:PATCHES]->
                  (c:OWLComp {name: $target, odoo_version: $v})
            RETURN r
        """, target="MyComp", pn="my_comp_patch",
             mod="viin_sale", v=TEST_VERSION).single()
    assert rec is not None
    assert rec["r"].get("unresolved") is None or rec["r"].get("unresolved") is False


def test_write_js_graph_patches_edge_unresolved(writer, neo4j_driver):
    """When PATCHES target OWLComp doesn't exist, placeholder is created with unresolved=true."""
    module = make_js_module("viin_sale")
    patch = JSPatchInfo(
        target="Missing",
        patch_name="missing_patch",
        module="viin_sale",
        odoo_version=TEST_VERSION,
        era="patch",
        file_path="/viin_sale/static/src/js/patch.js",
    )
    result = JSGraphResult(module=module, patches=[patch])
    writer.write_js_graph_results([result])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (j:JSPatch {target: $target, patch_name: $pn,
                              module: $mod, odoo_version: $v})
                  -[r:PATCHES {unresolved: true}]->
                  (ph:OWLComp {name: $target, module: '__unresolved__', odoo_version: $v})
            RETURN ph.unresolved AS flag
        """, target="Missing", pn="missing_patch",
             mod="viin_sale", v=TEST_VERSION).single()
    assert rec is not None
    assert rec["flag"] is True


def test_write_js_graph_extends_only_when_match(writer, neo4j_driver):
    """EXTENDS edge is NOT created when parent OWLComp doesn't exist (no placeholder)."""
    module = make_js_module("sale")
    comp = OWLCompInfo(
        name="Child",
        module="sale",
        odoo_version=TEST_VERSION,
        extends="Parent",  # Parent does not exist
        file_path="/sale/static/src/components/child.js",
    )
    result = JSGraphResult(module=module, components=[comp])
    writer.write_js_graph_results([result])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (c:OWLComp {name: 'Child', odoo_version: $v})
                  -[:EXTENDS]->(:OWLComp)
            RETURN count(*) AS cnt
        """, v=TEST_VERSION).single()
    assert rec["cnt"] == 0, "No EXTENDS edge or placeholder when parent missing"

    # Also verify no 'Parent' placeholder was created
    with neo4j_driver.session() as session:
        ph = session.run("""
            MATCH (c:OWLComp {name: 'Parent', odoo_version: $v})
            RETURN count(*) AS cnt
        """, v=TEST_VERSION).single()
    assert ph["cnt"] == 0, "No placeholder for unresolved EXTENDS parent"


def test_write_js_graph_bound_to_model(writer, neo4j_driver):
    """BOUND_TO edge is created when bound_model exists as a Model node."""
    # First seed the Model
    model_result = make_parse_result("sale", "sale.order")
    writer.write_results([model_result])

    module = make_js_module("sale")
    comp = OWLCompInfo(
        name="SaleOrderComp",
        module="sale",
        odoo_version=TEST_VERSION,
        bound_model="sale.order",
        file_path="/sale/static/src/components/sale_order_comp.js",
    )
    result = JSGraphResult(module=module, components=[comp])
    writer.write_js_graph_results([result])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (c:OWLComp {name: $comp_name, odoo_version: $v})
                  -[:BOUND_TO]->
                  (m:Model {name: $model_name, odoo_version: $v})
            RETURN count(*) AS cnt
        """, comp_name="SaleOrderComp", model_name="sale.order",
             v=TEST_VERSION).single()
    assert rec["cnt"] >= 1, "BOUND_TO edge should exist when model is indexed"


# --- CoreSymbol writer tests (M4.5 WI2.3, per ADR-0002) -------------------

from src.indexer.diff_engine import DiffResult  # noqa: E402
from src.indexer.models import CoreSymbolInfo  # noqa: E402


def test_write_core_symbol_node(writer, neo4j_driver):
    """write_core_symbols MERGEs a CoreSymbol node with composite key."""
    sym = CoreSymbolInfo(
        qualified_name="odoo.tools.safe_eval.safe_eval",
        kind="function",
        odoo_version=TEST_VERSION,
        signature="safe_eval(expr, context)",
        file_path="/odoo/tools/safe_eval.py",
        line=42,
        status="stable",
    )
    writer.write_core_symbols([sym])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
            RETURN cs
        """, qn=sym.qualified_name, v=TEST_VERSION).single()
    assert rec is not None
    assert rec["cs"]["kind"] == "function"
    assert rec["cs"]["signature"] == "safe_eval(expr, context)"
    assert rec["cs"]["status"] == "stable"


def test_write_core_symbol_idempotent_on_repeat(writer, neo4j_driver):
    """MERGE on (qualified_name, odoo_version) — repeat write doesn't duplicate."""
    sym = CoreSymbolInfo(
        qualified_name="odoo.fields.Float",
        kind="field_type",
        odoo_version=TEST_VERSION,
        status="stable",
    )
    writer.write_core_symbols([sym, sym])
    writer.write_core_symbols([sym])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
            RETURN count(cs) AS c
        """, qn=sym.qualified_name, v=TEST_VERSION).single()
    assert rec["c"] == 1


def test_write_diff_replaced_by_edge_when_target_exists(writer, neo4j_driver):
    """REPLACED_BY edge MERGEd when both old and new symbol nodes exist."""
    old = CoreSymbolInfo(
        qualified_name="odoo.fields.Field.group_operator",
        kind="field_type", odoo_version=TEST_VERSION,
        status="removed",
        replacement_qname="odoo.fields.Field.aggregator",
    )
    new = CoreSymbolInfo(
        qualified_name="odoo.fields.Field.aggregator",
        kind="field_type", odoo_version=TEST_VERSION,
        status="added",
    )
    writer.write_core_symbols([old, new])
    diff = DiffResult(
        replaced=[(
            "odoo.fields.Field.group_operator",
            "odoo.fields.Field.aggregator",
        )],
    )
    writer.write_diff_edges(diff, from_version=TEST_VERSION, to_version=TEST_VERSION)

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (a:CoreSymbol {qualified_name: $a_qn, odoo_version: $v})
                  -[:REPLACED_BY]->
                  (b:CoreSymbol {qualified_name: $b_qn, odoo_version: $v})
            RETURN count(*) AS c
        """, a_qn=old.qualified_name, b_qn=new.qualified_name,
             v=TEST_VERSION).single()
    assert rec["c"] == 1


def test_setup_indexes_creates_core_symbol_index(writer, neo4j_driver):
    """setup_indexes creates an index on (CoreSymbol.qualified_name, odoo_version)."""
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        indexes = session.run("SHOW INDEXES").data()
    labels_props = [
        (i.get("labelsOrTypes") or [], i.get("properties") or [])
        for i in indexes
    ]
    found = any(
        "CoreSymbol" in (lbls or [])
        and "qualified_name" in (props or [])
        and "odoo_version" in (props or [])
        for lbls, props in labels_props
    )
    assert found, f"CoreSymbol index missing. Got: {labels_props}"


# --- LintRule writer tests (M4.5 WI3) ----------------------------------

from src.indexer.models import LintRuleInfo  # noqa: E402


def test_write_lint_rule_node(writer, neo4j_driver):
    """write_lint_rules persists a LintRule node with composite key + props."""
    rule = LintRuleInfo(
        rule_id="E8502",
        odoo_version=TEST_VERSION,
        kind="pylint-odoo",
        message="Bad gettext usage",
        severity="error",
    )
    writer.write_lint_rules([rule])
    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (l:LintRule {rule_id: $rid, odoo_version: $v}) RETURN l
        """, rid="E8502", v=TEST_VERSION).single()
    assert rec is not None
    assert rec["l"]["kind"] == "pylint-odoo"
    assert rec["l"]["severity"] == "error"


def test_write_lint_rule_checks_edge_to_core_symbol(writer, neo4j_driver):
    """When rule.core_symbol_qname is set + target exists → CHECKS edge MERGEd."""
    sym = CoreSymbolInfo(
        qualified_name="odoo.models.BaseModel.unlink",
        kind="orm_method", odoo_version=TEST_VERSION,
    )
    writer.write_core_symbols([sym])
    rule = LintRuleInfo(
        rule_id="E8401",
        odoo_version=TEST_VERSION,
        kind="pylint-odoo",
        core_symbol_qname="odoo.models.BaseModel.unlink",
    )
    writer.write_lint_rules([rule])
    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (l:LintRule {rule_id: 'E8401', odoo_version: $v})
                  -[:CHECKS]->
                  (cs:CoreSymbol {qualified_name: $cs_qn, odoo_version: $v})
            RETURN count(*) AS c
        """, v=TEST_VERSION, cs_qn="odoo.models.BaseModel.unlink").single()
    assert rec["c"] == 1


def test_setup_indexes_creates_lint_rule_index(writer, neo4j_driver):
    """setup_indexes creates an index on (LintRule.rule_id, odoo_version)."""
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        indexes = session.run("SHOW INDEXES").data()
    found = any(
        "LintRule" in (i.get("labelsOrTypes") or [])
        and "rule_id" in (i.get("properties") or [])
        and "odoo_version" in (i.get("properties") or [])
        for i in indexes
    )
    assert found, "LintRule(rule_id, odoo_version) index missing"


# --- CLICommand + CLIFlag writer tests (M4.5 WI4) -----------------------

from src.indexer.models import CLICommandInfo, CLIFlagInfo  # noqa: E402


def test_write_cli_command_node(writer, neo4j_driver):
    """write_cli_commands MERGEs a CLICommand node."""
    cmd = CLICommandInfo(
        name="server", odoo_version=TEST_VERSION,
        description="Run Odoo server",
    )
    writer.write_cli_commands([cmd])
    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (c:CLICommand {name: 'server', odoo_version: $v}) RETURN c
        """, v=TEST_VERSION).single()
    assert rec is not None
    assert rec["c"]["description"] == "Run Odoo server"


def test_write_cli_flag_with_of_command_edge(writer, neo4j_driver):
    """CLIFlag → OF_COMMAND → CLICommand edge created when both exist."""
    writer.write_cli_commands([CLICommandInfo("server", TEST_VERSION)])
    writer.write_cli_flags([CLIFlagInfo(
        flag_name="--http-port",
        command_name="server",
        odoo_version=TEST_VERSION,
        type="int",
    )])
    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (f:CLIFlag {flag_name: '--http-port', odoo_version: $v})
                  -[:OF_COMMAND]->
                  (c:CLICommand {name: 'server', odoo_version: $v})
            RETURN count(*) AS c
        """, v=TEST_VERSION).single()
    assert rec["c"] == 1


def test_write_cli_flag_replacement_creates_replaced_by_edge(writer, neo4j_driver):
    """write_cli_flag_replacements creates REPLACED_BY between CLIFlag nodes."""
    writer.write_cli_commands([CLICommandInfo("server", TEST_VERSION)])
    writer.write_cli_flags([
        CLIFlagInfo(
            "--longpolling-port", "server", TEST_VERSION,
            status="deprecated",
            replacement_flag_name="--gevent-port",
        ),
        CLIFlagInfo("--gevent-port", "server", TEST_VERSION),
    ])
    writer.write_cli_flag_replacements(
        [("--longpolling-port", "--gevent-port")],
        command_name="server",
        from_version=TEST_VERSION,
        to_version=TEST_VERSION,
    )
    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (a:CLIFlag {flag_name: '--longpolling-port', odoo_version: $v})
                  -[:REPLACED_BY]->
                  (b:CLIFlag {flag_name: '--gevent-port', odoo_version: $v})
            RETURN count(*) AS c
        """, v=TEST_VERSION).single()
    assert rec["c"] == 1


def test_setup_indexes_creates_cli_indexes(writer, neo4j_driver):
    """setup_indexes creates CLICommand + CLIFlag indexes."""
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        indexes = session.run("SHOW INDEXES").data()
    cmd_found = any(
        "CLICommand" in (i.get("labelsOrTypes") or [])
        and "name" in (i.get("properties") or [])
        for i in indexes
    )
    flag_found = any(
        "CLIFlag" in (i.get("labelsOrTypes") or [])
        and "flag_name" in (i.get("properties") or [])
        for i in indexes
    )
    assert cmd_found, "CLICommand index missing"
    assert flag_found, "CLIFlag index missing"
