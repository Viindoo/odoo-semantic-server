# src/indexer/writer_neo4j.py
import logging

from neo4j import GraphDatabase

from .models import JSGraphResult, ParseResult, ViewParseResult

_logger = logging.getLogger(__name__)


def _write_parse_result(tx, result: ParseResult) -> None:
    module = result.module

    tx.run("""
        MERGE (m:Module {name: $name, odoo_version: $v})
        SET m.repo = $repo, m.path = $path, m.version_raw = $version_raw
    """, name=module.name, v=module.odoo_version,
         repo=module.repo, path=module.path, version_raw=module.version_raw)

    for dep in module.depends:
        tx.run("""
            MATCH (m:Module {name: $name, odoo_version: $v})
            MERGE (d:Module {name: $dep, odoo_version: $v})
            MERGE (m)-[:DEPENDS_ON]->(d)
        """, name=module.name, v=module.odoo_version, dep=dep)

    for model in result.models:
        tx.run("""
            MERGE (mod:Module {name: $module_name, odoo_version: $v})
            MERGE (m:Model {name: $name, module: $module_name, odoo_version: $v})
            SET m.is_abstract = $is_abstract,
                m.is_transient = $is_transient
            MERGE (m)-[:DEFINED_IN]->(mod)
        """, name=model.name, v=model.odoo_version,
             module_name=model.module,
             is_abstract=model.is_abstract,
             is_transient=model.is_transient)

        for parent_name in model.inherit:
            if parent_name == model.name:
                tx.run("""
                    MATCH (ext:Model {name: $name, module: $mod, odoo_version: $v})
                    MATCH (tip:Model {name: $name, odoo_version: $v})
                    WHERE tip.module <> $mod
                      AND NOT (:Model {name: $name, odoo_version: $v})-[:INHERITS]->(tip)
                    MERGE (ext)-[:INHERITS]->(tip)
                """, name=model.name, mod=model.module, v=model.odoo_version)
            else:
                rec = tx.run("""
                    MATCH (m:Model {name: $model_name, module: $mod, odoo_version: $v})
                    MATCH (parent:Model {name: $parent_name, odoo_version: $v})
                    MERGE (m)-[:INHERITS]->(parent)
                    RETURN 1 AS ok
                """, model_name=model.name, mod=model.module,
                     v=model.odoo_version, parent_name=parent_name).single()
                if rec is None:
                    _logger.warning(
                        "unresolved INHERITS: %s → %s (version %s) — parent model not indexed",
                        model.name, parent_name, model.odoo_version,
                    )
                    tx.run("""
                        MATCH (m:Model {name: $model_name, module: $mod, odoo_version: $v})
                        MERGE (placeholder:Model {name: $parent_name,
                                                  module: '__unresolved__', odoo_version: $v})
                        ON CREATE SET placeholder.unresolved = true
                        MERGE (m)-[:INHERITS {unresolved: true}]->(placeholder)
                    """, model_name=model.name, mod=model.module,
                         v=model.odoo_version, parent_name=parent_name)

        for delegated_model, via_field in model.inherits.items():
            rec = tx.run("""
                MATCH (m:Model {name: $name, module: $mod, odoo_version: $v})
                MATCH (d:Model {name: $delegated, odoo_version: $v})
                MERGE (m)-[:DELEGATES_TO {via_field: $via_field}]->(d)
                RETURN 1 AS ok
            """, name=model.name, mod=model.module, v=model.odoo_version,
                 delegated=delegated_model, via_field=via_field).single()
            if rec is None:
                _logger.warning(
                    "unresolved DELEGATES_TO: %s → %s (version %s) — target model not indexed",
                    model.name, delegated_model, model.odoo_version,
                )
                tx.run("""
                    MATCH (m:Model {name: $name, module: $mod, odoo_version: $v})
                    MERGE (placeholder:Model {name: $delegated,
                                              module: '__unresolved__', odoo_version: $v})
                    ON CREATE SET placeholder.unresolved = true
                    MERGE (m)-[:DELEGATES_TO {via_field: $via_field, unresolved: true}]
                          ->(placeholder)
                """, name=model.name, mod=model.module, v=model.odoo_version,
                     delegated=delegated_model, via_field=via_field)

        for fld in model.fields:
            tx.run("""
                MATCH (m:Model {name: $model_name, module: $mod, odoo_version: $v})
                MERGE (f:Field {name: $name, model: $model_name,
                               module: $mod, odoo_version: $v})
                SET f.ttype = $ttype, f.related = $related, f.compute = $compute,
                    f.stored = $stored, f.required = $required
                MERGE (f)-[:BELONGS_TO]->(m)
            """, model_name=model.name, mod=model.module, v=model.odoo_version,
                 name=fld.name, ttype=fld.ttype, related=fld.related,
                 compute=fld.compute, stored=fld.stored, required=fld.required)

        for mth in model.methods:
            tx.run("""
                MATCH (m:Model {name: $model_name, module: $mod, odoo_version: $v})
                MERGE (mth:Method {name: $name, model: $model_name,
                                   module: $mod, odoo_version: $v})
                SET mth.has_super_call = $has_super_call,
                    mth.decorators = $decorators
                MERGE (mth)-[:BELONGS_TO]->(m)
            """, model_name=model.name, mod=model.module, v=model.odoo_version,
                 name=mth.name, has_super_call=mth.has_super_call,
                 decorators=mth.decorators)


