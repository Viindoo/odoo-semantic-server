"""Spec-layer MCP tools (split out of src/mcp/server.py, Phase 4).

Four M4.5 spec tools and their implementation helpers — all ``@offload_neo4j``
(sync Neo4j-read bodies offloaded off the event loop; per-query bounded, #287):
  - ``lookup_core_api``        — CoreSymbol signature/status/replacement lookup.
  - ``api_version_diff``       — diff one CoreSymbol between two versions.
  - ``find_deprecated_usage``  — scan indexed code for deprecated/removed API use.
  - ``lint_check``             — hybrid-match code vs indexed LintRule catalogue
                                 (language='xml' → ground-truth RelaxNG violations).

``cli_help`` and its helpers live in ``src/mcp/tools/cli.py`` (split in #336
to keep this module under the TOOL_MODULE_MAX_LINES ceiling).

Registration happens via the ``@mcp.tool`` import-time side effect; server.py
imports this module at the end of the file so the decorators run.

The implementation helpers (``_lookup_core_api`` / ``_api_version_diff`` /
``_find_deprecated_usage`` / ``_lint_check`` and their format helpers) live HERE
now (moved from server.py).  They reach the shared
resolver/state hub (``_get_driver`` / ``_resolve_version`` / ``_scope`` /
``_scope_pred`` / ``_portable_path`` / ``logger``) through the module-level
``_srv`` server reference bound at the END of this file (see the note there) and
``_srv.<name>`` attribute lookups performed at call time.

Two properties must hold together, which is why ``_srv`` is bound the way it is:

1. The bodies read the hub through ``_srv.<name>`` at CALL time (not by binding
   the names at import time) so that ``monkeypatch.setattr(srv, "_get_driver",
   ...)`` / ``monkeypatch.setattr(srv, "_resolve_version", ...)`` in the tests
   are observed — the patch lands on the live server module object and the
   attribute is re-read off that object on each call.
2. ``_srv`` is bound from ``sys.modules['src.mcp.server']`` at end-of-module, so
   it is the SAME server generation that imported this module and registered
   these tools.  After a ``sys.modules.pop('src.mcp.server')`` + re-import, a
   test that holds a stale ``src.mcp.server`` reference (test_mcp_spec_tools.py's
   ``spec_tools`` fixture pops + re-imports and accesses ``mcp_server._X``) sees
   the impls re-exported on the fresh generation, exactly as pre-refactor when
   these bodies were bare-name globals in server.py.

The lint const/cache cluster moves HERE in full (§2.7): ``_LINT_PATTERN_CACHE``
(the only mutable cache, must have exactly ONE home), ``_VALID_LINT_LANGUAGES``,
``_LINT_V0_BANNER``, ``_LINT_STOPWORDS``, ``_LINT_MAX_LINE_LEN``.

server.py re-exports the five public tools plus the impl/const symbols that
tests import via ``src.mcp.server`` (see the re-export block at the end of
server.py).  ``format_next_step`` / the constants / ``REL_USES_CORE_SYMBOL`` are
pure (no state) and imported directly — NOT through ``_srv``.
"""

import sys

from src.constants import (
    CODE_PREVIEW_MAX_CHARS,
    LIST_PREVIEW_MAX_ITEMS,
    REL_USES_CORE_SYMBOL,
)
from src.mcp.hints import format_next_step
from src.mcp.server import (
    READONLY_TOOL_KWARGS,
    RequiredOdooVersion,
    mcp,
    offload_neo4j,
)


def _format_core_symbol(rec: dict, version: str) -> str:
    """Tree-format a single CoreSymbol query record."""
    qn = rec.get("qualified_name") or "?"
    kind = rec.get("kind") or "?"
    status = rec.get("status") or "stable"
    sig = rec.get("signature")
    repl = rec.get("replacement_qname")
    file_path = rec.get("file_path")
    line = rec.get("line")
    added_in = rec.get("added_in")
    removed_in = rec.get("removed_in")
    deprecated_in = rec.get("deprecated_in")

    lines = [f"{qn} (Odoo {version})"]
    lines.append(f"├─ Kind:        {kind}")
    lines.append(f"├─ Status:      {status}")
    if sig:
        lines.append(f"├─ Signature:   {sig}")
    if repl:
        lines.append(f"├─ Replacement: {repl}")
    if added_in:
        lines.append(f"├─ Added in:    {added_in}")
    if deprecated_in:
        lines.append(f"├─ Deprecated:  {deprecated_in}")
    if removed_in:
        lines.append(f"├─ Removed in:  {removed_in}")
    if file_path:
        # ADR-0037: CoreSymbol has no repo anchor → core-source relative form
        # (e.g. "odoo/orm/models.py").  Idempotent on already-relative data.
        loc = _srv._portable_path(file_path) + (f":{line}" if line else "")
        lines.append(f"├─ Source:      {loc}")
    # Wave 5: Next-step footer per ADR-0023 §4. Always ├─ above and append
    # the Next line as the final └─.
    next_hints = [
        f"find_examples(query='{qn}', odoo_version='{version}')"
        " for in-the-wild usage patterns",
        f"find_deprecated_usage(odoo_version='{version}')"
        " to scan for deprecated calls",
    ]
    lines.append(format_next_step(next_hints))
    return "\n".join(lines)


