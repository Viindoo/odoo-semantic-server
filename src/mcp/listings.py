"""Entity-listing helpers (split out of src/mcp/server.py, Phase 7 / A1).

The nine ``_list_*`` read helpers moved verbatim from the server hub — they back
the ``model_inspect`` / ``module_inspect`` / ``entity_lookup`` / ``describe_module``
discriminator tools (via ``src/mcp/inspect.py`` and
``src/mcp/tools/inspect_tools.py``) and the ``odoo://`` resource renders:

  ``_list_fields`` / ``_list_methods`` / ``_list_extenders`` /
  ``_list_views_core`` / ``_list_views`` / ``_list_views_by_module`` /
  ``_list_owl_components`` / ``_list_qweb_templates`` / ``_list_js_patches``

plus the cluster-private ``_JS_ERA_MAP`` constant (consumed only by
``_list_js_patches``).

This is NOT a tool module: it declares no ``@mcp.tool`` and is not part of the
import-time tool-registration side effect.  server.py imports these helpers and
re-exports them as ``src.mcp.server._list_*`` so the existing call sites keep
working unchanged (``src/mcp/inspect.py`` reaches them via ``srv._list_*`` at
call time; tests import several — e.g. ``_list_fields`` / ``_list_methods`` —
via ``from src.mcp.server import ...``).

The bodies reach the shared resolver/state hub (``_get_driver`` /
``_resolve_version`` / ``_scope`` / ``_scope_pred`` / ``_render_capped`` /
``_portable_path`` / ``_data_bounded`` / ``_single_bounded`` /
``_provenance_token``) through the module-level ``_srv`` server reference bound
at the END of this file (see the note there) and ``_srv.<name>`` attribute
lookups performed at call time, so ``monkeypatch.setattr(srv, ...)`` in tests is
still observed.  Peer-module helpers that are NOT hub state are imported directly
below: the preview-cap / magic-field constants from ``src.constants``; the
INHERITS-aware ORM helpers + ``OrmQueryTimeout`` + ``_edition_rank_cypher`` from
``src.mcp.orm``; ``format_next_step``; ``mint_refs``; ``render_list_block``.
Intra-module calls (``_list_views`` / ``_list_views_by_module`` ->
``_list_views_core``) stay bare names.
"""

import sys

from src.constants import (
    LIST_PREVIEW_FIELDS_MAX,
    LIST_PREVIEW_MAX_ITEMS,
    LIST_PREVIEW_PATCHES_MAX,
    MAGIC_FIELDS,
    REL_DEPENDS_ON,
)
from src.mcp.hints import format_next_step
from src.mcp.orm import (
    OrmQueryTimeout,
    _edition_rank_cypher,
)
from src.mcp.orm_queries import (
    _ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY,
    _ancestor_owner_names,
    _count_fields_with_inherited,
    _count_methods_with_inherited,
    _list_fields_with_inherited,
    _list_methods_with_inherited,
)
from src.mcp.refs import mint_refs
from src.mcp.tree_builder import render_list_block

# Sentinel api_key_id for direct _impl calls (tests, CLI) — refs are scoped to
# this namespace and do not collide with production tenant refs.  Used as a
# default-argument value (evaluated at def-time, so it cannot route through the
# end-of-module ``_srv`` bind).  Defined identically in server.py / inspect.py;
# this is a plain string sentinel, not hub state, so the local copy is correct.
_ANONYMOUS_API_KEY_ID = "anonymous"


