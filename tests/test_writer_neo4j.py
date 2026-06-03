# SPDX-License-Identifier: AGPL-3.0-or-later
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


def make_parse_result(module_name: str, model_name: str,
                      commit_sha: str | None = None) -> ParseResult:
    module = ModuleInfo(
        name=module_name, odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo", path="/tmp",
        depends=[], version_raw="", commit_sha=commit_sha,
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


def _explain_operators(session, cypher: str, **params) -> set[str]:
    """Return the set of operator types in the query plan for an EXPLAIN.

    EXPLAIN does not execute the query, so params can be dummy values.
    """
    plan = session.run("EXPLAIN " + cypher, **params).consume().plan
    ops: set[str] = set()
    stack = [plan]
    while stack:
        node = stack.pop()
        # Neo4j 5.x suffixes operator types with the planner tag, e.g.
        # "NodeIndexSeek@neo4j" — normalize to the bare operator name.
        ops.add(node["operatorType"].split("@", 1)[0])
        stack.extend(node.get("children", []) or [])
    return ops


@pytest.mark.parametrize(
    "label, lookup_cypher, lookup_params",
    [
        # (label, a representative point-lookup MATCH, dummy params)
        ("View", "MATCH (n:View {xmlid: $x, odoo_version: $v}) RETURN n",
         {"x": "m.v", "v": TEST_VERSION}),
        ("QWebTmpl", "MATCH (n:QWebTmpl {xmlid: $x, odoo_version: $v}) RETURN n",
         {"x": "m.t", "v": TEST_VERSION}),
        ("CoreSymbol",
         "MATCH (n:CoreSymbol {qualified_name: $q, odoo_version: $v}) RETURN n",
         {"q": "odoo.models.BaseModel.unlink", "v": TEST_VERSION}),
        ("LintRule", "MATCH (n:LintRule {rule_id: $r, odoo_version: $v}) RETURN n",
         {"r": "E8502", "v": TEST_VERSION}),
        ("CLICommand", "MATCH (n:CLICommand {name: $n, odoo_version: $v}) RETURN n",
         {"n": "server", "v": TEST_VERSION}),
        ("CLIFlag",
         "MATCH (n:CLIFlag {flag_name: $f, command_name: $c, odoo_version: $v}) RETURN n",
         {"f": "--addons-path", "c": "server", "v": TEST_VERSION}),
        ("PatternExample", "MATCH (n:PatternExample {pattern_id: $p}) RETURN n",
         {"p": "pat-1"}),
    ],
)
def test_setup_indexes_makes_lookups_index_backed(
    writer, neo4j_driver, label, lookup_cypher, lookup_params
):
    """After setup_indexes(), each entity's point-lookup is index-backed.

    Behavioral contract (the reason the indexes exist): a keyed lookup on each
    label resolves via a NodeIndexSeek, not a full NodeByLabelScan. This is
    strictly stronger than the previous SHOW INDEXES metadata assertions — it
    proves the created index is actually usable for the intended query, so a
    regression that drops the index, or creates one on the wrong property set,
    is caught by the query planner choosing a label scan instead of a seek.
    """
    with neo4j_driver.session() as session:
        # Indexes are created asynchronously; the planner only uses an index
        # once it is ONLINE.
        session.run("CALL db.awaitIndexes(30000)").consume()
        ops = _explain_operators(session, lookup_cypher, **lookup_params)
    assert "NodeIndexSeek" in ops, (
        f"{label} lookup is not index-backed (planner chose {sorted(ops)}); "
        f"setup_indexes() must create a usable index for this query"
    )
    assert "NodeByLabelScan" not in ops, (
        f"{label} lookup falls back to a full label scan: {sorted(ops)}"
    )


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


def test_write_relativizes_paths_when_repo_root_set(writer, neo4j_driver):
    """ADR-0037: with ModuleInfo.repo_root set, Module.path + JSPatch/OWLComp
    file_path are stored REPO-RELATIVE (never the server-absolute path)."""
    from pathlib import Path
    module = ModuleInfo(
        name="relmod", odoo_version=TEST_VERSION, repo="odoo_17.0_repo",
        path="/srv/clones/odoo_17.0_repo/addons/relmod", depends=[],
        repo_root=Path("/srv/clones/odoo_17.0_repo"),
    )
    abs_js = "/srv/clones/odoo_17.0_repo/addons/relmod/static/src/js/a.js"
    patch = JSPatchInfo(
        target="W", patch_name="p", module="relmod",
        odoo_version=TEST_VERSION, era="patch", file_path=abs_js,
    )
    comp = OWLCompInfo(
        name="W", module="relmod", odoo_version=TEST_VERSION, file_path=abs_js,
    )
    writer.write_results([ParseResult(module=module, models=[])])
    writer.write_js_graph_results([
        JSGraphResult(module=module, patches=[patch], components=[comp])
    ])

    with neo4j_driver.session() as session:
        mod_fp = session.run(
            "MATCH (m:Module {name:'relmod', odoo_version:$v}) RETURN m.path AS p",
            v=TEST_VERSION,
        ).single()["p"]
        jp_fp = session.run(
            "MATCH (j:JSPatch {module:'relmod', odoo_version:$v}) RETURN j.file_path AS fp",
            v=TEST_VERSION,
        ).single()["fp"]
        oc_fp = session.run(
            "MATCH (c:OWLComp {name:'W', module:'relmod', odoo_version:$v}) "
            "RETURN c.file_path AS fp",
            v=TEST_VERSION,
        ).single()["fp"]

    assert mod_fp == "addons/relmod", f"Module.path must be repo-relative, got {mod_fp!r}"
    assert jp_fp == "addons/relmod/static/src/js/a.js", jp_fp
    assert oc_fp == "addons/relmod/static/src/js/a.js", oc_fp
    for fp in (mod_fp, jp_fp, oc_fp):
        assert not fp.startswith("/"), f"absolute path leaked into storage: {fp!r}"


def test_write_stylesheet_relative_with_repo_root(writer, neo4j_driver):
    """ADR-0037: write_stylesheets(repo_root=...) stores Stylesheet.file_path
    repo-relative (MERGE key), and the GC live_paths/Module.path stay aligned."""
    from pathlib import Path

    from src.indexer.models import StylesheetInfo
    repo_root = Path("/srv/clones/odoo_17.0_repo")
    ss = StylesheetInfo(
        file_path="/srv/clones/odoo_17.0_repo/addons/web/static/src/scss/m.scss",
        module="web", odoo_version=TEST_VERSION, language="scss",
        selector_count=1,
    )
    writer.write_stylesheets([ss], profiles=["odoo_17.0_repo"], repo_root=repo_root)
    with neo4j_driver.session() as session:
        fp = session.run(
            "MATCH (s:Stylesheet {module:'web', odoo_version:$v}) RETURN s.file_path AS fp",
            v=TEST_VERSION,
        ).single()["fp"]
    assert fp == "addons/web/static/src/scss/m.scss", fp
    assert not fp.startswith("/")


def test_stylesheet_imports_no_cross_repo_edge_on_shared_relative_path(
    writer, neo4j_driver,
):
    """ADR-0037 regression: two repos at the SAME odoo_version that share an
    identical relative stylesheet path (community + enterprise overlay both ship
    ``addons/web/static/src/scss/variables.scss``) must NOT produce a spurious
    cross-repo :IMPORTS edge.  A SCSS @import resolves within the importing
    repo, so the :IMPORTS target MATCH is scoped by repo_id.

    Repo A (id=101): main.scss @imports variables.scss (same repo).
    Repo B (id=202): an unrelated variables.scss at the SAME relative path.
    Expected: exactly ONE :IMPORTS edge (A.main -> A.variables); zero edges
    cross into repo B's node.
    """
    from pathlib import Path

    from src.indexer.models import StylesheetInfo

    rel_vars = "addons/web/static/src/scss/variables.scss"
    rel_main = "addons/web/static/src/scss/main.scss"
    abs_vars_a = f"/srv/clones/repo_a/{rel_vars}"
    abs_main_a = f"/srv/clones/repo_a/{rel_main}"
    abs_vars_b = f"/srv/clones/repo_b/{rel_vars}"

    # Repo A: main.scss imports variables.scss (resolved to the same-repo abs path).
    main_a = StylesheetInfo(
        file_path=abs_main_a, module="web", odoo_version=TEST_VERSION,
        language="scss", import_count=1, imports=[abs_vars_a],
    )
    vars_a = StylesheetInfo(
        file_path=abs_vars_a, module="web", odoo_version=TEST_VERSION,
        language="scss",
    )
    # Repo B: an unrelated variables.scss at the identical RELATIVE path.
    vars_b = StylesheetInfo(
        file_path=abs_vars_b, module="web", odoo_version=TEST_VERSION,
        language="scss",
    )

    writer.write_stylesheets(
        [vars_b], profiles=["repo_b"],
        repo_root=Path("/srv/clones/repo_b"), repo_id=202,
    )
    # Target (vars_a) listed before the importer (main_a) so the :IMPORTS MATCH
    # finds it within the same batch (single-pass writer; ADR-0025 §D3 skips
    # when the target is not yet indexed).
    writer.write_stylesheets(
        [vars_a, main_a], profiles=["repo_a"],
        repo_root=Path("/srv/clones/repo_a"), repo_id=101,
    )

    with neo4j_driver.session() as session:
        edges = session.run(
            """
            MATCH (src:Stylesheet {odoo_version:$v})-[:IMPORTS]->(tgt:Stylesheet)
            RETURN src.repo_id AS src_repo, tgt.repo_id AS tgt_repo
            """,
            v=TEST_VERSION,
        ).data()

    assert len(edges) == 1, f"expected exactly one same-repo IMPORTS edge, got {edges!r}"
    assert edges[0]["src_repo"] == 101 and edges[0]["tgt_repo"] == 101, (
        f"IMPORTS edge crossed repos (must stay within repo 101): {edges[0]!r}"
    )


def test_write_lint_violation_relative_with_repo_root(writer, neo4j_driver):
    """ADR-0037: LintViolation.file_path (a MERGE-key component) is stored
    repo-relative when repo_root is passed — so the post-reindex cleanup cypher
    (which deletes absolute-keyed nodes) never deletes freshly-written data."""
    from pathlib import Path

    from src.indexer.models import LintViolationInfo
    lv = LintViolationInfo(
        file_path="/srv/clones/odoo_17.0_repo/addons/sale/views/sale_views.xml",
        line=12, rule="relaxng.tree_view", message="bad", view_xmlid="sale.v",
        odoo_version=TEST_VERSION, view_type="tree",
    )
    writer.write_lint_violations(
        [lv], profiles=["odoo_17.0_repo"],
        repo_root=Path("/srv/clones/odoo_17.0_repo"),
    )
    with neo4j_driver.session() as session:
        fp = session.run(
            "MATCH (lv:LintViolation {odoo_version:$v, rule:'relaxng.tree_view'}) "
            "RETURN lv.file_path AS fp",
            v=TEST_VERSION,
        ).single()["fp"]
    assert fp == "addons/sale/views/sale_views.xml", fp
    assert not fp.startswith("/"), f"absolute path leaked into LintViolation key: {fp!r}"


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


# CoreSymbol index lookup behavior covered by
# test_setup_indexes_makes_lookups_index_backed (parametrized).


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


# LintRule index lookup behavior covered by
# test_setup_indexes_makes_lookups_index_backed (parametrized).


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


# CLICommand + CLIFlag index lookup behavior covered by
# test_setup_indexes_makes_lookups_index_backed (parametrized).


# --- USES_CORE_SYMBOL edge tests (M4.5 WI6) -----------------------------


def _make_parse_result_with_method_refs(
    module_name: str, model_name: str, method_name: str, refs: list[str],
) -> ParseResult:
    """Build a ParseResult whose single Method carries `core_symbol_refs`."""
    module = ModuleInfo(
        name=module_name, odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo", path="/tmp",
        depends=[], version_raw="",
    )
    model = ModelInfo(
        name=model_name, module=module_name, odoo_version=TEST_VERSION,
        methods=[
            MethodInfo(
                name=method_name, has_super_call=False, decorators=[],
                core_symbol_refs=refs,
            ),
        ],
    )
    return ParseResult(module=module, models=[model])


def test_uses_core_symbol_edge_when_target_exists_and_deprecated(writer, neo4j_driver):
    """When a Method has core_symbol_refs and a deprecated CoreSymbol exists,
    USES_CORE_SYMBOL edge is MERGEd."""
    # Seed CoreSymbol
    sym = CoreSymbolInfo(
        qualified_name="odoo.models.BaseModel.name_get",
        kind="orm_method",
        odoo_version=TEST_VERSION,
        status="deprecated",
        replacement_qname="odoo.models.BaseModel.display_name",
    )
    writer.write_core_symbols([sym])

    # Seed Method with ref
    pr = _make_parse_result_with_method_refs(
        "viin_sale", "sale.order", "foo", refs=["name_get"],
    )
    writer.write_results([pr])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (mth:Method {name: 'foo', module: 'viin_sale', odoo_version: $v})
                  -[:USES_CORE_SYMBOL]->
                  (cs:CoreSymbol {qualified_name: $cs_qn, odoo_version: $v})
            RETURN count(*) AS c
        """, v=TEST_VERSION,
             cs_qn="odoo.models.BaseModel.name_get").single()
    assert rec["c"] == 1


def test_no_uses_core_symbol_edge_when_target_missing(writer, neo4j_driver):
    """Method has refs but no CoreSymbol indexed → silent skip, no placeholder."""
    pr = _make_parse_result_with_method_refs(
        "viin_sale", "sale.order", "bar", refs=["name_get"],
    )
    writer.write_results([pr])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (mth:Method {name: 'bar', module: 'viin_sale', odoo_version: $v})
                  -[:USES_CORE_SYMBOL]->()
            RETURN count(*) AS c
        """, v=TEST_VERSION).single()
    assert rec["c"] == 0


