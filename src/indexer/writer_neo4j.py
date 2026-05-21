# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/writer_neo4j.py
import logging

from neo4j import GraphDatabase

from src.constants import (
    NEO4J_WRITE_BATCH_SIZE,
    REL_CHECKS,
    REL_DEFINED_IN,
    REL_DEPENDS_ON,
    REL_IMPORTS,
    REL_INHERITS,
    REL_INHERITS_VIEW,
    REL_OF_COMMAND,
    REL_REPLACED_BY,
    REL_TARGETS_MODEL,
    REL_USES_CORE_SYMBOL,
)

from .diff_engine import DiffResult
from .models import (
    CLICommandInfo,
    CLIFlagInfo,
    CoreSymbolInfo,
    JSGraphResult,
    LintRuleInfo,
    ParseResult,
    PatternExample,
    StylesheetInfo,
    ViewParseResult,
)

_logger = logging.getLogger(__name__)


def _write_parse_result(tx, result: ParseResult, profiles: list[str]) -> None:
    module = result.module

    tx.run("""
        MERGE (m:Module {name: $name, odoo_version: $v})
        ON CREATE SET m.profile = $profiles
        ON MATCH  SET m.profile =
            [x IN coalesce(m.profile, []) WHERE NOT x IN $profiles] + $profiles
        SET m.repo = $repo, m.path = $path, m.version_raw = $version_raw,
            m.edition = $edition,
            m.viindoo_equivalent_qname = $vvq,
            m.last_commit_sha = $commit_sha
    """, name=module.name, v=module.odoo_version,
         repo=module.repo, path=module.path, version_raw=module.version_raw,
         edition=module.edition,
         vvq=module.viindoo_equivalent_qname,
         commit_sha=module.commit_sha,
         profiles=profiles)

    for dep in module.depends:
        tx.run(f"""
            MATCH (m:Module {{name: $name, odoo_version: $v}})
            MERGE (d:Module {{name: $dep, odoo_version: $v}})
            ON CREATE SET d.profile = $profiles
            ON MATCH  SET d.profile =
                [x IN coalesce(d.profile, []) WHERE NOT x IN $profiles] + $profiles
            MERGE (m)-[:{REL_DEPENDS_ON}]->(d)
        """, name=module.name, v=module.odoo_version, dep=dep,
             profiles=profiles)

    for model in result.models:
        tx.run(f"""
            MERGE (mod:Module {{name: $module_name, odoo_version: $v}})
            MERGE (m:Model {{name: $name, module: $module_name, odoo_version: $v}})
            ON CREATE SET m.is_transient = $is_transient,
                          m.is_abstract = $is_abstract,
                          m.had_explicit_name = $had_explicit_name,
                          m.is_definition = ($had_explicit_name AND NOT $name IN $inherit_list),
                          m.profile = $profiles
            ON MATCH  SET m.is_abstract = $is_abstract,
                          m.is_transient = $is_transient,
                          m.had_explicit_name =
                              coalesce(m.had_explicit_name, false) OR $had_explicit_name,
                          m.is_definition =
                              coalesce(m.is_definition, false)
                              OR ($had_explicit_name AND NOT $name IN $inherit_list),
                          m.profile =
                              [x IN coalesce(m.profile, []) WHERE NOT x IN $profiles] + $profiles
            MERGE (m)-[:{REL_DEFINED_IN}]->(mod)
        """, name=model.name, v=model.odoo_version,
             module_name=model.module,
             is_abstract=model.is_abstract,
             is_transient=model.is_transient,
             had_explicit_name=model.had_explicit_name,
             inherit_list=model.inherit,
             profiles=profiles)

        for idx, parent_name in enumerate(model.inherit):
            if parent_name == model.name:
                # Self-extend: module B's copy of `sale.order` inherits from the
                # definition node in another module.  The old guard used
                # `WHERE NOT (:Model {name})-[:INHERITS]->(tip)` which blocked
                # ANY new edge once ONE already existed — breaking multi-module
                # extension and also preventing `r.order` from being set on
                # existing stale edges.  Use per-pair MERGE instead:
                # ON CREATE sets order for new edges; ON MATCH backfills NULL
                # order from stale edges without overwriting valid existing values.
                tx.run(f"""
                    MATCH (ext:Model {{name: $name, module: $mod, odoo_version: $v}})
                    MATCH (tip:Model {{name: $name, odoo_version: $v}})
                    WHERE tip.module <> $mod
                    MERGE (ext)-[r:{REL_INHERITS}]->(tip)
                    ON CREATE SET r.order = $order
                    ON MATCH  SET r.order = coalesce(r.order, $order)
                """, name=model.name, mod=model.module, v=model.odoo_version,
                     order=idx)
            else:
                rec = tx.run(f"""
                    MATCH (m:Model {{name: $model_name, module: $mod, odoo_version: $v}})
                    MATCH (parent:Model {{name: $parent_name, odoo_version: $v}})
                    WHERE NOT coalesce(parent.unresolved, false)
                    MERGE (m)-[r:{REL_INHERITS}]->(parent)
                    SET r.order = $order
                    RETURN 1 AS ok
                """, model_name=model.name, mod=model.module,
                     v=model.odoo_version, parent_name=parent_name,
                     order=idx).single()
                if rec is None:
                    _logger.warning(
                        "unresolved INHERITS: %s → %s (version %s) — parent model not indexed",
                        model.name, parent_name, model.odoo_version,
                    )
                    tx.run(f"""
                        MATCH (m:Model {{name: $model_name, module: $mod, odoo_version: $v}})
                        MERGE (placeholder:Model {{name: $parent_name,
                                                  module: '__unresolved__', odoo_version: $v}})
                        ON CREATE SET placeholder.unresolved = true,
                                      placeholder.is_definition = false,
                                      placeholder.profile = $profiles
                        ON MATCH  SET placeholder.profile =
                            [x IN coalesce(placeholder.profile, [])
                             WHERE NOT x IN $profiles] + $profiles
                        MERGE (m)-[r:{REL_INHERITS} {{unresolved: true}}]->(placeholder)
                        SET r.order = $order
                    """, model_name=model.name, mod=model.module,
                         v=model.odoo_version, parent_name=parent_name,
                         order=idx, profiles=profiles)

        for delegated_model, via_field in model.inherits.items():
            rec = tx.run("""
                MATCH (m:Model {name: $name, module: $mod, odoo_version: $v})
                MATCH (d:Model {name: $delegated, odoo_version: $v})
                WHERE NOT coalesce(d.unresolved, false)
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
                    ON CREATE SET placeholder.unresolved = true,
                                  placeholder.is_definition = false,
                                  placeholder.profile = $profiles
                    ON MATCH  SET placeholder.profile =
                        [x IN coalesce(placeholder.profile, [])
                         WHERE NOT x IN $profiles] + $profiles
                    MERGE (m)-[:DELEGATES_TO {via_field: $via_field, unresolved: true}]
                          ->(placeholder)
                """, name=model.name, mod=model.module, v=model.odoo_version,
                     delegated=delegated_model, via_field=via_field,
                     profiles=profiles)

        for fld in model.fields:
            tx.run("""
                MATCH (m:Model {name: $model_name, module: $mod, odoo_version: $v})
                MERGE (f:Field {name: $name, model: $model_name,
                               module: $mod, odoo_version: $v})
                ON CREATE SET f.profile = $profiles
                ON MATCH  SET f.profile =
                    [x IN coalesce(f.profile, []) WHERE NOT x IN $profiles] + $profiles
                SET f.ttype = $ttype, f.related = $related, f.compute = $compute,
                    f.stored = $stored, f.required = $required,
                    f.comodel_name = $comodel_name
                MERGE (f)-[:BELONGS_TO]->(m)
            """, model_name=model.name, mod=model.module, v=model.odoo_version,
                 name=fld.name, ttype=fld.ttype, related=fld.related,
                 compute=fld.compute, stored=fld.stored, required=fld.required,
                 comodel_name=fld.comodel_name,
                 profiles=profiles)

        for mth in model.methods:
            tx.run("""
                MATCH (m:Model {name: $model_name, module: $mod, odoo_version: $v})
                MERGE (mth:Method {name: $name, model: $model_name,
                                   module: $mod, odoo_version: $v})
                ON CREATE SET mth.profile = $profiles
                ON MATCH  SET mth.profile =
                    [x IN coalesce(mth.profile, []) WHERE NOT x IN $profiles] + $profiles
                SET mth.has_super_call = $has_super_call,
                    mth.decorators = $decorators,
                    mth.convention_kind = $ck,
                    mth.super_safety = $ss,
                    mth.return_required = $rr,
                    mth.signature = $sig,
                    mth.depends = $depends
                MERGE (mth)-[:BELONGS_TO]->(m)
            """, model_name=model.name, mod=model.module, v=model.odoo_version,
                 name=mth.name, has_super_call=mth.has_super_call,
                 decorators=mth.decorators,
                 ck=mth.convention_kind, ss=mth.super_safety, rr=mth.return_required,
                 sig=mth.signature, depends=mth.depends, profiles=profiles)

            # M4.5 WI6: USES_CORE_SYMBOL edge — silent skip when target absent
            # or status not in {deprecated, removed} (per ADR-0002 §3 V0 scope).
            for ref in mth.core_symbol_refs:
                tx.run(f"""
                    MATCH (mth:Method {{name: $name, model: $model_name,
                                       module: $mod, odoo_version: $v}})
                    MATCH (cs:CoreSymbol {{odoo_version: $v}})
                    WHERE cs.qualified_name ENDS WITH '.' + $ref
                      AND cs.status IN ['deprecated', 'removed']
                    MERGE (mth)-[:{REL_USES_CORE_SYMBOL}]->(cs)
                """, name=mth.name, model_name=model.name, mod=model.module,
                     v=model.odoo_version, ref=ref)


