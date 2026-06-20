# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/orm_queries.py
"""Inherited-aware ORM query helpers (split out of src/mcp/orm.py, B2 refactor).

Read-side Neo4j traversal primitives for the ORM-validation + listing tools:

- ``_lookup_field`` — resolve a single field (direct / magic / inherited).
- ``_ancestor_tagged_prologue`` (+ the two compiled prologue constants and
  ``_EDGE_KIND_EXPR``) — the per-hop name-dedup ancestor-tagging Cypher shared by
  every inherited-aware helper (issue #273 / ADR-0048: NO variable-length path).
- ``_list_fields_with_inherited`` / ``_count_fields_with_inherited`` /
  ``_resolve_field_inherited`` and the symmetric ``_*_methods_*`` trio.
- ``_ancestor_owner_names`` — owner-model name set (depth 0-3).
- ``_traverse_field_chain`` — walk a dotted field path.
- ``_field_names_on_model`` — all field names (for typo suggestions).

This module is a PURE STRUCTURAL extraction — no behavior change. The shared
bottom layer (``_bounded`` / ``_scope`` / ``_scope_pred`` / the timeout infra /
``_edition_rank_cypher``) stays in ``src/mcp/orm.py`` and is imported here;
``src/mcp/orm.py`` re-exports every public name below so callers keep importing
them via ``src.mcp.orm`` unchanged. See ADR-0048 (same-name INHERITS topology +
ORM read bounds) and docs/adr/0023 (tree-grammar contract).
"""
from neo4j.exceptions import ClientError

from src.constants import (
    MAGIC_FIELDS,
    NEO4J_QUERY_TIMEOUT_SECONDS,
    RELATIONAL_TTYPES,
)
from src.mcp.orm import (
    OrmQueryTimeout,
    _bounded,
    _edition_rank_cypher,
    _is_tx_timeout,
    _lookup_timeout,
    _scope,
    _scope_pred,
)