def test_no_uses_core_symbol_edge_when_target_is_stable(writer, neo4j_driver):
    """V0 scope per ADR-0002 §3: only deprecated/removed CoreSymbol gets edges."""
    sym = CoreSymbolInfo(
        qualified_name="odoo.tools.safe_eval.safe_eval",
        kind="function",
        odoo_version=TEST_VERSION,
        status="stable",
    )
    writer.write_core_symbols([sym])

    pr = _make_parse_result_with_method_refs(
        "viin_sale", "sale.order", "baz", refs=["safe_eval"],
    )
    writer.write_results([pr])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (mth:Method {name: 'baz', module: 'viin_sale', odoo_version: $v})
                  -[:USES_CORE_SYMBOL]->()
            RETURN count(*) AS c
        """, v=TEST_VERSION).single()
    assert rec["c"] == 0  # stable status excluded by V0 scope


# --- M4.6 WI1: Module edition + viindoo_equivalent_qname --------------------


def test_write_module_edition_default_community(writer, neo4j_driver):
    """Module without explicit edition → defaults to 'community'."""
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: 'sale', odoo_version: $v}) "
            "RETURN m.edition AS ed, m.viindoo_equivalent_qname AS vvq",
            v=TEST_VERSION,
        ).single()
    assert rec["ed"] == "community"
    assert rec["vvq"] is None


# --- M4.6 WI2: Method convention props -------------------------------------


def test_write_method_convention_props(writer, neo4j_driver):
    """convention_kind / super_safety / return_required persisted on Method node."""
    module = ModuleInfo(
        name="sale", odoo_version=TEST_VERSION, repo="r", path="/tmp",
        depends=[], version_raw="",
    )
    model = ModelInfo(
        name="sale.order", module="sale", odoo_version=TEST_VERSION,
        methods=[
            MethodInfo(
                name="action_confirm", has_super_call=True,
                convention_kind="action", super_safety="always",
                return_required=True,
            ),
        ],
    )
    pr = ParseResult(module=module, models=[model])
    writer.write_results([pr])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (mth:Method {name: 'action_confirm', model: 'sale.order', "
            "module: 'sale', odoo_version: $v}) "
            "RETURN mth.convention_kind AS ck, mth.super_safety AS ss, "
            "mth.return_required AS rr",
            v=TEST_VERSION,
        ).single()
    assert rec["ck"] == "action"
    assert rec["ss"] == "always"
    assert rec["rr"] is True


# --- M4.6 WI3: PatternExample writes ---------------------------------------


def test_write_pattern_example_node_created(writer, neo4j_driver):
    """write_pattern_examples MERGE creates a PatternExample node with all props."""
    from src.indexer.models import PatternExample
    pe = PatternExample(
        pattern_id="t-pattern-1",
        intent_keywords=["compute", "depends"],
        file_ref="addons/sale/models/sale_order.py:1",
        snippet_text="@api.depends(...)\ndef _compute(self): ...",
        gotchas=["Missing Many2one root"],
        odoo_version_min=TEST_VERSION,
        language="python",
    )
    writer.write_pattern_examples([pe])
    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (p:PatternExample {pattern_id: 't-pattern-1'}) "
            "RETURN p.language AS lang, p.odoo_version_min AS v, "
            "p.intent_keywords AS kw, p.gotchas AS g",
        ).single()
    assert rec["lang"] == "python"
    assert rec["v"] == TEST_VERSION
    assert "compute" in rec["kw"]
    assert "Missing Many2one root" in rec["g"]


def test_write_pattern_example_idempotent(writer, neo4j_driver):
    """MERGE idempotent — calling write twice yields one node."""
    from src.indexer.models import PatternExample
    pe = PatternExample(
        pattern_id="t-pattern-idem",
        intent_keywords=["x"],
        file_ref="f:1",
        snippet_text="x",
        gotchas=["g"],
        odoo_version_min=TEST_VERSION,
        language="python",
    )
    writer.write_pattern_examples([pe])
    writer.write_pattern_examples([pe])
    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (p:PatternExample {pattern_id: 't-pattern-idem'}) "
            "RETURN count(p) AS c",
        ).single()
    assert rec["c"] == 1


def test_write_pattern_uses_core_symbol_when_target_exists(writer, neo4j_driver):
    """USES_CORE_SYMBOL edge when CoreSymbol target exists at same version."""
    from src.indexer.models import CoreSymbolInfo, PatternExample
    cs = CoreSymbolInfo(
        qualified_name="odoo.api.depends",
        kind="decorator",
        odoo_version=TEST_VERSION,
    )
    writer.write_core_symbols([cs])

    pe = PatternExample(
        pattern_id="t-pattern-ce",
        intent_keywords=["x"],
        file_ref="f:1",
        snippet_text="x",
        gotchas=["g"],
        odoo_version_min=TEST_VERSION,
        language="python",
        core_symbol_names=["odoo.api.depends"],
    )
    writer.write_pattern_examples([pe])
    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (p:PatternExample {pattern_id: 't-pattern-ce'})
                  -[:USES_CORE_SYMBOL]->(cs:CoreSymbol)
            RETURN cs.qualified_name AS qn
        """).single()
    assert rec["qn"] == "odoo.api.depends"


