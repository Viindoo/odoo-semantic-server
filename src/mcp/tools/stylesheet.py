"""Stylesheet MCP tools (split out of src/mcp/server.py, Phase 2).

Two M10A tools and their implementation helpers:
  - ``resolve_stylesheet`` (sync, ``@offload_neo4j`` — per-query bounded +
    clean-string-on-timeout, #287) — enumerate a module's :Stylesheet nodes +
    their @import chain.
  - ``find_style_override`` (``async def``, no offload — embeds async per
    ADR-0046) — literal-first / ANN search for a selector or variable across
    css/scss/less chunks + the :IMPORTS override chain.

Registration happens via the ``@mcp.tool`` import-time side effect; server.py
imports this module at the end of the file so the decorators run.

The implementation helpers (``_resolve_stylesheet``, ``_literal_style_lookup``,
``_find_style_override``) live HERE now (moved from server.py).  They reach the
shared resolver/state hub (``_get_driver`` / ``_resolve_version`` / ``_scope`` /
``_get_embedder`` / ``_find_style_override`` / ...) through the module-level
``_srv`` server reference bound at the END of this file (see the note there) and
``_srv.<name>`` attribute lookups performed at call time.

Two properties must hold together, which is why ``_srv`` is bound the way it is:

1. The bodies read the hub through ``_srv.<name>`` at CALL time (not by binding
   the names at import time) so that ``monkeypatch.setattr(srv, "_get_embedder",
   ...)`` / ``monkeypatch.setattr(srv, "_find_style_override", ...)`` in the
   tests are observed — the patch lands on the live server module object and the
   attribute is re-read off that object on each call.
2. ``_srv`` is bound from ``sys.modules['src.mcp.server']`` at end-of-module, so
   it is the SAME server generation that imported this module and registered
   these tools.  After a ``sys.modules.pop('src.mcp.server')`` + re-import, a
   test that holds a stale top-level ``srv`` binding (test_mcp_anti_freeze.py)
   calls the stale-generation tool object, whose ``_srv`` points back at that
   same stale generation — so its monkeypatch is still observed, exactly as it
   was pre-refactor when these bodies used bare-name globals in server.py.

server.py re-exports ``resolve_stylesheet`` / ``find_style_override`` (public
tools) plus ``_resolve_stylesheet`` / ``_find_style_override`` /
``_literal_style_lookup`` (impl helpers imported by tests via ``src.mcp.server``,
and ``_literal_style_lookup`` is also called by ``_find_examples`` still in the
hub).
"""

import asyncio
import sys
from contextlib import nullcontext

from src.constants import FIND_EXAMPLES_ANN_LIMIT, STYLE_CHUNK_TYPES
from src.mcp.hints import hints_for
from src.mcp.server import (
    READONLY_TOOL_KWARGS,
    RequiredOdooVersion,
    mcp,
    offload_neo4j,
)
from src.mcp.style_literal import ilike_pattern, is_literal_token, literal_column