def _list_fields(
    model: str,
    odoo_version: str = "auto",
    module: str | None = None,
    kind: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-2 — enumerate fields on a model, grouped by module.

    `kind` filters by Field.ttype (e.g. 'monetary', 'many2one').
    `module` restricts to one declaring module.  When ``module`` is set,
    magic-field synthetic rows are suppressed (module=``"<builtin>"`` would
    not match any real module filter value).
    `limit` caps the Cypher query size; the render cap is LIST_PREVIEW_FIELDS_MAX.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_FIELDS_MAX
    # Fetch at most cap rows via Cypher with SKIP for pagination.
    effective_limit = min(limit, cap)

    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        # INHERITS-aware enumeration: own fields (depth 0) + fields inherited
        # from mixins (depth 1-3 via INHERITS|DELEGATES_TO), deduped by name
        # with the nearest owner winning (child overrides mixin). The dedup +
        # SKIP/LIMIT happen IN-QUERY, so pagination is consistent with the
        # matching DISTINCT-name count below. Provenance fields owner_model /
        # inherit_depth / edge_kind are carried for the `inherited from` /
        # `delegated via` row labels. Bounded by _bounded() (issue #273).
        #
        # FIX (#284 follow-up): these two helpers are bounded + tx-timeout-mapped
        # to OrmQueryTimeout on a dense inheritance graph. The list path catches
        # that HERE and returns the clean degraded English string directly. The
        # @offload_neo4j boundary on model_inspect/entity_lookup (PR-1 #287) only
        # backstops a RAISED OrmQueryTimeout; this inline catch keeps the list
        # path self-contained and emits the timeout metric itself (PR-3 / issue
        # #287 M3, ADR-0050). Mirrors the detail path (_resolve_field): surface
        # the clean string instead of raising.
        try:
            rows = _list_fields_with_inherited(
                model, odoo_version, session, profile_name,
                module=module, kind=kind,
                skip=start_index, limit=effective_limit,
            )

            # Separate count query (same traversal + DISTINCT-name dedup) so the
            # "Showing X of N" total always matches the paginated, deduped rows.
            total = _count_fields_with_inherited(
                model, odoo_version, session, profile_name, module=module, kind=kind
            )
        except OrmQueryTimeout as exc:
            # List path has no _reraise_timeout (always returns a string), so the
            # tool-path timeout is counted here for parity with the resolvers
            # (PR-3 M3, ADR-0050). No resource ever reaches this list path, so
            # there is no double-count risk.
            _srv._metric_nonorm_query_timeout("model_inspect")
            return exc.user_message

    # D2: Build magic-field prelude for page 0 only when no module filter suppresses them.
    # Magic fields are rendered as a FIXED <builtin> prelude block that is OUTSIDE the
    # pagination/truncation logic for real fields.  The "Showing rows X–Y of N" line and
    # all start_index arithmetic operate ONLY on real (Neo4j) fields.
    # Dedup: skip a magic field if the model already declares it in Neo4j anywhere (model-
    # scoped, not page-scoped — fields on page 2+ would not be in `rows` and would cause
    # duplicates for e.g. display_name, write_date that appear late in the field list).
    magic_prelude_rows: list[dict] = []
    if start_index == 0 and module is None:
        magic_names_list = list(MAGIC_FIELDS.keys())
        # Dedup magic names against own AND inherited owners. A mixin can declare
        # a magic-named field (e.g. `display_name`), so the flat own-model check
        # alone would double-show it once the inherited rows surface it in the
        # paginated list. FIX-2 (review #283): reuse the shared bounded owner-set
        # helper (`_ancestor_owner_names`, the SAME _ANCESTOR_TAGGED_PROLOGUE the
        # listing uses) instead of a hand-rolled re-implementation of the 3-hop
        # BFS — one SSOT, both bounded. On a tx-timeout the magic check degrades
        # to the flat own-model dedup (existing_names from the model alone) so a
        # dense graph never crashes the whole list (it can at worst double-show a
        # magic-named field that a mixin also declares — a cosmetic degradation,
        # not a failure).
        existing_names: set[str] = set()
        try:
            with _srv._get_driver().session() as _dedup_session:
                owner_names = _ancestor_owner_names(
                    model, odoo_version, _dedup_session, profile_name
                )
                # RAW-ESCAPE fix: this was a BARE `_dedup_session.run(_bounded(...))`
                # — timeout-bounded but raising a RAW neo4j ClientError, so the
                # `except OrmQueryTimeout` below was BLIND to it (the same bug
                # class as the #286 override_rec fix). Route through
                # `_single_bounded` so a tx-timeout becomes OrmQueryTimeout and the
                # existing degrade-to-flat fallback actually fires.
                _dedup_rec = _srv._single_bounded(
                    _dedup_session,
                    """
                    UNWIND $owners AS owner_model
                    MATCH (f:Field {model: owner_model, odoo_version: $v})
                    WHERE f.name IN $magic_names
                      AND ($own IS NULL OR (size(f.profile) > 0
                           AND all(__p IN f.profile WHERE __p IN $own OR __p IN $shared)))
                      AND f.module <> '__unresolved__'
                    RETURN collect(DISTINCT f.name) AS names
                    """,
                    f"magic-field dedup for '{model}'",
                    owners=owner_names, v=odoo_version,
                    magic_names=magic_names_list, **_srv._scope(profile_name),
                )
            existing_names = set(_dedup_rec["names"]) if _dedup_rec else set()
        except OrmQueryTimeout:
            # Degrade to flat own-model magic dedup — never crash the list.
            try:
                with _srv._get_driver().session() as _flat_session:
                    # RAW-ESCAPE fix: same bare-`_bounded` → `_single_bounded`
                    # conversion so the inner `except OrmQueryTimeout` below
                    # actually catches a tx-timeout on the flat fallback too.
                    _flat_rec = _srv._single_bounded(
                        _flat_session,
                        """
                        MATCH (f:Field {model: $m, odoo_version: $v})
                        WHERE f.name IN $magic_names
                          AND ($own IS NULL OR (size(f.profile) > 0
                               AND all(__p IN f.profile
                                       WHERE __p IN $own OR __p IN $shared)))
                          AND f.module <> '__unresolved__'
                        RETURN collect(DISTINCT f.name) AS names
                        """,
                        f"magic-field flat dedup for '{model}'",
                        m=model, v=odoo_version,
                        magic_names=magic_names_list, **_srv._scope(profile_name),
                    )
                existing_names = set(_flat_rec["names"]) if _flat_rec else set()
            except OrmQueryTimeout:
                existing_names = set()
        magic_prelude_rows = [
            {
                "name": fname,
                "ttype": ttype,
            }
            for fname, (ttype, _comodel) in MAGIC_FIELDS.items()
            if fname not in existing_names
            and (kind is None or kind == ttype)
        ]

    header = f"Fields of {model} (Odoo {odoo_version})"

    # Render the <builtin> prelude block (always shown in full, no refs, not paginated).
    # Group header matches the old "repo=None → '?', module='<builtin>'" format so that
    # existing tests checking ``"<builtin>" in out`` continue to pass.
    lines = [header]
    if magic_prelude_rows:
        lines.append("├─ [?] <builtin>")
        builtin_tagged = [f"{r['name']} : {r['ttype']}" for r in magic_prelude_rows]
        lines.extend(render_list_block(builtin_tagged))

    if total == 0:
        # No real declared fields.
        if magic_prelude_rows:
            # Model has no declared fields but magic fields are present — the builtin block
            # IS the content. ADR-0023 §1.6: "(none)" means "empty IS the answer"; when
            # magic rows exist, the answer is not empty. Do NOT emit "(none)".
            # The builtin block was already appended above. Just add the Next footer.
            next_line = format_next_step([
                f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}')"
                " for behavior",
            ])
            lines.append(next_line)
        else:
            # Truly no fields at all (all filtered out by kind/module/profile, or model unknown).
            # Emit "(none)" sentinel so callers can detect completely empty result.
            lines.append("├─ (none)")
            next_line = format_next_step([
                f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}')"
                " for behavior",
            ])
            lines.append(next_line)
        return "\n".join(lines)

    # Mint opaque refs for real (Neo4j) rows only.
    field_items = [{"field_name": r["name"], "model": model} for r in rows]
    ref_ids = mint_refs(field_items, api_key_id, kind="field")

    # Group rows by (repo, module) preserving order.
    groups: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    order: list[tuple[str, str]] = []
    for r, ref_id in zip(rows, ref_ids):
        key = (r.get("repo") or "?", r.get("module") or "?")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((r, ref_id))

    for key in order:
        repo, mod_name = key
        lines.append(f"├─ [{repo}] {mod_name}")
        sub_items = groups[key]
        # Continuation hint uses start_index (ADR-0023 §5.5 Amendment 2026-05-19).
        # Do NOT suggest raising limit= — the cap is intentional (ADR-0023 §3).
        # The global start_index footer below handles cross-module pagination.
        more_hint = (
            f"model_inspect(model='{model}', method='fields', odoo_version='{odoo_version}',"
            f" start_index={start_index + cap})"
        )
        # Build rendered strings with inline refs.
        raw_rows = [r for r, _ in sub_items]
        def _fmt_field_row(r: dict) -> str:
            # B1: include stored/compute/comodel_name in field row summary.
            # WI-1 (#238): also surface related= / readonly / required so AI
            # clients don't try to set a non-writable field in create()/write().
            parts = [f"{r['name']} : {r['ttype']}"]
            if r.get("compute"):
                parts.append(f"compute={r['compute']}")
            elif not r.get("stored", True):
                # stored=False without compute is unusual but surfaceable.
                parts.append("stored=False")
            if r.get("related"):
                parts.append(f"related={r['related']}")
            if r.get("comodel_name"):
                parts.append(f"-> {r['comodel_name']}")
            # effective_readonly is None on pre-reindex nodes — only flag when
            # explicitly True (graceful degradation, mirrors detail view).
            if r.get("effective_readonly"):
                parts.append("readonly")
            if r.get("required"):
                parts.append("required")
            # Provenance token (ADR-0023 token-additive): tag fields that come
            # from a mixin so the AI client knows they are inherited, not own.
            # Own fields (owner_model == model) get no token — output unchanged.
            # _provenance_token is the SSOT for the wording (FIX-6, review #283).
            token = _srv._provenance_token(
                r.get("owner_model"), model, r.get("edge_kind"), r.get("via_field")
            )
            if token:
                parts.append(token)
            return " | ".join(parts)

        rendered_strs = _srv._render_capped(
            raw_rows,
            _fmt_field_row,
            cap=cap,
            more_hint=more_hint,
        )
        # Inject [ref=fN] prefix for non-hint rows.
        ref_iter = iter([ref_id for _, ref_id in sub_items])
        tagged: list[str] = []
        for row_str in rendered_strs:
            if row_str.startswith("... and "):
                tagged.append(row_str)
            else:
                ref_id = next(ref_iter, None)
                prefix = f"[ref={ref_id}] " if ref_id else ""
                tagged.append(f"{prefix}{row_str}")
        lines.extend(render_list_block(tagged))

    # Pagination hint — counts ONLY real fields (total from Neo4j, not +magic).
    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        # Pagination continuation hint (plain text, NOT <error> tag — ADR-0023
        # §Appendix B item #2: pagination is routine, not failure).
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call model_inspect(model='{model}', method='fields', odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif total > 0 and start_index >= total:
        # start_index past the end (cursor over-run): rows is empty, so the
        # "rows {start+1}-{end}" branch would render an inverted range
        # (e.g. "26-25 of 25"). Disclose the over-run cleanly instead.
        lines.append(
            f"├─ No rows at start_index={start_index} (total={total});"
            f" last row is at index {total - 1}."
        )
    elif start_index > 0:
        # Final page of a paginated sequence — disclose position.
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4. Prefer a real field name for the
    # drill-down hint; fall back to first magic field if no real field on this page.
    first_real_field = rows[0]["name"] if rows else None
    first_hint_field = first_real_field or (
        magic_prelude_rows[0]["name"] if magic_prelude_rows else None
    )
    next_hints: list[str] = []
    if first_hint_field:
        next_hints.append(
            f"model_inspect(model='{model}', method='field', field='{first_hint_field}'"
            f", odoo_version='{odoo_version}') for full chain",
        )
    next_hints.append(
        f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}')"
        " for behavior",
    )
    lines.append(format_next_step(next_hints))
    return "\n".join(lines)