def test_write_pattern_skips_uses_core_symbol_when_target_missing(
    writer, neo4j_driver,
):
    """No edge when CoreSymbol target absent — silent skip per ADR-0003 §5."""
    from src.indexer.models import PatternExample
    pe = PatternExample(
        pattern_id="t-pattern-skip",
        intent_keywords=["x"],
        file_ref="f:1",
        snippet_text="x",
        gotchas=["g"],
        odoo_version_min=TEST_VERSION,
        language="python",
        core_symbol_names=["nonexistent_symbol_xyz"],
    )
    writer.write_pattern_examples([pe])
    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (p:PatternExample {pattern_id: 't-pattern-skip'})"
            "-[:USES_CORE_SYMBOL]->() RETURN count(*) AS c",
        ).single()
    assert rec["c"] == 0


# PatternExample index lookup behavior covered by
# test_setup_indexes_makes_lookups_index_backed (parametrized).


# --- WI-3: had_explicit_name + is_definition + INHERITS order -------------------


def test_inherits_edge_has_order_property(writer, neo4j_driver):
    """Each INHERITS edge carries r.order matching its list position (0, 1, 2)."""
    # Seed 3 parent models and 1 child that inherits all three
    for parent_name in ("a.b", "c.d", "e.f"):
        parent_module = ModuleInfo(
            name=f"mod_{parent_name.replace('.', '_')}",
            odoo_version=TEST_VERSION,
            repo="test_repo", path="/tmp",
            depends=[], version_raw="",
        )
        parent_model = ModelInfo(
            name=parent_name,
            module=parent_module.name,
            odoo_version=TEST_VERSION,
        )
        writer.write_results([ParseResult(module=parent_module, models=[parent_model])])

    child_module = ModuleInfo(
        name="child_mod", odoo_version=TEST_VERSION,
        repo="child_repo", path="/tmp", depends=[], version_raw="",
    )
    child_model = ModelInfo(
        name="child.model", module="child_mod", odoo_version=TEST_VERSION,
        inherit=["a.b", "c.d", "e.f"],
        had_explicit_name=True,
    )
    writer.write_results([ParseResult(module=child_module, models=[child_model])])

    with neo4j_driver.session() as session:
        rows = session.run("""
            MATCH (child:Model {name: 'child.model', module: 'child_mod',
                                odoo_version: $v})
                  -[r:INHERITS]->(parent:Model {odoo_version: $v})
            RETURN parent.name AS parent_name, r.order AS order
            ORDER BY r.order ASC
        """, v=TEST_VERSION).data()

    assert len(rows) == 3, f"Expected 3 INHERITS edges, got {len(rows)}: {rows}"
    name_to_order = {r["parent_name"]: r["order"] for r in rows}
    assert name_to_order["a.b"] == 0
    assert name_to_order["c.d"] == 1
    assert name_to_order["e.f"] == 2


def test_model_node_is_definition_true_for_explicit_name(writer, neo4j_driver):
    """Model with had_explicit_name=True and no self-inherit → is_definition=True."""
    module = ModuleInfo(
        name="sale", odoo_version=TEST_VERSION,
        repo="sale_repo", path="/tmp", depends=[], version_raw="",
    )
    model = ModelInfo(
        name="sale.order", module="sale", odoo_version=TEST_VERSION,
        inherit=[],
        had_explicit_name=True,
    )
    writer.write_results([ParseResult(module=module, models=[model])])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (m:Model {name: 'sale.order', module: 'sale', odoo_version: $v})
            RETURN m.is_definition AS is_def, m.had_explicit_name AS had_name
        """, v=TEST_VERSION).single()
    assert rec["had_name"] is True
    assert rec["is_def"] is True


def test_model_node_is_definition_false_for_extension(writer, neo4j_driver):
    """Model with _name = _inherit[0] (Pattern C redeclare) → is_definition=False."""
    module = ModuleInfo(
        name="viin_sale", odoo_version=TEST_VERSION,
        repo="viin_repo", path="/tmp", depends=["sale"], version_raw="",
    )
    # Pattern C: _name = 'sale.order', _inherit = ['sale.order']
    model = ModelInfo(
        name="sale.order", module="viin_sale", odoo_version=TEST_VERSION,
        inherit=["sale.order"],
        had_explicit_name=True,
    )
    writer.write_results([ParseResult(module=module, models=[model])])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (m:Model {name: 'sale.order', module: 'viin_sale', odoo_version: $v})
            RETURN m.is_definition AS is_def, m.had_explicit_name AS had_name
        """, v=TEST_VERSION).single()
    assert rec["had_name"] is True
    assert rec["is_def"] is False  # name IN inherit_list → extension, not definition