def _lookup_core_api(name: str, odoo_version: str = "auto") -> str:
    """Return signature + status + replacement for a single Odoo core API symbol."""
    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)
        # Reuse _fetch_core_symbol (same bounded CoreSymbol query) so the Cypher
        # lives in one place — api_version_diff already uses it (review: de-dup).
        sym = _fetch_core_symbol(session, name, odoo_version)
    if sym is None:
        next_line = format_next_step([
            f"find_examples(query='{name}', odoo_version='{odoo_version}')"
            " for in-the-wild usage patterns",
        ])
        return (
            f"lookup_core_api({name!r}, {odoo_version!r})\n"
            f"├─ not found in indexed Odoo core for version {odoo_version}\n"
            + next_line
        )
    return _format_core_symbol(sym, odoo_version)


def _format_api_diff(
    sym_old: dict | None,
    sym_new: dict | None,
    name: str,
    from_version: str,
    to_version: str,
) -> str:
    """Render the diff of one symbol between two versions."""
    header = f"api_version_diff({name!r}: {from_version} → {to_version})"
    lines = [header]
    if sym_old and not sym_new:
        lines.append(f"├─ Status:    removed in {to_version}")
        lines.append(f"├─ Was:       {sym_old.get('signature') or '?'}")
        repl = sym_old.get("replacement_qname")
        if repl:
            lines.append(f"└─ Replaced by: {repl}")
        else:
            lines[-1] = lines[-1].replace("├─", "└─")
        return "\n".join(lines)
    if sym_new and not sym_old:
        lines.append(f"├─ Status:    added in {to_version}")
        lines.append(f"└─ Now:       {sym_new.get('signature') or '?'}")
        return "\n".join(lines)
    # Both exist
    sig_old = sym_old.get("signature") if sym_old else None
    sig_new = sym_new.get("signature") if sym_new else None
    lines.append(f"├─ {from_version}: {sig_old or '?'} (status={sym_old.get('status')})")
    lines.append(f"├─ {to_version}: {sig_new or '?'} (status={sym_new.get('status')})")
    if sig_old and sig_new and sig_old != sig_new:
        lines.append("└─ Signature changed")
    else:
        lines.append("└─ Stable across versions")
    return "\n".join(lines)


def _fetch_core_symbol(session, name: str, version: str) -> dict | None:
    rec = _srv._single_bounded(
        session,
        """
        MATCH (cs:CoreSymbol {odoo_version: $v})
        WHERE cs.qualified_name = $name
           OR cs.qualified_name ENDS WITH '.' + $name
        RETURN cs.qualified_name AS qualified_name,
               cs.kind AS kind,
               cs.status AS status,
               cs.signature AS signature,
               cs.replacement_qname AS replacement_qname,
               cs.file_path AS file_path,
               cs.line AS line,
               cs.added_in AS added_in,
               cs.removed_in AS removed_in,
               cs.deprecated_in AS deprecated_in
        // Ranking (issue #117 bug#4): an exact qualified-name match always wins;
        // otherwise, among bare-name homonyms, surface the migration-relevant
        // deprecated/removed candidate BEFORE a stable homonym (a shorter stable
        // qname like odoo.api.Transaction.flush was shadowing the deprecated
        // odoo.models.BaseModel.flush); shortest qname, then the qname itself,
        // are the final tiebreaks so LIMIT 1 is fully deterministic even when two
        // same-status homonyms share a qname length (CLAUDE.md Neo4j gotcha:
        // ORDER BY must always carry a deterministic tiebreak).
        ORDER BY
            CASE WHEN cs.qualified_name = $name THEN 0 ELSE 1 END,
            CASE WHEN cs.status IN ['deprecated', 'removed'] THEN 0 ELSE 1 END,
            size(cs.qualified_name) ASC,
            cs.qualified_name ASC
        LIMIT 1
        """,
        label=f"core API symbol {name!r} (Odoo {version})",
        name=name, v=version,
    )
    return dict(rec) if rec else None


