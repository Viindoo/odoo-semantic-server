"""Module-overview helpers (split out of src/mcp/server.py, Phase 7 / A1).

Two module-level read helpers moved verbatim from the server hub:
  - ``_describe_module``    — Layer-0 module overview (manifest + model/view/JS
    counts), surfaced by the ``describe_module`` / ``module_inspect`` tools and
    the ``odoo://{version}/module/{name}`` resource.
  - ``_module_dep_closure`` — transitive ``DEPENDS_ON`` closure + load order,
    surfaced by ``module_inspect(method='dependencies')``.

This is NOT a tool module: it declares no ``@mcp.tool`` and is not part of the
import-time tool-registration side effect.  server.py imports these two helpers
and re-exports them as ``src.mcp.server._describe_module`` /
``_module_dep_closure`` so the existing call sites keep working unchanged:
  - ``src/mcp/inspect.py`` and ``src/mcp/tools/inspect_tools.py`` reach them via
    ``srv._describe_module`` / ``srv._module_dep_closure`` at call time;
  - ``src/mcp/resources.py`` reaches ``_describe_module`` via ``_srv.`` ;
  - tests import them via ``from src.mcp.server import _describe_module``.

The bodies reach the shared resolver/state hub (``_get_driver`` /
``_resolve_version`` / ``_scope`` / ``_scope_pred`` / ``_render_capped`` /
``_portable_path`` / ``_edition_label`` / ``_data_bounded`` / ``_single_bounded``
/ ``_provenance_token``) through the module-level ``_srv`` server reference bound
at the END of this file (see the note there) and ``_srv.<name>`` attribute
lookups performed at call time, so ``monkeypatch.setattr(srv, ...)`` in tests is
still observed (the patch lands on the live server module object and the
attribute is re-read off it on each call).  Peer-module helpers that are NOT hub
state (``format_next_step``, ``_edition_label`` is hub, the ORM helpers, and the
preview-cap constants) are imported directly below.
"""

import sys

from src.constants import (
    LIST_PREVIEW_MAX_ITEMS,
    REL_DEPENDS_ON,
)
from src.mcp.hints import format_next_step


