# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/writer_neo4j.py
import logging

from neo4j import GraphDatabase

from src.constants import (
    NEO4J_DELETE_BATCH_ROWS,
    NEO4J_WRITE_BATCH_SIZE,
    REL_CHECKS,
    REL_DEFINED_IN,
    REL_DEPENDS_ON,
    REL_DEPENDS_ON_FIELD,
    REL_HAS_VIOLATION,
    REL_IMPORTS,
    REL_INHERITS,
    REL_INHERITS_VIEW,
    REL_OF_COMMAND,
    REL_REPLACED_BY,
    REL_TARGETS_MODEL,
    REL_USES_CORE_SYMBOL,
    REL_USES_FIELD,
)

from .diff_engine import DiffResult
from .models import (
    CLICommandInfo,
    CLIFlagInfo,
    CoreSymbolInfo,
    JSGraphResult,
    LintRuleInfo,
    LintViolationInfo,
    ParseResult,
    PatternExample,
    StylesheetInfo,
    ViewParseResult,
    to_repo_relative,
)

_logger = logging.getLogger(__name__)


def _profile_union_set(alias: str) -> str:
    """Cypher fragment for ON MATCH SET union-add of profile names (write-side).

    Returns the canonical dedup-add expression used by every defining node's
    ``ON MATCH SET <alias>.profile = ...`` clause:

        [x IN coalesce(<alias>.profile, []) WHERE NOT x IN $profiles] + $profiles

    Union-only by construction (ADR-0034): never resets, never removes an
    existing owner — it appends $profiles after stripping any names already
    present, so a node co-owned by a genuine collision keeps BOTH owners and
    stays fail-closed at the ADR-0034 read-side choke. Mirrors the read-side
    ``_scope_pred`` builder in src/mcp/server.py — SEE ALSO that function: the
    write-side union shape here and the read-side predicate there are coupled
    (a change to one's profile/empty-node semantics must be reflected in both).

    The ``$profiles`` token is a literal Cypher parameter in the returned string;
    it is bound by the caller's ``tx.run(..., profiles=...)`` kwargs and is NOT
    an f-string variable here.
    """
    return f"[x IN coalesce({alias}.profile, []) WHERE NOT x IN $profiles] + $profiles"


