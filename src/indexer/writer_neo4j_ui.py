# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/writer_neo4j_ui.py
"""UI-layer Neo4j writer — View / QWebTmpl / OWLComp / JSPatch / Stylesheet.

Extracted from writer_neo4j.py (B5 structural split, no behaviour change). Owns
``_write_view_parse_result``, ``_write_js_graph_result`` and
``_write_stylesheets_batch`` — every Cypher MERGE here is byte-identical to the
original.

The shared ``_profile_union_set`` Cypher fragment (ADR-0034 SSOT) lives in
``writer_neo4j`` and is imported lazily inside each function body to avoid an
import cycle (writer_neo4j re-exports this module at the bottom) — see
writer_neo4j_orm for the full rationale; the function-local import keeps each
child cold-importable.

``_logger`` is pinned to the name ``src.indexer.writer_neo4j`` (NOT ``__name__``)
so the unresolved-INHERITS_VIEW / unresolved-EXTENDS_TMPL WARNING records land
under the same logger the pre-split code used — tests assert on that exact logger
name via ``caplog.at_level(logger="src.indexer.writer_neo4j")``.
"""
import logging

from src.constants import (
    REL_DEFINED_IN,
    REL_IMPORTS,
    REL_INHERITS_VIEW,
    REL_TARGETS_MODEL,
)

from .models import (
    JSGraphResult,
    StylesheetInfo,
    ViewParseResult,
    to_repo_relative,
)

# Pinned logger name (see module docstring) — not __name__.
_logger = logging.getLogger("src.indexer.writer_neo4j")


def _write_view_parse_result(tx, result: ViewParseResult, profiles: list[str]) -> None:
    from .writer_neo4j import _profile_union_set

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
    from .writer_neo4j import _profile_union_set

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
            WITH j, c ORDER BY c.module ASC LIMIT 1
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
    from .writer_neo4j import _profile_union_set

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