def _describe_module(
    name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    *,
    include_description: bool = False,
    _reraise_timeout: bool = False,
) -> str:
    """Layer-0 module overview: manifest + model/view/JS counts.

    Distinct from check_module_exists (1–3 lines, YES/NO + edition) — this
    tool returns the full architecture tree (~10–15 lines) per ADR-0023 §1.7.
    Runs 1 Module query + 4 aggregate queries (Models defined, Models
    extended, Views by type, JS patches).

    Each query is routed through ``_data_bounded`` / ``_single_bounded`` with
    its OWN sub-step label so a tx-timeout becomes OrmQueryTimeout (clean
    English, no Cypher leaked) and the timeout message names which sub-step
    died. There is no internal catch: the raise propagates so the owning
    describe_module / module_inspect handler (now ``@offload_neo4j``) records the
    metric + returns the clean string (tool path), or the module resource
    handler records + returns it UNCACHED (resource path). ``_reraise_timeout``
    is signature parity with the sibling resolvers (the module resource render
    passes it); nothing here converts the timeout to a string, so both paths
    already propagate identically.
    """
    _ = _reraise_timeout  # parity-only flag; the timeout always propagates here.
    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        # Issue #121 (extended): `description` can be very long (up to ~9k chars),
        # so it is opt-in - only SELECTed (and only rendered) when the caller asks
        # for it via include_description=True. The default keeps the overview lean
        # and avoids pulling the big field off Neo4j at all.
        description_select = (
            ",\n                   m.description AS description"
            if include_description else ""
        )
        mod_rec = _srv._single_bounded(
            session,
            """
            MATCH (m:Module {name: $n, odoo_version: $v})
            WHERE ($own IS NULL OR (size(m.profile) > 0
                   AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN m.repo AS repo, m.path AS path, m.version_raw AS version_raw,
                   m.edition AS edition,
                   m.license AS license,
                   m.viindoo_equivalent_qname AS vvq,
                   m.license_notice AS license_notice,
                   m.repo_url AS repo_url,
                   m.auto_install AS auto_install,
                   m.application AS application,
                   m.category AS category,
                   m.summary AS summary,
                   m.shortdesc AS shortdesc,
                   m.author AS author,
                   m.website AS website,
                   m.price AS price,
                   m.currency AS currency,
                   m.old_technical_name AS old_technical_name,
                   m.external_python AS external_python,
                   m.external_bin AS external_bin"""
            + description_select
            + """
            """,
            f"module manifest for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_srv._scope(profile_name),
        )

        if not mod_rec:
            return (
                f"No module named '{name}' indexed for Odoo {odoo_version}."
            )

        # depends-list is intentionally NOT tenant-scoped (no _srv._scope_pred("d")).
        # It returns only d.name — dependency names from THIS module's own manifest.
        # Safety rests on the scoped `mod_rec` query above, which early-returns if the
        # caller is not entitled to module $n@$v; this is a SEPARATE session.run that
        # re-matches `m` by name+version only (NOT scoped) — fine, because that prior
        # gate already proved entitlement and d.name is just a name the caller's own
        # manifest declared. Contrast _module_dep_closure below, which filters `dep`
        # because it returns dependency node CONTENT (dep.repo / dep.repo_url).
        # Filtering names here would only hide a dep the tenant itself declared when
        # its name collides with another tenant's private module (ADR-0034 A3) — no
        # confidentiality gain, real UX loss.
        depends = _srv._data_bounded(
            session,
            f"""
            MATCH (m:Module {{name: $n, odoo_version: $v}})
                  -[:{REL_DEPENDS_ON}]->(d:Module)
            RETURN d.name AS name
            ORDER BY d.name ASC
            """,
            f"dependencies for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version,
        )

        defines = _srv._data_bounded(
            session,
            """
            MATCH (model:Model {module: $n, odoo_version: $v})
            WHERE coalesce(model.is_definition, false) = true
              AND model.module <> '__unresolved__'
              AND ($own IS NULL OR (size(model.profile) > 0
                   AND all(__p IN model.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN model.name AS name
            ORDER BY model.name ASC
            """,
            f"models defined in '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_srv._scope(profile_name),
        )

        extends = _srv._data_bounded(
            session,
            """
            MATCH (model:Model {module: $n, odoo_version: $v})
            WHERE coalesce(model.is_definition, false) = false
              AND model.module <> '__unresolved__'
              AND ($own IS NULL OR (size(model.profile) > 0
                   AND all(__p IN model.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN model.name AS name
            ORDER BY model.name ASC
            """,
            f"models extended in '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_srv._scope(profile_name),
        )

        view_breakdown = _srv._data_bounded(
            session,
            """
            MATCH (view:View {module: $n, odoo_version: $v})
            WHERE ($own IS NULL OR (size(view.profile) > 0
                   AND all(__p IN view.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN view.type AS type, count(view) AS c
            ORDER BY c DESC, type ASC
            """,
            f"view breakdown for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_srv._scope(profile_name),
        )

        js_rec = _srv._single_bounded(
            session,
            """
            MATCH (j:JSPatch {module: $n, odoo_version: $v})
            WHERE ($own IS NULL OR (size(j.profile) > 0
                   AND all(__p IN j.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN count(j) AS c
            """,
            f"JS patch count for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_srv._scope(profile_name),
        )
        js_count = js_rec["c"] if js_rec else 0

    lines = [f"{name} (Odoo {odoo_version})"]

    # Issue #121 P2 - the human-authored display name IS the module's identity
    # (e.g. "E-Invoice - Misa meInvoice Integrator"), so surface it at the very
    # top, right under the technical-name header. Rendered only when non-NULL
    # (graceful degrade before the --full backfill).
    if mod_rec.get("shortdesc"):
        lines.append(f"├─ Display name: {mod_rec['shortdesc']}")

    # B1/ADR-0037: render repo identity + repo-relative path so agents can locate
    # the module in their OWN checkout. Prefer the portable git URL (Repo URL);
    # the server checkout dirname (Repo:) is host-specific, shown only as a
    # fallback when no URL is known — never both (it would be redundant noise).
    if mod_rec.get("repo_url"):
        lines.append(f"├─ Repo URL: {mod_rec['repo_url']}")
    elif mod_rec.get("repo"):
        lines.append(f"├─ Repo: {mod_rec['repo']}")
    if mod_rec.get("path"):
        # Anchor strip on the dirname (mod_rec['repo']) for legacy absolute rows;
        # post-reindex mod_rec['path'] is already relative → idempotent no-op.
        _rel_path = _srv._portable_path(
            mod_rec["path"], repo=mod_rec.get("repo"), module=name
        )
        lines.append(f"├─ Path: {_rel_path}")

    # B2: render auto_install / application flags (only when True — not noise).
    if mod_rec.get("auto_install"):
        lines.append("├─ Auto-install: yes")
    if mod_rec.get("application"):
        lines.append("├─ Application: yes")

    # B2: render category when present.
    if mod_rec.get("category"):
        lines.append(f"├─ Category: {mod_rec['category']}")

    # B2: render external deps (python + bin) when non-empty.
    ext_py = mod_rec.get("external_python") or []
    ext_bin = mod_rec.get("external_bin") or []
    if ext_py or ext_bin:
        parts = []
        if ext_py:
            parts.append("python: " + ", ".join(ext_py))
        if ext_bin:
            parts.append("bin: " + ", ".join(ext_bin))
        lines.append("├─ External deps: " + "; ".join(parts))

    # ADR-0036: surface license_notice as a visible marker (D3 — never silent).
    # Only emitted when non-null (i.e. module is ingest_flagged; skip action
    # means the module never reaches here at all).
    if mod_rec.get("license_notice"):
        lines.append(f"├─ License notice: {mod_rec['license_notice']}")

    # Manifest sub-tree (non-last parent → "│   " sublist indent).
    lines.append("├─ Manifest:")
    manifest_rows: list[tuple[str, str]] = []
    if depends:
        # Inline list with cap + escape-hatch hint when truncated (G6).
        dep_names = ", ".join(d["name"] for d in depends[:LIST_PREVIEW_MAX_ITEMS])
        if len(depends) > LIST_PREVIEW_MAX_ITEMS:
            dep_names += (
                f", ... and {len(depends) - LIST_PREVIEW_MAX_ITEMS} more"
                f" (use module_inspect(name='{name}', method='dependencies'"
                f", odoo_version='{odoo_version}') for full list)"
            )
        manifest_rows.append(("Depends", dep_names))
    else:
        manifest_rows.append(("Depends", "—"))
    # WG-5 T1: human-readable edition label derived from license (preferred) or edition enum.
    edition_str = _srv._edition_label(mod_rec.get("edition"), mod_rec.get("license"))
    if mod_rec.get("vvq"):
        edition_str += f" (Viindoo equivalent: {mod_rec['vvq']})"
    manifest_rows.append(("Edition", edition_str))
    # Issue #121 P2 - raw manifest author (identity signal), only when non-NULL.
    if mod_rec.get("author"):
        manifest_rows.append(("Author", mod_rec["author"]))
    manifest_rows.append(("Version", mod_rec.get("version_raw") or "—"))
    if mod_rec.get("summary"):
        manifest_rows.append(("Summary", mod_rec["summary"]))
    # Issue #121 (extended) - extra manifest metadata, each only when non-NULL.
    if mod_rec.get("website"):
        manifest_rows.append(("Website", mod_rec["website"]))
    if mod_rec.get("old_technical_name"):
        manifest_rows.append(("Old technical name", mod_rec["old_technical_name"]))
    # Price is a paid/free signal: render even when 0.0 (a priced-but-free
    # marketplace module), so test `is not None`, not truthiness.
    if mod_rec.get("price") is not None:
        _cur = mod_rec.get("currency")
        _price_str = f"{mod_rec['price']} {_cur}" if _cur else f"{mod_rec['price']}"
        manifest_rows.append(("Price", _price_str))
    last_m = len(manifest_rows) - 1
    for i, (label, value) in enumerate(manifest_rows):
        conn = "└─" if i == last_m else "├─"
        lines.append(f"│   {conn} {label}: {value}")

    # Issue #121 (extended) - opt-in full description block (include_description),
    # rendered only when the field is present. Kept OUTSIDE the Manifest sub-tree
    # because it can be long / multi-line (RST), so a one-line sublist row would
    # be unreadable. The raw text is emitted verbatim under a top-level header.
    if include_description and mod_rec.get("description"):
        lines.append("├─ Description (from indexed manifest):")
        for _dl in mod_rec["description"].splitlines():
            # rstrip so a blank RST line renders as a bare "│" (no trailing space).
            lines.append(f"│   {_dl}".rstrip())

    # Defines models — count + capped inline preview.
    def_total = len(defines)
    if def_total > 0:
        def_preview_names = [d["name"] for d in defines[:LIST_PREVIEW_MAX_ITEMS]]
        def_preview = ", ".join(def_preview_names)
        if def_total > LIST_PREVIEW_MAX_ITEMS:
            overflow = def_total - LIST_PREVIEW_MAX_ITEMS
            first_def = defines[0]["name"]
            def_preview += (
                f", ... and {overflow} more"
                f" (use model_inspect(model='{first_def}', method='fields',"
                f" odoo_version='{odoo_version}'))"
            )
        lines.append(f"├─ Defines models: {def_total} ({def_preview})")
    else:
        lines.append("├─ Defines models: 0")

    # Extends models — count + capped inline preview.
    ext_total = len(extends)
    if ext_total > 0:
        ext_preview_names = [e["name"] for e in extends[:LIST_PREVIEW_MAX_ITEMS]]
        ext_preview = ", ".join(ext_preview_names)
        if ext_total > LIST_PREVIEW_MAX_ITEMS:
            overflow = ext_total - LIST_PREVIEW_MAX_ITEMS
            first_ext = extends[0]["name"]
            ext_preview += (
                f", ... and {overflow} more"
                f" (use model_inspect(model='{first_ext}', method='fields',"
                f" odoo_version='{odoo_version}'))"
            )
        lines.append(f"├─ Extends models: {ext_total} ({ext_preview})")
    else:
        lines.append("├─ Extends models: 0")

    # Views — total + by-type breakdown.
    view_total = sum(row["c"] for row in view_breakdown)
    if view_total > 0:
        breakdown_str = ", ".join(
            f"{row['c']} {row['type'] or 'unknown'}" for row in view_breakdown
        )
        lines.append(f"├─ Views: {view_total} ({breakdown_str})")
    else:
        lines.append("├─ Views: 0")

    # JS patches — last data branch. Marked ├─ so Wave 5 can append Next: footer.
    lines.append(f"├─ JS patches: {js_count}")

    # Wave 5: Next-step footer per ADR-0023 §4. Prefer the first defined model
    # (drill into its fields/views); fall back to extends if no defined model.
    # NOTE: cannot suggest check_module_exists (regression per §4.2 alignment).
    first_target = None
    if defines:
        first_target = defines[0]["name"]
    elif extends:
        first_target = extends[0]["name"]
    if first_target:
        next_hints = [
            f"model_inspect(model='{first_target}', method='fields', odoo_version='{odoo_version}')"
            " for declared fields",
            f"model_inspect(model='{first_target}', method='views', odoo_version='{odoo_version}')"
            " for module views",
        ]
    else:
        # No models defined or extended — skip footer entirely (no useful drill-down).
        next_hints = []
    if footer := format_next_step(next_hints):
        lines.append(footer)

    return "\n".join(lines)