def _lookup_field(
    model: str, field: str, odoo_version: str, session, profile_name: str | None = None
) -> dict | None:
    """Resolve a single field on a model. Returns ``{ttype, comodel, source}`` or None.

    Resolution order: (1) direct Field node (covers ``_inherit`` extension —
    OSM C1 stores fields under the shared ``_name``); (2) ORM magic field; (3)
    inherited via ``INHERITS`` (abstract mixin) or ``DELEGATES_TO`` (``_inherits``
    delegation) up to depth 3 — covers fields like ``message_ids`` that live on
    a mixin model, not the child.
    """
    try:
        rows = session.run(
            _bounded(
                f"""
                MATCH (f:Field {{name: $fn, model: $mn, odoo_version: $v}})
                WHERE {_scope_pred("f")}
                RETURN f.ttype AS ttype, f.comodel_name AS comodel
                ORDER BY f.module ASC
                LIMIT 1
                """
            ),
            fn=field, mn=model, v=odoo_version, **_scope(profile_name),
        ).data()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise _lookup_timeout(field, model, odoo_version) from exc
        raise
    if rows:
        return {"ttype": rows[0]["ttype"], "comodel": rows[0]["comodel"], "source": "direct"}

    if field in MAGIC_FIELDS:
        ttype, comodel = MAGIC_FIELDS[field]
        return {"ttype": ttype, "comodel": comodel, "source": "magic"}

    # Step 3 — inherited/delegated fallback. Per-hop name-dedup BFS (issue #273,
    # review r3 CRITICAL-1): collect the DISTINCT ancestor model *names* one hop
    # at a time, tag each name with the nearest depth it was reached at, then
    # join Field by name. This replaces the variable-length-path `*1..3` that
    # anchored on all K duplicate Model nodes and enumerated 20-86M paths.
    #
    # TWO STRUCTURAL FIXES over the first cut (both empirically proven necessary
    # on the un-cleaned prod K^2 mesh — review r3 measured 12.6s..TIMEOUT):
    #
    #   (1) PRUNE same-name DURING expansion, not only at the final WHERE. Each
    #       per-hop MATCH adds `h<i>.name <> <expansion-source-name>`, so the BFS
    #       never re-enters a same-name node and never re-expands the K-duplicate
    #       mesh. Lossless on old data: every per-hop MATCH re-anchors by NAME on
    #       ALL nodes of that name, so anything reachable via a same-name
    #       intermediate is already reachable directly from that name's own
    #       expansion (confirmed against prod). The first cut applied `pn <> $mn`
    #       only at the end → hop1 still collected $mn (same-name edges) and
    #       hop2/hop3 re-expanded from all K nodes across ~9.3k mesh edges.
    #
    #   (2) AGGREGATE to a SINGLE ROW before each subsequent hop. The anchor
    #       `MATCH (start:Model {name:$mn,...})` returns K rows (97-237 on prod);
    #       the old per-hop CALL subquery ran PER START ROW, multiplying every
    #       hop by K. Folding each hop into a `WITH collect(DISTINCT ...)` over
    #       all anchors yields one row carrying the deduped name list, so hop2
    #       runs once over hop1's <=16 names, hop3 once over hop2's. Flat
    #       OPTIONAL MATCH + WITH (no CALL subquery) — simplest shape that holds
    #       the "run each hop once" invariant, and sidesteps the Neo4j 5.26
    #       `CALL { WITH }` deprecation entirely.
    #
    # Predicates preserved (khaosat-273-orm.md §4.2):
    #   - odoo_version on every per-hop MATCH;
    #   - NOT coalesce(<node>.unresolved, false) on the anchor AND every hop node;
    #   - tenant scope choke ONLY on Field f via _scope_pred("f") (the single
    #     tenant boundary of step 3 — Model nodes carry no scope here);
    #   - pn <> $mn at the final WHERE as defense-in-depth (redundant once same-
    #     name is pruned during expansion, kept as a belt-and-braces guard).
    #
    # Semantics (flagged in PR): DEPTH-FIRST — a field on a nearer ancestor wins
    # over a farther one; within the same depth the tiebreak is parent name ASC
    # then f.module ASC. ORDER BY runs over the tiny joined set.
    try:
        rows = session.run(
            _bounded(
                """
                MATCH (start:Model {name: $mn, odoo_version: $v})
                WHERE NOT coalesce(start.unresolved, false)
                OPTIONAL MATCH (start)-[:INHERITS|DELEGATES_TO]->(h1:Model {odoo_version: $v})
                WHERE NOT coalesce(h1.unresolved, false) AND h1.name <> $mn
                WITH collect(DISTINCT h1.name) AS hop1
                UNWIND (CASE WHEN size(hop1) = 0 THEN [null] ELSE hop1 END) AS pn1
                OPTIONAL MATCH (:Model {name: pn1, odoo_version: $v})
                      -[:INHERITS|DELEGATES_TO]->(h2:Model {odoo_version: $v})
                WHERE pn1 IS NOT NULL AND NOT coalesce(h2.unresolved, false)
                      AND h2.name <> pn1
                WITH hop1, collect(DISTINCT h2.name) AS hop2
                UNWIND (CASE WHEN size(hop2) = 0 THEN [null] ELSE hop2 END) AS pn2
                OPTIONAL MATCH (:Model {name: pn2, odoo_version: $v})
                      -[:INHERITS|DELEGATES_TO]->(h3:Model {odoo_version: $v})
                WHERE pn2 IS NOT NULL AND NOT coalesce(h3.unresolved, false)
                      AND h3.name <> pn2
                WITH hop1, hop2, collect(DISTINCT h3.name) AS hop3
                WITH [n IN hop1 | {name: n, depth: 1}]
                     + [n IN hop2 | {name: n, depth: 2}]
                     + [n IN hop3 | {name: n, depth: 3}] AS tagged
                UNWIND tagged AS t
                WITH t.name AS pn, min(t.depth) AS depth
                WHERE pn <> $mn
                MATCH (f:Field {name: $fn, model: pn, odoo_version: $v})
                WHERE """ + _scope_pred("f") + """
                RETURN f.ttype AS ttype, f.comodel_name AS comodel
                ORDER BY depth ASC, pn ASC, f.module ASC
                LIMIT 1
                """
            ),
            fn=field, mn=model, v=odoo_version, **_scope(profile_name),
        ).data()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise _lookup_timeout(field, model, odoo_version) from exc
        raise
    if rows:
        return {"ttype": rows[0]["ttype"], "comodel": rows[0]["comodel"], "source": "inherited"}

    return None