def _write_view_parse_result(tx, result: ViewParseResult, profiles: list[str]) -> None:
    for view in result.views:
        tx.run("""
            MERGE (v:View {xmlid: $xmlid, odoo_version: $ver})
            ON CREATE SET v.profile = $profiles
            ON MATCH  SET v.profile =
                [x IN coalesce(v.profile, []) WHERE NOT x IN $profiles] + $profiles
            SET v.name = $name, v.model = $model, v.module = $module,
                v.type = $view_type, v.mode = $mode,
                v.xpaths_exprs = $xpaths_exprs,
                v.xpaths_positions = $xpaths_positions
        """, xmlid=view.xmlid, ver=view.odoo_version,
             name=view.name, model=view.model, module=view.module,
             view_type=view.view_type, mode=view.mode,
             xpaths_exprs=[x.expr for x in view.xpaths],
             xpaths_positions=[x.position for x in view.xpaths],
             profiles=profiles)

        tx.run(f"""
            MATCH (v:View {{xmlid: $xmlid, odoo_version: $ver}})
            MERGE (mod:Module {{name: $module, odoo_version: $ver}})
            MERGE (v)-[:{REL_DEFINED_IN}]->(mod)
        """, xmlid=view.xmlid, ver=view.odoo_version, module=view.module)

        # Create TARGETS_MODEL edge to all Model nodes with matching name in same version
        if view.model:
            tx.run(f"""
                MATCH (v:View {{xmlid: $xmlid, odoo_version: $ver}})
                MATCH (m:Model {{name: $model_name, odoo_version: $ver}})
                MERGE (v)-[:{REL_TARGETS_MODEL}]->(m)
            """, xmlid=view.xmlid, ver=view.odoo_version, model_name=view.model)

        if view.inherit_xmlid:
            rec = tx.run(f"""
                MATCH (ext:View {{xmlid: $xmlid, odoo_version: $ver}})
                MATCH (base:View {{xmlid: $inherit_xmlid, odoo_version: $ver}})
                WHERE NOT coalesce(base.unresolved, false)
                MERGE (ext)-[:{REL_INHERITS_VIEW}]->(base)
                RETURN 1 AS ok
            """, xmlid=view.xmlid, ver=view.odoo_version,
                 inherit_xmlid=view.inherit_xmlid).single()
            if rec is None:
                _logger.warning(
                    "unresolved INHERITS_VIEW: %s → %s (version %s) — parent view not indexed",
                    view.xmlid, view.inherit_xmlid, view.odoo_version,
                )
                tx.run(f"""
                    MATCH (ext:View {{xmlid: $xmlid, odoo_version: $ver}})
                    MERGE (placeholder:View {{xmlid: $inherit_xmlid,
                                             module: '__unresolved__', odoo_version: $ver}})
                    ON CREATE SET placeholder.unresolved = true,
                                  placeholder.profile = $profiles
                    ON MATCH  SET placeholder.profile =
                        [x IN coalesce(placeholder.profile, [])
                         WHERE NOT x IN $profiles] + $profiles
                    MERGE (ext)-[:{REL_INHERITS_VIEW} {{unresolved: true}}]->(placeholder)
                """, xmlid=view.xmlid, ver=view.odoo_version,
                     inherit_xmlid=view.inherit_xmlid, profiles=profiles)

    for qweb in result.qweb:
        tx.run("""
            MERGE (t:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
            ON CREATE SET t.profile = $profiles
            ON MATCH  SET t.profile =
                [x IN coalesce(t.profile, []) WHERE NOT x IN $profiles] + $profiles
            SET t.module = $module
        """, xmlid=qweb.xmlid, ver=qweb.odoo_version, module=qweb.module, profiles=profiles)

        tx.run(f"""
            MATCH (t:QWebTmpl {{xmlid: $xmlid, odoo_version: $ver}})
            MERGE (mod:Module {{name: $module, odoo_version: $ver}})
            MERGE (t)-[:{REL_DEFINED_IN}]->(mod)
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
                    ON CREATE SET placeholder.unresolved = true,
                                  placeholder.profile = $profiles
                    ON MATCH  SET placeholder.profile =
                        [x IN coalesce(placeholder.profile, [])
                         WHERE NOT x IN $profiles] + $profiles
                    MERGE (ext)-[:EXTENDS_TMPL {unresolved: true}]->(placeholder)
                """, xmlid=qweb.xmlid, ver=qweb.odoo_version,
                     inherit_xmlid=qweb.inherit_xmlid, profiles=profiles)


