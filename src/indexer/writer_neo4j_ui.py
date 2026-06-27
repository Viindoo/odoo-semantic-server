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
    REL_REPORTS_ON,
    REL_TARGETS_MODEL,
    REL_USES_TEMPLATE,
)

from .models import (
    AssetParseResult,
    JSGraphResult,
    StylesheetInfo,
    ViewParseResult,
    to_repo_relative,
)

# Pinned logger name (see module docstring) — not __name__.
_logger = logging.getLogger("src.indexer.writer_neo4j")


def _base_module_out_of_scope(tx, inherit_xmlid: str, odoo_version: str) -> bool:
    """Return True when the base xmlid's module is NOT indexed in any profile.

    Distinguishes a genuine coverage gap from an expected-by-design unresolvable
    reference (the category-B fix in the zero-warning wave):

    - Expected gap (-> True -> caller logs DEBUG): the base module exists in the
      graph only as a profile-less stub created by a DEPENDS_ON edge, or does not
      exist at all. This happens for OEEL-1 license-skipped modules (e.g.
      ``certificate``), modules absent from the target version (upgrade gap), and
      Enterprise modules not cloned into any indexed profile. The unresolved
      reference is correct behaviour, so a WARNING would be noise.

    - Genuine coverage gap (-> False -> caller logs WARNING): the base module IS
      indexed but the specific view/template it should contain is missing — a real
      indexer/parser gap worth surfacing.

    The base module name is the segment before the first ``.`` in the xmlid
    (e.g. ``certificate.foo`` -> ``certificate``). "Out of scope" means the module
    has NO indexed node at all: either no ``:Module`` row exists, OR the only row
    is a profile-less forward-reference stub created by a ``DEPENDS_ON`` MERGE
    (which sets NO ``profile`` property at all -> ``profile IS NULL``). A real
    indexed module is ALWAYS written with ``ON CREATE SET mod.profile = $profiles``
    by the model/view/qweb write paths, so its ``profile`` property is present
    (possibly an EMPTY list when indexed with no profiles, e.g. a profileless test
    run) -> NOT out of scope -> keep WARNING. Testing ``size(profile) = 0`` would
    wrongly classify such a genuinely-indexed-but-profileless module as out of
    scope and silently downgrade a real coverage gap to DEBUG.

    Read-only single-row check on the current transaction state — cheap, no
    placeholder side effects.
    """
    base_module = inherit_xmlid.split(".", 1)[0]
    if not base_module:
        return False
    rec = tx.run(
        """
        MATCH (m:Module {name: $base_module, odoo_version: $ver})
        RETURN m.profile IS NULL AS out_of_scope
        """,
        base_module=base_module, ver=odoo_version,
    ).single()
    # No module row at all -> never indexed -> out of scope (expected gap).
    if rec is None:
        return True
    return bool(rec["out_of_scope"])