# ---------------------------------------------------------------------------
# Inherited-aware enumeration helpers (read-side list/detail INHERITS-awareness)
# ---------------------------------------------------------------------------
#
# WHY these exist (separate from _lookup_field): the read-side list/detail tools
# (server.py `_list_fields`/`_resolve_field`/`_list_methods`/`_resolve_method`)
# flat-match `{model: $m}` and therefore MISS fields/methods inherited from an
# AbstractModel mixin (e.g. `res_ref` on `viin.approval.request` actually lives
# under `abstract.approval.request.fields`). `_lookup_field` already traverses
# INHERITS|DELEGATES_TO, but it returns a DELIBERATELY THIN record
# (`{ttype, comodel, source}`) tuned for the 4 ORM-validation tools — list/detail
# need the FULL record (compute/stored/related/required/... + provenance) plus a
# one-query enumeration (not N+1). So we add dedicated full-record helpers here
# and DO NOT touch `_lookup_field` (4 ORM tools depend on its thin shape).
#
# All four reuse the SAME per-hop name-dedup depth-3 shape as `_lookup_field`
# step-3 (issue #273): collect DISTINCT ancestor model NAMES one hop at a time,
# tagging each name with the nearest depth + edge kind it was first reached at,
# pruning same-name DURING expansion, aggregating to a single row between hops.
# NO variable-length path `*1..N` (that re-anchors on all K duplicate Model nodes
# and enumerates the K^2 same-name mesh → the original #273 explosion).
#
# edge_kind ∈ {"inherits", "delegates"}: INHERITS (abstract mixin) vs
# DELEGATES_TO (`_inherits` delegation). When a name is reachable by BOTH at the
# same depth, INHERITS wins (mixin composition is the stronger "is-a" relation).
#
# Dedup semantics: a field/method name appearing on both the child (depth 0) and
# an ancestor keeps the NEAREST one (child overrides mixin), matching Odoo MRO and
# `_lookup_field`'s depth-first contract. Dedup happens IN-QUERY, BEFORE SKIP/LIMIT,
# so pagination counts the deduped set correctly.

# Shared Cypher prologue builder: from $mn, build `tagged` = a list of
# {name, depth, kind} for the start model (depth 0) + ancestors at depth 1..3,
# per-hop name-dedup, no VLP. Callers append a MATCH on the entity (Field or
# Method) keyed by `owner_model` plus their own RETURN.
#
# `rels` parameterizes the relationship set traversed at each hop:
#   - FIELDS use ``"INHERITS|DELEGATES_TO"`` — fields ARE inherited through both
#     ``_inherit`` mixin composition (INHERITS) AND ``_inherits`` delegation
#     (DELEGATES_TO, fields-only related proxy on a separate table).
#   - METHODS use ``"INHERITS"`` ONLY — Python MRO inherits methods through
#     ``_inherit`` but ``_inherits`` delegation NEVER carries methods (unanimous
#     Odoo v8→v19; v9 core orm.rst:942-943 states fields-only explicitly). Pulling
#     methods across DELEGATES_TO would falsely advertise a delegated parent's
#     every method as inherited on the child (GAP-1) — active misinformation.
#
# Per hop we OPTIONAL MATCH the given edge type(s) and capture type(rel) so
# edge_kind is preserved; the same-name prune (`hX.name <> <source-name>`) runs on
# the matched node name. `start` may itself be K same-name nodes — the depth-0
# entry is keyed purely by $mn (one logical name), and hop1's
# `collect(DISTINCT ...)` folds all K anchors into one row before hop2/hop3,
# holding the run-each-hop-once invariant. On the INHERITS-only path every
# `kind` is `INHERITS` and every `via_field` is null (DELEGATES_TO is the only
# edge that carries via_field), so callers' edge_kind always resolves to
# `inherits` and via_field always to null — correct for methods.