def _list_methods(
    model: str,
    odoo_version: str = "auto",
    module: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-4 — enumerate methods on a model, grouped by module.

    Methods appearing in ≥2 modules for the same model are marked with `(*)`
    per ADR-0023 §5.3 to flag override-points.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_MAX_ITEMS
    # Fetch at most cap rows via Cypher with SKIP for pagination.
    effective_limit = min(limit, cap)

    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        # INHERITS-aware enumeration (symmetric to _list_fields): own methods
        # (depth 0) + methods inherited from mixins (depth 1-3 via INHERITS ONLY
        # — `_inherits` delegation NEVER carries methods, GAP-1; DELEGATES_TO is
        # the field-only path), deduped by name with the nearest owner winning.
        # Dedup + SKIP/LIMIT happen IN-QUERY so pagination matches the
        # DISTINCT-name count below. Carries owner_model for provenance labels
        # (edge_kind is always 'inherits' on the method path).
        # FIX (#284 follow-up): the three acquisitions below are all bounded by
        # the per-query Neo4j timeout. The list path catches a tx-timeout HERE and
        # returns the clean degraded string directly; the @offload_neo4j boundary
        # on model_inspect/entity_lookup (PR-1 #287) only backstops a RAISED
        # OrmQueryTimeout, so this inline catch keeps the list path self-contained
        # and emits the metric itself (PR-3 / issue #287 M4, ADR-0050).
        # Wrap the whole acquisition in `try/except OrmQueryTimeout: return
        # exc.user_message`, mirroring _resolve_field. The override_rec query was a BARE
        # `session.run(_bounded(...)).single()` that raises a RAW neo4j
        # ClientError on timeout (NOT routed through orm.py's ClientError ->
        # OrmQueryTimeout conversion), so it would not even reach this catch —
        # route it through `_single_bounded` (the SAME conversion helper the
        # codebase already uses) so its timeout becomes OrmQueryTimeout too.
        try:
            rows = _list_methods_with_inherited(
                model, odoo_version, session, profile_name,
                module=module, skip=start_index, limit=effective_limit,
            )
            # Map convention_kind → the `kind` key the existing renderer expects.
            for _r in rows:
                _r["kind"] = _r.get("convention_kind")

            total = _count_methods_with_inherited(
                model, odoo_version, session, profile_name, module=module
            )

            # Override-marker (GAP-2): a method is marked (*) when it is declared
            # in >=2 modules ON ITS OWNER MODEL. For an INHERITED method the owner
            # is the mixin, not the child — so counting modules only on {model: $m}
            # would never mark an inherited method even when it is overridden N
            # times on its owner. Compute the override set per (method_name,
            # owner_model) over the SAME INHERITS-only ancestor set the method
            # listing uses (NOT DELEGATES_TO — methods are not delegated, GAP-1),
            # so an inherited method overridden across modules on its owner gets
            # the (*) marker in the child listing. Keyed by (name, owner) so a
            # same-named method on two different owners cannot cross-contaminate
            # the marker. Routed through _single_bounded so a tx-timeout becomes
            # OrmQueryTimeout (not a raw ClientError) and joins the catch below.
            override_rec = _srv._single_bounded(
                session,
                _ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY + """
                MATCH (mth:Method {model: owner_model, odoo_version: $v})
                WHERE """ + _srv._scope_pred("mth") + """
                  AND mth.module <> '__unresolved__'
                WITH mth.name AS name, owner_model,
                     count(DISTINCT mth.module) AS modcount
                WHERE modcount >= 2
                RETURN collect([name, owner_model]) AS overrides
                """,
                f"method override markers (including inherited) for '{model}'"
                f" (Odoo {odoo_version})",
                mn=model, v=odoo_version, **_srv._scope(profile_name),
            )
        except OrmQueryTimeout as exc:
            # List path has no _reraise_timeout (always returns a string), so the
            # tool-path timeout is counted here for parity with the resolvers
            # (PR-3 M4, ADR-0050). No resource ever reaches this list path, so
            # there is no double-count risk.
            _srv._metric_nonorm_query_timeout("model_inspect")
            return exc.user_message
        override_keys = {
            (name, owner) for name, owner in (override_rec["overrides"] or [])
        } if override_rec else set()

    header = f"Methods of {model} (Odoo {odoo_version})"
    if total == 0:
        next_line = format_next_step([
            f"model_inspect(model='{model}', method='fields', odoo_version='{odoo_version}')"
            " for shape",
        ])
        return f"{header}\n├─ (none)\n{next_line}"

    # Mint opaque refs for each returned row (method kind).
    method_items = [{"method_name": r["name"], "model": model} for r in rows]
    ref_ids = mint_refs(method_items, api_key_id, kind="method")

    groups: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    order: list[tuple[str, str]] = []
    for r, ref_id in zip(rows, ref_ids):
        key = (r.get("repo") or "?", r.get("module") or "?")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((r, ref_id))

    lines = [header]
    for key in order:
        repo, mod_name = key
        lines.append(f"├─ [{repo}] {mod_name}")
        sub_indent = "│   "
        sub_items = groups[key]
        more_hint = (
            f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}',"
            f" start_index={start_index + cap})"
        )

        raw_rows = [r for r, _ in sub_items]

        def _fmt_method(r):
            marker = "(*)" if (r["name"], r.get("owner_model") or model) in override_keys else ""
            kind_str = r.get("kind") or "private"
            base = f"{r['name']}{marker} : {kind_str}"
            # Provenance token (ADR-0023 token-additive): tag inherited methods.
            # Methods are inherited via INHERITS only (Python MRO) — _inherits
            # delegation NEVER carries methods (GAP-1), so edge_kind is always
            # 'inherits' here and the token can only read "inherited from".
            # _provenance_token is the SSOT for the wording (FIX-6, review #283).
            token = _srv._provenance_token(
                r.get("owner_model"), model, r.get("edge_kind"), r.get("via_field")
            )
            if token:
                base += f" | {token}"
            return base

        rendered = _srv._render_capped(raw_rows, _fmt_method, cap=cap, more_hint=more_hint)
        # Inject [ref=mN] prefix for non-hint rows.
        ref_iter = iter([ref_id for _, ref_id in sub_items])
        tagged: list[str] = []
        for row_str in rendered:
            if row_str.startswith("... and "):
                tagged.append(row_str)
            else:
                ref_id = next(ref_iter, None)
                prefix = f"[ref={ref_id}] " if ref_id else ""
                tagged.append(f"{prefix}{row_str}")

        last_r = len(tagged) - 1
        for j, row in enumerate(tagged):
            r_conn = "└─" if j == last_r else "├─"
            lines.append(f"{sub_indent}{r_conn} {row}")

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        # Pagination continuation hint (plain text, NOT <error> tag).
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif total > 0 and start_index >= total:
        # start_index past the end (cursor over-run): rows is empty, so the
        # "rows {start+1}-{end}" branch would render an inverted range
        # (e.g. "26-25 of 25"). Disclose the over-run cleanly instead.
        lines.append(
            f"├─ No rows at start_index={start_index} (total={total});"
            f" last row is at index {total - 1}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4.
    first_method = rows[0]["name"] if rows else None
    next_hints: list[str] = []
    if first_method:
        next_hints.append(
            f"model_inspect(model='{model}', method='method', method_name='{first_method}'"
            f", odoo_version='{odoo_version}') for override chain",
        )
        next_hints.append(
            f"find_override_point(model='{model}', method='{first_method}'"
            f", odoo_version='{odoo_version}') for hook spot",
        )
    if footer := format_next_step(next_hints):
        lines.append(footer)
    return "\n".join(lines)


def _list_extenders(
    model: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-5 — list all modules that extend (but do not define) a model.

    Uses the same ranking heuristic as _resolve_model summary but filters to
    extension modules only (NOT coalesce(m.is_definition, false)).
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_MAX_ITEMS
    effective_limit = min(limit, cap)

    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        rows = _srv._data_bounded(
            session,
            f"""
            MATCH (m:Model {{name: $name, odoo_version: $v}})-[:DEFINED_IN]->(mod:Module)
            WHERE NOT coalesce(m.is_definition, false)
              AND ($own IS NULL OR (size(m.profile) > 0
                   AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
            WITH m, mod,
                 COUNT {{
                     (:Field {{model: $name, module: m.module, odoo_version: $v}})
                 }} AS field_count,
                 COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dependents,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN m.module AS module_name, coalesce(mod.repo_url, mod.repo) AS repo
            ORDER BY field_count DESC, dependents DESC, edition_rank ASC, mod_name ASC
            SKIP $skip
            LIMIT $limit
            """,
            f"extenders for '{model}' (Odoo {odoo_version})",
            name=model, v=odoo_version, **_srv._scope(profile_name),
            skip=start_index, limit=effective_limit,
        )

        total_rec = _srv._single_bounded(
            session,
            """
            MATCH (m:Model {name: $name, odoo_version: $v})-[:DEFINED_IN]->(:Module)
            WHERE NOT coalesce(m.is_definition, false)
              AND ($own IS NULL OR (size(m.profile) > 0
                   AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN count(m) AS c
            """,
            f"extender count for '{model}' (Odoo {odoo_version})",
            name=model, v=odoo_version, **_srv._scope(profile_name),
        )
        total = total_rec["c"] if total_rec else 0

    header = f"Extenders of {model} (Odoo {odoo_version})"
    if total == 0:
        next_line = format_next_step([
            f"model_inspect(model='{model}', method='summary', odoo_version='{odoo_version}')"
            " for model overview",
        ])
        return f"{header}\n├─ (none — model not extended or not indexed)\n{next_line}"

    # Mint opaque refs for each extender module.
    ext_items = [{"module_name": r["module_name"], "model": model} for r in rows]
    ref_ids = mint_refs(ext_items, api_key_id, kind="module")

    lines = [header]
    shown = len(rows)
    end_index = start_index + shown

    for (r, ref_id) in zip(rows, ref_ids):
        repo = r.get("repo") or "?"
        mod_name = r.get("module_name") or "?"
        lines.append(f"├─ [ref={ref_id}] [{repo}] {mod_name}")

    if total > end_index:
        next_count = min(cap, total - end_index)
        lines.append(
            f"├─ Showing rows {start_index + 1}-{end_index} of {total}."
            f" Call model_inspect(model='{model}', method='extenders',"
            f" odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {next_count}."
        )
    elif start_index >= total:
        # start_index past the end (cursor over-run): rows is empty, so the
        # "rows {start+1}-{end}" branch would render an inverted range
        # (e.g. "26-25 of 25"). Disclose the over-run cleanly instead.
        lines.append(
            f"├─ No rows at start_index={start_index} (total={total});"
            f" last row is at index {total - 1}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}-{end_index} of {total} (last page)."
        )
    else:
        # Single full page (total <= cap, start_index == 0): still disclose the
        # complete count so the agent knows nothing was truncated (L3).
        lines.append(f"├─ Showing all {total} of {total}.")

    next_hints: list[str] = []
    next_hints.append(
        f"model_inspect(model='{model}', method='summary', odoo_version='{odoo_version}')"
        " for model overview",
    )
    if footer := format_next_step(next_hints):
        lines.append(footer)
    return "\n".join(lines)


def _list_views_core(
    *,
    model: str | None = None,
    module: str | None = None,
    odoo_version: str = "auto",
    view_type: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Shared core for view listing — takes EITHER model OR module filter (not both).

    `view_type` filters by View.type (form/tree/list/kanban/search/...).
    'list' is the v18+ tag alias for 'tree'.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    if (model is None) == (module is None):
        raise ValueError(
            "_list_views_core requires exactly one of model= / module= (not both, not neither)"
        )

    cap = LIST_PREVIEW_MAX_ITEMS
    effective_limit = min(limit, cap)

    is_model_scoped = model is not None

    # T2 — list/tree alias: v17 stores 'tree' in DB while source XML uses <list>;
    # v18 hard-renamed to 'list' in DB.  Treat the two values as interchangeable
    # so that view_type='tree' matches v18 views (DB='list') and vice-versa.
    # Strategy: pass BOTH alias values to Cypher via a $view_types list so the
    # Cypher filter becomes `v.type IN $view_types` — a single predicate handles
    # NULL (no filter), single-value (exact), and alias-pair cases.
    if view_type is None:
        view_types: list[str] | None = None  # pass-through: no type filter
    elif view_type in ("tree", "list"):
        view_types = ["tree", "list"]  # alias pair
    else:
        view_types = [view_type]  # exact match for all other types

    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        scope_noun = f"'{model}'" if is_model_scoped else f"module '{module}'"
        if is_model_scoped:
            rows = _srv._data_bounded(
                session,
                f"""
                MATCH (v:View {{model: $filter_val, odoo_version: $ver}})
                WHERE ($own IS NULL OR (size(v.profile) > 0
                       AND all(__p IN v.profile WHERE __p IN $own OR __p IN $shared)))
                  AND ($view_types IS NULL OR v.type IN $view_types)
                  AND v.module <> '__unresolved__'
                OPTIONAL MATCH (mod:Module {{name: v.module, odoo_version: $ver}})
                WITH v, mod,
                     {_edition_rank_cypher("mod")},
                     mod.name AS mod_name
                RETURN v.xmlid AS xmlid, v.type AS type,
                       v.module AS module, coalesce(mod.repo_url, mod.repo) AS repo,
                       edition_rank, mod_name
                ORDER BY edition_rank ASC, mod_name ASC, v.xmlid ASC
                SKIP $skip
                LIMIT $limit
                """,
                f"view list for {scope_noun} (Odoo {odoo_version})",
                filter_val=model, ver=odoo_version, view_types=view_types,
                **_srv._scope(profile_name), skip=start_index, limit=effective_limit,
            )

            total_rec = _srv._single_bounded(
                session,
                """
                MATCH (v:View {model: $filter_val, odoo_version: $ver})
                WHERE ($own IS NULL OR (size(v.profile) > 0
                       AND all(__p IN v.profile WHERE __p IN $own OR __p IN $shared)))
                  AND ($view_types IS NULL OR v.type IN $view_types)
                  AND v.module <> '__unresolved__'
                RETURN count(v) AS c
                """,
                f"view count for {scope_noun} (Odoo {odoo_version})",
                filter_val=model, ver=odoo_version, view_types=view_types,
                **_srv._scope(profile_name),
            )
        else:
            rows = _srv._data_bounded(
                session,
                f"""
                MATCH (v:View {{module: $filter_val, odoo_version: $ver}})
                WHERE ($own IS NULL OR (size(v.profile) > 0
                       AND all(__p IN v.profile WHERE __p IN $own OR __p IN $shared)))
                  AND ($view_types IS NULL OR v.type IN $view_types)
                  AND v.module <> '__unresolved__'
                OPTIONAL MATCH (mod:Module {{name: v.module, odoo_version: $ver}})
                WITH v, mod,
                     {_edition_rank_cypher("mod")},
                     mod.name AS mod_name
                RETURN v.xmlid AS xmlid, v.type AS type,
                       v.module AS module, coalesce(mod.repo_url, mod.repo) AS repo,
                       edition_rank, mod_name
                ORDER BY edition_rank ASC, mod_name ASC, v.xmlid ASC
                SKIP $skip
                LIMIT $limit
                """,
                f"view list for {scope_noun} (Odoo {odoo_version})",
                filter_val=module, ver=odoo_version, view_types=view_types,
                **_srv._scope(profile_name), skip=start_index, limit=effective_limit,
            )

            total_rec = _srv._single_bounded(
                session,
                """
                MATCH (v:View {module: $filter_val, odoo_version: $ver})
                WHERE ($own IS NULL OR (size(v.profile) > 0
                       AND all(__p IN v.profile WHERE __p IN $own OR __p IN $shared)))
                  AND ($view_types IS NULL OR v.type IN $view_types)
                  AND v.module <> '__unresolved__'
                RETURN count(v) AS c
                """,
                f"view count for {scope_noun} (Odoo {odoo_version})",
                filter_val=module, ver=odoo_version, view_types=view_types,
                **_srv._scope(profile_name),
            )

        total = total_rec["c"] if total_rec else 0

    if is_model_scoped:
        header = f"Views of {model} (Odoo {odoo_version})"
        empty_hint = (
            f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}')"
            " for behavior"
        )
        pager_tool = f"model_inspect(model='{model}', method='views', odoo_version='{odoo_version}'"
    else:
        header = f"Views in module '{module}' (Odoo {odoo_version})"
        empty_hint = (
            f"describe_module(name='{module}', odoo_version='{odoo_version}')"
            " for model fields"
        )
        pager_tool = (
            f"module_inspect(name='{module}', method='views',"
            f" odoo_version='{odoo_version}'"
        )

    if total == 0:
        next_line = format_next_step([empty_hint])
        return f"{header}\n├─ (none)\n{next_line}"

    # Mint opaque refs for each returned row (view kind).
    view_items = [{"xmlid": r["xmlid"]} for r in rows]
    ref_ids = mint_refs(view_items, api_key_id, kind="view")

    groups: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    order: list[tuple[str, str]] = []
    for r, ref_id in zip(rows, ref_ids):
        key = (r.get("repo") or "?", r.get("module") or "?")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((r, ref_id))

    lines = [header]
    for key in order:
        repo, mod_name = key
        lines.append(f"├─ [{repo}] {mod_name}")
        sub_indent = "│   "
        sub_items = groups[key]
        more_hint = (
            f"{pager_tool}, start_index={start_index + cap})"
        )
        raw_rows = [r for r, _ in sub_items]
        rendered = _srv._render_capped(
            raw_rows,
            lambda r: f"{r['xmlid']} : {r.get('type') or 'unknown'}",
            cap=cap,
            more_hint=more_hint,
        )
        # Inject [ref=vN] prefix for non-hint rows.
        ref_iter = iter([ref_id for _, ref_id in sub_items])
        tagged: list[str] = []
        for row_str in rendered:
            if row_str.startswith("... and "):
                tagged.append(row_str)
            else:
                ref_id = next(ref_iter, None)
                prefix = f"[ref={ref_id}] " if ref_id else ""
                tagged.append(f"{prefix}{row_str}")

        last_r = len(tagged) - 1
        for j, row in enumerate(tagged):
            r_conn = "└─" if j == last_r else "├─"
            lines.append(f"{sub_indent}{r_conn} {row}")

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call {pager_tool},"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif total > 0 and start_index >= total:
        # start_index past the end (cursor over-run): rows is empty, so the
        # "rows {start+1}-{end}" branch would render an inverted range
        # (e.g. "26-25 of 25"). Disclose the over-run cleanly instead.
        lines.append(
            f"├─ No rows at start_index={start_index} (total={total});"
            f" last row is at index {total - 1}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4.
    first_xmlid = rows[0]["xmlid"] if rows else None
    next_hints: list[str] = []
    if first_xmlid:
        next_hints.append(
            f"entity_lookup(kind='view', xmlid='{first_xmlid}', odoo_version='{odoo_version}')"
            " for full xpath chain",
        )
    if is_model_scoped:
        next_hints.append(
            f"find_examples(query='{model} view', odoo_version='{odoo_version}')"
            " for inheritance patterns",
        )
    else:
        next_hints.append(
            f"find_examples(query='{module} view', odoo_version='{odoo_version}')"
            " for inheritance patterns",
        )
    lines.append(format_next_step(next_hints))
    return "\n".join(lines)


def _list_views(
    model: str,
    odoo_version: str = "auto",
    view_type: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Facade: model-scoped view listing (existing API — backward-compatible)."""
    return _list_views_core(
        model=model,
        odoo_version=odoo_version,
        view_type=view_type,
        profile_name=profile_name,
        limit=limit,
        start_index=start_index,
        api_key_id=api_key_id,
    )


def _list_views_by_module(
    module: str,
    odoo_version: str = "auto",
    view_type: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Facade: module-scoped view listing (new API for module_inspect router)."""
    return _list_views_core(
        module=module,
        odoo_version=odoo_version,
        view_type=view_type,
        profile_name=profile_name,
        limit=limit,
        start_index=start_index,
        api_key_id=api_key_id,
    )


def _list_owl_components(
    module: str,
    odoo_version: str = "auto",
    bound_model: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-5b — enumerate OWL components declared in a module.

    Era-aware: returns empty + warning for Odoo majors <= 13 (Widget era,
    no OWL components). When `bound_model` filter is set, emits a warning
    footer because parser_js.py:415 bound_model resolution is heuristic.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_MAX_ITEMS
    effective_limit = min(limit, cap)

    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        # Era guard: v8-v13 had Widget, not OWL. Return early with hint.
        try:
            major = int(odoo_version.split(".")[0])
        except (ValueError, AttributeError):
            major = 0
        if major and major <= 13:
            # Wave 5: still emit Next: footer suggesting module_inspect(method='js') for
            # era1 widget extensions (the natural era-aware drill-down).
            next_line = format_next_step([
                f"module_inspect(name='{module}', method='js'"
                f", odoo_version='{odoo_version}') for legacy widget extends",
            ])
            return (
                f"OWL components of {module} (Odoo {odoo_version})\n"
                "├─ (none) — Warning: No OWL components in v8-v13"
                " (Widget era). Use module_inspect(method='js') for legacy"
                " widget extensions.\n"
                + next_line
            )

        rows = _srv._data_bounded(
            session,
            """
            MATCH (c:OWLComp {module: $mod, odoo_version: $v})
            WHERE ($own IS NULL OR (size(c.profile) > 0
                   AND all(__p IN c.profile WHERE __p IN $own OR __p IN $shared)))
              AND ($bound_model IS NULL OR c.bound_model = $bound_model)
              AND c.module <> '__unresolved__'
            RETURN c.name AS name, c.bound_model AS bound_model,
                   c.template AS template
            ORDER BY c.name ASC
            SKIP $skip
            LIMIT $limit
            """,
            f"OWL components in '{module}' (Odoo {odoo_version})",
            mod=module, v=odoo_version, bound_model=bound_model,
            **_srv._scope(profile_name), skip=start_index, limit=effective_limit,
        )

        total_rec = _srv._single_bounded(
            session,
            """
            MATCH (c:OWLComp {module: $mod, odoo_version: $v})
            WHERE ($own IS NULL OR (size(c.profile) > 0
                   AND all(__p IN c.profile WHERE __p IN $own OR __p IN $shared)))
              AND ($bound_model IS NULL OR c.bound_model = $bound_model)
              AND c.module <> '__unresolved__'
            RETURN count(c) AS c
            """,
            f"OWL component count in '{module}' (Odoo {odoo_version})",
            mod=module, v=odoo_version, bound_model=bound_model,
            **_srv._scope(profile_name),
        )
        total = total_rec["c"] if total_rec else 0

    header = f"OWL components of {module} (Odoo {odoo_version})"
    if total == 0:
        lines = [header]
        if bound_model is not None:
            lines.append(
                "├─ Warning: bound_model resolution is heuristic"
                " — may miss components using dynamic this.props.resModel",
            )
        lines.append("├─ (none)")
        # Wave 5: suggest module_inspect qweb / js as siblings.
        lines.append(format_next_step([
            f"module_inspect(name='{module}', method='qweb'"
            f", odoo_version='{odoo_version}') for QWeb templates",
            f"module_inspect(name='{module}', method='js', odoo_version='{odoo_version}')"
            " for related patches",
        ]))
        return "\n".join(lines)

    # Mint opaque refs for each returned row.
    # Use field_name key so _infer_kind detects 'field' (prefix 'f').
    # OWL components have no native kind in PREFIX_BY_KIND; 'field' prefix
    # is acceptable for non-model-entity refs (future wave can add 'owl' kind).
    comp_items = [{"field_name": r["name"], "module": module} for r in rows]
    ref_ids = mint_refs(comp_items, api_key_id, kind="field")

    lines = [header]
    more_hint = (
        f"module_inspect(name='{module}', method='owl'"
        f", odoo_version='{odoo_version}', start_index={start_index + cap})"
    )
    raw_rows = rows
    rendered = _srv._render_capped(
        raw_rows,
        lambda r: (
            f"{r['name']} : {r.get('bound_model') or '(unbound)'}"
            + (f" | template={r['template']}" if r.get("template") else "")
        ),
        cap=cap,
        more_hint=more_hint,
    )
    # Inject [ref=fN] prefix for non-hint rows.
    ref_iter = iter(ref_ids)
    tagged: list[str] = []
    for row_str in rendered:
        if row_str.startswith("... and "):
            tagged.append(row_str)
        else:
            ref_id = next(ref_iter, None)
            prefix = f"[ref={ref_id}] " if ref_id else ""
            tagged.append(f"{prefix}{row_str}")

    # If bound_model filter used, the warning must precede the data (as ├─)
    # so the final data branch can still terminate cleanly.
    if bound_model is not None:
        lines.append(
            "├─ Warning: bound_model resolution is heuristic"
            " — may miss components using dynamic this.props.resModel"
        )

    for row in tagged:
        # Wave 5: All rows are ├─; Next: footer becomes the final └─.
        lines.append(f"├─ {row}")

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call module_inspect(name='{module}', method='owl',"
            f" odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"module_inspect(name='{module}', method='qweb', odoo_version='{odoo_version}')"
        " for QWeb templates",
        f"module_inspect(name='{module}', method='js', odoo_version='{odoo_version}')"
        " for related patches",
    ]))
    return "\n".join(lines)


def _list_qweb_templates(
    module: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-5c — enumerate QWeb templates declared in a module.

    Renders `xmlid : t-inherit=<parent or (root)>` per ADR-0023 §5.3.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_MAX_ITEMS
    effective_limit = min(limit, cap)

    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        rows = _srv._data_bounded(
            session,
            f"""
            MATCH (t:QWebTmpl {{module: $mod, odoo_version: $v}})
            WHERE {_srv._scope_pred("t")}
              AND t.module <> '__unresolved__'
            OPTIONAL MATCH (t)-[:EXTENDS_TMPL]->(parent:QWebTmpl)
            WHERE NOT coalesce(parent.unresolved, false)
              AND {_srv._scope_pred("parent")}
            RETURN t.xmlid AS xmlid, parent.xmlid AS parent_xmlid
            ORDER BY t.xmlid ASC
            SKIP $skip
            LIMIT $limit
            """,
            f"QWeb templates in '{module}' (Odoo {odoo_version})",
            mod=module, v=odoo_version, **_srv._scope(profile_name),
            skip=start_index, limit=effective_limit,
        )

        total_rec = _srv._single_bounded(
            session,
            """
            MATCH (t:QWebTmpl {module: $mod, odoo_version: $v})
            WHERE ($own IS NULL OR (size(t.profile) > 0
                   AND all(__p IN t.profile WHERE __p IN $own OR __p IN $shared)))
              AND t.module <> '__unresolved__'
            RETURN count(t) AS c
            """,
            f"QWeb template count in '{module}' (Odoo {odoo_version})",
            mod=module, v=odoo_version, **_srv._scope(profile_name),
        )
        total = total_rec["c"] if total_rec else 0

    header = f"QWeb templates of {module} (Odoo {odoo_version})"
    if total == 0:
        next_line = format_next_step([
            f"module_inspect(name='{module}', method='owl', odoo_version='{odoo_version}')"
            " for OWL components",
            f"describe_module(name='{module}', odoo_version='{odoo_version}')"
            " for module overview",
        ])
        return f"{header}\n├─ (none)\n{next_line}"

    # Mint opaque refs for each returned row.
    # QWeb templates have xmlid — use view kind (prefix 'v').
    tmpl_items = [{"xmlid": r["xmlid"]} for r in rows]
    ref_ids = mint_refs(tmpl_items, api_key_id, kind="view")

    lines = [header]
    more_hint = (
        f"module_inspect(name='{module}', method='qweb'"
        f", odoo_version='{odoo_version}', start_index={start_index + cap})"
    )
    rendered = _srv._render_capped(
        rows,
        lambda r: (
            f"{r['xmlid']} : t-inherit="
            f"{r.get('parent_xmlid') or '(root)'}"
        ),
        cap=cap,
        more_hint=more_hint,
    )
    # Inject [ref=vN] prefix for non-hint rows.
    ref_iter = iter(ref_ids)
    for row_str in rendered:
        if row_str.startswith("... and "):
            lines.append(f"├─ {row_str}")
        else:
            ref_id = next(ref_iter, None)
            prefix = f"[ref={ref_id}] " if ref_id else ""
            lines.append(f"├─ {prefix}{row_str}")

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call module_inspect(name='{module}', method='qweb',"
            f" odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"module_inspect(name='{module}', method='owl', odoo_version='{odoo_version}')"
        " for OWL components",
        f"find_examples(query='QWeb {module}', odoo_version='{odoo_version}')"
        " for inheritance patterns",
    ]))
    return "\n".join(lines)


# Era param mapping per ADR-0023 §5.3: user-facing era1/era2/era3 ↔
# stored JSPatch.era values ('extend'/'include'/'patch').
_JS_ERA_MAP = {
    "era1": "extend",
    "era2": "include",
    "era3": "patch",
    "extend": "extend",
    "include": "include",
    "patch": "patch",
}


def _list_js_patches(
    odoo_version: str = "auto",
    target: str | None = None,
    module: str | None = None,
    era: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-5d — enumerate JS patches across eras (Widget extend, mixin
    include, OWL patch).

    `era` accepts era1/era2/era3 (preferred) or extend/include/patch (stored
    values). `target` filters by patched component/widget name.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_PATCHES_MAX
    effective_limit = min(limit, cap)

    era_filter: str | None = None
    if era is not None:
        era_filter = _JS_ERA_MAP.get(era.lower())
        if era_filter is None:
            return (
                f"Invalid era '{era}'. Use era1, era2, or era3"
                " (or extend/include/patch)."
            )

    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        _js_label_noun = f"'{module}'" if module else (
            f"target '{target}'" if target else "all modules"
        )
        rows = _srv._data_bounded(
            session,
            f"""
            MATCH (j:JSPatch {{odoo_version: $v}})
            WHERE ($own IS NULL OR (size(j.profile) > 0
                   AND all(__p IN j.profile WHERE __p IN $own OR __p IN $shared)))
              AND ($target IS NULL OR j.target = $target)
              AND ($module IS NULL OR j.module = $module)
              AND ($era IS NULL OR j.era = $era)
              AND j.module <> '__unresolved__'
            OPTIONAL MATCH (mod:Module {{name: j.module, odoo_version: $v}})
            WITH j, mod,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN j.target AS target, j.patch_name AS patch_name,
                   j.era AS era, j.module AS module, coalesce(mod.repo_url, mod.repo) AS repo,
                   j.file_path AS file_path,
                   edition_rank, mod_name
            ORDER BY edition_rank ASC, mod_name ASC, j.target ASC, j.patch_name ASC
            SKIP $skip
            LIMIT $limit
            """,
            f"JS patches for {_js_label_noun} (Odoo {odoo_version})",
            v=odoo_version, target=target, module=module, era=era_filter,
            **_srv._scope(profile_name), skip=start_index, limit=effective_limit,
        )

        total_rec = _srv._single_bounded(
            session,
            """
            MATCH (j:JSPatch {odoo_version: $v})
            WHERE ($own IS NULL OR (size(j.profile) > 0
                   AND all(__p IN j.profile WHERE __p IN $own OR __p IN $shared)))
              AND ($target IS NULL OR j.target = $target)
              AND ($module IS NULL OR j.module = $module)
              AND ($era IS NULL OR j.era = $era)
              AND j.module <> '__unresolved__'
            RETURN count(j) AS c
            """,
            f"JS patch count for {_js_label_noun} (Odoo {odoo_version})",
            v=odoo_version, target=target, module=module, era=era_filter,
            **_srv._scope(profile_name),
        )
        total = total_rec["c"] if total_rec else 0

    parent = target or module or "all targets"
    header = f"JS patches on {parent} (Odoo {odoo_version})"
    if total == 0:
        # Wave 5: Next-step footer per ADR-0023 §4 — suggest OWL components
        # when module is known (era3 drill-down).
        if module:
            next_line = format_next_step([
                f"module_inspect(name='{module}', method='owl'"
                f", odoo_version='{odoo_version}') for v15+ components",
            ])
        else:
            next_line = format_next_step([
                f"find_examples(query='JS patch', odoo_version='{odoo_version}')"
                " for patch patterns",
            ])
        return f"{header}\n├─ (none)\n{next_line}"

    # Mint opaque refs for each returned row.
    # JS patches have module_name key → 'module' kind (prefix 'x').
    patch_items = [
        {"module_name": r.get("module") or "?", "target": r.get("target") or "?"}
        for r in rows
    ]
    ref_ids = mint_refs(patch_items, api_key_id, kind="module")

    groups: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    order: list[tuple[str, str]] = []
    for r, ref_id in zip(rows, ref_ids):
        key = (r.get("repo") or "?", r.get("module") or "?")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((r, ref_id))

    lines = [header]
    for key in order:
        repo, mod_name = key
        lines.append(f"├─ [{repo}] {mod_name}")
        sub_indent = "│   "
        sub_items = groups[key]
        more_hint = (
            f"module_inspect(name='{mod_name}', method='js', odoo_version='{odoo_version}',"
            f" start_index={start_index + cap})"
        )
        raw_rows = [r for r, _ in sub_items]

        def _fmt_js_patch(r: dict) -> str:
            base = f"{r['target']}.{r['patch_name']} : era={r.get('era') or '?'}"
            if r.get("file_path"):
                # ADR-0037: repo-relative path. Anchor on module (r['repo'] is now
                # the portable git URL via coalesce, not a path-prefix anchor).
                pp = _srv._portable_path(r["file_path"], module=r.get("module"))
                base += f" | {pp}"
            return base

        rendered = _srv._render_capped(
            raw_rows,
            _fmt_js_patch,
            cap=cap,
            more_hint=more_hint,
        )
        # Inject [ref=xN] prefix for non-hint rows.
        ref_iter = iter([ref_id for _, ref_id in sub_items])
        tagged: list[str] = []
        for row_str in rendered:
            if row_str.startswith("... and "):
                tagged.append(row_str)
            else:
                ref_id = next(ref_iter, None)
                prefix = f"[ref={ref_id}] " if ref_id else ""
                tagged.append(f"{prefix}{row_str}")

        last_r = len(tagged) - 1
        for j, row in enumerate(tagged):
            r_conn = "└─" if j == last_r else "├─"
            lines.append(f"{sub_indent}{r_conn} {row}")

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call module_inspect(name='{module or '...'}', method='js',"
            f" odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4. Prefer module-scoped OWL
    # drill-down when module is known; otherwise suggest find_examples.
    if module:
        next_hints = [
            f"module_inspect(name='{module}', method='owl'"
            f", odoo_version='{odoo_version}') for v15+ components",
            f"find_examples(query='JS patch', odoo_version='{odoo_version}')"
            " for patch patterns",
        ]
    else:
        next_hints = [
            f"find_examples(query='JS patch', odoo_version='{odoo_version}')"
            " for patch patterns",
        ]
    lines.append(format_next_step(next_hints))
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
# ``.get`` (not ``[...]``) so a cold ``import src.mcp.listings`` in a fresh
# interpreter (server not loaded) binds ``None`` instead of raising KeyError.
# In production this module is only ever imported via server.py's end-of-body
# pop+reimport, at which point ``src.mcp.server`` IS in sys.modules, so ``.get``
# returns the SAME generation as before — the helper bodies that dereference
# ``_srv`` only run through that path, never under a bare cold import.
_srv = sys.modules.get("src.mcp.server")