def _resolve_stylesheet(
    module: str,
    odoo_version: str = "auto",
) -> str:
    """Impl for resolve_stylesheet tool — no FastMCP wrapper overhead.

    Returns a tree listing all :Stylesheet nodes for *module* at *odoo_version*
    with their language, stat counters, and BFS :IMPORTS chain.
    """
    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        rows = _srv._data_bounded(
            session,
            f"""
            MATCH (ss:Stylesheet {{module: $mod, odoo_version: $v}})
            WHERE {_srv._scope_pred("ss")}
            RETURN ss.file_path AS file_path,
                   ss.language AS language,
                   ss.selector_count AS selector_count,
                   ss.variable_count AS variable_count,
                   ss.import_count AS import_count,
                   ss.mixin_count AS mixin_count
            ORDER BY ss.file_path ASC
            """,
            label=f"stylesheet list for {module!r} (Odoo {odoo_version})",
            mod=module, v=odoo_version, **_srv._scope(),
        )

    if not rows:
        footer = hints_for("resolve_stylesheet", name=module, ver=odoo_version)
        recovery = (
            "Recovery: describe_module(name=..., odoo_version=...) to verify module exists."
        )
        if footer:
            # footer ends with └─ Next: — use ├─ for Recovery so └─ stays last.
            lines = [
                f"resolve_stylesheet({module!r}, {odoo_version!r})",
                f"├─ not found — no Stylesheet nodes indexed for module '{module}'.",
                f"├─ {recovery}",
                footer,
            ]
        else:
            lines = [
                f"resolve_stylesheet({module!r}, {odoo_version!r})",
                f"├─ not found — no Stylesheet nodes indexed for module '{module}'.",
                f"└─ {recovery}",
            ]
        return "\n".join(lines)

    # I5: Batch-query all :IMPORTS edges in ONE session before the render loop
    # (avoids N+1 sessions — one session per row that had imp > 0).
    # Pattern: UNWIND file paths → collect imports per source, return as map.
    fps_with_imports = [r["file_path"] for r in rows if (r["import_count"] or 0) > 0]
    imports_by_fp: dict[str, list[dict]] = {}
    if fps_with_imports:
        with _srv._get_driver().session() as session:
            batch_rows = _srv._data_bounded(
                session,
                f"""
                UNWIND $fps AS fp
                MATCH (src:Stylesheet {{file_path: fp, module: $mod, odoo_version: $v}})
                      -[:IMPORTS]->(tgt:Stylesheet)
                WHERE {_srv._scope_pred("src")} AND {_srv._scope_pred("tgt")}
                RETURN fp, tgt.file_path AS import_path, tgt.module AS import_module
                ORDER BY fp ASC, tgt.file_path ASC
                """,
                label=f"stylesheet import chain for {module!r} (Odoo {odoo_version})",
                fps=fps_with_imports, mod=module, v=odoo_version, **_srv._scope(),
            )
        for br in batch_rows:
            imports_by_fp.setdefault(br["fp"], []).append(
                {"import_path": br["import_path"], "import_module": br["import_module"]}
            )

    header = f"resolve_stylesheet({module!r}, {odoo_version!r})"
    lines = [header, f"├─ Stylesheets: {len(rows)} file(s)"]

    for idx, row in enumerate(rows):
        is_last_row = idx == len(rows) - 1
        row_prefix = "└─" if is_last_row else "├─"
        fp = row["file_path"]
        lang = row["language"] or "css"
        sel = row["selector_count"] or 0
        var = row["variable_count"] or 0
        imp = row["import_count"] or 0
        mix = row["mixin_count"] or 0

        # Stat summary line
        stats_parts = [f"lang={lang}", f"selectors={sel}", f"vars={var}"]
        if mix:
            stats_parts.append(f"mixins={mix}")
        if imp:
            stats_parts.append(f"imports={imp}")

        # I4: Use grammar-valid prefixes only (per ADR-0023 §1, tested by
        # test_grammar_consistency_all_tools).  The import entries are rendered
        # at the same depth as Stats (not deeper), so they always land on a
        # valid allowed_start regardless of whether this is the last row:
        #   non-last row: sub_prefix="│   " → import lines start with "│   │   ├─" (valid)
        #   last row:     sub_prefix="    " → import lines start with "│       ├─" (valid)
        # Nesting import entries one level deeper (│   {sub_prefix}    {imp_pfx}) would
        # produce "│           ├─" for last-row which is NOT in allowed_starts.
        lines.append(f"│   {row_prefix} {_srv._portable_path(fp, module=module)}")
        sub_prefix = "    " if is_last_row else "│   "
        import_rows = imports_by_fp.get(fp, []) if imp > 0 else []

        if import_rows:
            lines.append(f"│   {sub_prefix}├─ Stats: {', '.join(stats_parts)}")
            lines.append(f"│   {sub_prefix}├─ Imports ({len(import_rows)}):")
            for i_idx, ir in enumerate(import_rows):
                is_last_imp = i_idx == len(import_rows) - 1
                imp_prefix = "└─" if is_last_imp else "├─"
                lines.append(
                    f"│   {sub_prefix}{imp_prefix} "
                    f"{_srv._portable_path(ir['import_path'], module=ir.get('import_module'))}"
                    f" [{ir['import_module']}]"
                )
        elif imp > 0:
            # imp_count > 0 but batch returned no edges (edges not yet resolved)
            lines.append(f"│   {sub_prefix}├─ Stats: {', '.join(stats_parts)}")
            lines.append(
                f"│   {sub_prefix}└─ Imports: edges not yet resolved (re-index to backfill)."
            )
        else:
            lines.append(f"│   {sub_prefix}├─ Stats: {', '.join(stats_parts)}")
            lines.append(f"│   {sub_prefix}└─ Imports: none")

    footer = hints_for("resolve_stylesheet", name=module, ver=odoo_version)
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def _literal_style_lookup(
    cur,
    query: str,
    odoo_version: str,
    allowed: list[str] | None,
    limit: int,
    extra_cols: list[str] | None = None,
) -> list[dict]:
    """Run a literal ILIKE query against the style chunk types (issue #255, WI-3).

    Performs an exact substring match on the relevant column (entity_name for
    selectors; content for variables) rather than ANN — so results are
    plan-independent and robust even when the embedder is unavailable.

    Column routing is determined by ``literal_column(query)``:
      - Selectors (./#/bare-ident/[/&/...) -> entity_name ILIKE
      - Variables ($/@) -> content ILIKE

    Args:
        cur:          Open psycopg2 cursor (inside _rls_read_tx).
        query:        The selector or variable string (confirmed literal).
        odoo_version: Resolved Odoo version string.
        allowed:      Tenant-filter list from _effective_allowed(), or None.
        limit:        Row cap (typically min(user_limit, FIND_EXAMPLES_ANN_LIMIT)).
        extra_cols:   Additional SQL columns to SELECT beyond the base set.
                      find_examples needs model_name, line_start, repo, repo_id;
                      find_style_override does not.

    Returns:
        List of dicts with keys: chunk_type, module, entity_name, file_path,
        chunk_idx, content, cosine (None), match ('literal').
        Extra columns (if requested) are included verbatim.

    Note: ORDER BY length(content) ASC, module ASC, chunk_idx ASC surfaces the
    most precise / shortest chunk first.  A pg_trgm GIN index can speed this up
    at >50k rows; see ADR-0047 for the trigger condition.  For the current ~5k
    css/scss/less row count, ILIKE with idx_embeddings_filter is microsecond-range.
    """
    col = literal_column(query)
    # Defense-in-depth: col and extra_cols are f-string-interpolated into the SQL
    # below. They are safe today (literal_column returns a closed 2-value set;
    # extra_cols comes from a module constant), but validate the allowlists here so a
    # future caller passing user input can never inject (#255 PR review hardening).
    # Use raise (not assert) so the barrier survives `python -O` (PR #257 follow-up).
    if col not in ("entity_name", "content"):
        raise ValueError(f"unexpected literal column: {col!r}")
    _ALLOWED_EXTRA_COLS = frozenset({"model_name", "line_start", "repo", "repo_id"})
    if extra_cols:
        _bad = [c for c in extra_cols if c not in _ALLOWED_EXTRA_COLS]
        if _bad:
            raise ValueError(f"disallowed extra_cols: {_bad}")
    pat = ilike_pattern(query)
    style_types = tuple(sorted(STYLE_CHUNK_TYPES))
    ph = ", ".join(["%s"] * len(style_types))
    prof_sql = "" if allowed is None else " AND profile_name = ANY(%s)"

    base_cols = "chunk_type, module, entity_name, file_path, chunk_idx, content"
    extra_sql = (", " + ", ".join(extra_cols)) if extra_cols else ""

    lit_params: list = [odoo_version, *style_types, pat]
    if allowed is not None:
        lit_params.append(allowed)
    lit_params.append(limit)

    cur.execute(
        f"""SELECT {base_cols}{extra_sql}
            FROM embeddings
            WHERE odoo_version = %s AND chunk_type IN ({ph})
              AND {col} ILIKE %s ESCAPE '\\'{prof_sql}
            ORDER BY length(content) ASC, module ASC, chunk_idx ASC
            LIMIT %s""",
        lit_params,
    )
    rows = cur.fetchall()
    n_base = len(base_cols.split(","))  # stays in sync if base_cols changes (m-4)
    result = []
    for r in rows:
        d: dict = dict(
            chunk_type=r[0], module=r[1], entity_name=r[2],
            file_path=r[3], chunk_idx=r[4], content=r[5],
            cosine=None, match="literal",
        )
        if extra_cols:
            for idx, ecol in enumerate(extra_cols, n_base):
                d[ecol] = r[idx]
        result.append(d)
    return result