def _write_parse_result(tx, result: ParseResult, profiles: list[str]) -> None:
    module = result.module

    tx.run(f"""
        MERGE (m:Module {{name: $name, odoo_version: $v}})
        ON CREATE SET m.profile = $profiles,
                      m.auto_install = $auto_install,
                      m.application = $application,
                      m.category = $category,
                      m.summary = $summary,
                      m.external_python = $external_python,
                      m.external_bin = $external_bin,
                      m.repo_url = $repo_url,
                      m.repo_id = $repo_id
        ON MATCH  SET m.profile =
                          {_profile_union_set("m")},
                      m.auto_install = $auto_install,
                      m.application = $application,
                      m.category = $category,
                      m.summary = coalesce($summary, m.summary),
                      m.external_python = $external_python,
                      m.external_bin = $external_bin,
                      m.repo_url = coalesce($repo_url, m.repo_url),
                      m.repo_id = coalesce($repo_id, m.repo_id)
        SET m.repo = $repo, m.path = $path, m.version_raw = $version_raw,
            m.edition = $edition,
            m.viindoo_equivalent_qname = $vvq,
            m.last_commit_sha = $commit_sha,
            m.license = $license,
            m.copyright_owner = $copyright_owner,
            m.license_notice = $license_notice
    """, name=module.name, v=module.odoo_version,
         repo=module.repo, path=module.relative_path(module.path),
         version_raw=module.version_raw,
         edition=module.edition,
         vvq=module.viindoo_equivalent_qname,
         commit_sha=module.commit_sha,
         license=module.license,
         copyright_owner=module.copyright_owner,
         license_notice=module.license_notice,
         auto_install=module.auto_install,
         application=module.application,
         category=module.category,
         summary=module.summary,
         external_python=module.external_python,
         external_bin=module.external_bin,
         repo_url=module.repo_url,
         repo_id=module.repo_id,
         profiles=profiles)

    for dep in module.depends:
        # SCOPE-CHOKE FIX (ADR-0034): a `depends` target is a node this run merely
        # REFERENCES, not one it OWNS. NEVER stamp this run's profile onto it —
        # doing so unions the depending tenant's private profile name onto a
        # shared-core node (e.g. `base` gaining `standard_viindoo_17` every time a
        # Viindoo module declares `depends: base`), which the all() choke then
        # correctly DENIES to callers not allowed on that name — re-hiding shared
        # core on the next reindex. The dep target's own profile is set ONLY by the
        # run that owns/defines it. A forward-referenced placeholder (never indexed
        # under its own profile) is created profile-less and is correctly DENIED to
        # scoped tenants by the F-6 `size(profile)>0` guard (admin still sees it).
        # ON CREATE leaves profile unset (-> []/absent = fail-closed); ON MATCH
        # touches nothing, so a dep target already indexed keeps its owning profile.
        tx.run(f"""
            MATCH (m:Module {{name: $name, odoo_version: $v}})
            MERGE (d:Module {{name: $dep, odoo_version: $v}})
            MERGE (m)-[:{REL_DEPENDS_ON}]->(d)
        """, name=module.name, v=module.odoo_version, dep=dep)

    for model in result.models:
        tx.run(f"""
            MERGE (mod:Module {{name: $module_name, odoo_version: $v}})
            ON CREATE SET mod.profile = $profiles
            ON MATCH  SET mod.profile =
                {_profile_union_set("mod")}
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
                              {_profile_union_set("m")}
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
                # ADR topology change (#273): extender targets definition node(s)
                # only (K×D edges instead of K² mesh). Nodes without is_definition=true
                # are other extenders — connecting to them adds no reachability and
                # caused the K² path explosion that hung the ORM tools on prod.
                tx.run(f"""
                    MATCH (ext:Model {{name: $name, module: $mod, odoo_version: $v}})
                    MATCH (tip:Model {{name: $name, odoo_version: $v}})
                    WHERE tip.module <> $mod
                      AND coalesce(tip.is_definition, false) = true
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
                    # SCOPE-CHOKE FIX (ADR-0034): the parent is a REFERENCED node
                    # this run does not own (its definition lives in a not-yet-
                    # indexed module). Do NOT stamp this run's profile onto the
                    # placeholder — that would union a foreign profile onto a node
                    # owned elsewhere. The placeholder uses the {name,
                    # module:'__unresolved__'} key (distinct from the real Model's
                    # real-module key, so it does NOT converge on it; gc_unresolved
                    # reconciles once the real parent is indexed). Created
                    # profile-less, it is correctly DENIED to scoped tenants by the
                    # F-6 `size(profile)>0` guard until reconciled. ON MATCH leaves
                    # profile untouched.
                    tx.run(f"""
                        MATCH (m:Model {{name: $model_name, module: $mod, odoo_version: $v}})
                        MERGE (placeholder:Model {{name: $parent_name,
                                                  module: '__unresolved__', odoo_version: $v}})
                        ON CREATE SET placeholder.unresolved = true,
                                      placeholder.is_definition = false
                        MERGE (m)-[r:{REL_INHERITS} {{unresolved: true}}]->(placeholder)
                        SET r.order = $order
                    """, model_name=model.name, mod=model.module,
                         v=model.odoo_version, parent_name=parent_name,
                         order=idx)

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
                # SCOPE-CHOKE FIX (ADR-0034): delegated target is a REFERENCED node
                # not owned by this run — do NOT stamp this run's profile. Created
                # profile-less -> F-6 fail-closed for scoped tenants until indexed
                # under its own owner. ON MATCH leaves profile untouched.
                tx.run("""
                    MATCH (m:Model {name: $name, module: $mod, odoo_version: $v})
                    MERGE (placeholder:Model {name: $delegated,
                                              module: '__unresolved__', odoo_version: $v})
                    ON CREATE SET placeholder.unresolved = true,
                                  placeholder.is_definition = false
                    MERGE (m)-[:DELEGATES_TO {via_field: $via_field, unresolved: true}]
                          ->(placeholder)
                """, name=model.name, mod=model.module, v=model.odoo_version,
                     delegated=delegated_model, via_field=via_field)

        for fld in model.fields:
            tx.run(f"""
                MATCH (m:Model {{name: $model_name, module: $mod, odoo_version: $v}})
                MERGE (f:Field {{name: $name, model: $model_name,
                               module: $mod, odoo_version: $v}})
                ON CREATE SET f.profile = $profiles
                ON MATCH  SET f.profile =
                    {_profile_union_set("f")}
                SET f.ttype = $ttype, f.related = $related, f.compute = $compute,
                    f.stored = $stored, f.required = $required,
                    f.comodel_name = $comodel_name,
                    f.string = $fstring, f.help = $fhelp,
                    f.readonly = $readonly, f.inverse = $inverse,
                    f.effective_readonly = $effective_readonly
                MERGE (f)-[:BELONGS_TO]->(m)
            """, model_name=model.name, mod=model.module, v=model.odoo_version,
                 name=fld.name, ttype=fld.ttype, related=fld.related,
                 compute=fld.compute, stored=fld.stored, required=fld.required,
                 comodel_name=fld.comodel_name,
                 fstring=fld.string, fhelp=fld.help,
                 readonly=fld.readonly, inverse=fld.inverse,
                 effective_readonly=fld.effective_readonly,
                 profiles=profiles)

        for mth in model.methods:
            tx.run(f"""
                MATCH (m:Model {{name: $model_name, module: $mod, odoo_version: $v}})
                MERGE (mth:Method {{name: $name, model: $model_name,
                                   module: $mod, odoo_version: $v}})
                ON CREATE SET mth.profile = $profiles
                ON MATCH  SET mth.profile =
                    {_profile_union_set("mth")}
                SET mth.has_super_call = $has_super_call,
                    mth.decorators = $decorators,
                    mth.convention_kind = $ck,
                    mth.super_safety = $ss,
                    mth.return_required = $rr,
                    mth.signature = $sig,
                    mth.depends = $depends,
                    mth.docstring = $docstring
                MERGE (mth)-[:BELONGS_TO]->(m)
            """, model_name=model.name, mod=model.module, v=model.odoo_version,
                 name=mth.name, has_super_call=mth.has_super_call,
                 decorators=mth.decorators,
                 ck=mth.convention_kind, ss=mth.super_safety, rr=mth.return_required,
                 sig=mth.signature, depends=mth.depends,
                 docstring=mth.docstring, profiles=profiles)

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

            # A2d: USES_FIELD edges — MATCH (not MERGE) on Field so no stub nodes.
            # F-13 fix: include module in Field MATCH key to avoid fan-out across
            #   modules that all define the same field name on the same model.
            #   Uses the method's own module (model.module) as the context module.
            # F-8 fix: batch with UNWIND to avoid 1 tx.run per field_ref (N+1).
            if mth.field_refs:
                tx.run(f"""
                    MATCH (mth:Method {{name: $mth_name, model: $model_name,
                                       module: $mod, odoo_version: $v}})
                    UNWIND $refs AS ref_name
                    MATCH (f:Field {{name: ref_name, model: $model_name,
                                    module: $mod, odoo_version: $v}})
                    MERGE (mth)-[:{REL_USES_FIELD}]->(f)
                """, mth_name=mth.name, model_name=model.name, mod=model.module,
                     v=model.odoo_version, refs=list(mth.field_refs))

            # A2d: DEPENDS_ON_FIELD edges from @api.depends paths — first segment only.
            # F-13 fix: include module in Field MATCH key (same as USES_FIELD above).
            # F-8 fix: batch with UNWIND — 1 tx.run per method instead of per dep-path.
            _dep_segs: list[str] = list(dict.fromkeys(
                dep.split('.')[0] for dep in mth.depends
            ))
            if _dep_segs:
                tx.run(f"""
                    MATCH (mth:Method {{name: $mth_name, model: $model_name,
                                       module: $mod, odoo_version: $v}})
                    UNWIND $segs AS first_seg
                    MATCH (f:Field {{name: first_seg, model: $model_name,
                                    module: $mod, odoo_version: $v}})
                    MERGE (mth)-[:{REL_DEPENDS_ON_FIELD}]->(f)
                """, mth_name=mth.name, model_name=model.name, mod=model.module,
                     v=model.odoo_version, segs=_dep_segs)


def _write_view_parse_result(tx, result: ViewParseResult, profiles: list[str]) -> None:
    for view in result.views:
        tx.run(f"""
            MERGE (v:View {{xmlid: $xmlid, odoo_version: $ver}})
            ON CREATE SET v.profile = $profiles
            ON MATCH  SET v.profile =
                {_profile_union_set("v")}
            SET v.name = $name, v.model = $model, v.module = $module,
                v.type = $view_type, v.mode = $mode,
                v.xpaths_exprs = $xpaths_exprs,
                v.xpaths_positions = $xpaths_positions,
                v.arch_snippet = $arch_snippet,
                v.unresolved = false
        """, xmlid=view.xmlid, ver=view.odoo_version,
             name=view.name, model=view.model, module=view.module,
             view_type=view.view_type, mode=view.mode,
             xpaths_exprs=[x.expr for x in view.xpaths],
             xpaths_positions=[x.position for x in view.xpaths],
             arch_snippet=view.arch_snippet,
             profiles=profiles)

        tx.run(f"""
            MATCH (v:View {{xmlid: $xmlid, odoo_version: $ver}})
            MERGE (mod:Module {{name: $module, odoo_version: $ver}})
            ON CREATE SET mod.profile = $profiles
            ON MATCH  SET mod.profile =
                {_profile_union_set("mod")}
            MERGE (v)-[:{REL_DEFINED_IN}]->(mod)
        """, xmlid=view.xmlid, ver=view.odoo_version, module=view.module,
             profiles=profiles)

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
                MERGE (ext)-[r:{REL_INHERITS_VIEW}]->(base)
                ON MATCH SET r.unresolved = false
                RETURN 1 AS ok
            """, xmlid=view.xmlid, ver=view.odoo_version,
                 inherit_xmlid=view.inherit_xmlid).single()
            if rec is None:
                _logger.warning(
                    "unresolved INHERITS_VIEW: %s → %s (version %s) — parent view not indexed",
                    view.xmlid, view.inherit_xmlid, view.odoo_version,
                )
                # Placeholder MERGE uses the same 2-property key as the real View
                # {xmlid, odoo_version} so that a subsequent real-View write converges
                # on the SAME node (avoiding "shadow" duplicates — see gc_unresolved).
                # ON CREATE stamps unresolved=true + module='__unresolved__';
                # ON MATCH leaves those fields untouched so a real View node already
                # indexed is never corrupted. NOTE: profile is deliberately NOT
                # merged here (see the SCOPE-CHOKE note below) — the placeholder is
                # created profile-less and a real View keeps its own owning profile.
                # SCOPE-CHOKE FIX (ADR-0034): the base view is a REFERENCED node not
                # owned by this run. The placeholder shares the real View's
                # {xmlid, odoo_version} key, so the real View (owned by another run)
                # converges on THIS node — stamping this run's profile here would
                # pollute the real View's owning profile. Do NOT set profile: created
                # profile-less -> F-6 fail-closed for scoped tenants until the real
                # View is indexed under its OWN owner; ON MATCH leaves profile
                # untouched so an already-indexed real View keeps its owner.
                tx.run(f"""
                    MATCH (ext:View {{xmlid: $xmlid, odoo_version: $ver}})
                    MERGE (placeholder:View {{xmlid: $inherit_xmlid, odoo_version: $ver}})
                    ON CREATE SET placeholder.unresolved = true,
                                  placeholder.module = '__unresolved__'
                    MERGE (ext)-[r:{REL_INHERITS_VIEW}]->(placeholder)
                    ON CREATE SET r.unresolved = true
                """, xmlid=view.xmlid, ver=view.odoo_version,
                     inherit_xmlid=view.inherit_xmlid)

    for qweb in result.qweb:
        tx.run(f"""
            MERGE (t:QWebTmpl {{xmlid: $xmlid, odoo_version: $ver}})
            ON CREATE SET t.profile = $profiles
            ON MATCH  SET t.profile =
                {_profile_union_set("t")}
            SET t.module = $module,
                t.unresolved = false
        """, xmlid=qweb.xmlid, ver=qweb.odoo_version, module=qweb.module, profiles=profiles)

        tx.run(f"""
            MATCH (t:QWebTmpl {{xmlid: $xmlid, odoo_version: $ver}})
            MERGE (mod:Module {{name: $module, odoo_version: $ver}})
            ON CREATE SET mod.profile = $profiles
            ON MATCH  SET mod.profile =
                {_profile_union_set("mod")}
            MERGE (t)-[:{REL_DEFINED_IN}]->(mod)
        """, xmlid=qweb.xmlid, ver=qweb.odoo_version, module=qweb.module,
             profiles=profiles)

        if qweb.inherit_xmlid:
            rec = tx.run("""
                MATCH (ext:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
                MATCH (base:QWebTmpl {xmlid: $inherit_xmlid, odoo_version: $ver})
                WHERE NOT coalesce(base.unresolved, false)
                MERGE (ext)-[r:EXTENDS_TMPL]->(base)
                ON MATCH SET r.unresolved = false
                RETURN 1 AS ok
            """, xmlid=qweb.xmlid, ver=qweb.odoo_version,
                 inherit_xmlid=qweb.inherit_xmlid).single()
            if rec is None:
                _logger.warning(
                    "unresolved EXTENDS_TMPL: %s → %s (version %s) — base template not indexed",
                    qweb.xmlid, qweb.inherit_xmlid, qweb.odoo_version,
                )
                # Same key-convergence fix as View: use 2-property MERGE key
                # {xmlid, odoo_version} to prevent shadow QWebTmpl nodes when the
                # real template is indexed before or after the placeholder.
                # SCOPE-CHOKE FIX (ADR-0034): base template is a REFERENCED node not
                # owned by this run; the placeholder shares the real QWebTmpl's
                # {xmlid, odoo_version} key (converges on the real node). Do NOT
                # stamp this run's profile — created profile-less -> F-6 fail-closed
                # until the real template is indexed under its OWN owner; ON MATCH
                # leaves profile untouched.
                tx.run("""
                    MATCH (ext:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
                    MERGE (placeholder:QWebTmpl {xmlid: $inherit_xmlid, odoo_version: $ver})
                    ON CREATE SET placeholder.unresolved = true,
                                  placeholder.module = '__unresolved__'
                    MERGE (ext)-[r:EXTENDS_TMPL]->(placeholder)
                    ON CREATE SET r.unresolved = true
                """, xmlid=qweb.xmlid, ver=qweb.odoo_version,
                     inherit_xmlid=qweb.inherit_xmlid)


def _write_js_graph_result(tx, result: JSGraphResult, profiles: list[str]) -> None:
    # Write OWLComp nodes first so PATCHES can resolve against them
    for comp in result.components:
        tx.run(f"""
            MERGE (mod:Module {{name: $module_name, odoo_version: $v}})
            ON CREATE SET mod.profile = $profiles
            ON MATCH  SET mod.profile =
                {_profile_union_set("mod")}
            MERGE (c:OWLComp {{name: $name, module: $module_name, odoo_version: $v}})
            ON CREATE SET c.profile = $profiles
            ON MATCH  SET c.profile =
                {_profile_union_set("c")}
            SET c.template = $template, c.extends = $extends,
                c.bound_model = $bound_model, c.file_path = $file_path
            MERGE (c)-[:{REL_DEFINED_IN}]->(mod)
        """, module_name=comp.module, v=comp.odoo_version,
             name=comp.name, template=comp.template, extends=comp.extends,
             bound_model=comp.bound_model,
             file_path=result.module.relative_path(comp.file_path),
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
            ON CREATE SET mod.profile = $profiles
            ON MATCH  SET mod.profile =
                {_profile_union_set("mod")}
            MERGE (j:JSPatch {{target: $target, patch_name: $patch_name,
                              module: $module_name, odoo_version: $v}})
            ON CREATE SET j.profile = $profiles
            ON MATCH  SET j.profile =
                {_profile_union_set("j")}
            SET j.era = $era, j.file_path = $file_path
            MERGE (j)-[:{REL_DEFINED_IN}]->(mod)
        """, module_name=patch.module, v=patch.odoo_version,
             target=patch.target, patch_name=patch.patch_name,
             era=patch.era,
             file_path=result.module.relative_path(patch.file_path),
             profiles=profiles)

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
            # SCOPE-CHOKE FIX (ADR-0034): the patched component is a REFERENCED node
            # not owned by this run — do NOT stamp this run's profile. Created
            # profile-less -> F-6 fail-closed for scoped tenants until the real
            # OWLComp is indexed under its own owner; ON MATCH leaves profile
            # untouched.
            tx.run("""
                MATCH (j:JSPatch {target: $target, patch_name: $pn,
                                  module: $mod, odoo_version: $v})
                MERGE (placeholder:OWLComp {name: $target,
                                            module: '__unresolved__', odoo_version: $v})
                ON CREATE SET placeholder.unresolved = true
                MERGE (j)-[:PATCHES {unresolved: true}]->(placeholder)
            """, target=patch.target, pn=patch.patch_name,
                 mod=patch.module, v=patch.odoo_version)


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
                l.core_symbol_qname = $cs,
                l.code_pattern = $cp
        """, rid=r.rule_id, v=r.odoo_version, kind=r.kind,
             msg=r.message, sev=r.severity, fp=r.file_pattern,
             fix=r.fix_template, cs=r.core_symbol_qname,
             cp=r.code_pattern)
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
    tx, stylesheets: list[StylesheetInfo], profiles: list[str],
    repo_root=None, repo_id=None,
) -> None:
    """MERGE :Stylesheet nodes + :DEFINED_IN -> :Module + :IMPORTS edges.

    Composite MERGE key: (file_path, module, odoo_version) per ADR-0025 §D1.
    Properties written:
      - language ∈ {css, scss, less}
      - selector_count, variable_count, import_count, mixin_count
      - profile[] — ancestor profile name array (per ADR-0016 Option Y)
      - repo_id — owning repo id (ADR-0037: disambiguates the :IMPORTS target)

    Relationships:
      - :Stylesheet -[:DEFINED_IN]-> :Module  (always written)
      - :Stylesheet -[:IMPORTS]-> :Stylesheet  (only when import target is indexed)
        Silent skip when the imported file_path is not found in Neo4j (per ADR-0025 §D3).

    The DEFINED_IN target Module is written with MERGE (not MATCH) and the
    profile-union pattern from ADR-0016 §D7 — if the Module hasn't been
    written yet (forward-reference race in parallel indexing), we create a
    stub so the Stylesheet is never orphaned.  The real Module write later
    in the same batch idempotently fills in repo/path/version_raw/etc.

    ADR-0037: *repo_root* (the repo checkout root) relativizes both the
    Stylesheet's own file_path (MERGE key) AND each resolved @import target so
    the :IMPORTS edge matches relative↔relative.  All stylesheets in one
    indexer run share the same repo_root.  None → paths stored verbatim
    (back-compat for callers that don't pass it).

    ADR-0037: *repo_id* scopes the :IMPORTS target MATCH.  Once file_path is
    repo-relative, two repos at the same odoo_version can hold the SAME relative
    path (e.g. community + enterprise overlay both ship
    ``addons/web/static/src/scss/variables.scss``).  Without repo_id the target
    MATCH would be ambiguous and create spurious cross-repo :IMPORTS edges.  A
    SCSS @import always resolves within the SAME repo as the importer, so we
    scope src+tgt to the same repo_id.  None → both src and tgt match only
    other repo_id-NULL nodes (back-compat: legacy nodes carry no repo_id; a
    None-id run still never crosses into a repo_id-bearing node).
    """
    for s in stylesheets:
        fp_rel = to_repo_relative(s.file_path, repo_root)
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
                          ss.repo_id = $repo_id,
                          ss.profile = $profiles
            ON MATCH  SET ss.language = $lang,
                          ss.selector_count = $sel,
                          ss.variable_count = $var,
                          ss.import_count = $imp,
                          ss.mixin_count = $mix,
                          ss.repo_id = coalesce($repo_id, ss.repo_id),
                          ss.profile =
                              {_profile_union_set("ss")}
            WITH ss
            MERGE (mod:Module {{name: $mod, odoo_version: $v}})
            ON CREATE SET mod.profile = $profiles
            ON MATCH  SET mod.profile =
                {_profile_union_set("mod")}
            MERGE (ss)-[:{REL_DEFINED_IN}]->(mod)
        """, fp=fp_rel, mod=s.module, v=s.odoo_version,
             lang=s.language, sel=s.selector_count, var=s.variable_count,
             imp=s.import_count, mix=s.mixin_count, repo_id=repo_id,
             profiles=profiles)

        # Write IMPORTS edges — silent skip when target Stylesheet not yet indexed.
        # Relativize the resolved target path the same way as the source so the
        # MERGE key match is relative↔relative (ADR-0037).  Scope BOTH src and
        # tgt by repo_id so a relative path shared across repos at the same
        # version can't create a cross-repo :IMPORTS edge (ADR-0037).
        for import_path in s.imports:
            # Cypher has no IS NOT DISTINCT FROM (Neo4j 5.x); use explicit
            # null-safe equality so a None repo_id matches only other None rows.
            tx.run(f"""
                MATCH (src:Stylesheet {{file_path: $src_fp, module: $mod, odoo_version: $v}})
                WHERE src.repo_id = $repo_id
                   OR (src.repo_id IS NULL AND $repo_id IS NULL)
                MATCH (tgt:Stylesheet {{file_path: $tgt_fp, odoo_version: $v}})
                WHERE tgt.repo_id = $repo_id
                   OR (tgt.repo_id IS NULL AND $repo_id IS NULL)
                MERGE (src)-[:{REL_IMPORTS}]->(tgt)
            """, src_fp=fp_rel, mod=s.module, v=s.odoo_version, repo_id=repo_id,
                 tgt_fp=to_repo_relative(import_path, repo_root))


# ---------------------------------------------------------------------------
# RelaxNG LintViolation writer (WI-E, M11)
# ---------------------------------------------------------------------------

def _write_lint_violations_batch(
    tx, violations: list[LintViolationInfo], profiles: list[str],
    repo_root=None,
) -> None:
    """MERGE :LintViolation nodes + :HAS_VIOLATION edge from owning :View.

    Composite MERGE key: (file_path, line, rule, odoo_version).
    The :HAS_VIOLATION edge source is the :View node keyed on (xmlid,
    odoo_version) — i.e. (view)-[:HAS_VIOLATION]->(lv).  Silent skip when the
    View does not yet exist — the edge will be created once the View is written
    (idempotent MERGE on next run).

    ADR-0037: *repo_root* relativizes file_path (a MERGE-key component) the same
    way as Stylesheet — without this, fresh nodes stay absolute-keyed and the
    post-reindex cleanup (ops/cleanup_absolute_path_nodes.cypher) would wrongly
    delete them.  None → stored verbatim (back-compat for callers without it).
    """
    for v in violations:
        fp_rel = to_repo_relative(v.file_path, repo_root)
        # Upsert the LintViolation node.
        # Composite key (file_path, line, rule, odoo_version) collapses multiple
        # same-line/same-rule messages into one node (last-write-wins, by design).
        tx.run(f"""
            MERGE (lv:LintViolation {{
                file_path: $fp, line: $line,
                rule: $rule, odoo_version: $ver
            }})
            ON CREATE SET lv.message = $msg,
                          lv.severity = $sev,
                          lv.view_xmlid = $xmlid,
                          lv.view_type = $vtype,
                          lv.profile = $profiles
            ON MATCH  SET lv.message = $msg,
                          lv.severity = $sev,
                          lv.view_xmlid = $xmlid,
                          lv.view_type = $vtype,
                          lv.profile =
                              {_profile_union_set("lv")}
        """, fp=fp_rel, line=v.line, rule=v.rule, ver=v.odoo_version,
             msg=v.message, sev=v.severity, xmlid=v.view_xmlid,
             vtype=v.view_type, profiles=profiles)

        # HAS_VIOLATION edge from :View to :LintViolation
        # Silent skip when the View node does not exist yet.
        tx.run(f"""
            MATCH (view:View {{xmlid: $xmlid, odoo_version: $ver}})
            MATCH (lv:LintViolation {{
                file_path: $fp, line: $line,
                rule: $rule, odoo_version: $ver
            }})
            MERGE (view)-[:{REL_HAS_VIOLATION}]->(lv)
        """, xmlid=v.view_xmlid, ver=v.odoo_version,
             fp=fp_rel, line=v.line, rule=v.rule)


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
                # T1: enable index-backed lookup by (model, odoo_version) without name
                # Covers impact_analysis Q3 (field/model entity_type) — avoids full scan
                # on deep-inheritance models (sale.order has 50+ extending modules).
                "CREATE INDEX IF NOT EXISTS FOR (n:Method)"
                " ON (n.model, n.odoo_version)",
                # T2: per-hop anchor lookup for ORM read rewrite (#273).
                # Each hop in the per-hop name-dedup CALL subquery MATCHes
                # Model(name, odoo_version) — without this index that is a full label
                # scan repeated for every ancestor set expansion.
                "CREATE INDEX IF NOT EXISTS FOR (n:Model)"
                " ON (n.name, n.odoo_version)",
                # T3: _field_names_on_model helper lookup (#273).
                # Covers the "did you mean" field-suggestion path that queries
                # Field(model, odoo_version) — currently a label scan on every miss.
                "CREATE INDEX IF NOT EXISTS FOR (n:Field)"
                " ON (n.model, n.odoo_version)",
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
                # WI-E RelaxNG lint violation layer (M11):
                "CREATE INDEX IF NOT EXISTS FOR (n:LintViolation)"
                " ON (n.file_path, n.line, n.rule, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:LintViolation)"
                " ON (n.view_xmlid, n.odoo_version)",
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
        repo_root=None,
        repo_id=None,
    ) -> None:
        """Persist :Stylesheet nodes + :DEFINED_IN + :IMPORTS edges.

        Idempotent MERGE on composite key (file_path, module, odoo_version).
        *profiles* is the ancestor profile name array (per ADR-0016 Option Y).
        *repo_root* relativizes file_path + @import targets to repo-relative
        form (ADR-0037); all stylesheets in one run share one repo_root.
        *repo_id* scopes the :IMPORTS target MATCH so a relative path shared
        across repos at the same version cannot create a cross-repo edge
        (ADR-0037); all stylesheets in one run share one repo_id.
        Batched at NEO4J_WRITE_BATCH_SIZE per transaction.
        IMPORTS edge write silently skips when the target file_path is not indexed.
        """
        if not stylesheets:
            return
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for batch in _chunked(stylesheets, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(
                    _write_stylesheets_batch, batch, _profiles, repo_root, repo_id,
                )

    def write_lint_violations(
        self,
        violations: list[LintViolationInfo],
        profiles: list[str] | None = None,
        repo_root=None,
    ) -> None:
        """Persist :LintViolation nodes + :HAS_VIOLATION edges to :View (WI-E, M11).

        Idempotent MERGE on composite key (file_path, line, rule, odoo_version).
        *profiles* is the ancestor profile name array (per ADR-0016 Option Y).
        *repo_root* relativizes file_path (MERGE-key component) per ADR-0037 — all
        violations in one run share one repo_root.
        Batched at NEO4J_WRITE_BATCH_SIZE per transaction.
        The :HAS_VIOLATION edge is silently skipped when the target :View has
        not yet been written — the edge is written on the next incremental run.
        Should be called after write_view_results() so View nodes exist.
        """
        if not violations:
            return
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for batch in _chunked(violations, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(
                    _write_lint_violations_batch, batch, _profiles, repo_root,
                )

    def delete_modules_scoped(self, repo_basename: str, odoo_version: str) -> dict:
        """DETACH DELETE Module(s) matching (repo, odoo_version) + cascading child nodes.

        Child nodes (Model/Field/Method/View/QWebTmpl/JSPatch/OWLComp) are
        scoped by (module_name, odoo_version) — they're deleted ONLY if their
        Module parent is being deleted in this call, to avoid orphan cleanup of
        nodes that belong to other repos in the same version.

        Implementation note: steps 2 and 3 use CALL {} IN TRANSACTIONS (batched
        implicit-transaction form) to avoid exceeding db.transaction.timeout on large
        repos (odoo core 17.0 can have millions of child nodes). CALL IN TRANSACTIONS
        must run in an auto-commit (implicit) session — this method already uses
        self.driver.session() directly, so NO managed execute_write wrapper is used.
        The per-repo Postgres advisory lock held by the caller (web_ui/routes/repos.py)
        guarantees no concurrent writes to the same repo+version pair during deletion.

        Outer-tx timeout caveat (verified Neo4j 5.26.25, 2026-06-10): batching bounds
        each INNER transaction, but the OUTER coordinating transaction of
        CALL IN TRANSACTIONS is itself subject to db.transaction.timeout. Deleting a
        very large repo (millions of nodes, hundreds of batches) whose TOTAL elapsed
        exceeds the configured timeout (600s, see docs/operations/timeouts.md) will
        have its outer tx terminated part-way. This is recoverable — already-committed
        batches persist and a re-run resumes the delete (idempotent DETACH DELETE) —
        but the Web UI surfaces a TransactionTimedOut error. For an exceptionally
        large repo delete, temporarily raise/disable db.transaction.timeout
        (CALL dbms.setConfigValue('db.transaction.timeout','0')) per the same
        guidance as ops/cleanup_same_name_inherits_mesh.cypher.

        Returns: {"modules": N, "children": M} counts.
        """
        with self.driver.session() as session:
            # Step 1: collect module names being deleted (lightweight point-lookup)
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

            # Step 2: delete child nodes in batches (NEO4J_DELETE_BATCH_ROWS) to stay
            # well under db.transaction.timeout (600s). CALL {} IN TRANSACTIONS requires
            # an implicit (auto-commit) transaction — session.run() here, NOT execute_write.
            # CALL (child) { ... } syntax required for Neo4j 5.23+ (5.x deprecates
            # CALL { WITH <var> } in favour of CALL (<var>) { }; both work on 5.26.25).
            children_row = session.run(
                f"""
                MATCH (child)
                WHERE child.module IN $names AND child.odoo_version = $version
                  AND (child:Model OR child:Field OR child:Method OR child:View
                       OR child:QWebTmpl OR child:JSPatch OR child:OWLComp)
                CALL (child) {{
                    DETACH DELETE child
                }} IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_ROWS} ROWS
                RETURN count(child) AS cc
                """,
                names=module_names,
                version=odoo_version,
            ).single()
            children_deleted = children_row["cc"] if children_row is not None else 0

            # Step 3: delete the Module nodes themselves (batched, same rationale)
            modules_row = session.run(
                f"""
                MATCH (m:Module {{repo: $repo, odoo_version: $version}})
                CALL (m) {{
                    DETACH DELETE m
                }} IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_ROWS} ROWS
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
            live_paths:   Repo-relative module path strings for this repo in this
                          run (ADR-0037: must match the relative form stored in
                          Module.path).  Modules NOT in this set are stale.  The
                          caller (pipeline) is responsible for relativizing the
                          scanner output before passing it here — a mismatch
                          (absolute live_paths vs relative Module.path) would
                          mark every node stale and DETACH DELETE the graph.

        Risk gate (enforced by caller): only called when len(live_paths) >= 1.

        ADR-0037 mixed-graph guard: live_paths is now repo-RELATIVE.  If this
        runs against a graph still holding pre-ADR-0037 ABSOLUTE Module.path
        (starts with '/'), EVERY module would mismatch live_paths and be DETACH
        DELETEd.  So before deleting, count absolute-path Module nodes for this
        repo+version; if any exist the graph is mixed/legacy — SKIP GC, log a
        warning, and return 0 so the operator runs a full ``--full`` reindex
        first (per docs/deploy/reindex-v8-v19-runbook.md).
        """
        with self.driver.session() as session:
            abs_count = session.run(
                """
                MATCH (m:Module {repo: $repo, odoo_version: $version})
                WHERE m.path STARTS WITH '/'
                RETURN count(m) AS n
                """,
                repo=repo,
                version=odoo_version,
            ).single()
            if abs_count is not None and abs_count["n"] > 0:
                _logger.warning(
                    "Module GC skipped: %d Module node(s) for repo %s version %s "
                    "still carry ABSOLUTE paths (pre-ADR-0037). Running relative-path "
                    "GC against them would delete the whole repo. Run a full --full "
                    "reindex first (see reindex-v8-v19-runbook.md).",
                    abs_count["n"], repo, odoo_version,
                )
                return 0
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

    def gc_unresolved_placeholders(self, odoo_version: str) -> dict[str, int]:
        """DETACH DELETE inert '__unresolved__' placeholder nodes for odoo_version.

        Placeholder nodes are created when the writer encounters a reference to
        a Model / View / QWebTmpl / OWLComp that has not been indexed yet (parent
        not found at write time).  All queries in server.py already filter these
        out at read time (``module <> '__unresolved__'`` / ``coalesce(unresolved,
        false) = false``), so they are invisible to users.  Over time they
        accumulate (2,068 on prod as of 2026-05-26) and produce "shadow" View
        pairs when the real View is later indexed against the old 3-key MERGE.

        This method deletes ALL placeholder nodes that carry ``unresolved=true``
        AND ``module='__unresolved__'``, scoped strictly to ``odoo_version``.
        DETACH DELETE removes incident edges (the ``{unresolved:true}`` relation
        edges) along with the node — no orphan edges remain.

        After deleting placeholders this method also calls
        :meth:`heal_resolved_unresolved_flags` as a defense-in-depth step
        (ADR-0007 §D5 extension).  That sibling clears ``unresolved=true`` flags
        that survived on already-resolved nodes/edges (stale artefacts from the
        old placeholder path before PR #194) — making 153 prod nodes and 326
        prod edges visible to MCP clients again.

        Safety argument:
        - server.py filters every placeholder at read time → deleting them
          changes nothing visible to MCP clients or the Web UI.
        - Scoped by ``odoo_version`` so cross-version/tenant data is never touched.
        - Idempotent: a second run returns zeros.
        - This is the companion cleanup for the writer fix (ADR-0007 §D5 extension)
          that closes the shadow-View producer going forward; this gc removes
          existing stale placeholders on the current graph.

        Returns a dict with per-label deleted counts, e.g.::

            {"Model": 260, "View": 629, "QWebTmpl": 373, "OWLComp": 806}
        """
        counts: dict[str, int] = {}
        labels = ["Model", "View", "QWebTmpl", "OWLComp"]
        with self.driver.session() as session:
            for label in labels:
                row = session.run(
                    f"""
                    MATCH (n:{label})
                    WHERE n.odoo_version = $version
                      AND n.module = '__unresolved__'
                      AND coalesce(n.unresolved, false) = true
                    DETACH DELETE n
                    RETURN count(n) AS deleted
                    """,
                    version=odoo_version,
                ).single()
                counts[label] = row["deleted"] if row is not None else 0
                if counts[label] > 0:
                    _logger.info(
                        "Placeholder GC: deleted %d __unresolved__ %s nodes for version %s",
                        counts[label], label, odoo_version,
                    )
        total = sum(counts.values())
        _logger.info(
            "Placeholder GC complete for version %s: %d total nodes deleted %s",
            odoo_version, total, counts,
        )
        # Defense-in-depth: heal any stale unresolved=true flags on already-resolved
        # nodes/edges that survived from the pre-PR-#194 placeholder path.
        self.heal_resolved_unresolved_flags(odoo_version)
        return counts

    def reconcile_same_name_inherits(self, odoo_version: str) -> int:
        """MERGE any missing extender-to-definition INHERITS edges for odoo_version.

        Background — topology change (#273, ADR new):
        The writer (W1) now emits K×D edges: each extender Model node (same name,
        is_definition=false) gets one INHERITS edge per definition node (is_definition=true)
        of the same name+version.  Before this fix it emitted K² mesh edges.

        Cross-repo write-order gap:
        When an extender repo is indexed BEFORE the definition repo (no topo order between
        repos, only within a single repo's module dependency tree), the definition node does
        not exist at write time → the writer MATCH tip returns 0 rows → 0 edges created.
        A subsequent index_repo run for the definition repo writes the definition node but
        does NOT retroactively connect the extenders in other repos that were written earlier.

        This post-pass reconciliation fills those gaps after all repos for the version have
        been written.  It is designed to be:
        - **Idempotent** (MERGE — safe to run twice, only creates missing edges).
        - **Version-scoped** (odoo_version parameter — only touches the current run's version).
          Physical plan note: the driving ``MATCH (ext:Model) WHERE ext.odoo_version = $version``
          carries no ``name`` predicate, so it is ONE :Model label scan filtered by version
          (the Model(name, odoo_version) index needs a name anchor and cannot serve a
          version-only filter). It does not scan other versions' rows beyond the label-scan
          membership test, but on a large graph this is a per-run linear cost that grows with
          the total :Model count. Acceptable under the 600s db.transaction.timeout for current
          graph sizes; if it becomes a hotspot, scope by the run's module-name set or add a
          Model(odoo_version) index (deferred — no new index added in this wave).
        - **Safe in both incremental and full-reindex runs** (runs at the end of
          _index_repo, after gc_unresolved_placeholders, for every version indexed in the run).

        Selection criterion for "extender" (who gets a reconciled edge):
        A Model node M is treated as an extender requiring reconciliation when ALL of:
          1. M.odoo_version == odoo_version (version-scoped).
          2. coalesce(M.is_definition, false) = false  (M is not the definition).
          3. M.module <> '__unresolved__'  (skip placeholder nodes — gc handles them).
          4. There exists at least one definition node D with the same name+version:
             D.name = M.name, D.odoo_version = M.odoo_version,
             coalesce(D.is_definition, false) = true, D.module <> M.module.
          5. M does NOT already have an INHERITS edge to D (the gap to fill).

        The Cypher MATCH for "extender has same-name out-edges" is NOT used as the criterion
        (that would conflate cross-name parent edges with same-name self-extend edges).
        Instead, the criterion is purely structural: name-match to a definition node and
        missing edge.  This is correct because:
        - If M is a pure cross-name extender (only has `_inherit['other.model']`), it will
          have a different name from any definition, so condition 4 never matches → no
          spurious edges created.
        - If M is a same-name extender that was written before its definition existed, it may
          have 0 same-name INHERITS out-edges currently → this pass creates the missing one.
        - If M already has the correct extender→definition edge, condition 5 excludes it
          from the MERGE → idempotent.

        `r.order` on the new edge:
        Prefer the minimum `r.order` from any existing same-name INHERITS out-edge on M
        (preserves the MRO position recorded at write time if at least one edge exists).
        Falls back to 0 when M has no same-name out-edges yet (cross-repo gap: no edge was
        created at write time, so we use the "lowest priority" sentinel — consistent with
        the writer's ON CREATE default for a model that only has `_inherit = ['own.name']`).

        Failure policy:
        Logs a WARNING and returns 0 on any Neo4j error — does NOT raise.  This mirrors
        the auto-reseed pattern (ADR-0007): a post-pass failure should never abort the
        indexer run; the graph is still correct up to this point, and the next run will
        retry the reconciliation.

        Concurrency (--profile-workers):
        Two profiles indexing the SAME version in parallel can run this MERGE pass
        concurrently and hit a Neo4j MERGE deadlock on the shared definition node. The
        failure policy above absorbs the deadlock (WARNING + return 0) rather than
        aborting, but the loser then leaves its gap unfilled for that run — re-run the
        profile, or accept the miss (the next full reindex fills it). Idempotent MERGE
        makes a re-run safe. An advisory lock per-version for the reconcile pass would
        serialize same-version parallel runs and prevent the deadlock entirely; this is
        tracked as future work (issue #279).

        Returns the number of INHERITS edges created (0 if already complete or on error).
        """
        try:
            with self.driver.session() as session:
                row = session.run(
                    f"""
                    // For each extender Model that lacks an edge to its definition node,
                    // determine the order to stamp on the new edge.
                    MATCH (ext:Model)
                    WHERE ext.odoo_version = $version
                      AND NOT coalesce(ext.is_definition, false)
                      AND ext.module <> '__unresolved__'
                    // Collect the minimum order from any existing same-name out-edge
                    // (there may be none if this is a pure cross-repo gap).
                    OPTIONAL MATCH (ext)-[existing_r:{REL_INHERITS}]->(same_name:Model)
                    WHERE same_name.name = ext.name
                      AND same_name.odoo_version = ext.odoo_version
                    WITH ext, min(existing_r.order) AS edge_order
                    // Find the definition node(s) for this extender's model name.
                    MATCH (def:Model)
                    WHERE def.name = ext.name
                      AND def.odoo_version = ext.odoo_version
                      AND coalesce(def.is_definition, false) = true
                      AND def.module <> ext.module
                    WITH ext, def, edge_order
                    // Only process pairs that are missing the edge (idempotency guard).
                    WHERE NOT (ext)-[:{REL_INHERITS}]->(def)
                    // MERGE creates the edge only when it does not already exist.
                    MERGE (ext)-[r:{REL_INHERITS}]->(def)
                    ON CREATE SET r.order = coalesce(edge_order, 0)
                    RETURN count(r) AS created
                    """,
                    version=odoo_version,
                ).single()
                created = row["created"] if row is not None else 0
                if created > 0:
                    _logger.info(
                        "Same-name INHERITS reconciliation: created %d edge(s) "
                        "for version %s (cross-repo write-order gap fill)",
                        created, odoo_version,
                    )
                else:
                    _logger.debug(
                        "Same-name INHERITS reconciliation: no gaps found for version %s",
                        odoo_version,
                    )
                return created
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Same-name INHERITS reconciliation failed for version %s: %s — "
                "indexer run continues; next run will retry",
                odoo_version, exc,
            )
            return 0

    def heal_resolved_unresolved_flags(self, odoo_version: str) -> dict[str, int]:
        """Clear stale ``unresolved=true`` flags on already-resolved View/QWebTmpl nodes
        and their incident edges, scoped to ``odoo_version``.

        **Why these flags are stale.**  Before PR #194, View/QWebTmpl placeholder MERGE
        keys used three properties (``{xmlid, module:'__unresolved__', odoo_version}``).
        When the real node was later indexed the old real-write SET block updated
        ``module=<real>`` but never cleared ``unresolved=true``.  The subsequent
        ``ops/cleanup_unresolved_placeholders.cypher`` deleted nodes where
        ``module='__unresolved__'``, but these nodes already had their module rewritten
        to the real value — so they survived with ``module=<real>`` AND
        ``unresolved=true``.  Their incident edges kept ``unresolved=true`` too.

        **Correctness argument.**  A node with ``module <> '__unresolved__'`` was
        written by a real indexer pass.  Its ``unresolved=true`` is an artefact of
        the old placeholder path; clearing it restores the correct visible state.
        An edge whose target has ``module <> '__unresolved__'`` is a resolved
        relationship; its ``unresolved=true`` is likewise stale.

        **Scope.**  Only ``View`` and ``QWebTmpl`` nodes are affected — their real
        MERGE key is ``{xmlid, odoo_version}`` (no module), so a real write can
        converge onto a former placeholder.  ``Model`` / ``OWLComp`` include
        ``module`` in their MERGE key, so real and placeholder are always distinct
        nodes and this gap never applies to them.

        **Safety.**  This method only SETs flag properties; it does NOT delete any
        nodes or edges.  Scoped by ``odoo_version`` so cross-version/tenant data is
        never touched.  Idempotent: a second run returns zeros.

        This is called automatically by ``gc_unresolved_placeholders`` as a
        defense-in-depth step (ADR-0007 §D5 extension).  A one-time ops script
        (``ops/cleanup_resolved_unresolved_flags.cypher``) clears the existing prod
        backlog independently of a GC run.

        Returns a dict with heal counts, e.g.::

            {"nodes": 153, "edges": 326}
        """
        with self.driver.session() as session:
            node_row = session.run(
                """
                MATCH (n)
                WHERE (n:View OR n:QWebTmpl)
                  AND n.odoo_version = $version
                  AND coalesce(n.unresolved, false) = true
                  AND coalesce(n.module, '') <> '__unresolved__'
                SET n.unresolved = false
                RETURN count(n) AS healed
                """,
                version=odoo_version,
            ).single()
            nodes_healed = node_row["healed"] if node_row is not None else 0

            edge_row = session.run(
                """
                MATCH ()-[r]->(t)
                WHERE r.unresolved = true
                  AND t.odoo_version = $version
                  AND coalesce(t.module, '') <> '__unresolved__'
                SET r.unresolved = false
                RETURN count(r) AS healed
                """,
                version=odoo_version,
            ).single()
            edges_healed = edge_row["healed"] if edge_row is not None else 0

        if nodes_healed > 0 or edges_healed > 0:
            _logger.info(
                "Heal resolved flags: cleared %d stale-unresolved nodes, "
                "%d stale-unresolved edges for version %s",
                nodes_healed, edges_healed, odoo_version,
            )
        else:
            _logger.debug(
                "Heal resolved flags: no stale flags found for version %s",
                odoo_version,
            )
        return {"nodes": nodes_healed, "edges": edges_healed}

    def gc_null_repo_dep_stubs(self, odoo_version: str) -> int:
        """DETACH DELETE childless dep-stub Module nodes for odoo_version.

        These are :Module nodes created by the dep-target MERGE
        (write_parse_result) for ``module.depends`` entries that were never
        indexed under their own profile.  Their MERGE key is
        ``{name, odoo_version}`` only — no ``repo``, no ``repo_id``, no
        ``DEFINED_IN`` children.  Because ``gc_stale_modules`` keys on a
        concrete non-NULL ``repo`` string it never matches these stubs.

        Safety:
        - Only deletes where ``m.repo_id IS NULL`` (absent) AND no
          ``DEFINED_IN`` child exists.
        - A node with ``repo_id`` set was written by a real indexer run —
          never deleted here.
        - DETACH DELETE removes incident DEPENDS_ON edges along with the node.
        - The dep-MERGE re-creates the stub + edge on the very next indexer
          run for any dep still declared, so deletion is safe — the stub
          resurrects automatically.
        - Scoped by ``odoo_version`` so cross-version data is never touched.
        - Idempotent: a second run returns 0.

        Must run AFTER all profiles for ``odoo_version`` have completed
        indexing in this pass (so a stub promoted to a real module in a
        later-running profile is not deleted before that profile runs).
        Place in ``index_all()`` AFTER the parallel/sequential profile loop,
        NOT per-profile and NOT per-repo.

        Returns count of deleted nodes.
        """
        with self.driver.session() as session:
            row = session.run(
                """
                MATCH (m:Module {odoo_version: $version})
                WHERE m.repo_id IS NULL
                  AND NOT EXISTS { (m)<-[:DEFINED_IN]-() }
                DETACH DELETE m
                RETURN count(m) AS deleted
                """,
                version=odoo_version,
            ).single()
        deleted = row["deleted"] if row is not None else 0
        if deleted > 0:
            _logger.info(
                "Dep-stub GC: deleted %d childless repo_id=NULL Module nodes "
                "for version %s",
                deleted, odoo_version,
            )
        else:
            _logger.debug(
                "Dep-stub GC: no childless NULL-repo Module stubs found "
                "for version %s",
                odoo_version,
            )
        return deleted

    def write_spec_metadata(
        self, kind: str, odoo_version: str, curate_status: str,
    ) -> None:
        """Upsert a SpecMetadata node recording curation status for a spec kind + version.

        Composite key: (kind, odoo_version). MERGE is idempotent.

        Args:
            kind:          'lint' | 'cli' — which spec category this metadata covers.
            odoo_version:  Odoo version label, e.g. '8.0', '17.0'.
            curate_status: 'pending' | 'complete' (the two values actually written
                by the pipeline: 'complete' when a static spec JSON declares
                ``_curate_status: complete``, else the 'pending' default). Any
                string is accepted per ADR-0002 §4; the MCP read side treats
                anything other than 'pending'/'complete' (incl. absent) as a
                data gap and discloses accordingly.
        """
        with self.driver.session() as session:
            session.run("""
                MERGE (sm:SpecMetadata {kind: $kind, odoo_version: $v})
                SET sm.curate_status = $curate_status
            """, kind=kind, v=odoo_version, curate_status=curate_status)
