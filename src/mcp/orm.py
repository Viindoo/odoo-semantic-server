# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/orm.py
"""ORM-level validation tools (M10.5 Phase 2).

Four standalone MCP tools that validate ORM constructs against the indexed
Field/Method graph *before* an AI client suggests them to a user:

- ``resolve_orm_chain``  — walk a dotted field path, return terminal type.
- ``validate_domain``    — check each domain term's field-path + operator.
- ``validate_depends``   — check ``@api.depends`` dependency paths.
- ``validate_relation``  — assert a field points at an expected comodel.

All four reuse the ``_traverse_field_chain`` primitive. They read Neo4j Field/
Method nodes (tagged by ``odoo_version`` at index time) so the tools themselves
are version-agnostic; the only version-aware logic is the domain operator set
(``valid_domain_operators`` in constants) and the era1 gate in validate_depends
(v8/v9 have no decorator depends — ``Method.depends`` is empty).

Late imports of ``src.mcp.server`` avoid a circular dependency (server.py
imports this module to register the four ``@mcp.tool`` wrappers), mirroring
``src/mcp/inspect.py``.

See docs/adr/0023-tool-output-completeness.md (tree-grammar contract) and
TASKS.md M10.5 Phase 2.
"""
import ast
import difflib

import neo4j
from neo4j.exceptions import ClientError

from src.constants import (
    EDITION_PRIORITY,
    EDITION_PRIORITY_ELSE,
    MAGIC_FIELDS,
    NEO4J_QUERY_TIMEOUT_SECONDS,
    RELATIONAL_TTYPES,
    valid_domain_operators,
)
from src.mcp.hints import hints_for


def _edition_rank_cypher(node_alias: str = "mod") -> str:
    """Cypher CASE expression for edition priority — mirrors server._edition_rank_cypher.

    Lower rank = higher priority (community=0 < enterprise=1 < viindoo=2 < oca=3).
    Used by the inherited-field/method dedup ORDER BY so the CE vs EE tiebreak
    matches the 5-tier ranking in server.py._resolve_field / _resolve_method.
    SSOT for the priority values is EDITION_PRIORITY in src/constants.py.
    """
    cases = " ".join(
        f"WHEN '{ed}' THEN {rank}"
        for ed, rank in sorted(EDITION_PRIORITY.items(), key=lambda x: x[1])
    )
    return (
        f"CASE {node_alias}.edition {cases} ELSE {EDITION_PRIORITY_ELSE} END"
        f" AS edition_rank"
    )


# Status codes raised when a transaction exceeds its timeout. There are TWO:
#   - Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration
#     is returned when the timeout comes from the *driver* (our per-query
#     neo4j.Query(text, timeout=...)) — verified against neo4j 5.28 + server 5.26.
#   - Neo.ClientError.Transaction.TransactionTimedOut
#     is returned when the timeout comes from the *server* config
#     (db.transaction.timeout, which Wave-0 sets to 600s on prod).
# We match the common prefix so BOTH surface as OrmQueryTimeout; any other
# ClientError (syntax, constraint, ...) still propagates unchanged.
#
# DRIVER-BUMP NOTE (L12): this reads exc.code (legacy Neo4j status string).
# neo4j-python driver 6.x moves to GQLSTATUS and may change how the code is
# exposed (e.g. exc.gql_status / a different attribute). When bumping the driver,
# re-verify _is_tx_timeout still matches both timeout variants, and update the
# matcher + the timeout-path test in tests/test_orm_dense_inheritance.py
# (which currently constructs the error by setting exc.code, itself deprecated).
_TX_TIMEOUT_CODE_PREFIX = "Neo.ClientError.Transaction.TransactionTimedOut"