def _find_style_override(
    selector_or_variable: str,
    odoo_version: str = "auto",
    limit: int = 5,
    *,
    _driver=None,
    _pg_conn=None,
    _embedder=None,
    _query_vec=None,
) -> str:
    """Impl for find_style_override tool — no FastMCP wrapper overhead.

    Literal-first lookup (issue #255): when the query is a verbatim CSS token
    (selector, variable, mixin name), an exact substring ILIKE query runs first
    against the relevant column (entity_name for selectors; content for SCSS/LESS
    variables).  ANN backfills any remaining slots.  This makes AC1/AC2 robust
    regardless of ANN recall collapse or embedder availability.

    Performs pgvector ANN on chunk_type in {css, scss, less} to find stylesheets
    declaring *selector_or_variable*, then traverses :IMPORTS to show which
    modules re-declare the same selector (override order - last writer wins
    in CSS cascade, first-match wins in SCSS/LESS @import chain).

    Example:
        find_style_override(".o_list_view", "17.0")
        -> find_style_override: ".o_list_view" (17.0)
          Found 2 result(s)  [2 literal + 0 semantic]
          -----------------------------------------
          #1 · literal match · match: literal · scss · [web] selector:.o_list_view
             File: addons/web/static/src/scss/views/list_view.scss
             Override chain: no importers found (no :IMPORTS edges).
             ┌──────────────────────────────────────────
             │ .o_list_view { display: flex; }
             └──────────────────────────────────────────

        find_style_override("$o-brand-primary", "17.0")
        -> returns the scss variable block whose content declares $o-brand-primary
          (entity_name is the file-stem variable group, e.g. variable:variables:variables).
          css entity_name is raw (no prefix); scss/less has 'selector:' prefix.
    """
    if not selector_or_variable.strip():
        return (
            "find_style_override: empty selector_or_variable — provide a CSS selector,"
            " SCSS variable, or mixin name.\nFound 0 results\n"
        )

    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    driver = _driver or _srv._get_driver()

    # #287 (review): bound version resolution too — Tier-3 _latest_version() is a
    # bounded Neo4j read that may raise OrmQueryTimeout, and this async body has no
    # @offload_neo4j backstop, so catch it inline like the importer BFS below.
    from src.mcp.orm import OrmQueryTimeout
    try:
        with driver.session() as session:
            odoo_version = _srv._resolve_version(odoo_version, session)
    except OrmQueryTimeout as exc:
        return _srv._nonorm_timeout_response(exc, "find_style_override")

    want_literal = is_literal_token(selector_or_variable)

    # For NL (non-literal) queries: embed BEFORE PG checkout so embedder failures
    # are surfaced early without touching the DB (preserves old degrade behavior).
    # For literal queries: skip embed here; it is attempted lazily inside the PG
    # context only as ANN backfill (non-fatal if embedder is down).
    pre_query_vec: list[float] | None = _query_vec
    embedder_for_body = _embedder
    if not want_literal and pre_query_vec is None:
        try:
            embedder_for_body = _embedder or _srv._get_embedder()
        except Exception:
            _srv.logger.warning("find_style_override: embedder unavailable", exc_info=True)
            _hint = hints_for("find_style_override", module="", ver=odoo_version)
            return (
                "find_style_override: embedder unavailable.\n"
                "Hint: check Ollama server is running and EMBEDDER_MODEL is loaded.\n"
                f"Found 0 results\n{_hint}"
            )
        try:
            capped = _srv._cap_query_text(embedder_for_body, selector_or_variable)
            instruct = getattr(embedder_for_body, "query_instruction", INSTRUCT_NL_TO_CODE)
            pre_query_vec = embedder_for_body.embed([instruct + capped])[0]
        except Exception:
            _srv.logger.warning("find_style_override: embedding failed", exc_info=True)
            _hint = hints_for("find_style_override", module="", ver=odoo_version)
            return (
                "find_style_override: embedding failed — try again shortly.\n"
                f"Found 0 results\n{_hint}"
            )
    elif want_literal and embedder_for_body is None:
        # Literal path: best-effort embedder fetch for ANN backfill.
        try:
            embedder_for_body = _srv._get_embedder()
        except Exception:
            embedder_for_body = None

    # C3 (WI-4): fail-closed tenant filter at the pgvector ANN layer (see
    # _find_examples). No explicit profile arg here -> tenant boundary only.
    allowed = _srv._effective_allowed(None)
    prof_sql = "" if allowed is None else " AND profile_name = ANY(%s)"

    _pg_ctx = nullcontext(_pg_conn) if _pg_conn is not None else _srv._checkout_pg()
    with _pg_ctx as pg:
        # RLS wiring (WI-7 / ADR-0034 A2): set app.allowed_profiles GUC for
        # the duration of this read transaction. Armed-but-dormant: owner
        # bypass means this is a no-op until ops enables FORCE RLS.
        with _srv._rls_read_tx(pg, allowed):
            with pg.cursor() as cur:
                # (1) LITERAL-FIRST: exact substring match, no embedder, plan-independent.
                literal_rows: list[dict] = []
                if want_literal:
                    literal_rows = _literal_style_lookup(
                        cur, selector_or_variable, odoo_version, allowed,
                        min(limit, FIND_EXAMPLES_ANN_LIMIT),
                    )

                # (2) SEMANTIC backfill — only if we still need rows.
                remaining = min(limit, FIND_EXAMPLES_ANN_LIMIT) - len(literal_rows)
                ann_rows: list[dict] = []
                if remaining > 0:
                    query_vec: list[float] | None = pre_query_vec
                    if query_vec is None and want_literal and embedder_for_body is not None:
                        # Literal with under-fill: lazy embed for ANN backfill.
                        try:
                            capped = _srv._cap_query_text(embedder_for_body, selector_or_variable)
                            instruct = getattr(
                                embedder_for_body, "query_instruction", INSTRUCT_NL_TO_CODE
                            )
                            query_vec = embedder_for_body.embed([instruct + capped])[0]
                        except Exception:
                            # Literal rows exist — degrade to literal-only.
                            query_vec = None

                    if query_vec is not None:
                        _srv._set_iterative_scan(cur)  # HNSW recall mitigation (ADR-0047)
                        style_types = tuple(sorted(STYLE_CHUNK_TYPES))
                        ph = ", ".join(["%s"] * len(style_types))
                        ann_params: list = [query_vec, odoo_version, *style_types]
                        if allowed is not None:
                            ann_params.append(allowed)
                        ann_params += [query_vec, remaining]
                        cur.execute(
                            f"""SELECT chunk_type, module, entity_name, file_path,
                                       chunk_idx, content,
                                       1 - (vec <=> %s::vector) AS cosine
                                FROM embeddings
                                WHERE odoo_version = %s AND chunk_type IN ({ph}){prof_sql}
                                ORDER BY vec <=> %s::vector LIMIT %s""",
                            ann_params,
                        )
                        ann_rows = [
                            dict(chunk_type=r[0], module=r[1], entity_name=r[2],
                                 file_path=r[3], chunk_idx=r[4], content=r[5],
                                 cosine=float(r[6]), match="semantic")
                            for r in cur.fetchall()
                        ]

    # (3) MERGE + DEDUP — literal first, ANN backfill, drop ANN dups of literal hits.
    seen = {
        (r["chunk_type"], r["module"], r["file_path"], r["entity_name"], r["chunk_idx"])
        for r in literal_rows
    }
    merged = literal_rows + [
        r for r in ann_rows
        if (r["chunk_type"], r["module"], r["file_path"], r["entity_name"], r["chunk_idx"])
        not in seen
    ]
    raw = merged[:limit]

    # G2: disclose literal vs semantic candidate counts.
    n_lit = len(literal_rows)
    n_sem = len(raw) - n_lit
    if want_literal:
        ann_note_style = f"{n_lit} literal + {n_sem} semantic"
    else:
        ann_used_style = min(limit, FIND_EXAMPLES_ANN_LIMIT)
        if ann_used_style >= FIND_EXAMPLES_ANN_LIMIT:
            ann_note_style = (
                f"Note: ANN search capped at {FIND_EXAMPLES_ANN_LIMIT} candidates"
                " — results beyond this pool are not considered"
            )
        else:
            ann_note_style = (
                f"showing {len(raw)} of up to {ann_used_style} semantic candidates"
                f" — increase `limit` (max {FIND_EXAMPLES_ANN_LIMIT}) for broader search"
            )
    header = (
        f'find_style_override: "{selector_or_variable}" ({odoo_version})\n'
        f"Found {len(raw)} result(s)  [{ann_note_style}]\n"
    )
    if not raw:
        footer = hints_for("find_style_override", module="", ver=odoo_version)
        return header + (footer if footer else "")

    # For each hit, check :IMPORTS override chain — which modules re-declare
    # the same selector (import same file path chain).
    #
    # #287: find_style_override is async (embeds on the event loop, then offloads
    # this body via asyncio.to_thread) so it has NO @offload_neo4j backstop. Each
    # per-result importer BFS is bounded via _data_bounded; a tx-timeout on ANY
    # row surfaces as OrmQueryTimeout, which is caught around the WHOLE render loop
    # (ADR-0023: a clean degraded string is preferred over an ambiguous partial
    # render). The metric fires exactly once.
    sep = "─" * 41
    lines = [header]
    try:
        for i, chunk in enumerate(raw, 1):
            entity = f'[{chunk["module"]}] {chunk["entity_name"]}'
            chunk_label = chunk["chunk_type"]
            if chunk["chunk_idx"] > 0:
                chunk_label += f" chunk {chunk['chunk_idx'] + 1}"
            # B1 fix: always emit a score-shaped token; append match: tag as suffix.
            cosine_val = chunk.get("cosine")
            if cosine_val is not None:
                score_token = f"score {cosine_val:.2f}"
            else:
                score_token = "literal match"
            match_tag = chunk.get("match", "semantic")
            lines.append(sep)
            lines.append(
                f"#{i} · {score_token} · match: {match_tag} · {chunk_label} · {entity}"
            )
            lines.append(
                f"   File: {_srv._portable_path(chunk['file_path'], module=chunk.get('module'))}"
            )

            # Find stylesheets that import this file (override chain — BFS depth 1)
            with driver.session() as session:
                importers = _srv._data_bounded(
                    session,
                    f"""
                    MATCH (tgt:Stylesheet {{file_path: $fp, odoo_version: $v}})
                          <-[:IMPORTS]-(src:Stylesheet)
                    WHERE {_srv._scope_pred("tgt")} AND {_srv._scope_pred("src")}
                    RETURN src.file_path AS importer_path, src.module AS importer_module
                    ORDER BY src.file_path ASC
                    """,
                    label=f"stylesheet override chain (Odoo {odoo_version})",
                    fp=chunk["file_path"], v=odoo_version, **_srv._scope(),
                )

            if importers:
                lines.append(f"   Override chain ({len(importers)} importer(s)):")
                for imp in importers:
                    _imp_path = _srv._portable_path(
                        imp["importer_path"], module=imp.get("importer_module")
                    )
                    lines.append(
                        f"   ├─ {_imp_path} [{imp['importer_module']}]"
                    )
            else:
                lines.append("   Override chain: no importers found (no :IMPORTS edges).")

            lines.append("   ┌" + "─" * 42)
            for line in chunk["content"].splitlines():
                lines.append(f"   │ {line}")
            lines.append("   └" + "─" * 42)
            lines.append("")
    except OrmQueryTimeout as exc:
        return _srv._nonorm_timeout_response(exc, "find_style_override")

    # Pass top-result module so hints render useful resolve_stylesheet/describe_module calls.
    top_module = raw[0]["module"] if raw else ""
    footer = hints_for("find_style_override", module=top_module, ver=odoo_version)
    if footer:
        lines.append(footer)
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def resolve_stylesheet(
    module: str,
    odoo_version: RequiredOdooVersion,
) -> str:
    """Enumerate CSS/SCSS stylesheets for an Odoo module and their @import dependencies.

    TRIGGER when: "what stylesheets does module X have", "show CSS files in
    website_sale", "list SCSS imports for web module", "module Y có file CSS/SCSS
    nào", "xem import chain stylesheet của module Z"
    PREFER over: find_style_override when you want an overview of all stylesheets
    in a module, not a specific selector search.
    SKIP when: searching for a specific CSS selector or SCSS variable across
    modules — use find_style_override.

    Args:
        module: Odoo module technical name (e.g. 'web', 'website_sale').

    Returns:
        Tree listing each stylesheet with language, stat counters
        (selectors, vars, mixins, imports), and the resolved @import dependency chain.

    Example:
        resolve_stylesheet("web", "17.0")
        → resolve_stylesheet('web', '17.0')
          ├─ Stylesheets: 2 file(s)
          │   ├─ addons/web/static/src/css/main.css
          │   │   ├─ Stats: lang=css, selectors=42, vars=0
          │   │   └─ Imports: none
          │   └─ addons/web/static/src/scss/variables.scss
          │       ├─ Stats: lang=scss, selectors=0, vars=15, mixins=3, imports=1
          │       ├─ Imports (1):
          │       └─ addons/web/static/src/scss/base.scss [web]
          └─ Next: find_style_override(...) | describe_module(...)

    See also: odoo://{version}/stylesheet/{module}/{file_path*}
    """
    return _resolve_stylesheet(module, odoo_version)