def _ancestor_tagged_prologue(rels: str) -> str:
    """Build the ancestor-tagging Cypher prologue for the given relationship set.

    ``rels`` is a Cypher relationship-type pattern: ``"INHERITS|DELEGATES_TO"``
    for the field path, ``"INHERITS"`` for the method path. The two literals are
    the ONLY accepted values (see module note above); both are compile-time
    constants below, never interpolated from request input.
    """
    return f"""
    MATCH (start:Model {{name: $mn, odoo_version: $v}})
    WHERE NOT coalesce(start.unresolved, false)
    OPTIONAL MATCH (start)-[r1:{rels}]->(h1:Model {{odoo_version: $v}})
    WHERE NOT coalesce(h1.unresolved, false) AND h1.name <> $mn
    WITH collect(DISTINCT {{name: h1.name, kind: type(r1), via_field: r1.via_field}}) AS hop1
    UNWIND (CASE WHEN size(hop1) = 0 THEN [null] ELSE hop1 END) AS e1
    OPTIONAL MATCH (:Model {{name: e1.name, odoo_version: $v}})
          -[r2:{rels}]->(h2:Model {{odoo_version: $v}})
    WHERE e1 IS NOT NULL AND NOT coalesce(h2.unresolved, false)
          AND h2.name <> e1.name
    WITH hop1, collect(DISTINCT {{name: h2.name, kind: type(r2), via_field: r2.via_field}}) AS hop2
    UNWIND (CASE WHEN size(hop2) = 0 THEN [null] ELSE hop2 END) AS e2
    OPTIONAL MATCH (:Model {{name: e2.name, odoo_version: $v}})
          -[r3:{rels}]->(h3:Model {{odoo_version: $v}})
    WHERE e2 IS NOT NULL AND NOT coalesce(h3.unresolved, false)
          AND h3.name <> e2.name
    WITH hop1, hop2,
         collect(DISTINCT {{name: h3.name, kind: type(r3),
                           via_field: r3.via_field}}) AS hop3
    WITH [{{name: $mn, depth: 0, kind: 'INHERITS', via_field: null}}]
         + [e IN hop1 WHERE e IS NOT NULL |
            {{name: e.name, depth: 1, kind: e.kind, via_field: e.via_field}}]
         + [e IN hop2 WHERE e IS NOT NULL |
            {{name: e.name, depth: 2, kind: e.kind, via_field: e.via_field}}]
         + [e IN hop3 WHERE e IS NOT NULL |
            {{name: e.name, depth: 3, kind: e.kind, via_field: e.via_field}}]
         AS tagged
    UNWIND tagged AS t
    // Nearest depth per ancestor name; at that depth, INHERITS (rank 0) beats
    // DELEGATES_TO (rank 1). owner_model = $mn itself is depth 0 (the child).
    // Pick depth+kind+via_field from the SAME winning row (order depth ASC,
    // kind_rank ASC, then head) so the kind_rank and via_field reported belong
    // to the nearest depth — not an independent min mixing two different rows.
    // via_field is non-null only for DELEGATES_TO edges (ADR-0023 provenance);
    // on the INHERITS-only path it is always null.
    WITH t.name AS owner_model,
         t.depth AS depth,
         CASE WHEN t.kind = 'DELEGATES_TO' THEN 1 ELSE 0 END AS kind_rank,
         t.via_field AS via_field
    ORDER BY owner_model ASC, depth ASC, kind_rank ASC
    WITH owner_model,
         head(collect({{depth: depth, kind_rank: kind_rank,
                       via_field: via_field}})) AS pick
    WITH owner_model, pick.depth AS depth, pick.kind_rank AS kind_rank,
         pick.via_field AS via_field
"""


# Field path: fields are inherited through BOTH INHERITS (mixin) and
# DELEGATES_TO (_inherits delegation, fields-only).
_ANCESTOR_TAGGED_PROLOGUE = _ancestor_tagged_prologue("INHERITS|DELEGATES_TO")

# Method path: methods are inherited through INHERITS (Python MRO) ONLY —
# _inherits delegation never carries methods (GAP-1, Odoo v8→v19 ground-truth).
_ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY = _ancestor_tagged_prologue("INHERITS")

# Maps the numeric kind_rank back to the public edge_kind token.
_EDGE_KIND_EXPR = "CASE WHEN kind_rank = 1 THEN 'delegates' ELSE 'inherits' END"