def test_write_module_edition_viindoo_with_equivalent(writer, neo4j_driver):
    """Viindoo module with viindoo_equivalent_qname set → both props persisted."""
    module = ModuleInfo(
        name="viin_helpdesk", odoo_version=TEST_VERSION,
        repo="acme_addons17", path="/home/x/acme_addons17/viin_helpdesk",
        depends=[], version_raw="",
        edition="viindoo", viindoo_equivalent_qname="viin_helpdesk",
    )
    pr = ParseResult(module=module, models=[])
    writer.write_results([pr])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: 'viin_helpdesk', odoo_version: $v}) "
            "RETURN m.edition AS ed, m.viindoo_equivalent_qname AS vvq",
            v=TEST_VERSION,
        ).single()
    assert rec["ed"] == "viindoo"
    assert rec["vvq"] == "viin_helpdesk"


# --- WI-10: TC-1..TC-5 integration regression for inherit semantics -----------


def test_tc1_pattern_d_self_extend_plus_mixin_edge_order(writer, neo4j_driver):
    """TC-1 — Pattern D: _inherit = ['x', 'mixin.alpha'] on a model that redeclares _name='x'.

    Synthetic Era2 source:
        class Y(models.Model):
            _name = 'x'
            _inherit = ['x', 'mixin.alpha']

    Assertions:
    - mod_b Model has had_explicit_name=True, is_definition=False (name in inherit list).
    - INHERITS {order:0} edge → mod_a:x (self-extend, position 0).
    - INHERITS {order:1} edge → mixin.alpha (mixin injection, position 1).
    """
    # Seed mod_a as the original definition of 'x'
    mod_a = ModuleInfo(
        name="mod_a", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=[], version_raw="",
    )
    model_a = ModelInfo(
        name="x", module="mod_a", odoo_version=TEST_VERSION,
        had_explicit_name=True,
    )
    # Seed mixin.alpha definition
    mod_mixin = ModuleInfo(
        name="mod_mixin", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=[], version_raw="",
    )
    model_mixin = ModelInfo(
        name="mixin.alpha", module="mod_mixin", odoo_version=TEST_VERSION,
        had_explicit_name=True,
    )
    # mod_b extends 'x' and injects 'mixin.alpha'
    mod_b = ModuleInfo(
        name="mod_b", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=["mod_a", "mod_mixin"], version_raw="",
    )
    model_b = ModelInfo(
        name="x", module="mod_b", odoo_version=TEST_VERSION,
        inherit=["x", "mixin.alpha"],
        had_explicit_name=True,   # _name = 'x' is present in class body
    )
    writer.write_results([
        ParseResult(module=mod_a, models=[model_a]),
        ParseResult(module=mod_mixin, models=[model_mixin]),
        ParseResult(module=mod_b, models=[model_b]),
    ])

    with neo4j_driver.session() as session:
        # 1. mod_b Model node: had_explicit_name=True, is_definition=False
        node_rec = session.run("""
            MATCH (m:Model {name: 'x', module: 'mod_b', odoo_version: $v})
            RETURN m.had_explicit_name AS had_name, m.is_definition AS is_def
        """, v=TEST_VERSION).single()
    assert node_rec is not None, "mod_b Model node for 'x' must exist"
    assert node_rec["had_name"] is True
    assert node_rec["is_def"] is False, (
        "had_explicit_name=True but name in inherit list → is_definition must be False"
    )

    with neo4j_driver.session() as session:
        # 2. INHERITS {order:0} → mod_a:x (self-extend)
        self_edge = session.run("""
            MATCH (mod_b_node:Model {name: 'x', module: 'mod_b', odoo_version: $v})
                  -[r:INHERITS {order: 0}]->
                  (mod_a_node:Model {name: 'x', module: 'mod_a', odoo_version: $v})
            RETURN r.order AS ord
        """, v=TEST_VERSION).single()
    assert self_edge is not None, "INHERITS{order:0} edge to mod_a:x must exist"

    with neo4j_driver.session() as session:
        # 3. INHERITS {order:1} → mixin.alpha
        mixin_edge = session.run("""
            MATCH (mod_b_node:Model {name: 'x', module: 'mod_b', odoo_version: $v})
                  -[r:INHERITS {order: 1}]->
                  (mixin_node:Model {name: 'mixin.alpha', odoo_version: $v})
            RETURN r.order AS ord
        """, v=TEST_VERSION).single()
    assert mixin_edge is not None, "INHERITS{order:1} edge to mixin.alpha must exist"


def test_tc2_delegation_only_no_inherits_edge(writer, neo4j_driver):
    """TC-2 — Pattern E: _inherits only → DELEGATES_TO edge, NO INHERITS edge.

    Synthetic:
        class Child(models.Model):
            _name = 'child'
            _inherits = {'parent': 'p_id'}

    Assertions:
    - DELEGATES_TO {via_field:'p_id'} edge exists.
    - NO INHERITS edge between child and parent (different relationship types).
    """
    base_module = ModuleInfo(
        name="base_tc2", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=[], version_raw="",
    )
    parent_model = ModelInfo(
        name="parent", module="base_tc2", odoo_version=TEST_VERSION,
        had_explicit_name=True,
    )
    child_module = ModuleInfo(
        name="child_tc2", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=["base_tc2"], version_raw="",
    )
    child_model = ModelInfo(
        name="child", module="child_tc2", odoo_version=TEST_VERSION,
        inherits={"parent": "p_id"},
        had_explicit_name=True,
    )
    writer.write_results([
        ParseResult(module=base_module, models=[parent_model]),
        ParseResult(module=child_module, models=[child_model]),
    ])

    with neo4j_driver.session() as session:
        # DELEGATES_TO edge must exist
        del_rec = session.run("""
            MATCH (:Model {name: 'child', odoo_version: $v})
                  -[r:DELEGATES_TO]->
                  (:Model {name: 'parent', odoo_version: $v})
            RETURN r.via_field AS via_field
        """, v=TEST_VERSION).single()
    assert del_rec is not None, "DELEGATES_TO edge must exist"
    assert del_rec["via_field"] == "p_id"

    with neo4j_driver.session() as session:
        # INHERITS edge must NOT exist (delegation ≠ prototype inheritance)
        inh_cnt = session.run("""
            MATCH (:Model {name: 'child', odoo_version: $v})
                  -[:INHERITS]->
                  (:Model {name: 'parent', odoo_version: $v})
            RETURN count(*) AS cnt
        """, v=TEST_VERSION).single()
    assert inh_cnt["cnt"] == 0, (
        "INHERITS edge must NOT exist for pure _inherits delegation — only DELEGATES_TO"
    )