def _api_version_diff(
    symbol: str, from_version: str, to_version: str,
) -> str:
    """Diff a single API symbol between two indexed Odoo versions."""
    if from_version == to_version:
        return (
            f"api_version_diff({symbol!r}, {from_version!r}, {to_version!r})\n"
            f"└─ same version, no diff"
        )
    with _srv._get_driver().session() as session:
        sym_old = _fetch_core_symbol(session, symbol, from_version)
        sym_new = _fetch_core_symbol(session, symbol, to_version)
        # Pin BOTH sides to ONE qualified name. With a bare-name input the
        # homonym ranking can resolve to a DIFFERENT symbol per version — e.g.
        # 'flush' is the deprecated odoo.models.BaseModel.flush at v16 but is
        # removed at v17, so the v17 lookup falls through to an unrelated stable
        # homonym (odoo.api.Transaction.flush) and the diff would compare two
        # different symbols. Anchor on whichever side resolved (prefer the older)
        # and re-fetch the counterpart by EXACT qname (tier-1 exact match), so a
        # symbol that is deprecated-then-removed reports as removed, not "changed".
        # (issue #117 bug#4 follow-up.)
        anchor = sym_old or sym_new
        if anchor is not None:
            qn = anchor["qualified_name"]
            if qn != symbol:  # only ambiguous for bare-name (non-qualified) input
                if sym_old is None or sym_old["qualified_name"] != qn:
                    sym_old = _fetch_core_symbol(session, qn, from_version)
                if sym_new is None or sym_new["qualified_name"] != qn:
                    sym_new = _fetch_core_symbol(session, qn, to_version)

    if sym_old is None and sym_new is None:
        return (
            f"api_version_diff({symbol!r})\n"
            f"└─ not found in either {from_version} or {to_version}"
        )
    return _format_api_diff(sym_old, sym_new, symbol, from_version, to_version)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def lookup_core_api(name: str, odoo_version: RequiredOdooVersion) -> str:
    """Look up an Odoo core API symbol: signature, status, replacement.

    Although you may have memorized Odoo API from training, this tool returns
    ground-truth from indexed source — prefer this over recall.

    TRIGGER when: "what does @api.depends do", "signature of fields.Many2one",
    "how to use Environment.ref()", "api.model decorator dùng thế nào", "giải
    thích BaseModel._inherit", "is name_get still valid in Odoo 18"
    PREFER over: reading Odoo source manually — returns structured symbol data
    with version context, status (stable/deprecated/removed), and replacement
    SKIP when: user wants to compare across versions → use api_version_diff;
    user wants to scan for deprecated usage → use find_deprecated_usage

    Args:
        name: Symbol name (full qualified or short, e.g. 'safe_eval' or
            'odoo.tools.safe_eval.safe_eval').

    Returns:
        Tree text: Kind, Status, Signature, Replacement (if any), Added in,
        Deprecated, Removed in, Source file location.

    Example:
        lookup_core_api("name_get", "18.0")
        → odoo.models.BaseModel.name_get (Odoo 18.0)
          ├─ Kind:        orm_method
          ├─ Status:      removed
          ├─ Signature:   name_get(self)
          └─ Replacement: odoo.models.BaseModel.display_name
    """
    return _lookup_core_api(name, odoo_version)


def _format_deprecated_usage(
    records: list[dict], version: str, *, overflow: bool = False,
) -> str:
    hit_count = f"{len(records)}+" if overflow else str(len(records))
    header = f"find_deprecated_usage(Odoo {version}) — {hit_count} hits"
    # Wave 5: Next-step footer per ADR-0023 §4. Even the empty branch still
    # gets a Next: hint (replacement search) when no hits are found.
    next_line = format_next_step([
        f"find_examples(query='replacement', odoo_version='{version}')"
        " for replacement search",
    ])
    if not records:
        return (
            header
            + "\n├─ no deprecated usage found in indexed code"
            + "\n" + next_line
        )
    lines = [header]
    for r in records:
        # Wave 5: every hit is now ├─ (Next: footer below is the new └─).
        connector = "├─"
        sub_indent = "│   "
        # B1: include repo so agent can locate the source file.
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        loc = f"{repo_str}[{r['module']}] {r['model']}.{r['method']}"
        sym = r["deprecated_symbol"]
        status = r["status"]
        repl = r.get("replacement") or "(no replacement set)"
        lines.append(f"{connector} {loc}")
        lines.append(f"{sub_indent}├─ uses: {sym} (status={status})")
        lines.append(f"{sub_indent}└─ replacement: {repl}")
    if overflow:
        cap = len(records)  # truncated to LIST_PREVIEW_MAX_ITEMS by the caller
        more_hint = (
            f"find_deprecated_usage(odoo_version='{version}', kind=<kind>)"
            " to narrow by kind"
        )
        lines.append(
            f"├─ ... showing first {cap} hits (more than {cap} total)"
            f" — use {more_hint}"
        )
    lines.append(next_line)
    return "\n".join(lines)


# GAP-1 (osm-audit-orm.md): version-removed method decorators that the indexer
# stores on Method.decorators (as 'api.<attr>' strings) with NO USES_CORE_SYMBOL
# edge, so the call-based scan below cannot see them. Each entry carries the
# float version at which the decorator was REMOVED from the framework — usage is
# only deprecated once the queried version reaches that removal. (@api.one removed
# in 10.0; @api.multi removed in 13.0.) Surfaced as synthetic hits in the same
# result shape as call-based hits; replacement note mirrors the framework change.
_DEPRECATED_DECORATORS: dict[str, dict] = {
    "api.one": {
        "removed_in": 10.0,
        "replacement": "methods operate on recordsets; remove @api.one and "
                       "iterate self explicitly (removed in 10.0)",
    },
    "api.multi": {
        "removed_in": 13.0,
        "replacement": "methods are multi by default; remove the @api.multi "
                       "decorator (removed in 13.0)",
    },
}