class OrmQueryTimeout(Exception):
    """A bounded ORM read query exceeded NEO4J_QUERY_TIMEOUT_SECONDS.

    Carries a user-facing English message (ADR-0023 tone, no Cypher leaked).
    Interface contract with the MCP wrapper layer (server.py): the wrapper
    catches this, increments the timeout metric, and returns ``user_message``
    to the client. The traversal/validation helpers deliberately do NOT
    catch-and-render it — they let it propagate to that wrapper.
    """

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


def _is_tx_timeout(exc: ClientError) -> bool:
    """True when a ClientError is a transaction-timeout (driver- or server-set)."""
    return (getattr(exc, "code", None) or "").startswith(_TX_TIMEOUT_CODE_PREFIX)


def _lookup_timeout(field: str, model: str, version: str) -> "OrmQueryTimeout":
    """Build an OrmQueryTimeout for a field-resolution timeout (ADR-0023 tone)."""
    return OrmQueryTimeout(
        f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while resolving "
        f"field '{field}' on '{model}' (Odoo {version}). The inheritance graph "
        f"for this model may be unusually dense - try a more specific model or "
        f"retry later."
    )


def _relation_timeout(comodel: str, target: str, version: str) -> "OrmQueryTimeout":
    """Build an OrmQueryTimeout for a relation subtype-check timeout (ADR-0023 tone)."""
    return OrmQueryTimeout(
        f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while checking "
        f"whether '{comodel}' is a subtype of '{target}' (Odoo {version}). The "
        f"inheritance graph may be unusually dense - try a more specific model "
        f"or retry later."
    )


def _bounded(text: str) -> "neo4j.Query":
    """Wrap Cypher text in a neo4j.Query carrying the per-query timeout.

    ``session.run`` does not accept a ``timeout`` kwarg for auto-commit
    transactions, but a ``neo4j.Query`` object does — this is the least-invasive
    way to bound every ORM read (issue #273).
    """
    return neo4j.Query(text, timeout=NEO4J_QUERY_TIMEOUT_SECONDS)


def _effective_allowed(profile_name):
    """Lazy shim — avoids circular import (server imports orm at module level).

    Delegates to src.mcp.server._effective_allowed for the tenant boundary +
    profile_name narrowing logic (ADR-0034 WI-4, C2 enforcement).
    """
    from src.mcp.server import _effective_allowed as _ea  # lazy: avoid circular import
    return _ea(profile_name)


def _scope(profile_name=None):
    """Lazy shim → src.mcp.server._scope (Neo4j own/shared array-filter params)."""
    from src.mcp.server import _scope as _s  # lazy: avoid circular import
    return _s(profile_name)


def _scope_pred(alias: str) -> str:
    """Lazy shim → src.mcp.server._scope_pred (canonical fail-closed predicate)."""
    from src.mcp.server import _scope_pred as _sp  # lazy: avoid circular import
    return _sp(alias)

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


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
    with :func:`_count_fields_with_inherited`. ``module``/``kind`` filter the
    kept (deduped) fields. Tenant choke (``_scope_pred("f")``) is applied on the
    Field node (ADR-0034 fail-closed). Bounded by ``_bounded()`` (issue #273).
    """
    try:
        rows = session.run(
            _bounded(
                _ANCESTOR_TAGGED_PROLOGUE + """
                MATCH (f:Field {model: owner_model, odoo_version: $v})
                WHERE """ + _scope_pred("f") + """
                  AND ($module IS NULL OR f.module = $module)
                  AND ($kind IS NULL OR f.ttype = $kind)
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
) -> int:
    """Count distinct field names on ``model`` including inherited (own + mixin).

    Applies the SAME traversal + name-dedup as
    :func:`_list_fields_with_inherited` then ``count(DISTINCT fname)`` — so the
    "Showing X of N" total stays consistent with the paginated rows. Bounded.
    """
    try:
        rec = session.run(
            _bounded(
                _ANCESTOR_TAGGED_PROLOGUE + """
                MATCH (f:Field {model: owner_model, odoo_version: $v})
                WHERE """ + _scope_pred("f") + """
                  AND ($module IS NULL OR f.module = $module)
                  AND ($kind IS NULL OR f.ttype = $kind)
                  AND f.module <> '__unresolved__'
                RETURN count(DISTINCT f.name) AS c
                """
            ),
            mn=model, v=odoo_version, module=module, kind=kind,
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
) -> int:
    """Count distinct method names on ``model`` including inherited (own + mixin).

    Same traversal (INHERITS only — GAP-1) + name-dedup as
    :func:`_list_methods_with_inherited` then ``count(DISTINCT mname)`` — keeps
    the method total consistent with the paginated rows. Bounded.
    """
    try:
        rec = session.run(
            _bounded(
                _ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY + """
                MATCH (mth:Method {model: owner_model, odoo_version: $v})
                WHERE """ + _scope_pred("mth") + """
                  AND ($module IS NULL OR mth.module = $module)
                  AND mth.module <> '__unresolved__'
                RETURN count(DISTINCT mth.name) AS c
                """
            ),
            mn=model, v=odoo_version, module=module,
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


def _suggest(name: str, candidates: list[str]) -> str | None:
    """Closest field name via difflib (approximates 'edit distance <= 2')."""
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.7)
    return matches[0] if matches else None