def test_tc3_pattern_b_vs_c_had_explicit_name_distinguishable(writer, neo4j_driver):
    """TC-3 — Pattern A/B/C: had_explicit_name + is_definition correctly persisted.

    Synthetic setup (all same model name, different modules):
        mod_a: Pattern A — _name only        → had_explicit_name=True,  is_definition=True
        mod_b: Pattern B — _inherit only     → had_explicit_name=False, is_definition=False
        mod_c: Pattern C — _name + _inherit  → had_explicit_name=True,  is_definition=False

    Parser-level coverage exists in test_parser_python.py:637-689. This test
    closes the writer→Neo4j integration gap (ADR-0004 Defined-in ranking).
    """
    mod_a = ModuleInfo(
        name="test_mod_a", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=[], version_raw="",
    )
    model_a = ModelInfo(
        name="test.alpha", module="test_mod_a", odoo_version=TEST_VERSION,
        inherit=[],
        had_explicit_name=True,  # Pattern A: only _name declared
    )

    mod_b = ModuleInfo(
        name="test_mod_b", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=["test_mod_a"], version_raw="",
    )
    model_b = ModelInfo(
        name="test.alpha", module="test_mod_b", odoo_version=TEST_VERSION,
        inherit=["test.alpha"],
        had_explicit_name=False,  # Pattern B: only _inherit, name auto-derived
    )

    mod_c = ModuleInfo(
        name="test_mod_c", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=["test_mod_a"], version_raw="",
    )
    model_c = ModelInfo(
        name="test.alpha", module="test_mod_c", odoo_version=TEST_VERSION,
        inherit=["test.alpha"],
        had_explicit_name=True,  # Pattern C: both _name and _inherit declared (redeclare)
    )

    writer.write_results([
        ParseResult(module=mod_a, models=[model_a]),
        ParseResult(module=mod_b, models=[model_b]),
        ParseResult(module=mod_c, models=[model_c]),
    ])

    with neo4j_driver.session() as session:
        # Pattern A (test_mod_a): had_explicit_name=True, is_definition=True
        rec_a = session.run("""
            MATCH (m:Model {name: 'test.alpha', module: 'test_mod_a', odoo_version: $v})
            RETURN m.had_explicit_name AS had_name, m.is_definition AS is_def
        """, v=TEST_VERSION).single()
    assert rec_a is not None, "Pattern A Model node (test_mod_a) must exist"
    assert rec_a["had_name"] is True, "Pattern A: had_explicit_name must be True"
    assert rec_a["is_def"] is True, (
        "Pattern A: _name explicit + name NOT in inherit list → is_definition must be True"
    )

    with neo4j_driver.session() as session:
        # Pattern B (test_mod_b): had_explicit_name=False, is_definition=False
        rec_b = session.run("""
            MATCH (m:Model {name: 'test.alpha', module: 'test_mod_b', odoo_version: $v})
            RETURN m.had_explicit_name AS had_name, m.is_definition AS is_def
        """, v=TEST_VERSION).single()
    assert rec_b is not None, "Pattern B Model node (test_mod_b) must exist"
    assert rec_b["had_name"] is False, "Pattern B: had_explicit_name must be False"
    assert rec_b["is_def"] is False, (
        "Pattern B: no explicit _name → is_definition must be False"
    )

    with neo4j_driver.session() as session:
        # Pattern C (test_mod_c): had_explicit_name=True but IS in inherit list
        rec_c = session.run("""
            MATCH (m:Model {name: 'test.alpha', module: 'test_mod_c', odoo_version: $v})
            RETURN m.had_explicit_name AS had_name, m.is_definition AS is_def
        """, v=TEST_VERSION).single()
    assert rec_c is not None, "Pattern C Model node (test_mod_c) must exist"
    assert rec_c["had_name"] is True, "Pattern C: had_explicit_name must be True"
    assert rec_c["is_def"] is False, (
        "Pattern C: _name explicit BUT name in inherit list (redeclare)"
        " → is_definition must be False"
    )


def test_tc4_multiple_inherits_keys_two_delegates_to_edges(writer, neo4j_driver):
    """TC-4 — Multiple _inherits keys → 2 DELEGATES_TO edges, one per parent.

    Synthetic:
        class Child(models.Model):
            _name = 'child'
            _inherits = {'parent.a': 'a_id', 'parent.b': 'b_id'}
    """
    mod_parents = ModuleInfo(
        name="parents_tc4", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=[], version_raw="",
    )
    parent_a = ModelInfo(
        name="parent.a", module="parents_tc4", odoo_version=TEST_VERSION,
        had_explicit_name=True,
    )
    parent_b = ModelInfo(
        name="parent.b", module="parents_tc4", odoo_version=TEST_VERSION,
        had_explicit_name=True,
    )
    mod_child = ModuleInfo(
        name="child_tc4", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=["parents_tc4"], version_raw="",
    )
    child_model = ModelInfo(
        name="child", module="child_tc4", odoo_version=TEST_VERSION,
        inherits={"parent.a": "a_id", "parent.b": "b_id"},
        had_explicit_name=True,
    )
    writer.write_results([
        ParseResult(module=mod_parents, models=[parent_a, parent_b]),
        ParseResult(module=mod_child, models=[child_model]),
    ])

    with neo4j_driver.session() as session:
        edges = session.run("""
            MATCH (c:Model {name: 'child', odoo_version: $v})
                  -[r:DELEGATES_TO]->
                  (p:Model {odoo_version: $v})
            RETURN p.name AS parent_name, r.via_field AS via_field
            ORDER BY p.name
        """, v=TEST_VERSION).data()

    assert len(edges) == 2, f"Expected 2 DELEGATES_TO edges, got {len(edges)}: {edges}"
    edge_map = {e["parent_name"]: e["via_field"] for e in edges}
    assert edge_map.get("parent.a") == "a_id", "parent.a via a_id edge missing"
    assert edge_map.get("parent.b") == "b_id", "parent.b via b_id edge missing"


def test_tc5_combined_inherit_and_inherits_distinct_edge_types(writer, neo4j_driver):
    """TC-5 — _inherit + _inherits on same model → 1 INHERITS + 1 DELEGATES_TO, not conflated.

    Synthetic:
        class Combo(models.Model):
            _name = 'combo'
            _inherit = ['mail.thread']
            _inherits = {'res.partner': 'partner_id'}
    """
    mod_bases = ModuleInfo(
        name="bases_tc5", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=[], version_raw="",
    )
    mail_thread = ModelInfo(
        name="mail.thread", module="bases_tc5", odoo_version=TEST_VERSION,
        had_explicit_name=True,
    )
    res_partner = ModelInfo(
        name="res.partner", module="bases_tc5", odoo_version=TEST_VERSION,
        had_explicit_name=True,
    )
    mod_combo = ModuleInfo(
        name="combo_tc5", odoo_version=TEST_VERSION,
        repo="test_repo", path="/tmp", depends=["bases_tc5"], version_raw="",
    )
    combo_model = ModelInfo(
        name="combo", module="combo_tc5", odoo_version=TEST_VERSION,
        inherit=["mail.thread"],
        inherits={"res.partner": "partner_id"},
        had_explicit_name=True,
    )
    writer.write_results([
        ParseResult(module=mod_bases, models=[mail_thread, res_partner]),
        ParseResult(module=mod_combo, models=[combo_model]),
    ])

    with neo4j_driver.session() as session:
        # 1. INHERITS edge to mail.thread (prototype inheritance)
        inh_rec = session.run("""
            MATCH (:Model {name: 'combo', odoo_version: $v})
                  -[r:INHERITS]->
                  (:Model {name: 'mail.thread', odoo_version: $v})
            RETURN count(r) AS cnt
        """, v=TEST_VERSION).single()
    assert inh_rec["cnt"] == 1, "Exactly 1 INHERITS edge to mail.thread must exist"

    with neo4j_driver.session() as session:
        # 2. DELEGATES_TO edge to res.partner (delegation)
        del_rec = session.run("""
            MATCH (:Model {name: 'combo', odoo_version: $v})
                  -[r:DELEGATES_TO]->
                  (:Model {name: 'res.partner', odoo_version: $v})
            RETURN r.via_field AS via_field
        """, v=TEST_VERSION).single()
    assert del_rec is not None, "DELEGATES_TO edge to res.partner must exist"
    assert del_rec["via_field"] == "partner_id"

    with neo4j_driver.session() as session:
        # 3. NO INHERITS edge to res.partner (not prototype inheritance)
        bad_inh = session.run("""
            MATCH (:Model {name: 'combo', odoo_version: $v})
                  -[:INHERITS]->
                  (:Model {name: 'res.partner', odoo_version: $v})
            RETURN count(*) AS cnt
        """, v=TEST_VERSION).single()
    assert bad_inh["cnt"] == 0, "res.partner must NOT be reached via INHERITS — only DELEGATES_TO"

    with neo4j_driver.session() as session:
        # 4. NO DELEGATES_TO edge to mail.thread (not delegation)
        bad_del = session.run("""
            MATCH (:Model {name: 'combo', odoo_version: $v})
                  -[:DELEGATES_TO]->
                  (:Model {name: 'mail.thread', odoo_version: $v})
            RETURN count(*) AS cnt
        """, v=TEST_VERSION).single()
    assert bad_del["cnt"] == 0, "mail.thread must NOT be reached via DELEGATES_TO — only INHERITS"