def _write_view_parse_result(tx, result: ViewParseResult, profiles: list[str]) -> None:
    import json

    from .writer_neo4j import _profile_union_set

    for view in result.views:
        # GAP-1 - conditional-visibility expressions serialized as a JSON blob on
        # the View node (no new node type - a property is enough for AI-agent
        # readout via model_inspect/view-resource). Each entry:
        # {element, attr, expr, field, legacy}. Empty list -> "[]" (never None,
        # so the property is always present and queryable). Neo4j has no native
        # map-list type, hence JSON-string.
        conditions_json = json.dumps([
            {
                "element": c.element,
                "attr": c.attr,
                "expr": c.expr,
                "field": c.field,
                "legacy": c.legacy,
            }
            for c in view.conditions
        ])
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
                v.conditions = $conditions,
                v.unresolved = false
        """, xmlid=view.xmlid, ver=view.odoo_version,
             name=view.name, model=view.model, module=view.module,
             view_type=view.view_type, mode=view.mode,
             xpaths_exprs=[x.expr for x in view.xpaths],
             xpaths_positions=[x.position for x in view.xpaths],
             arch_snippet=view.arch_snippet,
             conditions=conditions_json,
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
                # Category-B downgrade (see _base_module_out_of_scope): when the
                # base module is not indexed in any profile (license-skip, absent
                # version, or Enterprise-not-indexed) the gap is EXPECTED — log at
                # DEBUG. Keep WARNING only when the base module IS indexed but the
                # specific view is missing (a genuine coverage gap).
                _log = (
                    _logger.debug
                    if _base_module_out_of_scope(tx, view.inherit_xmlid, view.odoo_version)
                    else _logger.warning
                )
                _log(
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
        # GAP-11/GAP-12 - website `key=` + inheriting `mode=` written as plain
        # properties (None-safe: absent attributes stay null). `coalesce` on MATCH
        # preserves an already-set value when a later parse of the same template
        # omits the attribute (defensive against partial re-index ordering).
        tx.run(f"""
            MERGE (t:QWebTmpl {{xmlid: $xmlid, odoo_version: $ver}})
            ON CREATE SET t.profile = $profiles
            ON MATCH  SET t.profile =
                {_profile_union_set("t")}
            SET t.module = $module,
                t.key = coalesce($key, t.key),
                t.mode = coalesce($mode, t.mode),
                t.unresolved = false
        """, xmlid=qweb.xmlid, ver=qweb.odoo_version, module=qweb.module,
             key=qweb.key, mode=qweb.mode, profiles=profiles)

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
            # A3 cross-type EXTENDS_TMPL: a <template inherit_id="..."> may target a
            # base that was indexed as a plain :View node (a standard ir.ui.view
            # form/list record), not a :QWebTmpl — e.g.
            # viin_brand_account.account_tour_upload_bill extending the
            # account.account_tour_upload_bill form view. Match either label.
            # xmlid+odoo_version is unique across both labels, so this stays a
            # single-row lookup (.single() is safe — no multi-row risk).
            rec = tx.run("""
                MATCH (ext:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
                MATCH (base {xmlid: $inherit_xmlid, odoo_version: $ver})
                WHERE (base:QWebTmpl OR base:View)
                  AND NOT coalesce(base.unresolved, false)
                MERGE (ext)-[r:EXTENDS_TMPL]->(base)
                ON MATCH SET r.unresolved = false
                RETURN 1 AS ok
            """, xmlid=qweb.xmlid, ver=qweb.odoo_version,
                 inherit_xmlid=qweb.inherit_xmlid).single()
            if rec is None:
                # WI-D: the base may be an AssetBundle, not a QWebTmpl/View. In
                # v15+ Odoo declares asset bundles (web.assets_backend, ...) in the
                # __manifest__.py 'assets' dict — indexed here as :AssetBundle nodes
                # (keyed on `name`, NOT `xmlid`). A legacy module that still uses the
                # XML extension form <template inherit_id="web.assets_backend"> would
                # otherwise leave an unresolved EXTENDS_TMPL warning (the ~13 A2
                # warnings). Resolve it by linking the extender to the AssetBundle via
                # a dedicated EXTENDS_ASSET_BUNDLE edge (reuse the same cross-label
                # base-lookup spirit as the QWebTmpl OR View match above). name is the
                # composite key's identifying part; (name, odoo_version) is unique, so
                # this stays a single-row .single() lookup.
                ab = tx.run("""
                    MATCH (ext:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
                    MATCH (b:AssetBundle {name: $inherit_xmlid, odoo_version: $ver})
                    MERGE (ext)-[r:EXTENDS_ASSET_BUNDLE]->(b)
                    ON MATCH SET r.unresolved = false
                    RETURN 1 AS ok
                """, xmlid=qweb.xmlid, ver=qweb.odoo_version,
                     inherit_xmlid=qweb.inherit_xmlid).single()
                if ab is not None:
                    continue  # resolved against an AssetBundle — no warning/placeholder
                # Category-B downgrade — same rationale as INHERITS_VIEW above.
                _log = (
                    _logger.debug
                    if _base_module_out_of_scope(tx, qweb.inherit_xmlid, qweb.odoo_version)
                    else _logger.warning
                )
                _log(
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

    # GAP-2/GAP-5 - ir.actions.report records + v8-v13 <report> shorthand.
    # :Report node (composite MERGE key {xmlid, odoo_version}, same shape as
    # View/QWebTmpl). Two edges, both single-row deterministic (NO .single()
    # multi-row pattern WI-A removed):
    #   Report -[:REPORTS_ON]-> Model  (the business model the report runs on)
    #   Report -[:USES_TEMPLATE]-> QWebTmpl  (the report's QWeb template)
    for rep in result.reports:
        tx.run(f"""
            MERGE (rp:Report {{xmlid: $xmlid, odoo_version: $ver}})
            ON CREATE SET rp.profile = $profiles
            ON MATCH  SET rp.profile =
                {_profile_union_set("rp")}
            SET rp.name = $name, rp.model = $model, rp.module = $module,
                rp.report_type = $report_type, rp.report_name = $report_name,
                rp.report_file = $report_file, rp.paperformat = $paperformat,
                rp.unresolved = false
        """, xmlid=rep.xmlid, ver=rep.odoo_version,
             name=rep.name, model=rep.model, module=rep.module,
             report_type=rep.report_type, report_name=rep.report_name,
             report_file=rep.report_file, paperformat=rep.paperformat,
             profiles=profiles)

        tx.run(f"""
            MATCH (rp:Report {{xmlid: $xmlid, odoo_version: $ver}})
            MERGE (mod:Module {{name: $module, odoo_version: $ver}})
            ON CREATE SET mod.profile = $profiles
            ON MATCH  SET mod.profile =
                {_profile_union_set("mod")}
            MERGE (rp)-[:{REL_DEFINED_IN}]->(mod)
        """, xmlid=rep.xmlid, ver=rep.odoo_version, module=rep.module,
             profiles=profiles)

        # REPORTS_ON -> Model. A model name maps to K per-module Model nodes
        # (C1 schema); pick ONE deterministically. Prefer the definition node
        # (is_definition=true), else the highest field_count, with a stable
        # name tiebreak — LIMIT 1 keeps this single-row (no .single() multi-row).
        # Zero-silent-loss (consistent with USES_TEMPLATE below): RETURN + .single()
        # so a Report on an unindexed business model is surfaced — DEBUG when the
        # model's module is out of scope (expected gap), WARNING when it is a real
        # coverage gap (the model's module IS indexed but the model node is missing).
        if rep.model:
            on_model = tx.run(f"""
                MATCH (rp:Report {{xmlid: $xmlid, odoo_version: $ver}})
                MATCH (m:Model {{name: $model_name, odoo_version: $ver}})
                WITH rp, m
                ORDER BY coalesce(m.is_definition, false) DESC,
                         coalesce(m.field_count, 0) DESC, m.module ASC
                LIMIT 1
                MERGE (rp)-[:{REL_REPORTS_ON}]->(m)
                RETURN 1 AS ok
            """, xmlid=rep.xmlid, ver=rep.odoo_version,
                 model_name=rep.model).single()
            if on_model is None:
                _log = (
                    _logger.debug
                    if _base_module_out_of_scope(tx, rep.model, rep.odoo_version)
                    else _logger.warning
                )
                _log(
                    "unresolved REPORTS_ON: %s -> %s (version %s) — "
                    "business model not indexed",
                    rep.xmlid, rep.model, rep.odoo_version,
                )

        # USES_TEMPLATE -> QWebTmpl. The report_name is the template xmlid.
        # Single-row ({xmlid, odoo_version} is unique); skip silently when the
        # template is not (yet) indexed — DEBUG-not-WARNING when the template's
        # module is out of scope (WI-C helper), keeping reindex zero-noise.
        if rep.report_name:
            tmpl = tx.run(f"""
                MATCH (rp:Report {{xmlid: $xmlid, odoo_version: $ver}})
                MATCH (t:QWebTmpl {{xmlid: $tmpl_xmlid, odoo_version: $ver}})
                WHERE NOT coalesce(t.unresolved, false)
                MERGE (rp)-[:{REL_USES_TEMPLATE}]->(t)
                RETURN 1 AS ok
            """, xmlid=rep.xmlid, ver=rep.odoo_version,
                 tmpl_xmlid=rep.report_name).single()
            if tmpl is None:
                _log = (
                    _logger.debug
                    if _base_module_out_of_scope(tx, rep.report_name, rep.odoo_version)
                    else _logger.warning
                )
                _log(
                    "unresolved USES_TEMPLATE: %s -> %s (version %s) — "
                    "report template not indexed",
                    rep.xmlid, rep.report_name, rep.odoo_version,
                )


def _write_asset_parse_result(tx, result: AssetParseResult, profiles: list[str]) -> None:
    """Write :AssetBundle nodes + CONTRIBUTES_TO / INCLUDES_BUNDLE edges (WI-D).

    Graph shape (ADR-0052, survey eraBC §5):
      - (:AssetBundle {name, odoo_version})   composite MERGE key (same shape as
        Module/Model/View). Props: is_private (name CONTAINS '._'), module (the
        first/defining module — set ON CREATE only so it stays the definer when a
        later module also contributes), profile (ADR-0034 union).
      - (:Module)-[:CONTRIBUTES_TO {entries}]->(:AssetBundle)  one per module that
        lists this bundle in its manifest 'assets' dict; entries = JSON-serialized
        ordered entry list (str path | [op, ...]).
      - (:AssetBundle)-[:INCLUDES_BUNDLE]->(:AssetBundle)  for each ('include', X)
        composition reference (X may live in any module — MERGE the target so a
        forward reference is never orphaned, mirroring the placeholder convention).

    Single-row lookups only — every MERGE is keyed on (name, odoo_version), so no
    .single() multi-row pattern is reintroduced (the issue WI-A fixed). Reuses the
    ADR-0034 profile-union SSOT for the contributing Module and the AssetBundle.
    """
    import json

    from .writer_neo4j import _profile_union_set

    for c in result.contributions:
        is_private = "._" in c.bundle_name
        # MERGE the AssetBundle node + the contributing Module + CONTRIBUTES_TO edge.
        # module (definer) is set ON CREATE only so the first contributor owns the
        # `module` prop; later contributors still get a CONTRIBUTES_TO edge but do
        # not overwrite the definer.
        tx.run(f"""
            MERGE (b:AssetBundle {{name: $name, odoo_version: $ver}})
            ON CREATE SET b.profile = $profiles, b.module = $module,
                          b.is_private = $is_private
            ON MATCH  SET b.profile =
                {_profile_union_set("b")},
                          b.is_private = $is_private
            WITH b
            MERGE (mod:Module {{name: $module, odoo_version: $ver}})
            ON CREATE SET mod.profile = $profiles
            ON MATCH  SET mod.profile =
                {_profile_union_set("mod")}
            MERGE (mod)-[r:CONTRIBUTES_TO]->(b)
            SET r.entries = $entries
        """, name=c.bundle_name, ver=c.odoo_version, module=c.module,
             is_private=is_private, entries=json.dumps(c.entries),
             profiles=profiles)

        # INCLUDES_BUNDLE edges — ('include', X) composition. MERGE the target so a
        # not-yet-written referenced bundle is created (profile-less, ADR-0034
        # SCOPE-CHOKE: it is a REFERENCED node not owned by this run; a later real
        # write under its own owner converges on the same (name, odoo_version) key).
        for target in c.includes:
            tx.run("""
                MATCH (src:AssetBundle {name: $src_name, odoo_version: $ver})
                MERGE (tgt:AssetBundle {name: $tgt_name, odoo_version: $ver})
                ON CREATE SET tgt.is_private = $tgt_private
                MERGE (src)-[:INCLUDES_BUNDLE]->(tgt)
            """, src_name=c.bundle_name, tgt_name=target, ver=c.odoo_version,
                 tgt_private=("._" in target))


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
