"""Module-overview helpers (split out of src/mcp/server.py, Phase 7 / A1).

Two module-level read helpers moved verbatim from the server hub:
  - ``_describe_module``    — Layer-0 module overview (manifest + model/view/JS
    counts), surfaced by the ``describe_module`` / ``module_inspect`` tools and
    the ``odoo://{version}/module/{name}`` resource.
  - ``_module_dep_closure`` — transitive ``DEPENDS_ON`` closure + load order,
    surfaced by ``module_inspect(method='dependencies')``, plus the modules an
    install pulls in through ``auto_install`` (``_install_set``, #380; also
    rendered by ``impact_analysis`` via ``_impact_auto_install_line``).

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

import logging
import sys
import threading
import time

from src.constants import (
    IMPACT_MODULES_MAX,
    LIST_PREVIEW_MAX_ITEMS,
    REL_DEPENDS_ON,
)
from src.mcp.hints import format_next_step
from src.mcp.install_set import (
    COUNTRY_GATE_MIN_MAJOR,
    AutoInstalled,
    Baseline,
    InstallSet,
    compute_baseline,
    resolve_install_set,
)
from src.mcp.lifecycle_read import lifecycle_lines, one_line, owned_pred, read_module_lifecycle
from src.mcp.orm import OrmQueryTimeout
from src.mcp.tree_builder import render_list_block

logger = logging.getLogger(__name__)


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
    extended, Views by type, JS patches). The Module must be owned by a
    profile visible to the caller (dependency stubs are not modules); when it
    is not, the NO branch renders the lifecycle block (ledger history, reverse
    old_technical_name lookup, other indexed versions) instead, and both
    branches always end with a ``Next:`` footer.

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
            f"""
            MATCH (m:Module {{name: $n, odoo_version: $v}})
            WHERE {owned_pred(_srv, "m")}
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
            lifecycle = read_module_lifecycle(
                _srv, session, name, odoo_version, profile_name,
            )
            return _format_not_indexed(name, odoo_version, lifecycle)

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
    if shortdesc := one_line(mod_rec.get("shortdesc")):
        lines.append(f"├─ Display name: {shortdesc}")

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
    if author := one_line(mod_rec.get("author")):
        manifest_rows.append(("Author", author))
    manifest_rows.append(("Version", mod_rec.get("version_raw") or "—"))
    if summary := one_line(mod_rec.get("summary")):
        manifest_rows.append(("Summary", summary))
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
        # No model to drill into: the dependency closure is the next useful read,
        # so the footer is always emitted (ADR-0023 §4.3).
        next_hints = [
            f"module_inspect(name='{name}', method='dependencies',"
            f" odoo_version='{odoo_version}') for the dependency closure",
        ]
    lines.append(format_next_step(next_hints))

    return "\n".join(lines)


def _format_not_indexed(name: str, odoo_version: str, lifecycle) -> str:
    """describe_module NO branch: the lifecycle block, always closed by a Next footer.

    The footer never suggests check_module_exists (ADR-0023 §4.2): a known
    successor -> describe it; else the newest other version where the name is
    indexed; else list_available_profiles to see the indexed scope.
    """
    lines = [f"No module named '{name}' indexed for Odoo {odoo_version}."]
    lines.extend(lifecycle_lines(lifecycle))
    if lifecycle.successors:
        hints = [
            f"describe_module(name='{s}', odoo_version='{odoo_version}')"
            " for the successor"
            for s in lifecycle.successors
        ]
    elif lifecycle.other_versions:
        other = lifecycle.other_versions[-1][0]
        hints = [
            f"describe_module(name='{name}', odoo_version='{other}')"
            " for the newest version where it is indexed",
        ]
    else:
        hints = ["list_available_profiles() to see the indexed scope"]
    lines.append(format_next_step(hints))
    return "\n".join(lines)