def _broken_reason_text(err: dict) -> str:
    """English one-liner describing a broken traversal hop (ADR-0023 §2)."""
    field, model, ttype = err["field"], err["model"], err["ttype"]
    if err["reason"] == "missing":
        return f"field '{field}' not found on {model}"
    if err["reason"] == "not_relational":
        return (f"field '{field}' on {model} is type '{ttype}', not relational"
                " — cannot traverse further")
    return (f"field '{field}' on {model} has no recorded comodel"
            " — cannot traverse further")


# ---------------------------------------------------------------------------
# resolve_orm_chain
# ---------------------------------------------------------------------------


def _resolve_orm_chain(
    model: str, dotted_path: str, odoo_version: str = "auto", profile_name: str | None = None
) -> str:
    from src.mcp import server as srv

    dotted_path = (dotted_path or "").strip()
    if not [p for p in dotted_path.split(".") if p]:
        return ("Error: resolve_orm_chain requires a non-empty dotted_path"
                " (e.g. 'partner_id.country_id.code').")

    with srv._get_driver().session() as session:
        version = srv._resolve_version(odoo_version, session)
        result = _traverse_field_chain(model, dotted_path, version, session, profile_name)

    total = len([p for p in dotted_path.split(".") if p])
    lines = [f"{model}.{dotted_path} (Odoo {version})"]
    for step in result["steps"]:
        rel = f" -> {step['comodel']}" if step["comodel"] else ""
        terminal = (result["error"] is None and step is result["terminal"])
        tag = " (terminal)" if terminal else ""
        lines.append(f"├─ {step['model']}.{step['field']} : {step['ttype']}{rel}{tag}")

    err = result["error"]
    if err is not None:
        lines.append(f"├─ BROKEN at step {err['step'] + 1}/{total}: {_broken_reason_text(err)}")
        ctx_model, ctx_field = err["model"], err["field"]
    else:
        term = result["terminal"]
        ctx_model = term["comodel"] or term["model"]
        ctx_field = term["field"]

    footer = hints_for(
        "resolve_orm_chain", name=model, model=ctx_model, field=ctx_field, ver=version
    )
    if footer:
        lines.append(footer)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# validate_domain
# ---------------------------------------------------------------------------


def _parse_domain(domain) -> tuple[list | None, str | None]:
    """Parse a domain (str or already-list) into a Python list. Returns (list, error)."""
    if isinstance(domain, (list, tuple)):
        return list(domain), None
    if not isinstance(domain, str):
        return None, f"domain must be a string or list, got {type(domain).__name__}"
    text = domain.strip()
    if not text:
        return None, "domain is empty"
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError) as exc:
        return None, f"could not parse domain literal ({exc.__class__.__name__})"
    if not isinstance(parsed, (list, tuple)):
        return None, "domain must evaluate to a list of terms"
    return list(parsed), None