def _write_js_graph_result(tx, result: JSGraphResult, profiles: list[str]) -> None:
    # Write OWLComp nodes first so PATCHES can resolve against them
    for comp in result.components:
        tx.run(f"""
            MERGE (mod:Module {{name: $module_name, odoo_version: $v}})
            MERGE (c:OWLComp {{name: $name, module: $module_name, odoo_version: $v}})
            ON CREATE SET c.profile = $profiles
            ON MATCH  SET c.profile =
                [x IN coalesce(c.profile, []) WHERE NOT x IN $profiles] + $profiles
            SET c.template = $template, c.extends = $extends,
                c.bound_model = $bound_model, c.file_path = $file_path
            MERGE (c)-[:{REL_DEFINED_IN}]->(mod)
        """, module_name=comp.module, v=comp.odoo_version,
             name=comp.name, template=comp.template, extends=comp.extends,
             bound_model=comp.bound_model, file_path=comp.file_path,
             profiles=profiles)

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
        tx.run(f"""
            MERGE (mod:Module {{name: $module_name, odoo_version: $v}})
            MERGE (j:JSPatch {{target: $target, patch_name: $patch_name,
                              module: $module_name, odoo_version: $v}})
            ON CREATE SET j.profile = $profiles
            ON MATCH  SET j.profile =
                [x IN coalesce(j.profile, []) WHERE NOT x IN $profiles] + $profiles
            SET j.era = $era, j.file_path = $file_path
            MERGE (j)-[:{REL_DEFINED_IN}]->(mod)
        """, module_name=patch.module, v=patch.odoo_version,
             target=patch.target, patch_name=patch.patch_name,
             era=patch.era, file_path=patch.file_path, profiles=profiles)

        # PATCHES edge — try resolve to existing OWLComp, else create placeholder
        rec = tx.run("""
            MATCH (j:JSPatch {target: $target, patch_name: $pn,
                              module: $mod, odoo_version: $v})
            MATCH (c:OWLComp {name: $target, odoo_version: $v})
            WHERE NOT coalesce(c.unresolved, false)
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
                ON CREATE SET placeholder.unresolved = true,
                              placeholder.profile = $profiles
                ON MATCH  SET placeholder.profile =
                    [x IN coalesce(placeholder.profile, []) WHERE NOT x IN $profiles] + $profiles
                MERGE (j)-[:PATCHES {unresolved: true}]->(placeholder)
            """, target=patch.target, pn=patch.patch_name,
                 mod=patch.module, v=patch.odoo_version,
                 profiles=profiles)


