# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/writer_neo4j_orm.py
"""ORM-layer Neo4j writer — Module / Model / Field / Method (schema C1 core).

Extracted from writer_neo4j.py (B5 structural split, no behaviour change). This
module owns ``_write_parse_result``, the schema C1 LÕI: every Cypher MERGE here
is byte-identical to the original — the composite MERGE keys for
Module/Model/Field/Method and the same-name INHERITS topology (ADR-0048 K×D
extender→definition edges) are load-bearing and were copied verbatim.

The shared ``_profile_union_set`` Cypher fragment (ADR-0034 SSOT) lives in
``writer_neo4j`` and is imported lazily inside the function body — a module-level
``from .writer_neo4j import _profile_union_set`` would create an import cycle
(writer_neo4j re-exports this module at the bottom), and a function-local import
breaks that cycle so each child stays cold-importable on its own.

``_logger`` is pinned to the name ``src.indexer.writer_neo4j`` (NOT
``__name__``) so the unresolved-reference WARNING records land under the same
logger the pre-split code used — tests assert on that exact logger name via
``caplog.at_level(logger="src.indexer.writer_neo4j")``.
"""
import logging

from src.constants import (
    REL_DEFINED_IN,
    REL_DEPENDS_ON,
    REL_DEPENDS_ON_FIELD,
    REL_INHERITS,
    REL_USES_CORE_SYMBOL,
    REL_USES_FIELD,
)

from .models import ParseResult

# Pinned logger name (see module docstring) — not __name__.
_logger = logging.getLogger("src.indexer.writer_neo4j")


def _write_parse_result(tx, result: ParseResult, profiles: list[str]) -> None:
    from .writer_neo4j import _profile_union_set

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
                      AND coalesce(parent.is_definition, false) = true
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
                  AND coalesce(d.is_definition, false) = true
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