def _validate_domain(
    model: str, domain, odoo_version: str = "auto", profile_name: str | None = None
) -> str:
    from src.mcp import server as srv

    terms, parse_err = _parse_domain(domain)
    if parse_err is not None:
        return f"Error: {parse_err}. Expected e.g. [('partner_id.country_id', '=', 'VN')]."

    with srv._get_driver().session() as session:
        version = srv._resolve_version(odoo_version, session)
        operators = valid_domain_operators(version)

        rendered: list[str] = []
        errors = 0
        for term in terms:
            # Logical connectors are bare strings ('&', '|', '!') — skipped, not validated.
            if isinstance(term, str):
                if term in ("&", "|", "!"):
                    rendered.append(f"├─ logical operator '{term}' : skipped")
                else:
                    errors += 1
                    rendered.append(f"├─ '{term}' : ERROR unexpected string (not a 3-term tuple)")
                continue
            if not isinstance(term, (list, tuple)) or len(term) != 3:
                errors += 1
                rendered.append(f"├─ {term!r} : ERROR malformed term (expected (field, op, value))")
                continue

            field_path, op, _value = term
            problems: list[str] = []

            if not isinstance(field_path, str):
                problems.append("left operand is not a field-path string")
            else:
                chain = _traverse_field_chain(model, field_path, version, session, profile_name)
                if chain["error"] is not None:
                    err = chain["error"]
                    msg = _broken_reason_text(err)
                    # B1: "did you mean?" suggestion for missing fields (matches
                    # validate_depends error format in the same file).
                    if err["reason"] == "missing":
                        cands = _field_names_on_model(
                            err["model"], version, session, profile_name
                        )
                        hint = _suggest(err["field"], cands)
                        if hint:
                            msg += f" — did you mean '{hint}'?"
                    problems.append(msg)

            if op not in operators:
                problems.append(f"operator {op!r} not valid in Odoo {version}")

            if problems:
                errors += 1
                rendered.append(f"├─ {term!r} : ERROR " + "; ".join(problems))
            else:
                rendered.append(f"├─ {term!r} : OK")

    verdict = "OK" if errors == 0 else f"{errors} problem(s)"
    lines = [f"Domain validation: {model} (Odoo {version}) — {verdict}"]
    lines.extend(rendered)
    footer = hints_for("validate_domain", name=model, ver=version)
    if footer:
        lines.append(footer)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# validate_depends
# ---------------------------------------------------------------------------


def _validate_depends(
    model: str, method: str, odoo_version: str = "auto", profile_name: str | None = None
) -> str:
    from src.mcp import server as srv

    with srv._get_driver().session() as session:
        version = srv._resolve_version(odoo_version, session)
        try:
            rows = session.run(
                _bounded(
                    """
                    MATCH (mth:Method {name: $mn, model: $model, odoo_version: $v})
                    WHERE """ + _scope_pred("mth") + """
                    RETURN mth.depends AS depends
                    """
                ),
                mn=method, model=model, v=version, **_scope(profile_name),
            ).data()
        except ClientError as exc:
            if _is_tx_timeout(exc):
                raise OrmQueryTimeout(
                    f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while "
                    f"resolving @api.depends on '{model}.{method}' (Odoo {version}). "
                    f"Try a more specific model or retry later."
                ) from exc
            raise

        if not rows:
            return (f"Method '{method}' not found on model '{model}' in Odoo {version}.")

        # Union depends across all modules that define/override the method (dedup, ordered).
        seen: dict[str, None] = {}
        for r in rows:
            for dep in (r.get("depends") or []):
                seen.setdefault(dep, None)
        deps = list(seen)

        lines = [f"@api.depends on {model}.{method} (Odoo {version})"]
        if not deps:
            lines.append("├─ no @api.depends found — method is not a computed-field"
                         " dependency (or era1 v8/v9 uses store= triggers, not the decorator)")
            footer = hints_for("validate_depends", name=model, ver=version)
            if footer:
                lines.append(footer)
            return "\n".join(lines)

        errors = 0
        for dep in deps:
            segments = dep.split(".")
            if "id" in segments:
                errors += 1
                lines.append(f"├─ '{dep}' : ERROR cannot depend on 'id'"
                             " (Odoo raises NotImplementedError)")
                continue
            chain = _traverse_field_chain(model, dep, version, session, profile_name)
            if chain["error"] is None:
                lines.append(f"├─ '{dep}' : OK")
            else:
                errors += 1
                err = chain["error"]
                msg = _broken_reason_text(err)
                if err["reason"] == "missing":
                    cands = _field_names_on_model(err["model"], version, session, profile_name)
                    hint = _suggest(err["field"], cands)
                    if hint:
                        msg += f" — did you mean '{hint}'?"
                lines.append(f"├─ '{dep}' : ERROR {msg}")

    verdict = "all dependencies valid" if errors == 0 else f"{errors} invalid"
    lines[0] = f"@api.depends on {model}.{method} (Odoo {version}) — {verdict}"
    footer = hints_for("validate_depends", name=model, ver=version)
    if footer:
        lines.append(footer)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# validate_relation