def _chunked(items, size):
    """Yield successive chunks of `items` of length up to `size`."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _write_core_symbols_batch(tx, symbols: list[CoreSymbolInfo]) -> None:
    for s in symbols:
        tx.run("""
            MERGE (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
            SET cs.kind = $kind,
                cs.signature = $sig,
                cs.file_path = $fp,
                cs.line = $line,
                cs.status = $status,
                cs.replacement_qname = $repl
        """, qn=s.qualified_name, v=s.odoo_version,
             kind=s.kind, sig=s.signature, fp=s.file_path,
             line=s.line, status=s.status, repl=s.replacement_qname)


def _write_replaced_by_edges(tx, replaced: list[tuple[str, str]],
                             from_version: str, to_version: str) -> None:
    for old_qn, new_qn in replaced:
        tx.run(f"""
            MATCH (a:CoreSymbol {{qualified_name: $old_qn, odoo_version: $vfrom}})
            MATCH (b:CoreSymbol {{qualified_name: $new_qn, odoo_version: $vto}})
            MERGE (a)-[:{REL_REPLACED_BY}]->(b)
        """, old_qn=old_qn, new_qn=new_qn,
             vfrom=from_version, vto=to_version)


def _write_lint_rules_batch(tx, rules: list[LintRuleInfo]) -> None:
    for r in rules:
        tx.run("""
            MERGE (l:LintRule {rule_id: $rid, odoo_version: $v})
            SET l.kind = $kind,
                l.message = $msg,
                l.severity = $sev,
                l.file_pattern = $fp,
                l.fix_template = $fix,
                l.core_symbol_qname = $cs
        """, rid=r.rule_id, v=r.odoo_version, kind=r.kind,
             msg=r.message, sev=r.severity, fp=r.file_pattern,
             fix=r.fix_template, cs=r.core_symbol_qname)
        # CHECKS edge: when rule is bound to a specific CoreSymbol, link them.
        if r.core_symbol_qname:
            tx.run(f"""
                MATCH (l:LintRule {{rule_id: $rid, odoo_version: $v}})
                MATCH (cs:CoreSymbol {{qualified_name: $cs_qn, odoo_version: $v}})
                MERGE (l)-[:{REL_CHECKS}]->(cs)
            """, rid=r.rule_id, v=r.odoo_version, cs_qn=r.core_symbol_qname)


def _write_cli_commands_batch(tx, commands: list[CLICommandInfo]) -> None:
    for c in commands:
        tx.run("""
            MERGE (c:CLICommand {name: $name, odoo_version: $v})
            SET c.description = $desc,
                c.file_path = $fp
        """, name=c.name, v=c.odoo_version,
             desc=c.description, fp=c.file_path)


def _write_cli_flags_batch(tx, flags: list[CLIFlagInfo]) -> None:
    for f in flags:
        tx.run("""
            MERGE (f:CLIFlag {flag_name: $fn, command_name: $cmd, odoo_version: $v})
            SET f.status = $status,
                f.default = $default,
                f.type = $type,
                f.help = $help,
                f.replacement_flag_name = $repl,
                f.env_name = $env,
                f.posix_only = $posix
        """, fn=f.flag_name, cmd=f.command_name, v=f.odoo_version,
             status=f.status, default=f.default, type=f.type, help=f.help,
             repl=f.replacement_flag_name, env=f.env_name, posix=f.posix_only)
        # OF_COMMAND edge: link the flag to its command if the CLICommand exists.
        tx.run(f"""
            MATCH (f:CLIFlag {{flag_name: $fn, command_name: $cmd, odoo_version: $v}})
            MATCH (c:CLICommand {{name: $cmd, odoo_version: $v}})
            MERGE (f)-[:{REL_OF_COMMAND}]->(c)
        """, fn=f.flag_name, cmd=f.command_name, v=f.odoo_version)


def _write_pattern_examples_batch(tx, patterns: list[PatternExample]) -> None:
    """MERGE PatternExample nodes + USES_CORE_SYMBOL edges (silent skip per ADR-0003)."""
    for p in patterns:
        tx.run("""
            MERGE (pe:PatternExample {pattern_id: $pid})
            SET pe.intent_keywords = $kw,
                pe.file_ref = $fr,
                pe.snippet_text = $sn,
                pe.gotchas = $g,
                pe.odoo_version_min = $vmin,
                pe.language = $lang
        """, pid=p.pattern_id, kw=p.intent_keywords, fr=p.file_ref,
             sn=p.snippet_text, g=p.gotchas, vmin=p.odoo_version_min,
             lang=p.language)
        # USES_CORE_SYMBOL edges — silent skip when no CoreSymbol matches
        # (M4.5 not shipped yet, or symbol simply absent at this version).
        for cs_name in p.core_symbol_names:
            tx.run(f"""
                MATCH (pe:PatternExample {{pattern_id: $pid}})
                MATCH (cs:CoreSymbol {{odoo_version: $v}})
                WHERE cs.qualified_name = $cs
                   OR cs.qualified_name ENDS WITH '.' + $cs
                MERGE (pe)-[:{REL_USES_CORE_SYMBOL}]->(cs)
            """, pid=p.pattern_id, v=p.odoo_version_min, cs=cs_name)


def _write_cli_flag_replacements(tx, replaced: list[tuple[str, str]],
                                 command_name: str,
                                 from_version: str, to_version: str) -> None:
    for old_fn, new_fn in replaced:
        tx.run(f"""
            MATCH (a:CLIFlag {{flag_name: $a_fn, command_name: $cmd, odoo_version: $vfrom}})
            MATCH (b:CLIFlag {{flag_name: $b_fn, command_name: $cmd, odoo_version: $vto}})
            MERGE (a)-[:{REL_REPLACED_BY}]->(b)
        """, a_fn=old_fn, b_fn=new_fn, cmd=command_name,
             vfrom=from_version, vto=to_version)


# ---------------------------------------------------------------------------
# CSS/SCSS stylesheet writer (WI-A1, ADR-0025)
# ---------------------------------------------------------------------------

def _write_stylesheets_batch(
    tx, stylesheets: list[StylesheetInfo], profiles: list[str]
) -> None:
    """MERGE :Stylesheet nodes + :DEFINED_IN -> :Module + :IMPORTS edges.

    Composite MERGE key: (file_path, module, odoo_version) per ADR-0025 §D1.
    Properties written:
      - language ∈ {css, scss}
      - selector_count, variable_count, import_count, mixin_count
      - profile[] — ancestor profile name array (per ADR-0016 Option Y)

    Relationships:
      - :Stylesheet -[:DEFINED_IN]-> :Module  (always written)
      - :Stylesheet -[:IMPORTS]-> :Stylesheet  (only when import target is indexed)
        Silent skip when the imported file_path is not found in Neo4j (per ADR-0025 §D3).

    The DEFINED_IN target Module is written with MERGE (not MATCH) and the
    profile-union pattern from ADR-0016 §D7 — if the Module hasn't been
    written yet (forward-reference race in parallel indexing), we create a
    stub so the Stylesheet is never orphaned.  The real Module write later
    in the same batch idempotently fills in repo/path/version_raw/etc.
    """
    for s in stylesheets:
        # MERGE the Stylesheet node + set properties + DEFINED_IN edge.
        # Module is MERGE'd (not MATCH'd) to avoid orphan Stylesheet nodes
        # if the host Module is written by a later batch (parallel-indexer
        # ordering not guaranteed) — see ADR-0016 §D7 stub-ownership policy.
        tx.run(f"""
            MERGE (ss:Stylesheet {{file_path: $fp, module: $mod, odoo_version: $v}})
            ON CREATE SET ss.language = $lang,
                          ss.selector_count = $sel,
                          ss.variable_count = $var,
                          ss.import_count = $imp,
                          ss.mixin_count = $mix,
                          ss.profile = $profiles
            ON MATCH  SET ss.language = $lang,
                          ss.selector_count = $sel,
                          ss.variable_count = $var,
                          ss.import_count = $imp,
                          ss.mixin_count = $mix,
                          ss.profile =
                              [x IN coalesce(ss.profile, []) WHERE NOT x IN $profiles]
                              + $profiles
            WITH ss
            MERGE (mod:Module {{name: $mod, odoo_version: $v}})
            ON CREATE SET mod.profile = $profiles
            ON MATCH  SET mod.profile =
                [x IN coalesce(mod.profile, []) WHERE NOT x IN $profiles] + $profiles
            MERGE (ss)-[:{REL_DEFINED_IN}]->(mod)
        """, fp=s.file_path, mod=s.module, v=s.odoo_version,
             lang=s.language, sel=s.selector_count, var=s.variable_count,
             imp=s.import_count, mix=s.mixin_count, profiles=profiles)

        # Write IMPORTS edges — silent skip when target Stylesheet not yet indexed
        for import_path in s.imports:
            tx.run(f"""
                MATCH (src:Stylesheet {{file_path: $src_fp, module: $mod, odoo_version: $v}})
                MATCH (tgt:Stylesheet {{file_path: $tgt_fp, odoo_version: $v}})
                MERGE (src)-[:{REL_IMPORTS}]->(tgt)
            """, src_fp=s.file_path, mod=s.module, v=s.odoo_version,
                 tgt_fp=import_path)


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
                # M4.5 spec layer (per ADR-0002):
                "CREATE INDEX IF NOT EXISTS FOR (n:CoreSymbol)"
                " ON (n.qualified_name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:LintRule)"
                " ON (n.rule_id, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:CLICommand)"
                " ON (n.name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:CLIFlag)"
                " ON (n.flag_name, n.command_name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:SpecMetadata)"
                " ON (n.kind, n.odoo_version)",
                # M4.6 pattern layer (per ADR-0003):
                "CREATE INDEX IF NOT EXISTS FOR (n:PatternExample)"
                " ON (n.pattern_id)",
                "CREATE INDEX IF NOT EXISTS FOR (n:PatternExample)"
                " ON (n.language, n.odoo_version_min)",
                # WI-A1 stylesheet layer (per ADR-0025):
                "CREATE INDEX IF NOT EXISTS FOR (n:Stylesheet)"
                " ON (n.file_path, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Stylesheet)"
                " ON (n.module, n.odoo_version)",
            ]:
                session.run(stmt)

    def write_results(
        self,
        results: list[ParseResult],
        profiles: list[str] | None = None,
    ) -> None:
        """Persist ParseResult nodes (Module/Model/Field/Method).

        *profiles* is the ancestor profile name array (self at index 0, root last)
        written as a ``profile`` list property on every node. Empty list written
        when caller doesn't supply a value (backward-compat for unit tests).
        """
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_parse_result, result, _profiles)

    def write_view_results(
        self,
        results: list[ViewParseResult],
        profiles: list[str] | None = None,
    ) -> None:
        """Persist View and QWebTmpl nodes. *profiles* written as node property."""
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_view_parse_result, result, _profiles)

    def write_js_graph_results(
        self,
        results: list[JSGraphResult],
        profiles: list[str] | None = None,
    ) -> None:
        """Persist OWLComp and JSPatch nodes. *profiles* written as node property."""
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_js_graph_result, result, _profiles)

    # --- M4.5 spec layer (CoreSymbol + diff edges) -------------------------

    def write_core_symbols(self, symbols: list[CoreSymbolInfo]) -> None:
        """Persist a batch of CoreSymbol nodes (idempotent MERGE).

        Composite key: (qualified_name, odoo_version). Mutable props (kind,
        signature, file_path, line, status, replacement_qname) updated via SET.
        Batched at 500/transaction to stay under driver memory budget.
        """
        if not symbols:
            return
        with self.driver.session() as session:
            for batch in _chunked(symbols, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(_write_core_symbols_batch, batch)

    def write_diff_edges(
        self, diff: DiffResult, *, from_version: str, to_version: str,
    ) -> None:
        """Persist cross-version diff edges (currently REPLACED_BY only).

        Per ADR-0002 §2: ADDED_IN / REMOVED_IN are represented via `cs.status`
        property (set during write_core_symbols), not as separate edges. Only
        REPLACED_BY needs an actual edge because it links two distinct nodes.
        """
        if not diff.replaced:
            return
        with self.driver.session() as session:
            session.execute_write(
                _write_replaced_by_edges,
                diff.replaced, from_version, to_version,
            )

    def write_lint_rules(self, rules: list[LintRuleInfo]) -> None:
        """Persist a batch of LintRule nodes (idempotent MERGE).

        Composite key: (rule_id, odoo_version). Optionally creates a
        CHECKS edge to a CoreSymbol when `core_symbol_qname` is set and
        the target node already exists.
        """
        if not rules:
            return
        with self.driver.session() as session:
            for batch in _chunked(rules, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(_write_lint_rules_batch, batch)

    def write_cli_commands(self, commands: list[CLICommandInfo]) -> None:
        """Persist CLICommand nodes (idempotent MERGE on (name, odoo_version))."""
        if not commands:
            return
        with self.driver.session() as session:
            for batch in _chunked(commands, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(_write_cli_commands_batch, batch)

    def write_cli_flags(self, flags: list[CLIFlagInfo]) -> None:
        """Persist CLIFlag nodes + OF_COMMAND edges (when target CLICommand exists)."""
        if not flags:
            return
        with self.driver.session() as session:
            for batch in _chunked(flags, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(_write_cli_flags_batch, batch)

    def write_cli_flag_replacements(
        self,
        replaced: list[tuple[str, str]],
        *,
        command_name: str,
        from_version: str,
        to_version: str,
    ) -> None:
        """Persist REPLACED_BY edges between CLIFlag nodes."""
        if not replaced:
            return
        with self.driver.session() as session:
            session.execute_write(
                _write_cli_flag_replacements,
                replaced, command_name, from_version, to_version,
            )

    def fetch_core_symbols(self, odoo_version: str) -> list:
        """Fetch all CoreSymbolInfo for a version from Neo4j.

        Returns a list of CoreSymbolInfo-like dicts re-constructed as CoreSymbolInfo
        objects so diff_engine can compare them. Used by index_core lifecycle diff.
        """
        from .models import CoreSymbolInfo
        with self.driver.session() as session:
            rows = session.run("""
                MATCH (cs:CoreSymbol {odoo_version: $v})
                RETURN cs.qualified_name AS qualified_name,
                       cs.kind AS kind,
                       cs.odoo_version AS odoo_version,
                       cs.signature AS signature,
                       cs.file_path AS file_path,
                       cs.line AS line,
                       cs.status AS status,
                       cs.replacement_qname AS replacement_qname
            """, v=odoo_version).data()
        return [
            CoreSymbolInfo(
                qualified_name=r["qualified_name"],
                kind=r["kind"] or "function",
                odoo_version=r["odoo_version"],
                signature=r.get("signature"),
                file_path=r.get("file_path"),
                line=r.get("line"),
                status=r.get("status") or "stable",
                replacement_qname=r.get("replacement_qname"),
            )
            for r in rows
        ]

    def write_lifecycle_properties(
        self,
        diff,  # DiffResult — import avoided at module level for circularity
        *,
        from_version: str,
        to_version: str,
    ) -> None:
        """Write added_in / removed_in / deprecated_in properties on CoreSymbol nodes.

        Per ADR-0002 §2 (revised): lifecycle expressed as properties on CoreSymbol
        for query simplicity. REPLACED_BY is the only true edge.

        - added (in to_version)   → cs.added_in = to_version  on the NEW node
        - removed (from from_version) → cs.removed_in = to_version  on the OLD node
        - deprecated (in to_version)  → cs.deprecated_in = to_version  on the NEW node
        """
        if not diff:
            return
        with self.driver.session() as session:
            for sym in diff.added:
                session.run("""
                    MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
                    SET cs.added_in = $added_in
                """, qn=sym.qualified_name, v=sym.odoo_version, added_in=to_version)

            for sym in diff.removed:
                # sym.odoo_version is from_version (old list)
                session.run("""
                    MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
                    SET cs.removed_in = $removed_in
                """, qn=sym.qualified_name, v=from_version, removed_in=to_version)

            deprecated = getattr(diff, "deprecated", [])
            for sym in deprecated:
                session.run("""
                    MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
                    SET cs.deprecated_in = $deprecated_in
                """, qn=sym.qualified_name, v=sym.odoo_version, deprecated_in=to_version)

    def write_pattern_examples(self, patterns: list[PatternExample]) -> None:
        """Persist PatternExample nodes (idempotent MERGE on `pattern_id`).

        USES_CORE_SYMBOL edges to CoreSymbol nodes are silently skipped when
        the target does not exist — M4.5 graceful skip per ADR-0003 §5.
        Batched at 200/transaction (smaller than CoreSymbol's 500 because
        each pattern can fan-out N edge MERGEs).
        """
        if not patterns:
            return
        with self.driver.session() as session:
            for batch in _chunked(patterns, 200):
                session.execute_write(_write_pattern_examples_batch, batch)

    def write_stylesheets(
        self,
        stylesheets: list[StylesheetInfo],
        profiles: list[str] | None = None,
    ) -> None:
        """Persist :Stylesheet nodes + :DEFINED_IN + :IMPORTS edges.

        Idempotent MERGE on composite key (file_path, module, odoo_version).
        *profiles* is the ancestor profile name array (per ADR-0016 Option Y).
        Batched at NEO4J_WRITE_BATCH_SIZE per transaction.
        IMPORTS edge write silently skips when the target file_path is not indexed.
        """
        if not stylesheets:
            return
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for batch in _chunked(stylesheets, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(_write_stylesheets_batch, batch, _profiles)

    def delete_modules_scoped(self, repo_basename: str, odoo_version: str) -> dict:
        """DETACH DELETE Module(s) matching (repo, odoo_version) + cascading child nodes.

        Child nodes (Model/Field/Method/View/QWebTmpl/JSPatch/OWLComp) are
        scoped by (module_name, odoo_version) — they're deleted ONLY if their
        Module parent is being deleted in this call, to avoid orphan cleanup of
        nodes that belong to other repos in the same version.

        Returns: {"modules": N, "children": M} counts.
        """
        with self.driver.session() as session:
            # Step 1: collect module names being deleted
            module_names_row = session.run(
                """
                MATCH (m:Module {repo: $repo, odoo_version: $version})
                RETURN collect(m.name) AS names
                """,
                repo=repo_basename,
                version=odoo_version,
            ).single()

            if module_names_row is None or not module_names_row["names"]:
                return {"modules": 0, "children": 0}

            module_names = module_names_row["names"]

            # Step 2: delete child nodes scoped to those module names
            # Use separate MATCH per label (Neo4j 5.x label OR in WHERE is valid
            # but collecting across labels requires UNION approach for count accuracy;
            # here we use a single MATCH with label filter via WHERE + IN for module names).
            children_row = session.run(
                """
                MATCH (child)
                WHERE child.module IN $names AND child.odoo_version = $version
                  AND (child:Model OR child:Field OR child:Method OR child:View
                       OR child:QWebTmpl OR child:JSPatch OR child:OWLComp)
                WITH collect(child) AS children
                UNWIND children AS c
                DETACH DELETE c
                RETURN count(c) AS cc
                """,
                names=module_names,
                version=odoo_version,
            ).single()
            children_deleted = children_row["cc"] if children_row is not None else 0

            # Step 3: delete the Module nodes themselves
            modules_row = session.run(
                """
                MATCH (m:Module {repo: $repo, odoo_version: $version})
                WITH collect(m) AS mods
                UNWIND mods AS m
                DETACH DELETE m
                RETURN count(m) AS mc
                """,
                repo=repo_basename,
                version=odoo_version,
            ).single()
            modules_deleted = modules_row["mc"] if modules_row is not None else 0

        return {"modules": modules_deleted, "children": children_deleted}

    def gc_stale_modules(
        self, repo: str, odoo_version: str, live_paths: set[str],
    ) -> int:
        """Delete Module nodes for this repo+version whose 'path' is not in live_paths.

        Returns count deleted. Uses DETACH DELETE so all edges (DEFINED_IN,
        DEPENDS_ON, etc.) are removed along with the stale node.

        Args:
            repo:         m.repo value (repo root dir name, e.g. 'odoo_17.0').
            odoo_version: Odoo version label, e.g. '17.0'.
            live_paths:   Absolute path strings returned by the scanner for this
                          repo in this run. Modules NOT in this set are stale.

        Risk gate (enforced by caller): only called when len(live_paths) >= 1.
        """
        with self.driver.session() as session:
            row = session.run(
                """
                MATCH (m:Module {repo: $repo, odoo_version: $version})
                WHERE NOT m.path IN $live_paths
                DETACH DELETE m
                RETURN count(m) AS n
                """,
                repo=repo,
                version=odoo_version,
                live_paths=list(live_paths),
            ).single()
        return row["n"] if row is not None else 0

    def write_spec_metadata(
        self, kind: str, odoo_version: str, curate_status: str,
    ) -> None:
        """Upsert a SpecMetadata node recording curation status for a spec kind + version.

        Composite key: (kind, odoo_version). MERGE is idempotent.

        Args:
            kind:          'lint' | 'cli' — which spec category this metadata covers.
            odoo_version:  Odoo version label, e.g. '8.0', '17.0'.
            curate_status: 'pending' | 'done' (or any string per ADR-0002 §4).
        """
        with self.driver.session() as session:
            session.run("""
                MERGE (sm:SpecMetadata {kind: $kind, odoo_version: $v})
                SET sm.curate_status = $curate_status
            """, kind=kind, v=odoo_version, curate_status=curate_status)