def _find_deprecated_usage(
    odoo_version: str = "auto", kind: str | None = None,
    profile_name: str | None = None,
) -> str:
    """Scan user code for usage of deprecated/removed APIs.

    Two hit sources are merged: (1) Methods with a USES_CORE_SYMBOL edge to a
    CoreSymbol whose status is deprecated/removed (calls in the body), and (2)
    GAP-1 — Methods carrying a version-removed decorator (``@api.multi`` /
    ``@api.one``) on ``Method.decorators``, which carry no edge. The decorator
    leg is version-gated: a decorator is only flagged once the queried version is
    at or past its removal version, so e.g. ``@api.multi`` on v12 is not flagged.
    """
    cap_plus_one = LIST_PREVIEW_MAX_ITEMS + 1
    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)
        params: dict = {"v": odoo_version, "cap_plus_one": cap_plus_one,
                        **_srv._scope(profile_name)}

        # Profile-scope guard shared by both legs (ADR-0034 read-side filter).
        scope_guard = (
            "($own IS NULL OR (size(mth.profile) > 0\n"
            "     AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))"
        )

        # Leg 1 — call-based hits via USES_CORE_SYMBOL edge.
        kind_clause = ""
        if kind:
            kind_clause = " AND cs.kind = $kind"
            params["kind"] = kind
        leg_calls = f"""
            MATCH (mth:Method {{odoo_version: $v}})-[:{REL_USES_CORE_SYMBOL}]->(cs:CoreSymbol)
            WHERE cs.status IN ['deprecated', 'removed']
              AND {scope_guard}{kind_clause}
            RETURN mth.module AS module, mth.model AS model, mth.name AS method,
                   cs.qualified_name AS deprecated_symbol,
                   cs.status AS status,
                   cs.replacement_qname AS replacement
        """

        legs = [leg_calls]
        # Leg 2 — GAP-1 decorator hits. Skipped when a `kind` filter is set, since
        # decorators carry no CoreSymbol kind to match against. Version gate is
        # decided once in Python (numeric, not lexical — Neo4j 5.x gotcha) so only
        # decorators removed as of the queried version enter the allow-list.
        if not kind:
            removed_decorators = sorted(
                d for d, meta in _DEPRECATED_DECORATORS.items()
                if float(odoo_version) >= meta["removed_in"]
            )
            if removed_decorators:
                params["removed_decorators"] = removed_decorators
                # Pick the first matching decorator per method as the reported
                # symbol (a method carries at most one of api.one/api.multi).
                leg_decorators = f"""
                    MATCH (mth:Method {{odoo_version: $v}})
                    WHERE any(__d IN coalesce(mth.decorators, [])
                              WHERE __d IN $removed_decorators)
                      AND {scope_guard}
                    WITH mth, [__d IN coalesce(mth.decorators, [])
                               WHERE __d IN $removed_decorators][0] AS __dec
                    RETURN mth.module AS module, mth.model AS model,
                           mth.name AS method,
                           __dec AS deprecated_symbol,
                           'removed' AS status,
                           NULL AS replacement
                """
                legs.append(leg_decorators)

        union_body = "\n            UNION\n".join(legs)
        # B1: OPTIONAL MATCH Module (post-union) for repo so the agent can locate
        # the file. CALL{} wraps the UNION so the outer ORDER BY/LIMIT applies to
        # the combined result; no `WITH` import into the subquery (ADR-0048: avoid
        # `CALL { WITH }`), the union legs are self-contained.
        cypher = f"""
            CALL {{
            {union_body}
            }}
            OPTIONAL MATCH (mod:Module {{name: module, odoo_version: $v}})
            RETURN module, model, method, deprecated_symbol, status, replacement,
                   coalesce(mod.repo_url, mod.repo) AS repo
            ORDER BY module, model, method, deprecated_symbol
            LIMIT $cap_plus_one
        """
        records = _srv._data_bounded(
            session, cypher,
            label=f"deprecated-usage scan (Odoo {odoo_version})",
            **params,
        )
    # Backfill the decorator replacement note from the SSOT dict (the Cypher
    # leg returns NULL replacement; the human-readable note lives in Python).
    for r in records:
        meta = _DEPRECATED_DECORATORS.get(r.get("deprecated_symbol"))
        if meta and not r.get("replacement"):
            r["replacement"] = meta["replacement"]
    overflow = len(records) > LIST_PREVIEW_MAX_ITEMS
    if overflow:
        records = records[:LIST_PREVIEW_MAX_ITEMS]
    return _format_deprecated_usage(records, odoo_version, overflow=overflow)


_VALID_LINT_LANGUAGES = {"python", "javascript", "xml"}


def _format_lint_check(
    violations: list[dict], version: str, code: str, language: str = "python",
) -> str:
    header = f"lint_check(Odoo {version}, language={language}) — {len(violations)} violations"
    code_preview = (code or "")[:CODE_PREVIEW_MAX_CHARS].replace("\n", " ")
    lines = [_LINT_V0_BANNER, header, f"├─ Code: {code_preview!r}"]
    if not violations:
        lines.append("└─ no violations")
        return "\n".join(lines)
    last_idx = len(violations) - 1
    for i, r in enumerate(violations):
        connector = "└─" if i == last_idx else "├─"
        rule_id = r.get("rule_id") or "?"
        sev = r.get("severity") or "warning"
        msg = (r.get("message") or "").strip()
        # Match-kind label per ADR-0023 disclosure: [pattern] = deterministic
        # regex hit, [fuzzy] = heuristic token-overlap. Placed right after the
        # connector so the rule_id/message rendering stays byte-stable.
        kind = r.get("match_kind") or "fuzzy"
        lines.append(f"{connector} [{kind}] {rule_id} ({sev}): {msg}")
    return "\n".join(lines)