@mcp.tool(**READONLY_TOOL_KWARGS)
async def find_style_override(
    selector_or_variable: str,
    odoo_version: RequiredOdooVersion,
    limit: int = 5,
) -> str:
    """Find CSS selectors or SCSS variables/mixins across modules + override order.

    Searches indexed css/scss/less stylesheets to locate where the selector or
    variable is declared, then shows which modules re-declare it (override order —
    last writer wins in the CSS cascade).

    TRIGGER when: "which module overrides .o_form_view selector", "where is
    $primary variable defined", "find CSS override for .btn-primary", "module
    nào override selector X", "tìm định nghĩa biến SCSS Y trong codebase"
    PREFER over: find_examples when looking specifically for CSS/SCSS patterns
    rather than Python/XML code examples.
    SKIP when: you want a full list of all stylesheets in a module — use
    resolve_stylesheet instead.

    Args:
        selector_or_variable: CSS selector, SCSS variable (e.g. '$primary'),
            or mixin name to search for.
        limit: Max results to return (default 5).

    Returns:
        Ranked matches with the declaration snippet, a relevance score, and the
        override chain showing which modules import the matched file.

    Example:
        find_style_override(".o_list_view", "17.0")
        → find_style_override: ".o_list_view" (17.0)
          Found 2 result(s)  [2 literal + 0 semantic]
          -----------------------------------------
          #1 · literal match · match: literal · scss · [web] selector:.o_list_view
             File: addons/web/static/src/scss/views/list_view.scss
             Override chain: no importers found (no :IMPORTS edges).
    """
    # #227: empty input -> sync impl returns the guard string; otherwise embed
    # async (bounded, short timeout) then offload the blocking body off-loop.
    # Issue #255 (WI-5): literal tokens skip pre-embed — the sync body handles
    # the optional ANN backfill inside asyncio.to_thread (off the event loop).
    if not selector_or_variable.strip():
        return _srv._find_style_override(selector_or_variable, odoo_version, limit)
    from src.embedding.instructions import INSTRUCT_NL_TO_CODE
    query_vec: list[float] | None = None
    embedder = None
    if not is_literal_token(selector_or_variable):
        # NL query: pre-embed on the event loop (bounded, short timeout).
        try:
            embedder = _srv._get_embedder()
        except Exception:
            _srv.logger.warning("find_style_override: embedder unavailable", exc_info=True)
            return (
                "find_style_override: embedder unavailable.\n"
                "Hint: check Ollama server is running and EMBEDDER_MODEL is loaded.\n"
                "Found 0 results\n"
                f"{hints_for('find_style_override', module='', ver=odoo_version)}"
            )
        try:
            instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
            query_vec = await _srv._embed_query(embedder, instruct, selector_or_variable)
        except _srv.EmbedOverloaded as e:
            return (
                f"find_style_override: {e}\nFound 0 results\n"
                f"{hints_for('find_style_override', module='', ver=odoo_version)}"
            )
        except Exception:
            _srv.logger.warning("find_style_override: embedding failed", exc_info=True)
            return (
                "find_style_override: embedding failed — try again shortly.\n"
                "Found 0 results\n"
                f"{hints_for('find_style_override', module='', ver=odoo_version)}"
            )
    else:
        # Literal token: best-effort embedder fetch for ANN backfill.
        # Failure here is non-fatal — sync body will degrade to literal-only.
        try:
            embedder = _srv._get_embedder()
        except Exception:
            embedder = None
    return await asyncio.to_thread(
        _srv._find_style_override,
        selector_or_variable, odoo_version, limit,
        _embedder=embedder, _query_vec=query_vec,
    )


# Bind the owning server module generation AFTER the tool functions are defined.
# sys.modules['src.mcp.server'] at THIS point is the generation that is importing
# this module (server.py imports this module from the very end of its own body,
# and that generation registered these tools onto its `mcp`). Binding at
# end-of-module — rather than via a top-level `from src.mcp import server`, which
# reads the stale `src.mcp` package attribute after a pop+reimport — makes `_srv`
# track the SAME generation as the tool objects defined above. That restores the
# pre-refactor bare-name behaviour: a test holding a stale top-level `srv`
# binding calls the stale-gen tool, whose `_srv` points back at that same stale
# gen, so monkeypatch.setattr(srv, ...) still takes effect. The bodies above read
# the hub through `_srv.<name>` at call time so those patches are observed.
_srv = sys.modules["src.mcp.server"]
