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
    MAGIC_FIELDS,
    NEO4J_QUERY_TIMEOUT_SECONDS,
    RELATIONAL_TTYPES,
    valid_domain_operators,
)
from src.mcp.hints import hints_for

# Status codes raised when a transaction exceeds its timeout. There are TWO:
#   - Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration
#     is returned when the timeout comes from the *driver* (our per-query
#     neo4j.Query(text, timeout=...)) — verified against neo4j 5.28 + server 5.26.
#   - Neo.ClientError.Transaction.TransactionTimedOut
#     is returned when the timeout comes from the *server* config
#     (db.transaction.timeout, which Wave-0 sets to 600s on prod).
# We match the common prefix so BOTH surface as OrmQueryTimeout; any other
# ClientError (syntax, constraint, ...) still propagates unchanged.
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
                """
                MATCH (f:Field {name: $fn, model: $mn, odoo_version: $v})
                WHERE ($own IS NULL OR (size(f.profile) > 0
                       AND all(__p IN f.profile WHERE __p IN $own OR __p IN $shared)))
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

    # Step 3 — inherited/delegated fallback. Per-hop name-dedup BFS (issue #273):
    # collect the DISTINCT ancestor model *names* one hop at a time (each hop
    # collapses the K-duplicate same-name mesh to <=16 distinct names), tag each
    # name with the nearest depth it was reached at, then join Field by name.
    # This replaces the variable-length-path `*1..3` that anchored on all K
    # duplicate Model nodes and enumerated 20-86M paths to find <=16 names.
    #
    # Predicates preserved (khaosat-273-orm.md §4.2):
    #   - odoo_version on every per-hop MATCH;
    #   - NOT coalesce(<node>.unresolved, false) on the anchor AND every hop node
    #     (tighter than the old VLP, which only filtered the terminal node);
    #   - tenant scope choke ONLY on Field f via _scope_pred("f") (the single
    #     tenant boundary of step 3 — Model nodes carry no scope here);
    #   - pn <> $mn excludes same-name "ancestors" (the unclean mesh).
    #
    # Semantics CHANGE (flagged in PR): the old query ranked the winning field by
    # parent.name ASC over the whole depth-1..3 set. This one is DEPTH-FIRST — a
    # field on a nearer ancestor wins over a farther one; within the same depth
    # the tiebreak is parent name ASC then f.module ASC. ORDER BY runs over the
    # tiny (<=16 names x few Field rows) joined set, so it cannot force the full
    # enumeration the old ORDER-BY-before-LIMIT did.
    try:
        rows = session.run(
            _bounded(
                """
                MATCH (start:Model {name: $mn, odoo_version: $v})
                WHERE NOT coalesce(start.unresolved, false)
                CALL {
                    WITH start
                    MATCH (start)-[:INHERITS|DELEGATES_TO]->(h1:Model {odoo_version: $v})
                    WHERE NOT coalesce(h1.unresolved, false)
                    RETURN collect(DISTINCT h1.name) AS hop1
                }
                CALL {
                    WITH hop1
                    UNWIND hop1 AS pn1
                    MATCH (:Model {name: pn1, odoo_version: $v})
                          -[:INHERITS|DELEGATES_TO]->(h2:Model {odoo_version: $v})
                    WHERE NOT coalesce(h2.unresolved, false)
                    RETURN collect(DISTINCT h2.name) AS hop2
                }
                CALL {
                    WITH hop2
                    UNWIND hop2 AS pn2
                    MATCH (:Model {name: pn2, odoo_version: $v})
                          -[:INHERITS|DELEGATES_TO]->(h3:Model {odoo_version: $v})
                    WHERE NOT coalesce(h3.unresolved, false)
                    RETURN collect(DISTINCT h3.name) AS hop3
                }
                WITH [n IN hop1 | {name: n, depth: 1}]
                     + [n IN hop2 | {name: n, depth: 2}]
                     + [n IN hop3 | {name: n, depth: 3}] AS tagged
                UNWIND tagged AS t
                WITH t.name AS pn, min(t.depth) AS depth
                WHERE pn <> $mn
                MATCH (f:Field {name: $fn, model: pn, odoo_version: $v})
                WHERE ($own IS NULL OR (size(f.profile) > 0
                       AND all(__p IN f.profile WHERE __p IN $own OR __p IN $shared)))
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
                """
                MATCH (f:Field {model: $mn, odoo_version: $v})
                WHERE ($own IS NULL OR (size(f.profile) > 0
                       AND all(__p IN f.profile WHERE __p IN $own OR __p IN $shared)))
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
                    WHERE ($own IS NULL OR (size(mth.profile) > 0
                           AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
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
            # Per-hop name-dedup over 5 INHERITS hops (issue #273): the old
            # `MATCH (c)-[:INHERITS*1..5]->(t)` anchored on all K duplicate
            # comodel nodes and enumerated the depth-5 mesh even in the common
            # MISMATCH case (no subtype → exhaustive negative search). This
            # collapses each hop to its DISTINCT ancestor names first, so the
            # working set stays <=16 names per hop.
            #
            # Predicates preserved (khaosat-273-orm.md §4.2): INHERITS only (no
            # DELEGATES_TO); odoo_version on every MATCH; unresolved filter on
            # the anchor AND every hop node; _scope_pred("c") on the anchor and
            # _scope_pred("t") on the re-bound target (intermediate hops carry
            # no scope, matching the original); pn <> $comodel excludes the
            # same-name mesh; LIMIT 1, no ORDER BY (existence check).
            try:
                rec = session.run(
                    _bounded(
                        f"""
                        MATCH (c:Model {{name: $comodel, odoo_version: $v}})
                        WHERE NOT coalesce(c.unresolved, false)
                          AND {_scope_pred("c")}
                        CALL {{
                            WITH c
                            MATCH (c)-[:INHERITS]->(h1:Model {{odoo_version: $v}})
                            WHERE NOT coalesce(h1.unresolved, false)
                            RETURN collect(DISTINCT h1.name) AS hop1
                        }}
                        CALL {{
                            WITH hop1
                            UNWIND hop1 AS pn1
                            MATCH (:Model {{name: pn1, odoo_version: $v}})
                                  -[:INHERITS]->(h2:Model {{odoo_version: $v}})
                            WHERE NOT coalesce(h2.unresolved, false)
                            RETURN collect(DISTINCT h2.name) AS hop2
                        }}
                        CALL {{
                            WITH hop2
                            UNWIND hop2 AS pn2
                            MATCH (:Model {{name: pn2, odoo_version: $v}})
                                  -[:INHERITS]->(h3:Model {{odoo_version: $v}})
                            WHERE NOT coalesce(h3.unresolved, false)
                            RETURN collect(DISTINCT h3.name) AS hop3
                        }}
                        CALL {{
                            WITH hop3
                            UNWIND hop3 AS pn3
                            MATCH (:Model {{name: pn3, odoo_version: $v}})
                                  -[:INHERITS]->(h4:Model {{odoo_version: $v}})
                            WHERE NOT coalesce(h4.unresolved, false)
                            RETURN collect(DISTINCT h4.name) AS hop4
                        }}
                        CALL {{
                            WITH hop4
                            UNWIND hop4 AS pn4
                            MATCH (:Model {{name: pn4, odoo_version: $v}})
                                  -[:INHERITS]->(h5:Model {{odoo_version: $v}})
                            WHERE NOT coalesce(h5.unresolved, false)
                            RETURN collect(DISTINCT h5.name) AS hop5
                        }}
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
