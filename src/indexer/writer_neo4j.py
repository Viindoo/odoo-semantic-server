# src/indexer/writer_neo4j.py
import logging

from neo4j import GraphDatabase

from .models import ParseResult

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
            ]:
                session.run(stmt)

    def write_results(self, results: list[ParseResult]) -> None:
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_parse_result, result)