def _module_dep_closure(
    name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Transitive DEPENDS_ON closure for a module — returns all dependencies + load order.

    Traverses (:Module)-[:DEPENDS_ON*]->(:Module) up to depth 20 to collect
    the full transitive closure.  Then computes a topological load order using
    path-length as a proxy (shorter path = loaded earlier) with alphabetical
    tiebreak for determinism.  Each dependency line shows [repo] name (repo_url).

    B2: This is surfaced as module_inspect(method='dependencies') per ADR-0028
    consolidation — no new top-level tool.
    """
    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        # Verify the module exists first.
        exists_rec = _srv._single_bounded(
            session,
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            f"WHERE {_srv._scope_pred('m')} "
            "RETURN count(m) AS c",
            f"module existence for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_srv._scope(profile_name),
        )
        exists = exists_rec["c"] if exists_rec else 0
        if not exists:
            return f"No module named '{name}' indexed for Odoo {odoo_version}."

        # Collect full transitive closure with min path-length (Dijkstra-style)
        # and repo/repo_url for each dependency.
        # PATHS(p) gives the variable-length path; length(p) = hop count.
        # §2.4: this is a VLP (`DEPENDS_ON*1..20`) — the #273-class risk. We only
        # BOUND it here (the 30s per-query timeout is the load-bearing protection);
        # a per-hop name-dedup rewrite is OUT of scope for this hardening wave. If
        # nonorm_query_timeout_total{tool="module_inspect"} spikes on this path,
        # escalate to the per-hop rewrite. `DEPENDS_ON` is a manifest-dependency
        # DAG (far less dense than the same-name INHERITS mesh), so the depth-20
        # cap + 30s bound make it safe now.
        dep_rows = _srv._data_bounded(
            session,
            f"""
            MATCH path = (:Module {{name: $n, odoo_version: $v}})
                         -[:{REL_DEPENDS_ON}*1..20]->(dep:Module {{odoo_version: $v}})
            WHERE ($own IS NULL OR (size(dep.profile) > 0
                   AND all(__p IN dep.profile WHERE __p IN $own OR __p IN $shared)))
            WITH dep, min(length(path)) AS min_depth
            RETURN dep.name AS dep_name,
                   dep.repo AS repo,
                   dep.repo_url AS repo_url,
                   min_depth
            ORDER BY min_depth DESC, dep.name ASC
            """,
            f"dependency closure for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_srv._scope(profile_name),
        )

    if not dep_rows:
        lines = [f"{name} dependency closure (Odoo {odoo_version})"]
        lines.append("├─ No transitive dependencies found.")
        lines.append(format_next_step([
            f"describe_module(name='{name}', odoo_version='{odoo_version}')"
            " for full module overview",
        ]))
        return "\n".join(lines)

    # Build load order: sort by (min_depth DESC, name ASC) — already ordered by Cypher.
    # Odoo loads deepest transitive dependencies FIRST (e.g. 'base' before 'sale').
    # index 1 = first to be installed / loaded; deepest deps have highest min_depth.
    lines = [f"{name} dependency closure (Odoo {odoo_version})"]
    lines.append(f"├─ Transitive dependencies ({len(dep_rows)}) — load order:")
    last_idx = len(dep_rows) - 1
    for i, row in enumerate(dep_rows):
        connector = "└─" if i == last_idx else "├─"
        repo_str = f"[{row['repo']}] " if row.get("repo") else ""
        url_str = f"  ({row['repo_url']})" if row.get("repo_url") else ""
        lines.append(
            f"│   {connector} {i + 1:>2}. {repo_str}{row['dep_name']}{url_str}"
        )
    lines.append(format_next_step([
        f"describe_module(name='{name}', odoo_version='{odoo_version}')"
        " for full module overview",
        f"module_inspect(name='{name}', method='summary', odoo_version='{odoo_version}')"
        " for manifest detail",
    ]))
    return "\n".join(lines)


# Bind the owning server module generation AFTER the helper functions are defined.
# sys.modules['src.mcp.server'] at THIS point is the generation that is importing
# this module (server.py imports this module from near the end of its own body).
# Binding at end-of-module — rather than via a top-level ``from src.mcp import
# server``, which reads the stale ``src.mcp`` package attribute after a
# pop+reimport — makes ``_srv`` track the SAME generation that imported this
# module.  That restores the pre-refactor bare-name behaviour: a test holding a
# stale top-level ``srv`` binding (after sys.modules.pop('src.mcp.server') +
# reimport) calls into these helpers via that stale generation's re-export, whose
# ``_srv`` points back at the same stale generation, so monkeypatch.setattr(srv,
# ...) still takes effect.  The bodies read the hub through ``_srv.<name>`` at
# call time so those patches are observed.
#
# ``.get`` (not ``[...]``) so a cold ``import src.mcp.describe`` in a fresh
# interpreter (server not loaded) binds ``None`` instead of raising KeyError.
# In production this module is only ever imported via server.py's end-of-body
# pop+reimport, at which point ``src.mcp.server`` IS in sys.modules, so ``.get``
# returns the SAME generation as before — the helper bodies that dereference
# ``_srv`` only run through that path, never under a bare cold import.
_srv = sys.modules.get("src.mcp.server")