def test_module_node_has_last_commit_sha_after_write(writer, neo4j_driver):
    result = make_parse_result("sale", "sale.order",
                               commit_sha="abc123def456789abcdef")
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            "RETURN m.last_commit_sha AS sha",
            n="sale", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert rec["sha"] == "abc123def456789abcdef"


def test_module_node_handles_none_commit_sha(writer, neo4j_driver):
    result = make_parse_result("sale", "sale.order", commit_sha=None)
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            "RETURN m.last_commit_sha AS sha",
            n="sale", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert rec["sha"] is None


def test_re_merge_updates_last_commit_sha(writer, neo4j_driver):
    # Write first time with old sha
    result1 = make_parse_result("sale", "sale.order",
                                commit_sha="oldsha0000000000000000")
    writer.write_results([result1])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            "RETURN m.last_commit_sha AS sha",
            n="sale", v=TEST_VERSION
        ).single()
    assert rec["sha"] == "oldsha0000000000000000"

    # Write second time with new sha (re-MERGE)
    result2 = make_parse_result("sale", "sale.order",
                                commit_sha="newsha1111111111111111")
    writer.write_results([result2])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            "RETURN m.last_commit_sha AS sha",
            n="sale", v=TEST_VERSION
        ).single()
    assert rec["sha"] == "newsha1111111111111111"


def test_module_merge_key_excludes_last_commit_sha(writer, neo4j_driver):
    """ADR-0001 invariant: last_commit_sha is mutable, not part of MERGE key.

    Regression guard: if last_commit_sha moves into MERGE key, writes with
    different commit_sha would create duplicate Module nodes instead of
    re-MERGing the same node.

    NOTE: differing commit_sha values (e.g. 'aaa...' vs 'bbb...') is load-bearing —
    if both writes used the SAME commit_sha, this test would silently pass even if
    last_commit_sha was accidentally moved into the MERGE key. The differing values
    force the regression path to be exercised.
    """
    # Write first time with commit_sha="aaa..."
    result1 = make_parse_result("store_model", "store.config",
                                commit_sha="aaaaaaaaaaaaaaaaaaaa")
    writer.write_results([result1])

    # Write second time for SAME (name, odoo_version) but different commit_sha="bbb..."
    result2 = make_parse_result("store_model", "store.config",
                                commit_sha="bbbbbbbbbbbbbbbbbbbb")
    writer.write_results([result2])

    # After both writes: exactly 1 Module node should exist (idempotent MERGE key)
    with neo4j_driver.session() as session:
        count_rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN count(m) AS c",
            n="store_model", v=TEST_VERSION
        ).single()
    assert count_rec["c"] == 1, (
        f"ADR-0001 violation: expected 1 Module node, got {count_rec['c']}. "
        "last_commit_sha must not be in MERGE key."
    )

    # Verify latest value wins (second write's commit_sha)
    with neo4j_driver.session() as session:
        sha_rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m.last_commit_sha AS sha",
            n="store_model", v=TEST_VERSION
        ).single()
    assert sha_rec["sha"] == "bbbbbbbbbbbbbbbbbbbb", (
        f"Expected latest commit_sha 'bbbbbbbbbbbbbbbbbbbb', got {sha_rec['sha']}"
    )


# ---------------------------------------------------------------------------
# M8 — Profile array property tests (ADR-0016 Option Y)
# ---------------------------------------------------------------------------

def test_module_node_carries_profile_array(writer, neo4j_driver):
    """write_results with profiles arg writes m.profile list on Module node."""
    profiles = ["internal_17", "standard_17", "odoo_17"]
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result], profiles=profiles)

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m.profile AS p",
            n="sale", v=TEST_VERSION,
        ).single()

    assert rec is not None
    assert rec["p"] == profiles


def test_model_node_carries_profile_array(writer, neo4j_driver):
    """write_results with profiles arg writes m.profile list on Model node."""
    profiles = ["internal_17", "odoo_17"]
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result], profiles=profiles)

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Model {name: $n, module: $mod, odoo_version: $v}) "
            "RETURN m.profile AS p",
            n="sale.order", mod="sale", v=TEST_VERSION,
        ).single()

    assert rec is not None
    assert rec["p"] == profiles


def test_profile_array_overwritten_on_reindex(writer, neo4j_driver):
    """Re-indexing the same profile is idempotent (no duplicates); union semantics."""
    first_profiles = ["internal_17"]
    # Re-index with the same profile — should not duplicate entries.
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result], profiles=first_profiles)
    writer.write_results([result], profiles=first_profiles)

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m.profile AS p",
            n="sale", v=TEST_VERSION,
        ).single()

    # Union semantics: re-indexing the same profile must not duplicate it.
    assert rec["p"] == ["internal_17"]


def test_two_sibling_profiles_union_on_shared_module_node(writer, neo4j_driver):
    """Profiles A and B both index a Module with the same key; both appear in profile array."""
    profile_a = ["profile_A"]
    profile_b = ["profile_B"]
    result = make_parse_result("sale", "sale.order")

    writer.write_results([result], profiles=profile_a)
    writer.write_results([result], profiles=profile_b)

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m.profile AS p",
            n="sale", v=TEST_VERSION,
        ).single()

    assert set(rec["p"]) == {"profile_A", "profile_B"}, (
        f"Expected both profiles in union; got {rec['p']!r}"
    )


def test_third_profile_does_not_evict_prior_sibling(writer, neo4j_driver):
    """A, B, C all index the same node; all three must appear (no eviction)."""
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result], profiles=["prof_A"])
    writer.write_results([result], profiles=["prof_B"])
    writer.write_results([result], profiles=["prof_C"])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m.profile AS p",
            n="sale", v=TEST_VERSION,
        ).single()

    assert set(rec["p"]) == {"prof_A", "prof_B", "prof_C"}, (
        f"Expected all three profiles; got {rec['p']!r}"
    )


def test_same_profile_reindex_does_not_duplicate_entries(writer, neo4j_driver):
    """Index profile A twice; assert no duplicates in the profile array."""
    result = make_parse_result("sale", "sale.order")
    profiles = ["internal_17", "standard_17", "odoo_17"]
    writer.write_results([result], profiles=profiles)
    writer.write_results([result], profiles=profiles)

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m.profile AS p",
            n="sale", v=TEST_VERSION,
        ).single()

    p = rec["p"]
    assert len(p) == len(set(p)), (
        f"Duplicate entries in profile array after re-index: {p!r}"
    )


def test_profile_filter_in_ancestor_query(writer, neo4j_driver):
    """Cypher `$profile_name IN m.profile` returns node indexed under child profile."""
    # Index "sale" module under internal_17 (which includes odoo_17 in chain)
    profiles = ["internal_17", "standard_17", "odoo_17"]
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result], profiles=profiles)

    # Query filtering on ancestor profile "odoo_17" — should still find the node
    # because odoo_17 is IN the profile array
    with neo4j_driver.session() as session:
        rows = session.run(
            "MATCH (m:Module {odoo_version: $v}) "
            "WHERE $pn IN m.profile "
            "RETURN m.name AS name",
            v=TEST_VERSION, pn="odoo_17",
        ).data()

    names = [r["name"] for r in rows]
    assert "sale" in names, (
        f"Expected 'sale' when filtering profile_name='odoo_17' (ancestor), got {names}"
    )


def test_write_results_no_profiles_empty_array(writer, neo4j_driver):
    """write_results without profiles arg writes empty profile array (backward compat)."""
    result = make_parse_result("some_module", "some.model")
    writer.write_results([result])  # no profiles kwarg

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m.profile AS p",
            n="some_module", v=TEST_VERSION,
        ).single()

    assert rec is not None
    assert rec["p"] == [] or rec["p"] is None  # empty list or null — no crash


# ---------------------------------------------------------------------------
# A2 — Module/Method enrichment + USES_FIELD / DEPENDS_ON_FIELD edges
# ---------------------------------------------------------------------------