def _write_view_parse_result(tx, result: ViewParseResult) -> None:
    for view in result.views:
        tx.run("""
            MERGE (v:View {xmlid: $xmlid, odoo_version: $ver})
            SET v.name = $name, v.model = $model, v.module = $module,
                v.type = $view_type, v.mode = $mode,
                v.xpaths_exprs = $xpaths_exprs,
                v.xpaths_positions = $xpaths_positions
        """, xmlid=view.xmlid, ver=view.odoo_version,
             name=view.name, model=view.model, module=view.module,
             view_type=view.view_type, mode=view.mode,
             xpaths_exprs=[x.expr for x in view.xpaths],
             xpaths_positions=[x.position for x in view.xpaths])

        tx.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
            MERGE (mod:Module {name: $module, odoo_version: $ver})
            MERGE (v)-[:DEFINED_IN]->(mod)
        """, xmlid=view.xmlid, ver=view.odoo_version, module=view.module)

        # Create TARGETS_MODEL edge to all Model nodes with matching name in same version
        if view.model:
            tx.run("""
                MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
                MATCH (m:Model {name: $model_name, odoo_version: $ver})
                MERGE (v)-[:TARGETS_MODEL]->(m)
            """, xmlid=view.xmlid, ver=view.odoo_version, model_name=view.model)

        if view.inherit_xmlid:
            rec = tx.run("""
                MATCH (ext:View {xmlid: $xmlid, odoo_version: $ver})
                MATCH (base:View {xmlid: $inherit_xmlid, odoo_version: $ver})
                WHERE NOT coalesce(base.unresolved, false)
                MERGE (ext)-[:INHERITS_VIEW]->(base)
                RETURN 1 AS ok
            """, xmlid=view.xmlid, ver=view.odoo_version,
                 inherit_xmlid=view.inherit_xmlid).single()
            if rec is None:
                _logger.warning(
                    "unresolved INHERITS_VIEW: %s → %s (version %s) — parent view not indexed",
                    view.xmlid, view.inherit_xmlid, view.odoo_version,
                )
                tx.run("""
                    MATCH (ext:View {xmlid: $xmlid, odoo_version: $ver})
                    MERGE (placeholder:View {xmlid: $inherit_xmlid,
                                             module: '__unresolved__', odoo_version: $ver})
                    ON CREATE SET placeholder.unresolved = true
                    MERGE (ext)-[:INHERITS_VIEW {unresolved: true}]->(placeholder)
                """, xmlid=view.xmlid, ver=view.odoo_version,
                     inherit_xmlid=view.inherit_xmlid)

    for qweb in result.qweb:
        tx.run("""
            MERGE (t:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
            SET t.module = $module
        """, xmlid=qweb.xmlid, ver=qweb.odoo_version, module=qweb.module)

        tx.run("""
            MATCH (t:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
            MERGE (mod:Module {name: $module, odoo_version: $ver})
            MERGE (t)-[:DEFINED_IN]->(mod)
        """, xmlid=qweb.xmlid, ver=qweb.odoo_version, module=qweb.module)

        if qweb.inherit_xmlid:
            rec = tx.run("""
                MATCH (ext:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
                MATCH (base:QWebTmpl {xmlid: $inherit_xmlid, odoo_version: $ver})
                WHERE NOT coalesce(base.unresolved, false)
                MERGE (ext)-[:EXTENDS_TMPL]->(base)
                RETURN 1 AS ok
            """, xmlid=qweb.xmlid, ver=qweb.odoo_version,
                 inherit_xmlid=qweb.inherit_xmlid).single()
            if rec is None:
                _logger.warning(
                    "unresolved EXTENDS_TMPL: %s → %s (version %s) — base template not indexed",
                    qweb.xmlid, qweb.inherit_xmlid, qweb.odoo_version,
                )
                tx.run("""
                    MATCH (ext:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
                    MERGE (placeholder:QWebTmpl {xmlid: $inherit_xmlid,
                                                 module: '__unresolved__', odoo_version: $ver})
                    ON CREATE SET placeholder.unresolved = true
                    MERGE (ext)-[:EXTENDS_TMPL {unresolved: true}]->(placeholder)
                """, xmlid=qweb.xmlid, ver=qweb.odoo_version,
                     inherit_xmlid=qweb.inherit_xmlid)


def _write_js_graph_result(tx, result: JSGraphResult) -> None:
    # Write OWLComp nodes first so PATCHES can resolve against them
    for comp in result.components:
        tx.run("""
            MERGE (mod:Module {name: $module_name, odoo_version: $v})
            MERGE (c:OWLComp {name: $name, module: $module_name, odoo_version: $v})
            SET c.template = $template, c.extends = $extends,
                c.bound_model = $bound_model, c.file_path = $file_path
            MERGE (c)-[:DEFINED_IN]->(mod)
        """, module_name=comp.module, v=comp.odoo_version,
             name=comp.name, template=comp.template, extends=comp.extends,
             bound_model=comp.bound_model, file_path=comp.file_path)

        # EXTENDS edge — only when parent OWLComp exists in same version (no placeholder)
        if comp.extends:
            tx.run("""
                MATCH (child:OWLComp {name: $name, module: $mod, odoo_version: $v})
                MATCH (parent:OWLComp {name: $parent, odoo_version: $v})
                MERGE (child)-[:EXTENDS]->(parent)
            """, name=comp.name, mod=comp.module, v=comp.odoo_version,
                 parent=comp.extends)

        # BOUND_TO edge — only when Model exists; skip silently otherwise
        if comp.bound_model:
            tx.run("""
                MATCH (c:OWLComp {name: $name, module: $mod, odoo_version: $v})
                MATCH (m:Model {name: $bound, odoo_version: $v})
                MERGE (c)-[:BOUND_TO]->(m)
            """, name=comp.name, mod=comp.module, v=comp.odoo_version,
                 bound=comp.bound_model)

    # Write JSPatch nodes + PATCHES edges
    for patch in result.patches:
        tx.run("""
            MERGE (mod:Module {name: $module_name, odoo_version: $v})
            MERGE (j:JSPatch {target: $target, patch_name: $patch_name,
                              module: $module_name, odoo_version: $v})
            SET j.era = $era, j.file_path = $file_path
            MERGE (j)-[:DEFINED_IN]->(mod)
        """, module_name=patch.module, v=patch.odoo_version,
             target=patch.target, patch_name=patch.patch_name,
             era=patch.era, file_path=patch.file_path)

        # PATCHES edge — try resolve to existing OWLComp, else create placeholder
        rec = tx.run("""
            MATCH (j:JSPatch {target: $target, patch_name: $pn,
                              module: $mod, odoo_version: $v})
            MATCH (c:OWLComp {name: $target, odoo_version: $v})
            MERGE (j)-[:PATCHES]->(c)
            RETURN 1
        """, target=patch.target, pn=patch.patch_name,
             mod=patch.module, v=patch.odoo_version).single()
        if rec is None:
            tx.run("""
                MATCH (j:JSPatch {target: $target, patch_name: $pn,
                                  module: $mod, odoo_version: $v})
                MERGE (placeholder:OWLComp {name: $target,
                                            module: '__unresolved__', odoo_version: $v})
                ON CREATE SET placeholder.unresolved = true
                MERGE (j)-[:PATCHES {unresolved: true}]->(placeholder)
            """, target=patch.target, pn=patch.patch_name,
                 mod=patch.module, v=patch.odoo_version)


class Neo4jWriter:
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

    def setup_indexes(self) -> None:
        with self.driver.session() as session:
            for stmt in [
                "CREATE INDEX IF NOT EXISTS FOR (n:Module) ON (n.name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Model)  ON (n.name, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Field)"
                " ON (n.name, n.model, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Method)"
                " ON (n.name, n.model, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:View) ON (n.xmlid, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:QWebTmpl) ON (n.xmlid, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:JSPatch)"
                " ON (n.target, n.patch_name, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:OWLComp)"
                " ON (n.name, n.module, n.odoo_version)",
            ]:
                session.run(stmt)

    def write_results(self, results: list[ParseResult]) -> None:
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_parse_result, result)

    def write_view_results(self, results: list[ViewParseResult]) -> None:
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_view_parse_result, result)

    def write_js_graph_results(self, results: list[JSGraphResult]) -> None:
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_js_graph_result, result)