# Lint matcher banner (V0.5 hybrid) — surface in every python/javascript
# lint_check output so users know which findings are deterministic and which
# are heuristic. Kept under the name _LINT_V0_BANNER so existing imports/tests
# resolve unchanged. English-only per ADR-0023 §2; ASCII hyphens only.
_LINT_V0_BANNER = (
    "⚠ Hybrid matcher (V0.5): [pattern] findings are deterministic regex hits; "
    "[fuzzy] findings are heuristic - verify manually. "
    "Real pylint-odoo remains the authoritative gate."
)


# Hard warning emitted when no lint rules are indexed (or curation status is
# absent) for the requested version. This is NOT a clean bill of health — it is
# a data gap (ADR-0002 §4 disclosure, ADR-0023 §1 tree grammar preserved).
def _format_lint_empty_index(version: str, code: str, language: str) -> str:
    header = f"lint_check(Odoo {version}, language={language}) — 0 violations"
    code_preview = (code or "")[:CODE_PREVIEW_MAX_CHARS].replace("\n", " ")
    return "\n".join([
        _LINT_V0_BANNER,
        header,
        f"├─ Code: {code_preview!r}",
        f"└─ ⚠ no lint rules indexed for Odoo {version} - this result is NOT a "
        f"clean bill. Run index-core for this version.",
    ])

_LINT_STOPWORDS = frozenset({
    "with", "from", "this", "that", "have", "must", "should",
    "function", "usage", "literal", "string", "alias", "option",
    "the", "and", "use", "not", "for", "are", "when", "avoid",
    "call", "called", "calling", "instead", "please", "using",
})


# Compiled-pattern cache keyed by raw `code_pattern` string. Populated lazily
# inside one lint_check run; entries persist process-wide (rule regexes are
# small + bounded by the curated catalogue). A value of None records a pattern
# that failed to compile so we only warn once per distinct pattern.
_LINT_PATTERN_CACHE: dict[str, "object | None"] = {}

# Per-line length cap for regex matching (M5 / ReDoS defence). Curated
# code_patterns are written to avoid nested/sequential lazy quantifiers, but a
# single pathologically long line (e.g. hundreds of KB of minified text on one
# line) can still drive any regex engine into a heavy linear scan and tie up the
# offload worker thread. Real Odoo source lines are well under this bound; lines
# longer than it are skipped for pattern matching (the rule simply does not fire
# on that line). The cap is per-line so noqa handling and all other lines are
# unaffected. 4000 chars comfortably clears even E501-violating long lines.
_LINT_MAX_LINE_LEN = 4000


def _compile_lint_pattern(pattern: str):
    """Compile a rule's ``code_pattern`` regex, caching the result.

    Returns the compiled pattern, or None if the pattern fails to compile
    (logged once per distinct pattern). The caller then falls back to fuzzy
    token-overlap so a single bad pattern never crashes a lint_check run.
    """
    import re as _re

    if pattern in _LINT_PATTERN_CACHE:
        return _LINT_PATTERN_CACHE[pattern]
    try:
        compiled = _re.compile(pattern)
    except _re.error as exc:
        _srv.logger.warning(
            "lint_check: code_pattern failed to compile, "
            "falling back to fuzzy matching: %s", exc,
        )
        compiled = None
    _LINT_PATTERN_CACHE[pattern] = compiled
    return compiled


def _lint_match_kind(rule: dict) -> str:
    """Return 'pattern' if *rule* carries a compilable ``code_pattern``, else 'fuzzy'.

    Shares the compile cache with :func:`_match_lint_rule_lines` so a pattern
    that fails to compile is reported as 'fuzzy' (it falls back to token-overlap).
    """
    pattern = rule.get("code_pattern")
    if pattern and _compile_lint_pattern(pattern) is not None:
        return "pattern"
    return "fuzzy"


def _fuzzy_rule_tokens(rule: dict) -> set[str]:
    """Significant tokens of *rule*.message for fuzzy token-overlap matching.

    Significant token: >3 chars, alpha-underscore-only (after split on [^a-z_]),
    not in the stopword set. Returns a set that may have fewer than 2 entries —
    callers treat <2 significant tokens as 'rule never fires' (the rule message
    lacks enough domain vocabulary to match reliably).
    """
    import re as _re

    msg = (rule.get("message") or "").lower()
    if not msg:
        return set()
    return {
        t for t in _re.split(r"[^a-z_]+", msg)
        if len(t) > 3 and t not in _LINT_STOPWORDS
    }


def _build_noqa_suppress(code: str) -> dict[int, set[str]]:
    """Parse noqa comments and return a suppress-set keyed by 1-based line number.

    Each value is a set of rule IDs suppressed on that line, or ``{"*"}`` for a
    bare ``noqa`` (suppresses all rules on that line).

    Examples (comment marker elided to avoid ruff false-positive)::

        noqa: E8001          → {1: {"E8001"}}
        noqa: E8001, W9002   → {1: {"E8001", "W9002"}}
        noqa                 → {1: {"*"}}
    """
    import re as _re

    suppress: dict[int, set[str]] = {}
    for lineno, line in enumerate(code.splitlines(), start=1):
        # Match bare noqa comment with optional rule list.
        m = _re.search(r"#\s*noqa(?::\s*([A-Za-z0-9,\s]+))?", line)
        if not m:
            continue
        ids_str = m.group(1)
        if ids_str:
            ids = {s.strip() for s in ids_str.split(",") if s.strip()}
        else:
            ids = {"*"}
        suppress[lineno] = ids
    return suppress