def _list_fields_with_inherited(
    model: str,
    odoo_version: str,
    session,
    profile_name: str | None = None,
    module: str | None = None,
    kind: str | None = None,
    skip: int = 0,
    limit: int = 50,
    name_filter: str | None = None,
) -> list[dict]:
    """Enumerate fields on ``model`` INCLUDING those inherited from mixins.

    One query: own fields (depth 0) + inherited (depth 1-3 via
    ``INHERITS|DELEGATES_TO``), deduped by field name with the nearest owner
    winning (child overrides mixin). Returns a FULL record per field — the
    superset of what ``server.py._list_fields`` renders, plus provenance:

        {name, ttype, module, repo, stored, compute, comodel_name, related,
         required, effective_readonly, owner_model, inherit_depth, edge_kind}

    ``owner_model`` is the model the kept field is actually declared on
    (``== model`` for own fields); ``inherit_depth`` is 0 for own, 1-3 for
    inherited; ``edge_kind`` ∈ {``inherits``, ``delegates``}.

    Dedup runs IN-QUERY before ``SKIP``/``LIMIT`` so pagination is consistent
    with :func:`_count_fields_with_inherited`. ``module``/``kind``/``name_filter``
    filter the kept (deduped) fields. ``name_filter`` is a case-insensitive
    substring match on ``f.name``. Tenant choke (``_scope_pred("f")``) is applied
    on the Field node (ADR-0034 fail-closed). Bounded by ``_bounded()`` (issue #273).
    """
    try:
        rows = session.run(
            _bounded(
                _ANCESTOR_TAGGED_PROLOGUE + """
                MATCH (f:Field {model: owner_model, odoo_version: $v})
                WHERE """ + _scope_pred("f") + """
                  AND ($module IS NULL OR f.module = $module)
                  AND ($kind IS NULL OR f.ttype = $kind)
                  AND ($name_filter IS NULL OR toLower(f.name) CONTAINS toLower($name_filter))
                  AND f.module <> '__unresolved__'
                OPTIONAL MATCH (mod:Module {name: f.module, odoo_version: $v})
                // Dedup by field NAME: nearest depth wins (child overrides mixin);
                // tiebreak depth ASC, edition_rank ASC (CE before EE), module ASC.
                // edition_rank restores the same CE/EE tiebreak as the old
                // _list_fields/_edition_rank_cypher ordering (V1 regression fix).
                WITH f.name AS fname, depth, """ + _EDGE_KIND_EXPR + """ AS edge_kind,
                     via_field, f, mod, owner_model,
                     """ + _edition_rank_cypher("mod") + """
                ORDER BY fname ASC, depth ASC, edition_rank ASC, f.module ASC
                WITH fname, head(collect({
                    f: f, mod: mod, depth: depth,
                    owner: owner_model, edge_kind: edge_kind, via_field: via_field
                })) AS pick
                RETURN pick.f.name AS name, pick.f.ttype AS ttype,
                       pick.f.module AS module,
                       coalesce(pick.mod.repo_url, pick.mod.repo) AS repo,
                       pick.f.stored AS stored, pick.f.compute AS compute,
                       pick.f.comodel_name AS comodel_name,
                       pick.f.related AS related, pick.f.required AS required,
                       pick.f.effective_readonly AS effective_readonly,
                       pick.owner AS owner_model, pick.depth AS inherit_depth,
                       pick.edge_kind AS edge_kind, pick.via_field AS via_field
                ORDER BY name ASC
                SKIP $skip LIMIT $limit
                """
            ),
            mn=model, v=odoo_version, module=module, kind=kind,
            name_filter=name_filter,
            skip=skip, limit=limit, **_scope(profile_name),
        ).data()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise OrmQueryTimeout(
                f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while "
                f"listing fields (including inherited) on '{model}' (Odoo "
                f"{odoo_version}). The inheritance graph may be unusually dense "
                f"- try a more specific model or retry later."
            ) from exc
        raise
    return rows


def _count_fields_with_inherited(
    model: str,
    odoo_version: str,
    session,
    profile_name: str | None = None,
    module: str | None = None,
    kind: str | None = None,
    name_filter: str | None = None,
) -> int:
    """Count distinct field names on ``model`` including inherited (own + mixin).

    Applies the SAME traversal + name-dedup + filters as
    :func:`_list_fields_with_inherited` then ``count(DISTINCT fname)`` — so the
    "Showing X of N" total stays consistent with the paginated rows. ``name_filter``
    is a case-insensitive substring match applied identically to the list query
    (risk R4: omitting it here causes the total to diverge from the filtered rows).
    Bounded.
    """
    try:
        rec = session.run(
            _bounded(
                _ANCESTOR_TAGGED_PROLOGUE + """
                MATCH (f:Field {model: owner_model, odoo_version: $v})
                WHERE """ + _scope_pred("f") + """
                  AND ($module IS NULL OR f.module = $module)
                  AND ($kind IS NULL OR f.ttype = $kind)
                  AND ($name_filter IS NULL OR toLower(f.name) CONTAINS toLower($name_filter))
                  AND f.module <> '__unresolved__'
                RETURN count(DISTINCT f.name) AS c
                """
            ),
            mn=model, v=odoo_version, module=module, kind=kind,
            name_filter=name_filter,
            **_scope(profile_name),
        ).single()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise OrmQueryTimeout(
                f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while "
                f"counting fields (including inherited) on '{model}' (Odoo "
                f"{odoo_version}). The inheritance graph may be unusually dense "
                f"- try a more specific model or retry later."
            ) from exc
        raise
    return rec["c"] if rec else 0


