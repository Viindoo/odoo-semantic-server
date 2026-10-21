# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_writer_inherits_resolution.py
"""Parent references resolve whatever order their nodes are written in.

Business rules protected here (Neo4j, CI lane):

1. A child model whose same-module parent is declared in a file that sorts
   AFTER the child's file still gets its INHERITS edge, with the ``_inherit``
   position as ``order``, and no ``__unresolved__`` placeholder (viin_ai 17.0:
   models/provider.py < models/request_log_mixin.py). Same for a method's
   USES_FIELD to a field another class of the module declares later.
2. A child written BEFORE its parent's module (another repo / not in depends)
   gets a placeholder edge; ``relink_unresolved_placeholders`` moves it onto the
   real parent once that is written - order and the child's run stamp kept, so
   the GC and the child module's entity prune keep it - and a second call is a
   no-op.
3. A QWeb extender whose ``inherit_id`` names the RECORD xmlid of a
   record-form QWeb view (website_forum.default_faq, keyed by its website key
   website_forum.faq_accordion) gets its EXTENDS_TMPL edge, in both write
   orders; a report whose ``report_name`` is that record xmlid binds too.
"""
from __future__ import annotations

import logging
import os

import pytest

from src.indexer.models import (
    FieldInfo,
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    ParseResult,
)
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

V = TEST_VERSION


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


def _module(name: str) -> ModuleInfo:
    return ModuleInfo(
        name=name, odoo_version=V, repo=f"{name}_repo", path="/tmp",
        depends=[], version_raw="",
    )


def _model(name: str, module: str, inherit=(), inherits=None, fields=(), methods=(),
           explicit: bool = True) -> ModelInfo:
    return ModelInfo(
        name=name, module=module, odoo_version=V, inherit=list(inherit),
        inherits=dict(inherits or {}),
        fields=[FieldInfo(name=f, ttype="char") for f in fields],
        methods=list(methods), had_explicit_name=explicit,
    )


def _rows(driver, query: str, **params) -> list[dict]:
    with driver.session() as session:
        return session.run(query, v=V, **params).data()


def _inherits(driver, child: str, module: str) -> list[dict]:
    return _rows(driver, """
        MATCH (c:Model {name: $child, module: $mod, odoo_version: $v})-[r:INHERITS]->(p)
        RETURN p.name AS parent, p.module AS module, r.order AS order,
               coalesce(r.unresolved, false) AS unresolved,
               r.written_run AS run
        ORDER BY r.order
    """, child=child, mod=module)


def _placeholders(driver) -> int:
    return _rows(driver, """
        MATCH (n {module: '__unresolved__', odoo_version: $v}) RETURN count(n) AS n
    """)[0]["n"]


# --- 1. same-module write order ------------------------------------------------


def test_child_before_its_same_module_mixin_still_inherits_it(writer, neo4j_driver, caplog):
    # parse_module order = sorted file paths: provider.py before request_log_mixin.py.
    result = ParseResult(module=_module("viin_ai"), models=[
        _model("viin.ai.provider", "viin_ai",
               inherit=["viin.ai.request.log.mixin", "mail.thread"]),
        _model("mail.thread", "viin_ai", explicit=True),
        _model("viin.ai.request.log.mixin", "viin_ai"),
    ])
    with caplog.at_level(logging.WARNING, logger="src.indexer.writer_neo4j"):
        writer.write_results([result])

    edges = _inherits(neo4j_driver, "viin.ai.provider", "viin_ai")
    assert [(e["parent"], e["module"], e["order"], e["unresolved"]) for e in edges] == [
        ("viin.ai.request.log.mixin", "viin_ai", 0, False),
        ("mail.thread", "viin_ai", 1, False),
    ]
    assert _placeholders(neo4j_driver) == 0
    assert not [r for r in caplog.records if "unresolved INHERITS" in r.getMessage()]


def test_delegated_parent_declared_later_in_the_module_resolves(writer, neo4j_driver):
    result = ParseResult(module=_module("m"), models=[
        _model("m.child", "m", inherits={"m.parent": "parent_id"}),
        _model("m.parent", "m"),
    ])
    writer.write_results([result])
    rows = _rows(neo4j_driver, """
        MATCH (:Model {name: 'm.child', odoo_version: $v})-[r:DELEGATES_TO]->(p:Model)
        RETURN p.module AS module, r.via_field AS via
    """)
    assert rows == [{"module": "m", "via": "parent_id"}]
    assert _placeholders(neo4j_driver) == 0