def _module_dep_closure(
    name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Transitive DEPENDS_ON closure for a module — returns all dependencies + load order.

    Walks (:Module)-[:DEPENDS_ON]->(:Module) ONE hop at a time (per-hop
    name-deduped BFS, ADR-0048: NO variable-length `*1..N` path — the dependency
    graph reaches the same Module by many paths, which a VLP would enumerate
    combinatorially). Collects the full transitive closure (depth capped at 20)
    and records each dep's shortest path length as a load-order proxy (shorter
    path = loaded earlier) with alphabetical tiebreak for determinism.  Each
    dependency line shows [repo] name (repo_url).

    Tenant scope (`_scope_pred`, ADR-0034 choke-point) is applied in Cypher to
    every hop's SOURCE module (the walk never passes through a module the caller
    cannot see) and again at the final inclusion step. The whole closure shares
    one ~30s wall-clock budget so a deep chain fails fast like the old bounded
    query; the auto-install part degrades to one line on a timeout.

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

        # Transitive DEPENDS_ON closure via per-hop name-deduped BFS (ADR-0048:
        # NO VLP `*1..N` — the dependency graph reaches the same Module by many
        # distinct paths, which a variable-length match enumerates combinatorially
        # even when the node set is small [the #273-class blow-up the old depth-20
        # + 30s bound only papered over]). We walk ONE hop at a time and dedupe by
        # module name; the FIRST hop a dep is seen at is its shortest path length
        # = the Odoo load-order proxy (BFS reaches shallower deps first). A Python
        # loop (not the fixed-3-hop `_ANCESTOR_TAGGED_PROLOGUE` helper) is used
        # because a dependency closure is variable-depth (cap 20), not shallow.
        #
        # Phase 1 walks only FROM modules the caller can see (`_scope_pred` on the
        # source `m`): a dependency reached only through
        # another tenant's private module is not followed, so the closure is no
        # oracle on an invisible module's own depends. Phase 2 still filters the
        # listed deps (a visible module's dependency on an invisible one is cut
        # there). For an admin (own=None) both filters are no-ops.
        #
        # All hops share ONE wall-clock deadline equal to the per-query timeout,
        # so a deep chain fails fast (~30s total) instead of up to 20 separate
        # 30s hops holding a read slot for minutes.
        deadline = time.monotonic() + _srv.NEO4J_QUERY_TIMEOUT_SECONDS
        closure_label = f"dependency closure for '{name}' (Odoo {odoo_version})"
        min_depth: dict[str, int] = {}  # dep_name -> shortest hop count
        frontier: list[str] = [name]
        for depth in range(1, 21):
            if not frontier:
                break
            if time.monotonic() > deadline:
                raise _srv._nonorm_timeout(closure_label)
            hop_rows = _srv._data_bounded(
                session,
                f"""
                MATCH (m:Module {{odoo_version: $v}})
                      -[:{REL_DEPENDS_ON}]->(dep:Module {{odoo_version: $v}})
                WHERE m.name IN $frontier AND {_srv._scope_pred('m')}
                RETURN DISTINCT dep.name AS dep_name
                """,
                closure_label,
                v=odoo_version, frontier=frontier, **_srv._scope(profile_name),
            )
            next_frontier: list[str] = []
            for row in hop_rows:
                dep_name = row["dep_name"]
                # Skip the root (a cycle pointing back) and anything already seen
                # at a shallower/equal depth — BFS keeps the minimum.
                if dep_name == name or dep_name in min_depth:
                    continue
                min_depth[dep_name] = depth
                next_frontier.append(dep_name)
            frontier = next_frontier

        # Phase 2: scope-filter the closure and fetch repo metadata in ONE bounded
        # Cypher query (ADR-0034 choke-point via `_scope_pred` — never reimplemented
        # in Python). Only deps that pass the tenant filter are surfaced, exactly
        # like the old query's `WHERE <scope on dep>`.
        dep_rows: list[dict] = []
        if min_depth:
            scoped = _srv._data_bounded(
                session,
                f"""
                MATCH (dep:Module {{odoo_version: $v}})
                WHERE dep.name IN $names AND {_srv._scope_pred('dep')}
                RETURN dep.name AS dep_name, dep.repo AS repo, dep.repo_url AS repo_url
                """,
                closure_label,
                v=odoo_version, names=list(min_depth), **_srv._scope(profile_name),
            )
            # Deepest transitive dep first (Odoo loads it earliest), alphabetical
            # tiebreak for determinism — same order as the old Cypher ORDER BY.
            dep_rows = [
                {"dep_name": r["dep_name"], "repo": r.get("repo"),
                 "repo_url": r.get("repo_url"), "min_depth": min_depth[r["dep_name"]]}
                for r in scoped
            ]
            dep_rows.sort(key=lambda r: (-r["min_depth"], r["dep_name"]))

        # #380: what installing this module pulls in through auto_install.
        # Informational: a timeout here degrades to one line and keeps the
        # closure already computed above (ADR-0023), instead of replacing the
        # whole answer with the timeout message.
        try:
            install = _install_set(session, [name], odoo_version, profile_name,
                                   closure_label, deadline=deadline)
        except OrmQueryTimeout:
            _install_set_timed_out("module_inspect", name, odoo_version)
            install = None

    lines = [f"{name} dependency closure (Odoo {odoo_version})"]
    if not dep_rows:
        lines.append("├─ No transitive dependencies found.")
    else:
        # Render the load order (dep_rows already sorted above: min_depth DESC,
        # name ASC). Odoo loads deepest transitive dependencies FIRST (e.g. 'base'
        # before 'sale'); index 1 = first to be installed / loaded.
        lines.append(f"├─ Transitive dependencies ({len(dep_rows)}) - load order:")
        last_idx = len(dep_rows) - 1
        for i, row in enumerate(dep_rows):
            connector = "└─" if i == last_idx else "├─"
            repo_str = f"[{row['repo']}] " if row.get("repo") else ""
            url_str = f"  ({row['repo_url']})" if row.get("repo_url") else ""
            lines.append(
                f"│   {connector} {i + 1:>2}. {repo_str}{row['dep_name']}{url_str}"
            )
    lines.extend(_auto_install_branch(install, name))
    hints = [
        f"describe_module(name='{name}', odoo_version='{odoo_version}')"
        " for full module overview",
    ]
    if dep_rows:
        hints.append(
            f"module_inspect(name='{name}', method='summary', odoo_version='{odoo_version}')"
            " for manifest detail",
        )
    lines.append(format_next_step(hints))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Install set: depends closure + auto_install fixpoint (#380)
# ---------------------------------------------------------------------------

# Baseline cache: the fresh-database fixpoint from `base` does
# not depend on the request, only on the version and the caller's scope, so it
# is reused for a short time. The key is the exact scope the reads ran under
# (ADR-0034: a tenant never reuses another scope's result). Bounded in size and
# age; a stale entry is at most _BASELINE_TTL_SECONDS old (an index run that
# changes `base`'s triggers shows up after that).
_BASELINE_TTL_SECONDS = 120.0
_BASELINE_CACHE_MAX = 64
_baseline_cache: dict[tuple, tuple[float, Baseline]] = {}
_baseline_lock = threading.Lock()


def _scope_key(scope: dict) -> tuple:
    own = scope.get("own")
    return (
        None if own is None else tuple(sorted(own)),
        tuple(sorted(scope.get("shared") or ())),
    )


def _cached_baseline(key: tuple, compute) -> Baseline:
    now = time.monotonic()
    with _baseline_lock:
        hit = _baseline_cache.get(key)
        if hit is not None and now - hit[0] < _BASELINE_TTL_SECONDS:
            return hit[1]
    value = compute()
    with _baseline_lock:
        if len(_baseline_cache) >= _BASELINE_CACHE_MAX:
            # Drop expired entries first, then the oldest one.
            for k in [k for k, (t, _) in _baseline_cache.items()
                      if now - t >= _BASELINE_TTL_SECONDS]:
                _baseline_cache.pop(k, None)
            while len(_baseline_cache) >= _BASELINE_CACHE_MAX:
                oldest = min(_baseline_cache, key=lambda k: _baseline_cache[k][0])
                _baseline_cache.pop(oldest, None)
        _baseline_cache[key] = (now, value)
    return value


def clear_install_baseline_cache() -> None:
    """Drop every cached baseline (tests; after a reindex if needed)."""
    with _baseline_lock:
        _baseline_cache.clear()


def _odoo_major(odoo_version: str) -> int | None:
    try:
        return int(str(odoo_version).split(".")[0])
    except (ValueError, IndexError):
        return None


def _multi_profile_caveat(scope: dict, profile_name: str | None) -> str | None:
    """Caveat when the install set mixes several profiles.

    Odoo installs from ONE addons path; OSM resolves over every module the
    caller can see. With no profile filter (explicit or session pin) and a scope
    of more than one profile, the result assumes they all share one addons path
    (an Enterprise or Viindoo auto-install module can appear for a CE-only
    reader). A profile filter narrows to that profile and its parents, which is
    the addons path the reader asked for, so no caveat is shown then.
    """
    if profile_name or _srv._resolve_profile(None):
        return None
    own = scope.get("own")
    if own is None:
        span = "every indexed profile"
    else:
        count = len(set(own) | set(scope.get("shared") or ()))
        if count <= 1:
            return None
        span = f"{count} profiles"
    return (
        f"assumes every module of this scope ({span}) is on one addons path;"
        " pass profile_name= to resolve against one profile and its parents"
    )


def _install_set(
    session,
    roots: list[str],
    odoo_version: str,
    profile_name: str | None,
    label: str,
    *,
    deadline: float | None = None,
) -> InstallSet:
    """Resolve what installing ``roots`` installs, against the indexed graph.

    Binds ``install_set.resolve_install_set`` to two bounded reads, each ONE
    hop (ADR-0048, no variable-length path): the direct ``DEPENDS_ON`` of a
    frontier of names, and the auto-install candidates of a round. A module is
    available only when it is owned by a profile and visible to the caller
    (``owned_pred`` = the ADR-0034 choke-point plus the "not a dependency stub"
    guard), so a candidate outside the caller's scope is never added and never
    named. The dependencies of an unavailable module are not returned either
    Nothing is reached THROUGH a module the caller cannot see.
    Triggers come from ``Module.auto_install_triggers``; a node indexed before
    that property existed (``auto_install = true`` and no triggers) falls back
    to its ``DEPENDS_ON`` targets, exact for the boolean form. From
    :data:`COUNTRY_GATE_MIN_MAJOR` the candidates carry ``Module.countries``
    (Odoo's v18+ country gate, see ``src.mcp.install_set``).

    All reads share ``deadline`` (default: one per-query timeout from now); the
    fresh-database baseline is cached per (version, scope) for
    ``_BASELINE_TTL_SECONDS``. Raises ``OrmQueryTimeout``; callers render a
    degraded line.
    """
    if deadline is None:
        deadline = time.monotonic() + _srv.NEO4J_QUERY_TIMEOUT_SECONDS
    scope = _srv._scope(profile_name)
    available = owned_pred(_srv, "m")
    major = _odoo_major(odoo_version)
    country_gate = major is None or major >= COUNTRY_GATE_MIN_MAJOR

    def _check_deadline() -> None:
        if time.monotonic() > deadline:
            raise _srv._nonorm_timeout(label)

    def depends_of(names: list[str]) -> dict[str, tuple[bool, list[str]]]:
        _check_deadline()
        rows = _srv._data_bounded(
            session,
            f"""
            UNWIND $names AS n
            OPTIONAL MATCH (m:Module {{name: n, odoo_version: $v}})
            OPTIONAL MATCH (m)-[:{REL_DEPENDS_ON}]->(d:Module {{odoo_version: $v}})
            WITH n, m, collect(DISTINCT d.name) AS deps
            WITH n, deps, (m IS NOT NULL AND {available}) AS ok
            RETURN n AS name, ok AS available,
                   CASE WHEN ok THEN deps ELSE [] END AS deps
            ORDER BY name ASC
            """,
            label,
            names=names, v=odoo_version, **scope,
        )
        return {
            r["name"]: (bool(r["available"]), sorted(r["deps"] or []))
            for r in rows
        }

    def candidates(installed: frozenset[str], excluded: frozenset[str]):
        _check_deadline()
        rows = _srv._data_bounded(
            session,
            f"""
            MATCH (m:Module {{odoo_version: $v}})
            WHERE (m.auto_install_triggers IS NOT NULL OR m.auto_install = true)
              AND NOT m.name IN $installed AND NOT m.name IN $excluded
              AND {available}
            OPTIONAL MATCH (m)-[:{REL_DEPENDS_ON}]->(d:Module {{odoo_version: $v}})
            WITH m, collect(DISTINCT d.name) AS deps
            WITH m.name AS name, coalesce(m.auto_install_triggers, deps) AS triggers,
                 coalesce(m.countries, []) AS countries
            WHERE all(t IN triggers WHERE t IN $installed)
            RETURN name, triggers, countries
            ORDER BY name ASC
            """,
            label,
            v=odoo_version, installed=sorted(installed), excluded=sorted(excluded),
            **scope,
        )
        return [
            (r["name"], list(r["triggers"] or []), list(r.get("countries") or []))
            for r in rows
        ]

    baseline = _cached_baseline(
        (odoo_version, *_scope_key(scope)),
        lambda: compute_baseline(depends_of, candidates),
    )
    result = resolve_install_set(roots, depends_of, candidates,
                                 country_gate=country_gate, baseline=baseline)
    result.caveat = _multi_profile_caveat(scope, profile_name)
    return result


def _install_set_timed_out(tool: str, subject: str, odoo_version: str) -> None:
    """Record a degraded auto-install part (metric + log + degraded marker)."""
    _srv._metric_nonorm_query_timeout(tool)
    logger.warning("%s: auto-install set timed out for %r (Odoo %s); rendered degraded",
                   tool, subject, odoo_version)
    from src.mcp.degraded import mark_degraded  # noqa: PLC0415 - leaf import
    mark_degraded(f"{tool} auto-install set timed out")


def _auto_install_label(entry: AutoInstalled) -> str:
    if entry.required_by:
        return f"{entry.name} (dependency of {entry.required_by})"
    if entry.triggers:
        return f"{entry.name} (triggered by: {', '.join(entry.triggers)})"
    return f"{entry.name} (installed with every new database)"


def _country_conditional_text(install: InstallSet, cap: int) -> str:
    items = [
        f"{mod} ({', '.join(c.upper() for c in countries)})"
        for mod, countries in sorted(install.country_conditional.items())
    ]
    text = ", ".join(items[:cap])
    if len(items) > cap:
        text += f", ... and {len(items) - cap} more"
    return text


def _auto_install_branch(install: InstallSet | None, name: str) -> list[str]:
    """module_inspect(dependencies) branch: what auto_install adds (#380).

    The list is capped at ``LIST_PREVIEW_MAX_ITEMS`` (ADR-0023 section 3) with
    an "... and N more" row. ``install=None`` is the timed-out case: one
    degraded line, the rest of the answer is kept.
    """
    if install is None:
        return [
            "├─ Auto-installed with this module: not computed (the query timed out;"
            " retry later, or narrow with profile_name=)"
        ]
    lines: list[str] = []
    if install.auto:
        lines.append(f"├─ Auto-installed with this module ({len(install.auto)}):")
        rows = [_auto_install_label(e) for e in install.auto[:LIST_PREVIEW_MAX_ITEMS]]
        if len(install.auto) > LIST_PREVIEW_MAX_ITEMS:
            rows.append(f"... and {len(install.auto) - LIST_PREVIEW_MAX_ITEMS} more")
        if install.truncated:
            rows.append("... stopped at the round limit, the list may be incomplete")
        lines.extend(render_list_block(rows, prefix="│   "))
    elif name in install.baseline:
        lines.append(
            "├─ Auto-installed with this module: none"
            " (it is already installed with every new database)"
        )
    else:
        lines.append("├─ Auto-installed with this module: none")
    if install.country_conditional:
        lines.append(
            "├─ Country-conditional, auto-installed only when a company is in one"
            f" of its countries ({len(install.country_conditional)}): "
            + _country_conditional_text(install, LIST_PREVIEW_MAX_ITEMS)
        )
    if install.unsatisfiable:
        missing = ", ".join(
            f"{mod} (needs {dep})" for mod, dep in sorted(install.unsatisfiable.items())
        )
        lines.append(
            "├─ Not auto-installed, a dependency is not indexed in this scope"
            f" ({len(install.unsatisfiable)}): {missing}"
        )
    if install.caveat:
        lines.append(f"├─ Note: the install set {install.caveat}")
    return lines


def _impact_auto_install_line(
    session,
    model_name: str,
    odoo_version: str,
    profile_name: str | None,
    label: str,
) -> str | None:
    """impact_analysis row: modules auto-installed with the model's definer (#380).

    The roots are the modules that DEFINE the model (``is_definition``) and the
    caller can see; every database that has the model has these modules too.
    Returns None when no defining module is visible. Informational and
    unscored: a timeout renders a degraded row instead of failing the whole
    impact report (ADR-0023).
    """
    try:
        roots = [
            r["name"] for r in _srv._data_bounded(
                session,
                f"""
                MATCH (md:Model {{name: $mn, odoo_version: $v}})-[:DEFINED_IN]->(m:Module)
                WHERE coalesce(md.is_definition, false) = true AND {owned_pred(_srv, "m")}
                RETURN DISTINCT m.name AS name
                ORDER BY name ASC
                """,
                label,
                mn=model_name, v=odoo_version, **_srv._scope(profile_name),
            )
        ]
        if not roots:
            return None
        install = _install_set(session, roots, odoo_version, profile_name, label)
    except OrmQueryTimeout:
        _install_set_timed_out("impact_analysis", model_name, odoo_version)
        return ("├─ Auto-installed with the defining module: not computed"
                " (the query timed out; retry later, or narrow with profile_name=)")
    noun = "module" if len(roots) == 1 else "modules"
    header = f"Auto-installed with the defining {noun} {', '.join(roots)}"
    note = f" (note: {install.caveat})" if install.caveat else ""
    extra = ""
    if install.country_conditional:
        extra = (
            f"; country-conditional ({len(install.country_conditional)}): "
            + _country_conditional_text(install, IMPACT_MODULES_MAX)
        )
    if not install.auto:
        return f"├─ {header}: none{extra}{note}"
    rows = [_auto_install_label(e) for e in install.auto]
    preview = ", ".join(rows[:IMPACT_MODULES_MAX])
    if len(rows) > IMPACT_MODULES_MAX:
        preview += f", ... and {len(rows) - IMPACT_MODULES_MAX} more"
    return f"├─ {header} ({len(rows)}): {preview}{extra}{note}"


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