def _resolve_field_inherited(
    model: str,
    field: str,
    odoo_version: str,
    session,
    profile_name: str | None = None,
) -> dict | None:
    """Resolve ONE inherited field — full record of its nearest ancestor.

    Fallback for ``server.py._resolve_field`` when a flat exact-match on the
    child model MISSES: walks ``INHERITS|DELEGATES_TO`` (depth 1-3, same per-hop
    dedup shape) and returns the FULL field record of the NEAREST ancestor that
    declares ``field``, with ``owner_model`` + ``edge_kind`` for provenance.

    Returns ``None`` when the field is not declared on any ancestor (caller then
    keeps its existing "not found" path). Returns the full superset record::

        {name, ttype, module, repo, stored, compute, comodel_name, related,
         required, effective_readonly, owner_model, inherit_depth, edge_kind}

    Depth 0 (the child itself) is intentionally INCLUDED in the traversal so a
    same-name ``_inherit`` extension that the caller's flat match somehow missed
    still resolves; in practice the caller only calls this AFTER a flat MISS, so
    the winning owner is an ancestor (depth >= 1). Tenant choke on Field; bounded.
    """
    try:
        rows = session.run(
            _bounded(
                _ANCESTOR_TAGGED_PROLOGUE + """
                MATCH (f:Field {name: $fn, model: owner_model, odoo_version: $v})
                WHERE """ + _scope_pred("f") + """
                  AND f.module <> '__unresolved__'
                OPTIONAL MATCH (mod:Module {name: f.module, odoo_version: $v})
                WITH f, mod, depth, kind_rank, owner_model, via_field,
                     """ + _edition_rank_cypher("mod") + """
                RETURN f.name AS name, f.ttype AS ttype, f.module AS module,
                       coalesce(mod.repo_url, mod.repo) AS repo,
                       f.stored AS stored, f.compute AS compute,
                       f.comodel_name AS comodel_name, f.related AS related,
                       f.required AS required,
                       f.effective_readonly AS effective_readonly,
                       f.string AS string, f.help AS help,
                       owner_model AS owner_model, depth AS inherit_depth,
                       """ + _EDGE_KIND_EXPR + """ AS edge_kind,
                       via_field AS via_field
                ORDER BY depth ASC, kind_rank ASC, edition_rank ASC, f.module ASC
                LIMIT 1
                """
            ),
            fn=field, mn=model, v=odoo_version, **_scope(profile_name),
        ).data()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise _lookup_timeout(field, model, odoo_version) from exc
        raise
    return rows[0] if rows else None


def _list_methods_with_inherited(
    model: str,
    odoo_version: str,
    session,
    profile_name: str | None = None,
    module: str | None = None,
    skip: int = 0,
    limit: int = 50,
    name_filter: str | None = None,
) -> list[dict]:
    """Enumerate methods on ``model`` INCLUDING those inherited from mixins.

    Symmetric to :func:`_list_fields_with_inherited` but over ``:Method`` nodes
    (matched ``{model, odoo_version}`` exactly like ``server.py._list_methods``).
    One query: own (depth 0) + inherited (depth 1-3 via ``INHERITS`` ONLY —
    ``_inherits`` delegation NEVER carries methods, GAP-1), deduped by method name
    (nearest owner wins). Returns a FULL record per method::

        {name, convention_kind, module, repo, signature, docstring, decorators,
         has_super_call, depends, owner_model, inherit_depth, edge_kind}

    ``edge_kind`` is always ``inherits`` on this path (methods can only be
    inherited, never delegated). Tenant choke (``_scope_pred("mth")``) on the
    Method node (ADR-0034). Dedup in-query before SKIP/LIMIT. Bounded (issue #273).
    """
    try:
        rows = session.run(
            _bounded(
                _ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY + """
                MATCH (mth:Method {model: owner_model, odoo_version: $v})
                WHERE """ + _scope_pred("mth") + """
                  AND ($module IS NULL OR mth.module = $module)
                  AND ($name_filter IS NULL OR toLower(mth.name) CONTAINS toLower($name_filter))
                  AND mth.module <> '__unresolved__'
                OPTIONAL MATCH (mod:Module {name: mth.module, odoo_version: $v})
                WITH mth.name AS mname, depth, mth, mod, owner_model,
                     """ + _edition_rank_cypher("mod") + """
                ORDER BY mname ASC, depth ASC, edition_rank ASC, mth.module ASC
                WITH mname, head(collect({
                    mth: mth, mod: mod, depth: depth, owner: owner_model
                })) AS pick
                RETURN pick.mth.name AS name,
                       pick.mth.convention_kind AS convention_kind,
                       pick.mth.module AS module,
                       coalesce(pick.mod.repo_url, pick.mod.repo) AS repo,
                       pick.mth.signature AS signature,
                       pick.mth.docstring AS docstring,
                       pick.mth.decorators AS decorators,
                       pick.mth.has_super_call AS has_super_call,
                       pick.mth.depends AS depends,
                       pick.owner AS owner_model, pick.depth AS inherit_depth,
                       'inherits' AS edge_kind
                ORDER BY name ASC
                SKIP $skip LIMIT $limit
                """
            ),
            mn=model, v=odoo_version, module=module,
            name_filter=name_filter,
            skip=skip, limit=limit, **_scope(profile_name),
        ).data()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise OrmQueryTimeout(
                f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while "
                f"listing methods (including inherited) on '{model}' (Odoo "
                f"{odoo_version}). The inheritance graph may be unusually dense "
                f"- try a more specific model or retry later."
            ) from exc
        raise
    return rows