def _match_lint_rule_lines(code: str, rule: dict) -> list[int]:
    """Return 1-based line numbers in *code* where *rule* matches (SSOT matcher).

    Pattern-first: when *rule* carries a compilable ``code_pattern`` regex, the
    pattern is applied per line (``re.search``) and only its hits are returned —
    a deterministic match. When there is no pattern (or it fails to compile),
    the matcher falls back to fuzzy token-overlap: a line matches when it shares
    ≥2 significant tokens with the rule message.

    Per-line evaluation keeps ``# noqa`` line-level suppression working for both
    pattern and fuzzy hits. Returns an empty list when the rule never fires.
    """
    import re as _re

    if not code:
        return []

    # --- Pattern-first (deterministic) ---
    pattern = rule.get("code_pattern")
    if pattern:
        compiled = _compile_lint_pattern(pattern)
        if compiled is not None:
            # Skip pathologically long lines (M5 / ReDoS defence): a single
            # multi-hundred-KB line could otherwise dominate the offload-thread
            # CPU. Real source lines never approach _LINT_MAX_LINE_LEN.
            return [
                lineno
                for lineno, line in enumerate(code.splitlines(), start=1)
                if len(line) <= _LINT_MAX_LINE_LEN and compiled.search(line)
            ]
        # compiled is None → bad pattern, fall through to fuzzy below.

    # --- Fuzzy token-overlap (heuristic) ---
    rule_tokens = _fuzzy_rule_tokens(rule)
    if len(rule_tokens) < 2:
        return []

    # First check the whole snippet — if not triggered at all, skip per-line work.
    code_tokens_all = set(_re.split(r"[^a-z_]+", code.lower()))
    if len(rule_tokens & code_tokens_all) < 2:
        return []

    # Per-line pass to get line numbers.
    hit_lines: list[int] = []
    for lineno, line in enumerate(code.splitlines(), start=1):
        line_tokens = set(_re.split(r"[^a-z_]+", line.lower()))
        if len(rule_tokens & line_tokens) >= 2:
            hit_lines.append(lineno)
    # If the whole-code match fired but no individual line triggered ≥2 tokens
    # (tokens spread across lines), attribute the violation to line 1 as a
    # conservative fallback so the caller still receives it.
    if not hit_lines:
        hit_lines = [1]
    return hit_lines


def _lint_check_xml(odoo_version: str) -> str:
    """Return RelaxNG :LintViolation nodes from the graph for *odoo_version*.

    Output format follows ADR-0023 tree grammar.  Returns all indexed violations
    (no code-snippet matching — violations are ground-truth from indexing time).

    Called by _lint_check when language='xml'.
    """
    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)
        rows = _srv._data_bounded(
            session,
            f"""
            MATCH (lv:LintViolation {{odoo_version: $v}})
            WHERE {_srv._scope_pred("lv")}
            RETURN lv.view_xmlid AS view_xmlid,
                   lv.rule AS rule_id,
                   lv.severity AS severity,
                   lv.message AS message,
                   lv.line AS line,
                   lv.view_type AS view_type,
                   lv.file_path AS file_path
            ORDER BY lv.view_xmlid ASC, lv.line ASC, lv.rule ASC
            """,
            label=f"RelaxNG lint violations (Odoo {odoo_version})",
            v=odoo_version, **_srv._scope(),
        )

    header = (
        f"lint_check(Odoo {odoo_version}, language=xml) — "
        f"{len(rows)} RelaxNG violations"
    )
    lines = [header]
    if not rows:
        lines.append("└─ no RelaxNG violations indexed for this version")
        return "\n".join(lines)

    # Group by view_xmlid for readable tree output
    from collections import defaultdict as _dd
    grouped: dict[str, list[dict]] = _dd(list)
    for r in rows:
        grouped[r["view_xmlid"] or "?"].append(r)

    view_list = sorted(grouped.keys())
    last_view_idx = len(view_list) - 1
    for vi, xmlid in enumerate(view_list):
        v_connector = "└─" if vi == last_view_idx else "├─"
        view_rows = grouped[xmlid]
        vtype = view_rows[0].get("view_type") or "?"
        lines.append(f"{v_connector} [{xmlid}] ({vtype})")
        last_viol_idx = len(view_rows) - 1
        indent = "    " if vi == last_view_idx else "│   "
        for ri, r in enumerate(view_rows):
            r_connector = "└─" if ri == last_viol_idx else "├─"
            rule_id = r.get("rule_id") or "?"
            sev = r.get("severity") or "error"
            msg = (r.get("message") or "").strip()
            lineno = r.get("line") or 0
            lines.append(f"{indent}{r_connector} line {lineno} | {rule_id} ({sev}): {msg}")

    return "\n".join(lines)