def _make_enriched_parse_result(
    module_name: str = "test_mod",
    model_name: str = "test.model",
    *,
    auto_install: bool = False,
    application: bool = False,
    category: str | None = None,
    summary: str | None = None,
    external_python: list | None = None,
    external_bin: list | None = None,
    repo_url: str | None = None,
    repo_id: int | None = None,
    fields_list: list | None = None,
    methods_list: list | None = None,
) -> ParseResult:
    """Build a ParseResult with A2 enrichment fields for integration tests."""
    module = ModuleInfo(
        name=module_name,
        odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo",
        path="/tmp",
        depends=[],
        version_raw="99.0.1.0.0",
        auto_install=auto_install,
        application=application,
        category=category,
        summary=summary,
        external_python=external_python or [],
        external_bin=external_bin or [],
        repo_url=repo_url,
        repo_id=repo_id,
    )
    model = ModelInfo(
        name=model_name,
        module=module_name,
        odoo_version=TEST_VERSION,
        fields=fields_list or [],
        methods=methods_list or [],
    )
    return ParseResult(module=module, models=[model])


def test_a2b_module_node_has_enrichment_fields(writer, neo4j_driver):
    """A2b: Module node persists auto_install, application, category, external_python,
    external_bin after write_results."""
    result = _make_enriched_parse_result(
        auto_install=True,
        application=True,
        category="Accounting",
        external_python=["pdfminer", "reportlab"],
        external_bin=["wkhtmltopdf"],
    )
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m",
            n="test_mod", v=TEST_VERSION,
        ).single()

    assert rec is not None
    node = rec["m"]
    assert node["auto_install"] is True
    assert node["application"] is True
    assert node["category"] == "Accounting"
    assert "pdfminer" in node["external_python"]
    assert "reportlab" in node["external_python"]
    assert "wkhtmltopdf" in node["external_bin"]


def test_summary_module_node_persists_and_coalesce(writer, neo4j_driver):
    """summary: Module node stores summary; write with summary=None does not overwrite."""
    result = _make_enriched_parse_result(
        module_name="sum_mod",
        summary="Manage sales orders",
    )
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m",
            n="sum_mod", v=TEST_VERSION,
        ).single()

    assert rec is not None
    assert rec["m"]["summary"] == "Manage sales orders"

    # Second write with summary=None must NOT erase the existing value (coalesce).
    result2 = _make_enriched_parse_result(module_name="sum_mod", summary=None)
    writer.write_results([result2])

    with neo4j_driver.session() as session:
        rec2 = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m",
            n="sum_mod", v=TEST_VERSION,
        ).single()

    assert rec2["m"]["summary"] == "Manage sales orders"


def test_a2c_module_node_has_repo_provenance(writer, neo4j_driver):
    """A2c: Module node persists repo_url and repo_id."""
    result = _make_enriched_parse_result(
        repo_url="https://github.com/example/odoo",
        repo_id=42,
    )
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m",
            n="test_mod", v=TEST_VERSION,
        ).single()

    assert rec is not None
    node = rec["m"]
    assert node["repo_url"] == "https://github.com/example/odoo"
    assert node["repo_id"] == 42


def test_a2a_method_node_has_docstring(writer, neo4j_driver):
    """A2a: Method node persists docstring field."""
    methods_list = [
        MethodInfo(
            name="action_confirm",
            has_super_call=True,
            decorators=[],
            docstring="Confirm the sale order.",
        )
    ]
    result = _make_enriched_parse_result(methods_list=methods_list)
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (mth:Method {name: $n, model: $m, odoo_version: $v}) RETURN mth",
            n="action_confirm", m="test.model", v=TEST_VERSION,
        ).single()

    assert rec is not None
    assert rec["mth"]["docstring"] == "Confirm the sale order."


def test_a2d_uses_field_edge_created(writer, neo4j_driver):
    """A2d: USES_FIELD edge from Method to Field when field_refs contains a known Field."""
    fields_list = [FieldInfo(name="amount", ttype="float")]
    methods_list = [
        MethodInfo(
            name="_compute_total",
            has_super_call=False,
            decorators=["api.depends"],
            field_refs=["amount"],
        )
    ]
    result = _make_enriched_parse_result(
        fields_list=fields_list,
        methods_list=methods_list,
    )
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            """
            MATCH (mth:Method {name: $mth_name, model: $m, odoo_version: $v})
                  -[:USES_FIELD]->
                  (f:Field {name: $f_name, model: $m, odoo_version: $v})
            RETURN count(*) AS cnt
            """,
            mth_name="_compute_total", m="test.model",
            f_name="amount", v=TEST_VERSION,
        ).single()

    assert rec["cnt"] == 1


def test_a2d_depends_on_field_edge_created(writer, neo4j_driver):
    """A2d: DEPENDS_ON_FIELD edge from Method to Field when depends path matches a Field."""
    fields_list = [FieldInfo(name="amount", ttype="float")]
    methods_list = [
        MethodInfo(
            name="_compute_total",
            has_super_call=False,
            decorators=["api.depends"],
            depends=["amount"],
            field_refs=[],
        )
    ]
    result = _make_enriched_parse_result(
        fields_list=fields_list,
        methods_list=methods_list,
    )
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            """
            MATCH (mth:Method {name: $mth_name, model: $m, odoo_version: $v})
                  -[:DEPENDS_ON_FIELD]->
                  (f:Field {name: $f_name, model: $m, odoo_version: $v})
            RETURN count(*) AS cnt
            """,
            mth_name="_compute_total", m="test.model",
            f_name="amount", v=TEST_VERSION,
        ).single()

    assert rec["cnt"] == 1


def test_a2d_depends_on_field_first_segment_only(writer, neo4j_driver):
    """A2d: @api.depends('partner_id.name') uses 'partner_id' as first segment."""
    fields_list = [FieldInfo(name="partner_id", ttype="many2one")]
    methods_list = [
        MethodInfo(
            name="_compute_partner_name",
            has_super_call=False,
            decorators=["api.depends"],
            depends=["partner_id.name"],
            field_refs=[],
        )
    ]
    result = _make_enriched_parse_result(
        fields_list=fields_list,
        methods_list=methods_list,
    )
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            """
            MATCH (mth:Method {name: $mth_name, model: $m, odoo_version: $v})
                  -[:DEPENDS_ON_FIELD]->
                  (f:Field {name: 'partner_id', model: $m, odoo_version: $v})
            RETURN count(*) AS cnt
            """,
            mth_name="_compute_partner_name", m="test.model", v=TEST_VERSION,
        ).single()

    assert rec["cnt"] == 1


def test_a2d_nonexistent_field_ref_no_edge(writer, neo4j_driver):
    """A2d: field_ref to non-existent field produces NO edge (MATCH, not MERGE - no stub)."""
    fields_list = []  # no fields in model
    methods_list = [
        MethodInfo(
            name="action_confirm",
            has_super_call=False,
            decorators=[],
            field_refs=["env", "nonexistent_field"],
        )
    ]
    result = _make_enriched_parse_result(
        fields_list=fields_list,
        methods_list=methods_list,
    )
    writer.write_results([result])

    with neo4j_driver.session() as session:
        # No USES_FIELD edges should exist
        rec = session.run(
            """
            MATCH (mth:Method {name: $n, model: $m, odoo_version: $v})
            OPTIONAL MATCH (mth)-[:USES_FIELD]->(f:Field)
            RETURN count(f) AS edge_cnt
            """,
            n="action_confirm", m="test.model", v=TEST_VERSION,
        ).single()
        # Also verify no stub Field nodes created for 'env' or 'nonexistent_field'
        stub_rec = session.run(
            """
            MATCH (f:Field {odoo_version: $v})
            WHERE f.name IN ['env', 'nonexistent_field']
            RETURN count(f) AS stub_cnt
            """,
            v=TEST_VERSION,
        ).single()

    assert rec["edge_cnt"] == 0, "No USES_FIELD edges for non-existent fields"
    assert stub_rec["stub_cnt"] == 0, "No stub Field nodes created for non-existent field refs"


# --- T3 (F-13): USES_FIELD MATCH must include module to prevent cross-module fan-out ---