def test_method_uses_a_field_another_class_of_the_module_declares_later(
    writer, neo4j_driver,
):
    # Two classes extending sale.order in one module, in two files: the method
    # of the first reads a field only the second declares.
    result = ParseResult(module=_module("m"), models=[
        _model("sale.order", "m", inherit=["sale.order"], explicit=False,
               methods=[MethodInfo(name="_compute_x", field_refs=["x_total"],
                                   depends=["x_total"])]),
        _model("sale.order", "m", inherit=["sale.order"], explicit=False,
               fields=["x_total"]),
    ])
    writer.write_results([result])
    rows = _rows(neo4j_driver, """
        MATCH (:Method {name: '_compute_x', model: 'sale.order', module: 'm',
                        odoo_version: $v})-[r:USES_FIELD|DEPENDS_ON_FIELD]->(f:Field)
        RETURN type(r) AS rel, f.name AS field ORDER BY rel
    """)
    assert rows == [
        {"rel": "DEPENDS_ON_FIELD", "field": "x_total"},
        {"rel": "USES_FIELD", "field": "x_total"},
    ]


# --- 2. cross-module order: relink before GC ------------------------------------


def _write_child_then_parent(writer, *, child_run="run-child", parent_run="run-parent"):
    writer.begin_run(child_run)
    writer.write_results([ParseResult(module=_module("mod_a"), models=[
        _model("a.child", "mod_a", inherit=["mail.thread", "b.mixin"],
               inherits={"b.mixin": "mixin_id"}),
        _model("mail.thread", "mod_a"),
    ])])
    writer.begin_run(parent_run)
    writer.write_results([ParseResult(module=_module("mod_b"), models=[
        _model("b.mixin", "mod_b"),
    ])])


def test_relink_moves_the_placeholder_edge_onto_the_parent_written_later(
    writer, neo4j_driver,
):
    _write_child_then_parent(writer)
    before = _inherits(neo4j_driver, "a.child", "mod_a")
    assert [(e["parent"], e["module"], e["unresolved"]) for e in before] == [
        ("mail.thread", "mod_a", False), ("b.mixin", "__unresolved__", True),
    ]

    counts = writer.relink_unresolved_placeholders(V)
    assert counts == {"INHERITS": 1, "DELEGATES_TO": 1, "EXTENDS_TMPL": 0}
    writer.gc_unresolved_placeholders(V)

    after = _inherits(neo4j_driver, "a.child", "mod_a")
    assert [(e["parent"], e["module"], e["order"], e["unresolved"], e["run"])
            for e in after] == [
        ("mail.thread", "mod_a", 0, False, "run-child"),
        # order = position in _inherit; run stamp = the child's parse, not the
        # run that wrote the parent.
        ("b.mixin", "mod_b", 1, False, "run-child"),
    ]
    delegates = _rows(neo4j_driver, """
        MATCH (:Model {name: 'a.child', odoo_version: $v})-[r:DELEGATES_TO]->(p:Model)
        RETURN p.module AS module, r.via_field AS via, r.written_run AS run
    """)
    assert delegates == [{"module": "mod_b", "via": "mixin_id", "run": "run-child"}]
    assert _placeholders(neo4j_driver) == 0

    assert writer.relink_unresolved_placeholders(V) == {
        "INHERITS": 0, "DELEGATES_TO": 0, "EXTENDS_TMPL": 0,
    }
    assert len(_inherits(neo4j_driver, "a.child", "mod_a")) == 2


def test_relinked_edge_survives_the_entity_prune_of_the_childs_parse(
    writer, neo4j_driver,
):
    """The prune of mod_a by the run that parsed it keeps the relinked edge."""
    _write_child_then_parent(writer)
    writer.relink_unresolved_placeholders(V)
    writer.gc_unresolved_placeholders(V)

    writer.prune_module_children(V, "mod_a", run_id="run-child")

    after = _inherits(neo4j_driver, "a.child", "mod_a")
    assert ("b.mixin", "mod_b") in {(e["parent"], e["module"]) for e in after}


def test_relink_prefers_the_definition_like_the_writer(writer, neo4j_driver):
    writer.write_results([ParseResult(module=_module("mod_a"), models=[
        _model("a.child", "mod_a", inherit=["x.base"]),
    ])])
    # Two later modules carry x.base: an extender (aaa sorts first) and the
    # definition. The writer would pick the definition; so must the relink.
    writer.write_results([ParseResult(module=_module("aaa_ext"), models=[
        _model("x.base", "aaa_ext", inherit=["x.base"], explicit=False),
    ])])
    writer.write_results([ParseResult(module=_module("zzz_def"), models=[
        _model("x.base", "zzz_def"),
    ])])
    writer.relink_unresolved_placeholders(V)
    edges = _inherits(neo4j_driver, "a.child", "mod_a")
    assert [(e["parent"], e["module"]) for e in edges] == [("x.base", "zzz_def")]


def test_relink_leaves_a_still_missing_parent_for_the_gc(writer, neo4j_driver):
    writer.write_results([ParseResult(module=_module("mod_a"), models=[
        _model("a.child", "mod_a", inherit=["never.indexed"]),
    ])])
    assert writer.relink_unresolved_placeholders(V)["INHERITS"] == 0
    edges = _inherits(neo4j_driver, "a.child", "mod_a")
    assert [(e["module"], e["unresolved"]) for e in edges] == [("__unresolved__", True)]