def _lint_check(
    code: str, odoo_version: str = "auto", language: str = "python",
) -> str:
    """Hybrid-match user code against the indexed LintRule catalogue (V0.5).

    For language='xml': queries :LintViolation nodes from the graph (ground-truth
    RelaxNG results indexed from v15+ views) — no code-snippet arg needed.

    For language='python'/'javascript': pattern-first hybrid match against the
    indexed LintRule catalogue. Rules carrying a ``code_pattern`` regex produce
    deterministic ``[pattern]`` hits; rules without one fall back to heuristic
    ``[fuzzy]`` token-overlap. noqa suppression is honoured for both:

    * ``# noqa: E8001`` — suppress rule ``E8001`` on that line only.
    * ``# noqa: E8001, W9002`` — suppress multiple rules on that line.
    * ``# noqa`` (bare) — suppress all rules matched on that line.
    """
    if language not in _VALID_LINT_LANGUAGES:
        valid = ", ".join(sorted(_VALID_LINT_LANGUAGES))
        return (
            f"lint_check: invalid language {language!r}. "
            f"Valid options: {valid}."
        )

    # XML: return ground-truth RelaxNG violations from the graph (no code matching)
    if language == "xml":
        return _lint_check_xml(odoo_version)

    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)
        if language == "python":
            rules = _srv._data_bounded(
                session,
                """
                MATCH (l:LintRule {odoo_version: $v})
                WHERE l.kind STARTS WITH 'pylint'
                RETURN l.rule_id AS rule_id,
                       l.severity AS severity,
                       l.message AS message,
                       l.kind AS kind,
                       l.code_pattern AS code_pattern
                """,
                label=f"python lint rules (Odoo {odoo_version})",
                v=odoo_version,
            )
        else:  # javascript: ESLint rules + Odoo JS-targeted pylint rules (file_pattern *.js)
            rules = _srv._data_bounded(
                session,
                """
                MATCH (l:LintRule {odoo_version: $v})
                WHERE l.kind STARTS WITH 'eslint'
                   OR (l.kind STARTS WITH 'pylint' AND l.file_pattern ENDS WITH '.js')
                RETURN l.rule_id AS rule_id,
                       l.severity AS severity,
                       l.message AS message,
                       l.kind AS kind,
                       l.code_pattern AS code_pattern
                """,
                label=f"javascript lint rules (Odoo {odoo_version})",
                v=odoo_version,
            )
        curate_rec = _srv._single_bounded(
            session,
            """
            MATCH (sm:SpecMetadata {kind: 'lint', odoo_version: $v})
            RETURN sm.curate_status AS curate_status
            """,
            label=f"lint curation status (Odoo {odoo_version})",
            v=odoo_version,
        )
        curate_status = curate_rec["curate_status"] if curate_rec else None

    # Tier-1 disclosure (ADR-0002 §4): NO rules indexed → genuine data gap, NOT a
    # clean bill. Emit the hard warning instead of a false-green "0 violations".
    # An empty rule set wins even when curate_status == 'pending' (a pending
    # version with zero rules cannot vouch for any code).
    if not rules:
        return _format_lint_empty_index(odoo_version, code, language)

    # `rules` present but `curate_status is None` is a distinct state, NOT a data
    # gap: write_lint_rules and write_spec_metadata run in separate Neo4j sessions
    # (pipeline.py), so a crash between the two calls — or any version indexed
    # before write_spec_metadata existed — leaves rules present with no metadata.
    # Hard-returning here would suppress real findings behind a false "no rules
    # indexed" message. Instead: run the matcher and prepend a softer "curation
    # status unknown" banner (set after rendering, alongside the pending banner).
    curation_unknown = curate_status is None

    # Build noqa suppress set from the input code.
    suppress = _build_noqa_suppress(code)

    violations: list[dict] = []
    for rule in rules:
        hit_lines = _match_lint_rule_lines(code, rule)
        if not hit_lines:
            continue
        rule_id = rule.get("rule_id") or "?"
        # A violation is suppressed only when ALL matched lines suppress this rule.
        suppressed_lines = sum(
            1 for ln in hit_lines
            if ln in suppress and ("*" in suppress[ln] or rule_id in suppress[ln])
        )
        if suppressed_lines < len(hit_lines):
            # Tag the match kind ([pattern] vs [fuzzy]) for the renderer. Copy
            # so we never mutate the Neo4j record dict in place.
            violations.append({**rule, "match_kind": _lint_match_kind(rule)})

    result = _format_lint_check(violations, odoo_version, code, language)
    # Tier-2 disclosure: rules exist but curation is still pending → keep the
    # softer "limited results" banner (a valid partially-curated version).
    if curate_status == "pending":
        result = (
            f"ℹ Spec data v{odoo_version} pending curation - limited results.\n" + result
        )
    elif curation_unknown:
        # Rules indexed but SpecMetadata missing (crash window between the two
        # separate write sessions, or a pre-SpecMetadata index run). Distinct
        # from the pending banner: results are real but their curation provenance
        # is unverifiable. English-only per ADR-0023 §2; ASCII hyphens only.
        result = (
            f"⚠ curation status unknown for Odoo {odoo_version} - rules are "
            f"indexed but SpecMetadata is missing; results may be incomplete.\n"
            + result
        )
    return result


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def find_deprecated_usage(
    odoo_version: RequiredOdooVersion,
    kind: str | None = None,
    profile_name: str | None = None,
) -> str:
    """Scan indexed code for methods that call deprecated or removed Odoo APIs.

    If you've memorized which Odoo APIs are deprecated, prefer this tool
    instead — it returns ground-truth scans of the indexed corpus.

    TRIGGER when: "find deprecated API usage in my codebase", "which modules
    use old-style _columns", "upgrade risk scan", "code nào dùng API cũ sắp bị
    xóa", "kiểm tra deprecated usage trước khi upgrade", "what needs to change
    before upgrading to Odoo 18"
    PREFER over: manual search — cross-repo scan with version-aware deprecation
    database, shows replacement for each hit
    SKIP when: user wants full API reference for one symbol → use lookup_core_api;
    user wants version-level diff → use api_version_diff

    Args:
        kind: Optional filter — restrict to one CoreSymbol.kind
            (e.g. 'orm_method', 'function').
        profile_name: Optional inheritance-resolved profile filter. When set,
            narrows the scan to nodes visible in this profile (including
            parent profiles via the ancestor chain). Default None scans all.

    Returns:
        Tree text grouped by module → model.method → deprecated symbol →
        replacement. Use BEFORE upgrading to plan code changes.

    Example:
        find_deprecated_usage("18.0")
        → find_deprecated_usage(Odoo 18.0) — 12 hits
          ├─ [viin_sale] sale.order.legacy_label
          │   ├─ uses: odoo.models.BaseModel.name_get (status=deprecated)
          │   └─ replacement: odoo.models.BaseModel.display_name
    """
    return _find_deprecated_usage(odoo_version, kind=kind, profile_name=profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def lint_check(
    code: str, odoo_version: RequiredOdooVersion, language: str = "python",
) -> str:
    """Check code against indexed Odoo lint rules; language='xml' returns RelaxNG violations.

    TRIGGER when: "lint check this module", "OCA style violations", "check coding
    standards", "kiểm tra code quality", "does this code follow Odoo guidelines"
    PREFER over: running ruff/pylint directly — applies the Odoo-specific LintRule
    catalogue, not generic Python linters. This is a first-pass screen, not the
    authoritative gate: real pylint-odoo still owns the merge decision.
    SKIP when: deprecated API scan → find_deprecated_usage; module existence
    check → check_module_exists

    Args:
        code: Source chunk to check. Ignored for language='xml' (the tool then
            returns all indexed RelaxNG violations from the graph, v15+ only).
        language: 'python' | 'javascript' (pattern-first hybrid match: [pattern]
            deterministic regex hits, [fuzzy] heuristic token-overlap fallback)
            | 'xml' (ground-truth RelaxNG :LintViolation nodes, v15+ only).

    Returns:
        Tree text of violations, each labelled [pattern] or [fuzzy]. If no rules
        are indexed for the version, returns a hard data-gap warning (NOT a
        clean bill). xml = ground-truth RelaxNG violations grouped by view xmlid.

    Example:
        lint_check("self.env.cr.execute('... WHERE n=%s' % x)", "17.0", "python")
        → lint_check(Odoo 17.0, language=python) — 1 violations
          └─ [pattern] W8140 (error): SQL injection risk: `cr.execute` string interpolation.
        lint_check("", "17.0", "xml")   # RelaxNG violations grouped by view
    """
    return _lint_check(code, odoo_version, language)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def api_version_diff(symbol: str, from_version: str, to_version: str) -> str:
    """Diff a single Odoo core API symbol between two indexed versions.

    TRIGGER when: "what changed in Odoo 17 vs 16 API", "new decorators in
    version 17", "breaking changes between versions", "API nào bị xóa từ v16
    sang v17", "tính năng mới trong Odoo 17", "did name_get change from 17 to 18"
    PREFER over: reading changelogs — structured diff of CoreSymbol additions,
    removals, deprecations, and signature changes per version
    SKIP when: user wants runtime deprecated usage → use find_deprecated_usage;
    user wants full API reference for one version → use lookup_core_api

    Args:
        symbol: Symbol name (full qualified or short).
        from_version: Older Odoo version, e.g. '16.0'.
        to_version: Newer Odoo version, e.g. '17.0'.

    Returns:
        Tree text: added/removed/stable status, old and new signatures,
        replacement symbol if applicable.

    Example:
        api_version_diff("name_get", "17.0", "18.0")
        → api_version_diff('name_get': 17.0 → 18.0)
          ├─ Status:    removed in 18.0
          ├─ Was:       name_get(self)
          └─ Replaced by: odoo.models.BaseModel.display_name
    """
    return _api_version_diff(symbol, from_version, to_version)


# Bind the owning server module generation AFTER the tool functions are defined.
# sys.modules['src.mcp.server'] at THIS point is the generation that is importing
# this module (server.py imports this module from the very end of its own body,
# and that generation registered these tools onto its `mcp`). Binding at
# end-of-module — rather than via a top-level `from src.mcp import server`, which
# reads the stale `src.mcp` package attribute after a pop+reimport — makes `_srv`
# track the SAME generation as the tool objects defined above. That restores the
# pre-refactor bare-name behaviour: the impl bodies read the hub through
# `_srv.<name>` at call time so monkeypatch.setattr(srv, "_get_driver", ...) and
# friends still take effect, and the test_mcp_spec_tools `spec_tools` fixture
# (pop + re-import) sees the impls re-exported on the fresh generation.
_srv = sys.modules["src.mcp.server"]