def _count_methods_with_inherited(
    model: str,
    odoo_version: str,
    session,
    profile_name: str | None = None,
    module: str | None = None,
    name_filter: str | None = None,
) -> int:
    """Count distinct method names on ``model`` including inherited (own + mixin).

    Same traversal (INHERITS only — GAP-1) + name-dedup + filters as
    :func:`_list_methods_with_inherited` then ``count(DISTINCT mname)`` — keeps
    the method total consistent with the paginated rows. ``name_filter`` is
    applied identically to the list query (risk R4: omitting it causes the total
    to diverge from the filtered rows). Bounded.
    """
    try:
        rec = session.run(
            _bounded(
                _ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY + """
                MATCH (mth:Method {model: owner_model, odoo_version: $v})
                WHERE """ + _scope_pred("mth") + """
                  AND ($module IS NULL OR mth.module = $module)
                  AND ($name_filter IS NULL OR toLower(mth.name) CONTAINS toLower($name_filter))
                  AND mth.module <> '__unresolved__'
                RETURN count(DISTINCT mth.name) AS c
                """
            ),
            mn=model, v=odoo_version, module=module,
            name_filter=name_filter,
            **_scope(profile_name),
        ).single()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise OrmQueryTimeout(
                f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while "
                f"counting methods (including inherited) on '{model}' (Odoo "
                f"{odoo_version}). The inheritance graph may be unusually dense "
                f"- try a more specific model or retry later."
            ) from exc
        raise
    return rec["c"] if rec else 0


def _resolve_method_inherited(
    model: str,
    method: str,
    odoo_version: str,
    session,
    profile_name: str | None = None,
) -> dict | None:
    """Resolve ONE inherited method — full record of its nearest ancestor.

    Fallback for ``server.py._resolve_method`` when a flat exact-match on the
    child model MISSES. Walks ``INHERITS`` ONLY (depth 1-3, same per-hop dedup
    shape) — ``_inherits`` delegation NEVER carries methods (GAP-1) — and returns
    the FULL method record of the NEAREST ancestor that declares ``method``, with
    ``owner_model`` provenance::

        {name, convention_kind, module, repo, signature, docstring, decorators,
         has_super_call, depends, owner_model, inherit_depth, edge_kind}

    ``edge_kind`` is always ``inherits`` here. Returns ``None`` when not declared
    on any ancestor. Tenant choke on Method (ADR-0034); bounded.
    """
    try:
        rows = session.run(
            _bounded(
                _ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY + """
                MATCH (mth:Method {name: $mthn, model: owner_model, odoo_version: $v})
                WHERE """ + _scope_pred("mth") + """
                  AND mth.module <> '__unresolved__'
                OPTIONAL MATCH (mod:Module {name: mth.module, odoo_version: $v})
                WITH mth, mod, depth, owner_model,
                     """ + _edition_rank_cypher("mod") + """
                RETURN mth.name AS name,
                       mth.convention_kind AS convention_kind,
                       mth.module AS module,
                       coalesce(mod.repo_url, mod.repo) AS repo,
                       mth.signature AS signature, mth.docstring AS docstring,
                       mth.decorators AS decorators,
                       mth.has_super_call AS has_super_call,
                       mth.depends AS depends,
                       owner_model AS owner_model, depth AS inherit_depth,
                       'inherits' AS edge_kind
                ORDER BY depth ASC, edition_rank ASC, mth.module ASC
                LIMIT 1
                """
            ),
            mthn=method, mn=model, v=odoo_version, **_scope(profile_name),
        ).data()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise OrmQueryTimeout(
                f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while "
                f"resolving method '{method}' on '{model}' (Odoo {odoo_version}). "
                f"The inheritance graph may be unusually dense - try a more "
                f"specific model or retry later."
            ) from exc
        raise
    return rows[0] if rows else None


