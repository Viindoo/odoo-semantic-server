# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/orm_validators.py
"""ORM-validation tool implementations (split out of src/mcp/orm.py, B2 refactor).

The impls behind the four ``@mcp.tool`` ORM-validation wrappers (registered in
``src/mcp/tools/orm_tools.py``):

- ``_resolve_orm_chain``  — walk a dotted field path, return terminal type.
- ``_validate_domain``    — check each domain term's field-path + operator.
- ``_validate_depends``   — check ``@api.depends`` dependency paths.
- ``_validate_relation``  — assert a field points at an expected comodel.

Plus the small render/parse helpers ``_parse_domain`` / ``_suggest`` /
``_broken_reason_text``. All four reuse the ``_traverse_field_chain`` primitive
(now in ``src/mcp/orm_queries.py``).

This module is a PURE STRUCTURAL extraction — no behavior change. Late imports of
``src.mcp.server`` avoid a circular dependency (server imports orm at module
level), mirroring the original ``src/mcp/orm.py`` and ``src/mcp/inspect.py``.
``src/mcp/orm.py`` re-exports every public name below so callers keep importing
them via ``src.mcp.orm`` unchanged. See docs/adr/0023 (tree-grammar contract) and
TASKS.md M10.5 Phase 2.
"""
import ast
import difflib

from neo4j.exceptions import ClientError

from src.constants import (
    NEO4J_QUERY_TIMEOUT_SECONDS,
    RELATIONAL_TTYPES,
    valid_domain_operators,
)
from src.mcp.hints import hints_for
from src.mcp.orm import (
    OrmQueryTimeout,
    _bounded,
    _is_tx_timeout,
    _relation_timeout,
    _scope,
    _scope_pred,
)

# NOTE: the query-helper primitives ``_traverse_field_chain`` / ``_lookup_field``
# / ``_field_names_on_model`` live in ``src.mcp.orm_queries`` and are imported
# LAZILY inside each validator body (right next to the existing ``import server
# as srv`` lazy import), NOT at module top. Reason: ``src.mcp.orm`` is both the
# shared bottom layer AND the facade that re-exports both children, so a module-
# top ``from src.mcp.orm_queries import ...`` here would close a child-to-child
# import cycle (orm -> orm_validators -> orm_queries -> orm) that breaks a cold
# ``import src.mcp.orm_queries`` as the entry point. These helpers are only
# called at request time, so a function-local import is behavior-neutral.


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
    from src.mcp.orm_queries import _traverse_field_chain

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
    from src.mcp.orm_queries import _field_names_on_model, _traverse_field_chain

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
    from src.mcp.orm_queries import _field_names_on_model, _traverse_field_chain

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
    from src.mcp.orm_queries import _field_names_on_model, _lookup_field

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