# ---------------------------------------------------------------------------


def _validate_relation(
    model: str, field: str, target_model: str,
    odoo_version: str = "auto", profile_name: str | None = None,
) -> str:
    from src.mcp import server as srv

    with srv._get_driver().session() as session:
        version = srv._resolve_version(odoo_version, session)
        info = _lookup_field(model, field, version, session, profile_name)

        header = f"Relation check: {model}.{field} -> {target_model} (Odoo {version})"

        if info is None:
            cands = _field_names_on_model(model, version, session, profile_name)
            hint = _suggest(field, cands)
            tail = f" — did you mean '{hint}'?" if hint else ""
            lines = [header, f"└─ ERROR field '{field}' not found on {model}{tail}"]
            return "\n".join(lines)  # error tree: no Next footer (terminal)

        ttype = (info["ttype"] or "").lower()
        if ttype not in RELATIONAL_TTYPES:
            lines = [header,
                     f"├─ MISMATCH {field} is type '{info['ttype']}', not a relational field"]
            footer = hints_for("validate_relation", name=model, field=field, ver=version)
            if footer:
                lines.append(footer)
            return "\n".join(lines)

        actual = info["comodel"]
        ok = False
        if actual == target_model:
            ok = True
        elif actual:
            # Accept when the field's comodel is a subtype of target_model
            # (comodel INHERITS* target_model) — e.g. field -> a mixin's subtype.
            #
            # Per-hop name-dedup over 5 INHERITS hops (issue #273, review r3
            # CRITICAL-1): the old `MATCH (c)-[:INHERITS*1..5]->(t)` anchored on
            # all K duplicate comodel nodes and enumerated the depth-5 mesh even
            # in the common MISMATCH case (exhaustive negative search).
            #
            # Same TWO structural fixes as _lookup_field step-3:
            #   (1) prune same-name DURING expansion — each hop adds
            #       `h<i>.name <> <expansion-source-name>`, so the BFS never
            #       re-enters the K-duplicate mesh (lossless: per-hop MATCH
            #       re-anchors by name on ALL nodes of that name);
            #   (2) aggregate to a SINGLE row before each subsequent hop via flat
            #       OPTIONAL MATCH + WITH collect(DISTINCT ...) — the anchor's K
            #       rows fold into one row up front, so each hop runs exactly
            #       once instead of once-per-anchor-row. Flat shape also drops
            #       the Neo4j 5.26 `CALL { WITH }` deprecation.
            #
            # Predicates preserved (khaosat-273-orm.md §4.2): INHERITS only (no
            # DELEGATES_TO); odoo_version on every MATCH; unresolved filter on
            # the anchor AND every hop node; _scope_pred("c") on the anchor and
            # _scope_pred("t") on the re-bound target (intermediate hops carry
            # no scope, matching the original); pn <> $comodel as defense-in-depth
            # at the final WHERE; LIMIT 1, no ORDER BY (existence check).
            try:
                rec = session.run(
                    _bounded(
                        f"""
                        MATCH (c:Model {{name: $comodel, odoo_version: $v}})
                        WHERE NOT coalesce(c.unresolved, false)
                          AND {_scope_pred("c")}
                        OPTIONAL MATCH (c)-[:INHERITS]->(h1:Model {{odoo_version: $v}})
                        WHERE NOT coalesce(h1.unresolved, false) AND h1.name <> $comodel
                        WITH collect(DISTINCT h1.name) AS hop1
                        UNWIND (CASE WHEN size(hop1) = 0 THEN [null] ELSE hop1 END) AS pn1
                        OPTIONAL MATCH (:Model {{name: pn1, odoo_version: $v}})
                              -[:INHERITS]->(h2:Model {{odoo_version: $v}})
                        WHERE pn1 IS NOT NULL AND NOT coalesce(h2.unresolved, false)
                              AND h2.name <> pn1
                        WITH hop1, collect(DISTINCT h2.name) AS hop2
                        UNWIND (CASE WHEN size(hop2) = 0 THEN [null] ELSE hop2 END) AS pn2
                        OPTIONAL MATCH (:Model {{name: pn2, odoo_version: $v}})
                              -[:INHERITS]->(h3:Model {{odoo_version: $v}})
                        WHERE pn2 IS NOT NULL AND NOT coalesce(h3.unresolved, false)
                              AND h3.name <> pn2
                        WITH hop1, hop2, collect(DISTINCT h3.name) AS hop3
                        UNWIND (CASE WHEN size(hop3) = 0 THEN [null] ELSE hop3 END) AS pn3
                        OPTIONAL MATCH (:Model {{name: pn3, odoo_version: $v}})
                              -[:INHERITS]->(h4:Model {{odoo_version: $v}})
                        WHERE pn3 IS NOT NULL AND NOT coalesce(h4.unresolved, false)
                              AND h4.name <> pn3
                        WITH hop1, hop2, hop3, collect(DISTINCT h4.name) AS hop4
                        UNWIND (CASE WHEN size(hop4) = 0 THEN [null] ELSE hop4 END) AS pn4
                        OPTIONAL MATCH (:Model {{name: pn4, odoo_version: $v}})
                              -[:INHERITS]->(h5:Model {{odoo_version: $v}})
                        WHERE pn4 IS NOT NULL AND NOT coalesce(h5.unresolved, false)
                              AND h5.name <> pn4
                        WITH hop1, hop2, hop3, hop4, collect(DISTINCT h5.name) AS hop5
                        WITH hop1 + hop2 + hop3 + hop4 + hop5 AS all_names
                        UNWIND all_names AS pn
                        WITH DISTINCT pn
                        WHERE pn <> $comodel AND pn = $target
                        MATCH (t:Model {{name: pn, odoo_version: $v}})
                        WHERE {_scope_pred("t")}
                        RETURN 1 AS ok LIMIT 1
                        """
                    ),
                    comodel=actual, target=target_model, v=version,
                    **_scope(profile_name),
                ).single()
            except ClientError as exc:
                if _is_tx_timeout(exc):
                    raise _relation_timeout(actual, target_model, version) from exc
                raise
            ok = rec is not None

    lines = [header]
    if ok:
        lines.append(f"├─ OK {field} is {info['ttype']} -> {actual}")
    else:
        msg = f"├─ MISMATCH {field} is {info['ttype']} -> {actual or '(no comodel recorded)'}" \
              f" (expected {target_model})"
        lines.append(msg)
    footer = hints_for("validate_relation", name=model, field=field, ver=version)
    if footer:
        lines.append(footer)
    return "\n".join(lines)