def test_uses_field_scoped_to_own_module_no_fanout(writer, neo4j_driver):
    """F-13: method in module A referencing 'x' must only get USES_FIELD to A's Field 'x',
    NOT to module B's Field 'x' on the same model."""
    # Module A: defines field 'x' + a method that references it
    mod_a = ModuleInfo(
        name="mod_a", odoo_version=TEST_VERSION,
        repo="repo_a", path="/tmp", depends=[], version_raw="",
    )
    model_a = ModelInfo(
        name="test.model", module="mod_a", odoo_version=TEST_VERSION,
        fields=[FieldInfo(name="x", ttype="char")],
        methods=[MethodInfo(name="compute_x", field_refs=["x"])],
    )

    # Module B: also defines field 'x' on the same model (via _inherit extension)
    mod_b = ModuleInfo(
        name="mod_b", odoo_version=TEST_VERSION,
        repo="repo_b", path="/tmp", depends=["mod_a"], version_raw="",
    )
    model_b = ModelInfo(
        name="test.model", module="mod_b", odoo_version=TEST_VERSION,
        fields=[FieldInfo(name="x", ttype="char")],
        methods=[],
    )

    writer.write_results([
        ParseResult(module=mod_a, models=[model_a]),
        ParseResult(module=mod_b, models=[model_b]),
    ])

    with neo4j_driver.session() as session:
        # USES_FIELD from compute_x (mod_a) must link only to mod_a's Field 'x'
        rows = session.run(
            """
            MATCH (mth:Method {name: $n, model: $m, module: $mod_a, odoo_version: $v})
                  -[:USES_FIELD]->(f:Field {name: 'x', model: $m, odoo_version: $v})
            RETURN f.module AS fmod
            """,
            n="compute_x", m="test.model", mod_a="mod_a", v=TEST_VERSION,
        ).data()

    modules_hit = {r["fmod"] for r in rows}
    assert "mod_a" in modules_hit, "USES_FIELD must connect to mod_a's field"
    assert "mod_b" not in modules_hit, (
        "USES_FIELD must NOT fan-out to mod_b's field (F-13 cross-module fan-out)"
    )


# --- T4 (F-8): USES_FIELD / DEPENDS_ON_FIELD batch count verification ---


def test_uses_field_batch_edges_correct(writer, neo4j_driver):
    """F-8: batched UNWIND USES_FIELD must produce same edges as single-per-field loop."""
    fields_list = [
        FieldInfo(name="amount_total", ttype="monetary"),
        FieldInfo(name="partner_id", ttype="many2one"),
        FieldInfo(name="state", ttype="selection"),
    ]
    methods_list = [
        MethodInfo(
            name="_compute_all",
            field_refs=["amount_total", "partner_id", "state"],
        )
    ]
    result = _make_enriched_parse_result(
        fields_list=fields_list,
        methods_list=methods_list,
    )
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            """
            MATCH (mth:Method {name: $n, model: $m, odoo_version: $v})
                  -[:USES_FIELD]->(f:Field {model: $m, odoo_version: $v})
            RETURN count(f) AS cnt
            """,
            n="_compute_all", m="test.model", v=TEST_VERSION,
        ).single()

    assert rec["cnt"] == 3, (
        f"Expected 3 USES_FIELD edges (one per field_ref), got {rec['cnt']}"
    )


def test_depends_on_field_batch_deduplicates(writer, neo4j_driver):
    """F-8: DEPENDS_ON_FIELD with duplicate dotted paths de-duplicates by first segment."""
    fields_list = [
        FieldInfo(name="partner_id", ttype="many2one"),
    ]
    methods_list = [
        MethodInfo(
            name="_compute_partner",
            depends=["partner_id.name", "partner_id.email", "state"],
        )
    ]
    # Note: 'state' field is NOT in fields_list, so the MATCH for it finds nothing
    result = _make_enriched_parse_result(
        fields_list=fields_list,
        methods_list=methods_list,
    )
    writer.write_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            """
            MATCH (mth:Method {name: $n, model: $m, odoo_version: $v})
                  -[:DEPENDS_ON_FIELD]->(f:Field {model: $m, odoo_version: $v})
            RETURN count(f) AS cnt
            """,
            n="_compute_partner", m="test.model", v=TEST_VERSION,
        ).single()

    # partner_id.name and partner_id.email both reduce to first seg 'partner_id' → 1 edge
    # state has no Field node → 0 edges (MATCH, not MERGE)
    assert rec["cnt"] == 1, (
        f"Expected 1 DEPENDS_ON_FIELD edge (dedup 'partner_id.name'+'partner_id.email'), "
        f"got {rec['cnt']}"
    )


# --- T5 (F-12): Module MERGE ON MATCH must not overwrite repo_url/repo_id with NULL ---


def test_module_merge_coalesce_preserves_repo_url(writer, neo4j_driver):
    """F-12: Writing a module first with repo_url, then again with repo_url=None,
    must keep the original repo_url on the node (coalesce guard)."""
    # First write: module with a real repo_url
    mod_with_url = ModuleInfo(
        name="sale", odoo_version=TEST_VERSION,
        repo="odoo_repo", path="/tmp", depends=[], version_raw="",
        repo_url="git@github.com:odoo/odoo.git",
        repo_id=42,
    )
    result1 = ParseResult(module=mod_with_url, models=[])
    writer.write_results([result1])

    # Second write: same module but repo_url=None (dependency stub scenario)
    mod_no_url = ModuleInfo(
        name="sale", odoo_version=TEST_VERSION,
        repo="odoo_repo", path="/tmp", depends=[], version_raw="",
        repo_url=None,
        repo_id=None,
    )
    result2 = ParseResult(module=mod_no_url, models=[])
    writer.write_results([result2])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            "RETURN m.repo_url AS url, m.repo_id AS rid",
            n="sale", v=TEST_VERSION,
        ).single()

    assert rec["url"] == "git@github.com:odoo/odoo.git", (
        f"repo_url was overwritten with NULL (F-12); got {rec['url']!r}"
    )
    assert rec["rid"] == 42, (
        f"repo_id was overwritten with NULL (F-12); got {rec['rid']!r}"
    )


# --- T2: arch_snippet on View node (integration) ---


def test_view_node_arch_snippet_written(writer, neo4j_driver):
    """T2: View node in Neo4j must have arch_snippet for base views; None for extension views."""
    from src.indexer.models import ViewInfo, ViewParseResult, XPathInfo

    mod = ModuleInfo(
        name="sale", odoo_version=TEST_VERSION,
        repo="odoo_repo", path="/tmp", depends=[], version_raw="",
    )
    base_view = ViewInfo(
        xmlid="sale.view_order_form",
        name="Sale Order Form",
        model="sale.order",
        module="sale",
        odoo_version=TEST_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
        arch="<field name='arch' type='xml'><form><sheet><group name='main'>"
             "<field name='name'/></group></sheet></form></field>",
        arch_snippet="<field name='arch' type='xml'><form>",
    )
    ext_view = ViewInfo(
        xmlid="sale.view_order_form_inherit",
        name="Sale Order Form Inherit",
        model="sale.order",
        module="sale",
        odoo_version=TEST_VERSION,
        view_type="form",
        mode="extension",
        inherit_xmlid="sale.view_order_form",
        xpaths=[XPathInfo(expr="//field[@name='name']", position="after")],
        arch_snippet=None,
    )
    vpr = ViewParseResult(module=mod, views=[base_view, ext_view])
    writer.write_view_results([vpr])

    with neo4j_driver.session() as session:
        rows = session.run(
            "MATCH (v:View {odoo_version: $v}) RETURN v.xmlid AS xmlid, v.arch_snippet AS snip",
            v=TEST_VERSION,
        ).data()

    data = {r["xmlid"]: r["snip"] for r in rows}
    assert data.get("sale.view_order_form") is not None, "base view must have arch_snippet"
    assert "<form>" in (data.get("sale.view_order_form") or ""), "arch_snippet keeps form structure"
    assert data.get("sale.view_order_form_inherit") is None, "extension view arch_snippet=None"