def _ancestor_owner_names(
    model: str,
    odoo_version: str,
    session,
    profile_name: str | None = None,
) -> list[str]:
    """Owner-model name set for ``model``: itself + INHERITS|DELEGATES_TO ancestors.

    Returns the DISTINCT model names reachable from ``model`` through the FIELD
    inheritance edges (``INHERITS|DELEGATES_TO``, depth 0-3) using the SAME
    per-hop name-dedup prologue (:data:`_ANCESTOR_TAGGED_PROLOGUE`) as the
    field-listing helpers — so a caller that needs "which models own the fields
    of ``model``" (e.g. the magic-field dedup in ``server._list_fields``) reuses
    the one bounded, explosion-safe traversal instead of re-hand-rolling it.

    ``model`` itself (depth 0) is always included. Bounded by :func:`_bounded`;
    a transaction-timeout ``ClientError`` is mapped to :class:`OrmQueryTimeout`
    so the caller can degrade gracefully rather than crash. NO tenant choke is
    applied here — Model nodes carry no scope (matching the prologue's own
    contract; the Field-level choke happens at the caller's Field MATCH).
    """
    try:
        rec = session.run(
            _bounded(
                _ANCESTOR_TAGGED_PROLOGUE + """
                RETURN collect(DISTINCT owner_model) AS names
                """
            ),
            mn=model, v=odoo_version, **_scope(profile_name),
        ).single()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise OrmQueryTimeout(
                f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while "
                f"resolving the owner-model set of '{model}' (Odoo "
                f"{odoo_version}). The inheritance graph may be unusually dense "
                f"- try a more specific model or retry later."
            ) from exc
        raise
    return list(rec["names"]) if rec and rec["names"] else [model]


def _traverse_field_chain(
    model: str, dotted_path: str, odoo_version: str, session, profile_name: str | None = None
) -> dict:
    """Walk a dotted field path (e.g. ``partner_id.country_id.code``).

    Returns a dict::

        {
          "steps":    [ {model, field, ttype, comodel}, ... ],  # resolved so far
          "terminal": {model, field, ttype, comodel} | None,    # last hop if OK
          "error":    {step, model, field, ttype, reason} | None,
        }

    ``reason`` ∈ {``missing``, ``not_relational``, ``dangling_comodel``}.
    Intermediate hops must be relational (many2one/one2many/many2many) with a
    recorded comodel; the terminal hop may be any type.
    """
    parts = [p for p in dotted_path.split(".") if p]
    if not parts:
        # Dots-only / empty path — no hop to resolve. Guard so callers never
        # subscript a None terminal (e.g. resolve_orm_chain('model', '.')).
        return {"steps": [], "terminal": None,
                "error": {"step": 0, "model": model, "field": dotted_path,
                          "ttype": None, "reason": "missing"}}
    steps: list[dict] = []
    current_model = model
    last_idx = len(parts) - 1

    for i, part in enumerate(parts):
        info = _lookup_field(current_model, part, odoo_version, session, profile_name)
        if info is None:
            return {"steps": steps, "terminal": None,
                    "error": {"step": i, "model": current_model, "field": part,
                              "ttype": None, "reason": "missing"}}
        step = {"model": current_model, "field": part,
                "ttype": info["ttype"], "comodel": info["comodel"]}
        steps.append(step)

        if i == last_idx:
            return {"steps": steps, "terminal": step, "error": None}

        # Intermediate hop — must be relational with a comodel to continue.
        if (info["ttype"] or "").lower() not in RELATIONAL_TTYPES:
            return {"steps": steps, "terminal": None,
                    "error": {"step": i, "model": current_model, "field": part,
                              "ttype": info["ttype"], "reason": "not_relational"}}
        if not info["comodel"]:
            return {"steps": steps, "terminal": None,
                    "error": {"step": i, "model": current_model, "field": part,
                              "ttype": info["ttype"], "reason": "dangling_comodel"}}
        current_model = info["comodel"]

    return {"steps": steps, "terminal": steps[-1] if steps else None, "error": None}


def _field_names_on_model(
    model: str, odoo_version: str, session, profile_name: str | None = None
) -> list[str]:
    """All field names declared on a model (+ magic fields) — for typo suggestions."""
    try:
        rows = session.run(
            _bounded(
                f"""
                MATCH (f:Field {{model: $mn, odoo_version: $v}})
                WHERE {_scope_pred("f")}
                RETURN DISTINCT f.name AS name
                """
            ),
            mn=model, v=odoo_version, **_scope(profile_name),
        ).data()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise OrmQueryTimeout(
                f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while "
                f"listing fields on '{model}' (Odoo {odoo_version}). Try a more "
                f"specific model or retry later."
            ) from exc
        raise
    names = {r["name"] for r in rows} | set(MAGIC_FIELDS)
    return sorted(names)
