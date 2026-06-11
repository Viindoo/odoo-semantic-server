"""Discovery-layer MCP tools (split out of src/mcp/server.py, Phase 5).

Five discovery tools and their implementation helpers:
  - ``find_examples``        — semantic (+ literal-style + lexical-fallback)
                               search over the indexed pgvector chunks.
  - ``impact_analysis``      — blast-radius of changing a field/method/model
                               (views, methods, JS patches, dependent modules),
                               risk-scored LOW/MEDIUM/HIGH.
  - ``suggest_pattern``      — ANN-rank curated PatternExample chunks by intent.
  - ``check_module_exists``  — module-indexed check + EE-confusion guard.
  - ``find_override_point``  — Method override chain + convention/anti-pattern
                               hints (+ cross-version method diff).

``find_examples`` and ``suggest_pattern`` are ``async def`` (no offload
decorator): they embed the query on the event loop (bounded, short timeout) and
then offload the blocking Neo4j/PG body via ``asyncio.to_thread`` (ADR-0046).
``impact_analysis`` uses ``@offload_bounded_nonorm`` (the non-ORM heavy-read
pool, #276 G5). ``check_module_exists`` and ``find_override_point`` use
``@offload``.

Registration happens via the ``@mcp.tool`` import-time side effect; server.py
imports this module at the end of the file so the decorators run.

The implementation helpers (``_find_examples`` / ``_compute_risk`` /
``_impact_analysis`` / ``_suggest_pattern`` / ``_format_suggest_pattern`` /
``_ee_confusion_live`` / ``_check_module_exists`` / ``_format_check_module_exists``
/ ``_anti_patterns_for_convention`` / ``_fetch_method_for_diff`` /
``_diff_method_across_versions`` / ``_find_override_point`` /
``_format_find_override_point``) live HERE now (moved from server.py).  They
reach the shared resolver/state hub (``_get_driver`` / ``_get_embedder`` /
``_resolve_version`` / ``_scope`` / ``_scope_pred`` / ``_effective_allowed`` /
``_checkout_pg`` / ``_rls_read_tx`` / ``_portable_path`` / ``_repo_url_for_id`` /
``_set_iterative_scan`` / ``_cap_query_text`` / ``_single_bounded`` /
``_data_bounded`` / ``_render_capped`` / ``_edition_label`` / ``_embed_query`` /
``EmbedOverloaded`` / ``logger`` / ``_literal_style_lookup`` and the rerank
scoring constants ``_RERANK_LOG_COEFF`` / ``_RERANK_CHAIN_BOOST`` /
``_LITERAL_RANK_FLOOR`` / ``_LITERAL_RANK_EPS``) through the module-level
``_srv`` server reference bound at the END of this file (see the note there) and
``_srv.<name>`` attribute lookups performed at call time.

Two properties must hold together, which is why ``_srv`` is bound the way it is:

1. The bodies read the hub through ``_srv.<name>`` at CALL time (not by binding
   the names at import time) so that ``monkeypatch.setattr(srv, "_get_driver",
   ...)`` / ``monkeypatch.setattr(srv, "_find_style_override", ...)`` etc. in the
   tests are observed — the patch lands on the live server module object and the
   attribute is re-read off that object on each call.
2. ``_srv`` is bound from ``sys.modules['src.mcp.server']`` at end-of-module, so
   it is the SAME server generation that imported this module and registered
   these tools.  After a ``sys.modules.pop('src.mcp.server')`` + re-import, a
   test that holds a stale ``src.mcp.server`` reference (e.g. the find_examples /
   impact_analysis / pattern-tool fixtures) sees the impls re-exported on the
   fresh generation, exactly as pre-refactor when these bodies were bare-name
   globals in server.py.

``_literal_style_lookup`` lives in tools/stylesheet.py (Phase 2) and is
re-exported on server.py — ``_find_examples`` reaches it through ``_srv`` for the
literal-first style path.  The two discovery-only constants
``_VALID_PATTERN_LANGUAGES`` and ``_ANTI_PATTERNS_BASE`` move HERE in full (§2.7,
verified discovery-only).

server.py re-exports the five public tools plus the impl symbols that tests
import via ``src.mcp.server`` (see the re-export block at the end of server.py).
The pure helpers (``format_next_step`` / ``render_list_block`` /
``is_literal_token`` / ``lexical_example_lookup`` / the ``src.constants`` values)
carry no state and are imported directly — NOT through ``_srv``.
"""

import asyncio
import math
import sys
from contextlib import nullcontext

from src.constants import (
    FIND_EXAMPLES_ANN_LIMIT,
    GLOBAL_PROFILE,
    IMPACT_MODULES_MAX,
    IMPACT_RISK_HIGH_THRESHOLD,
    IMPACT_RISK_MED_THRESHOLD,
    LIST_PREVIEW_MAX_ITEMS,
    REL_DEPENDS_ON,
    REL_DEPENDS_ON_FIELD,
    REL_TARGETS_MODEL,
    REL_USES_FIELD,
    SNIPPET_PREVIEW_MAX_LINES,
    STYLE_CHUNK_TYPES,
    VALID_CHUNK_TYPES,
)
from src.mcp.example_lexical import lexical_example_lookup
from src.mcp.hints import format_next_step
from src.mcp.server import (
    READONLY_TOOL_KWARGS,
    RequiredOdooVersion,
    mcp,
    offload,
    offload_bounded_nonorm,
)
from src.mcp.style_literal import is_literal_token
from src.mcp.tree_builder import render_list_block

# Discovery-only module-level constants (§2.7 — verified used only by the tools
# in this module; no cross-module / test importer reads them via server).
_VALID_PATTERN_LANGUAGES = ("python", "xml", "js", "all")
_ANTI_PATTERNS_BASE = [
    "Old-style super(ClassName, self) — use plain super() in Python 3",
    "Missing return after super() — caller gets None, breaks chain",
]


def _find_examples(
    query: str,
    odoo_version: str = "auto",
    limit: int = 5,
    context_module: str | None = None,
    chunk_types: list[str] | None = None,
    profile_name: str | None = None,
    *,
    _driver=None,
    _pg_conn=None,
    _embedder=None,
    _query_vec=None,
    _use_lexical: bool = False,
) -> str:
    # _query_vec: when the async tool wrapper has already embedded the query off
    # the event loop (#227), it passes the vector here so this blocking body can
    # run inside asyncio.to_thread without re-embedding. When None (sync tests,
    # entity_lookup, CLI), we embed synchronously as before — never on a loop.
    if not query.strip():
        # ADR-0023 §2: tool output must be English-only.
        return (
            "find_examples: empty query — provide a description of the"
            " feature you want to find\nFound 0 results\n"
        )

    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    driver = _driver or _srv._get_driver()

    with driver.session() as session:
        if odoo_version in ("auto", "latest"):
            odoo_version = _srv._resolve_version("auto", session)

    selected_types = [t for t in (chunk_types or []) if t in VALID_CHUNK_TYPES]

    # Issue #255 (WI-7, Decision E): literal-first for style-only queries.
    # Only engage when ALL requested chunk_types are style types AND the query is
    # a verbatim CSS/SCSS token.  NL queries and non-style chunk_types are untouched.
    style_only = bool(selected_types) and set(selected_types) <= STYLE_CHUNK_TYPES
    want_literal = style_only and is_literal_token(query)

    # MAJOR-1 (issue #255 review): defer the embedder fetch until we know a literal
    # query actually needs ANN backfill. A pure-literal style path never fetches the
    # embedder, so a literal lookup survives an init-time embedder failure
    # (EmbedderDimMismatch, config error) — symmetric with _find_style_override.
    embedder = _embedder
    query_vec: list[float] | None = None
    if not want_literal:
        # Standard path: embed now (sync body / pre-embedded async path).
        if _query_vec is not None:
            query_vec = _query_vec
        elif _use_lexical:
            # Caller already tried and failed to embed (async wrapper embed-failure
            # path, or explicit lexical-only mode for testing).  Skip embed entirely
            # and fall through to the lexical fallback below.
            pass
        else:
            try:
                if embedder is None:
                    embedder = _srv._get_embedder()
            except Exception:
                # Embedder init failed — fall back to lexical keyword search.
                _use_lexical = True
            if not _use_lexical:
                try:
                    # Cap the query to the token budget before INSTRUCT so a giant
                    # paste cannot blow the embedder context (#227, sync path).
                    capped = _srv._cap_query_text(embedder, query)
                    instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
                    query_vec = embedder.embed([instruct + capped])[0]
                except Exception:
                    # Embed failed — fall back to lexical keyword search.
                    _use_lexical = True
    else:
        # Literal style token: carry pre-embedded vec (may be None from async wrapper).
        query_vec = _query_vec

    # C3 (WI-4): fail-closed tenant filter at the pgvector ANN layer. The
    # Neo4j rerank only deprioritises non-allowed modules — it does NOT drop
    # their chunks — so isolation MUST be enforced here, in the SQL, before
    # rows are fetched. allowed=None -> admin/unrestricted (no clause);
    # allowed=[] -> deny-all (ANY('{}') matches nothing). global sentinel rows
    # (profile_name='__global__') are excluded when scoped — fail-closed.
    allowed = _srv._effective_allowed(profile_name)
    prof_sql = "" if allowed is None else " AND profile_name = ANY(%s)"

    # Extra columns needed by find_examples (beyond the base 6 in _srv._literal_style_lookup).
    _STYLE_EXTRA_COLS = ["model_name", "line_start", "repo", "repo_id"]

    # Use injected connection (test path) or check out from pool (production).
    _pg_ctx = nullcontext(_pg_conn) if _pg_conn is not None else _srv._checkout_pg()
    with _pg_ctx as pg:
        # RLS wiring (WI-7 / ADR-0034 A2): set app.allowed_profiles GUC for
        # the duration of this read transaction. Armed-but-dormant: owner
        # bypass means this is a no-op until ops enables FORCE RLS.
        with _srv._rls_read_tx(pg, allowed):
            with pg.cursor() as cur:
                # (0) LEXICAL FALLBACK (issue #264, WI-9): when the embedder is
                # unavailable (embedder init or embed call failed), run a
                # keyword ILIKE search against entity_name.  Results are labelled
                # match: lexical to signal degraded quality.  Tenant choke
                # (ADR-0034) is preserved: allowed is passed through unchanged.
                if _use_lexical:
                    lex_rows = lexical_example_lookup(
                        cur, query, odoo_version, allowed,
                        min(limit, FIND_EXAMPLES_ANN_LIMIT),
                        selected_types,
                        extra_cols=_STYLE_EXTRA_COLS,
                    )
                    for r in lex_rows:
                        r.setdefault("model_name", None)
                        r.setdefault("line_start", None)
                        r.setdefault("repo", None)
                        r.setdefault("repo_id", None)
                    # Return with a degraded banner so agents know quality is lower.
                    if not lex_rows:
                        return (
                            f'find_examples: "{query}" ({odoo_version})\n'
                            "Found 0 results  "
                            "[degraded: embedder unavailable — lexical search returned nothing]\n"
                        )
                    header = (
                        f'find_examples: "{query}" ({odoo_version})\n'
                        f"Found {len(lex_rows)} results  "
                        "[degraded: embedder unavailable — lexical keyword match]\n"
                    )
                    sep = "─" * 41
                    lines = [header]
                    for i, chunk in enumerate(lex_rows, 1):
                        entity = f'[{chunk["module"]}] {chunk["entity_name"]}'
                        if chunk["model_name"] and chunk["chunk_type"] == "view":
                            entity += f" (model: {chunk['model_name']})"
                        chunk_label = chunk["chunk_type"]
                        if chunk["chunk_idx"] > 0:
                            chunk_label += f" chunk {chunk['chunk_idx'] + 1}"
                        lines.append(sep)
                        lines.append(
                            f"#{i} · score - · match: lexical"
                            f" · {chunk_label} · {entity}"
                        )
                        file_path = _srv._portable_path(
                            chunk["file_path"] or "",
                            repo=chunk.get("repo"), module=chunk.get("module"),
                        )
                        repo_label = (
                            _srv._repo_url_for_id(chunk.get("repo_id"))
                            or chunk.get("repo")
                        )
                        repo_pfx = f"[{repo_label}] " if repo_label else ""
                        line_sfx = (
                            f":{chunk['line_start']}"
                            if chunk.get("line_start") is not None else ""
                        )
                        lines.append(f"   File: {repo_pfx}{file_path}{line_sfx}")
                        lines.append("   ┌" + "─" * 42)
                        for line in chunk["content"].splitlines():
                            lines.append(f"   │ {line}")
                        lines.append("   └" + "─" * 42)
                        lines.append("")
                    lines.append(format_next_step([
                        f"suggest_pattern(intent='{query}', odoo_version='{odoo_version}')"
                        " for curated patterns",
                    ]))
                    return "\n".join(lines)

                # (1) LITERAL-FIRST for style-only queries (issue #255 WI-7).
                literal_rows: list[dict] = []
                if want_literal:
                    literal_rows = _srv._literal_style_lookup(
                        cur, query, odoo_version, allowed,
                        min(limit, FIND_EXAMPLES_ANN_LIMIT),
                        extra_cols=_STYLE_EXTRA_COLS,
                    )
                    # Fill in missing keys expected by the render loop.
                    for r in literal_rows:
                        r.setdefault("model_name", None)
                        r.setdefault("line_start", None)
                        r.setdefault("repo", None)
                        r.setdefault("repo_id", None)

                # (2) ANN: for non-literal paths, or as backfill when literal under-fills.
                remaining = min(limit, FIND_EXAMPLES_ANN_LIMIT) - len(literal_rows)
                ann_rows: list[dict] = []
                if remaining > 0 and query_vec is None and want_literal:
                    # Literal style path — attempt lazy embed for backfill. Fetch
                    # the embedder here (not at the top) so an embedder failure
                    # degrades to literal-only instead of erroring the whole call.
                    try:
                        if embedder is None:
                            embedder = _srv._get_embedder()
                        capped = _srv._cap_query_text(embedder, query)
                        instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
                        query_vec = embedder.embed([instruct + capped])[0]
                    except Exception:
                        query_vec = None  # degrade to literal-only

                if remaining > 0 and query_vec is not None:
                    if selected_types:
                        placeholders = ",".join(["%s"] * len(selected_types))
                        _srv._set_iterative_scan(cur)  # HNSW recall mitigation (AC5/ADR-0047)
                        params = [query_vec, odoo_version, *selected_types]
                        if allowed is not None:
                            params.append(allowed)
                        params += [query_vec, remaining if want_literal
                                   else min(limit, FIND_EXAMPLES_ANN_LIMIT)]
                        cur.execute(
                            f"""SELECT chunk_type, module, entity_name, model_name, file_path,
                                       chunk_idx, content, 1 - (vec <=> %s::vector) AS cosine,
                                       line_start, repo, repo_id
                                FROM embeddings
                                WHERE odoo_version = %s AND chunk_type IN ({placeholders}){prof_sql}
                                ORDER BY vec <=> %s::vector LIMIT %s""",
                            params,
                        )
                    else:
                        _srv._set_iterative_scan(cur)  # HNSW recall mitigation (AC5/ADR-0047)
                        params = [query_vec, odoo_version]
                        if allowed is not None:
                            params.append(allowed)
                        params += [query_vec, min(limit, FIND_EXAMPLES_ANN_LIMIT)]
                        cur.execute(
                            f"""SELECT chunk_type, module, entity_name, model_name, file_path,
                                      chunk_idx, content, 1 - (vec <=> %s::vector) AS cosine,
                                      line_start, repo, repo_id
                               FROM embeddings WHERE odoo_version = %s{prof_sql}
                               ORDER BY vec <=> %s::vector LIMIT %s""",
                            params,
                        )
                    ann_rows = [
                        dict(chunk_type=r[0], module=r[1], entity_name=r[2], model_name=r[3],
                             file_path=r[4], chunk_idx=r[5], content=r[6], cosine=float(r[7]),
                             line_start=r[8], repo=r[9], repo_id=r[10], match="semantic")
                        for r in cur.fetchall()
                    ]

    # (3) MERGE + DEDUP for literal-first paths.
    if want_literal:
        seen = {
            (r["chunk_type"], r["module"], r["file_path"], r["entity_name"], r["chunk_idx"])
            for r in literal_rows
        }
        raw = literal_rows + [
            r for r in ann_rows
            if (r["chunk_type"], r["module"], r["file_path"], r["entity_name"], r["chunk_idx"])
            not in seen
        ]
        raw = raw[:min(limit, FIND_EXAMPLES_ANN_LIMIT)]
    else:
        raw = ann_rows  # standard ANN path, no literal rows

    raw = [c for c in raw if c["module"] != "__unresolved__"]

    # Neo4j centrality rerank + optional context_module boost.
    # Two UNWIND batch queries replace the previous N+1 per-chunk loop.
    # Coefficients (_srv._RERANK_LOG_COEFF, _srv._RERANK_CHAIN_BOOST) extracted as
    # module-level constants so tests/test_calibration_eval.py grid sweep can
    # monkey-patch them. Baseline (0.02, 0.20) calibrated against 100-query
    # Vi+En eval set 2026-05-11.
    module_names = list({c["module"] for c in raw})
    with driver.session() as session:
        dep_rows = session.run(
            f"UNWIND $names AS name"
            f" MATCH (m:Module {{name: name, odoo_version: $v}})"
            f" WHERE {_srv._scope_pred('m')}"
            f" WITH m, name"
            f" OPTIONAL MATCH (dep)-[:{REL_DEPENDS_ON}]->(m)"
            f" RETURN name, count(dep) AS dependents",
            names=module_names, v=odoo_version, **_srv._scope(profile_name),
        ).data()
        dependents_map = {r["name"]: r["dependents"] for r in dep_rows}

        in_chain_set: set[str] = set()
        if context_module and module_names:
            chain_rows = session.run(
                "MATCH (ctx:Module {name: $ctx, odoo_version: $v})"
                " -[:DEPENDS_ON*1..]->(tgt:Module)"
                " WHERE tgt.name IN $names"
                f" AND {_srv._scope_pred('ctx')}"
                " RETURN DISTINCT tgt.name AS name",
                ctx=context_module, v=odoo_version, names=module_names,
                **_srv._scope(profile_name),
            ).data()
            in_chain_set = {r["name"] for r in chain_rows}

    # M1 fix: literal rows have cosine=None — guard against TypeError in score math.
    # Assign a floor score with a small epsilon to preserve SQL ORDER BY order so
    # literal rows always sort above semantic hits (LITERAL_RANK_FLOOR > max cosine*rerank).
    n_lit = sum(1 for c in raw if c.get("cosine") is None)
    lit_idx = 0
    for chunk in raw:
        dependents = dependents_map.get(chunk["module"], 0)
        if chunk.get("cosine") is None:
            # Literal hit: floor score preserves SQL order, ranks above all semantic.
            chunk["score"] = _srv._LITERAL_RANK_FLOOR + (n_lit - lit_idx) * _srv._LITERAL_RANK_EPS
            lit_idx += 1
        else:
            chunk["score"] = chunk["cosine"] * (
                1 + _srv._RERANK_LOG_COEFF * math.log(dependents + 1)
            )
        if chunk["module"] in in_chain_set:
            chunk["score"] += _srv._RERANK_CHAIN_BOOST

    reranked = sorted(raw, key=lambda c: c["score"], reverse=True)[:limit]

    # G2: disclose ANN/literal candidate counts so callers know the search pool size.
    if want_literal:
        n_lit_shown = sum(1 for c in reranked if c.get("cosine") is None)
        n_sem_shown = len(reranked) - n_lit_shown
        ann_note = f"{n_lit_shown} literal + {n_sem_shown} semantic"
    else:
        ann_used = min(limit, FIND_EXAMPLES_ANN_LIMIT)
        if ann_used >= FIND_EXAMPLES_ANN_LIMIT:
            # User requested limit >= ANN cap: the search pool is hard-capped.
            ann_note = (
                f"Note: ANN search capped at {FIND_EXAMPLES_ANN_LIMIT} candidates"
                " — results beyond this pool are not considered"
            )
        else:
            # User requested fewer results than the ANN cap allows.
            ann_note = (
                f"showing {len(reranked)} of up to {ann_used} semantic candidates"
                f" — increase `limit` (max {FIND_EXAMPLES_ANN_LIMIT}) for broader search"
            )
    header = (
        f'find_examples: "{query}" ({odoo_version})\n'
        f"Found {len(reranked)} results  [{ann_note}]\n"
    )
    if not reranked:
        return header

    sep = "─" * 41
    lines = [header]
    for i, chunk in enumerate(reranked, 1):
        entity = f'[{chunk["module"]}] {chunk["entity_name"]}'
        # For view chunks, show the model so readers know which UI the view belongs to
        if chunk["model_name"] and chunk["chunk_type"] == "view":
            entity += f" (model: {chunk['model_name']})"
        # For sliding-window chunks, show the window index so readers know it's a partial
        chunk_label = chunk["chunk_type"]
        if chunk["chunk_idx"] > 0:
            chunk_label += f" chunk {chunk['chunk_idx'] + 1}"
        lines.append(sep)
        # Issue #255 (B1): always emit a score-shaped token; append match: tag as suffix.
        match_tag = chunk.get("match", "semantic")
        lines.append(
            f"#{i} · score {chunk['score']:.2f} · match: {match_tag}"
            f" · {chunk_label} · {entity}"
        )
        # B2: render [repo] file_path:line_start when provenance data is present (A3).
        # ADR-0037: emit a repo-relative path, never a server-absolute one.
        file_path = _srv._portable_path(
            chunk["file_path"] or "",
            repo=chunk.get("repo"), module=chunk.get("module"),
        )
        # ADR-0037: prefer the portable git URL; fall back to dirname only when absent.
        repo_label = _srv._repo_url_for_id(chunk.get("repo_id")) or chunk.get("repo")
        repo_pfx = f"[{repo_label}] " if repo_label else ""
        line_sfx = f":{chunk['line_start']}" if chunk.get("line_start") is not None else ""
        lines.append(f"   File: {repo_pfx}{file_path}{line_sfx}")
        lines.append("   ┌" + "─" * 42)
        for line in chunk["content"].splitlines():
            lines.append(f"   │ {line}")
        lines.append("   └" + "─" * 42)
        lines.append("")
    # Wave 5: Next-step footer per ADR-0023 §4. find_examples is a drill-down
    # entry-point; suggest moving to curated patterns or the canonical method.
    lines.append(format_next_step([
        f"suggest_pattern(intent='{query}', odoo_version='{odoo_version}')"
        " for curated patterns",
    ]))
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
async def find_examples(
    query: str,
    odoo_version: RequiredOdooVersion,
    limit: int = 5,
    context_module: str | None = None,
    chunk_types: list[str] | None = None,
    profile_name: str | None = None,
) -> str:
    """Semantic search for real code examples from the indexed Odoo codebase.

    Degrades to lexical keyword match if the embedder is unavailable
    (results labelled `match: lexical` in that case).

    TRIGGER when: "show me examples of wizard usage", "how is mail.thread used
    in codebase", "give me code example for X pattern", "ví dụ code dùng X
    trong codebase", "cách dùng X trong thực tế", "how to send email in Odoo"
    PREFER over: LLM-generated examples — returns real indexed code, not
    hallucinated patterns or outdated snippets from training data
    SKIP when: user wants to know if a module exists → use check_module_exists;
    user wants pattern guidance with gotchas → use suggest_pattern

    Args:
        query: Feature description (EN or VN).
        limit: Number of results (default 5, max 20).
        context_module: Boost results from modules this module depends on.
        chunk_types: Filter by type: method, field, view, qweb, js_era1,
            js_era2, js_era3. Default: all types.
        profile_name: Optional profile / tenant scope filter.

    Returns:
        Header + N results ranked by relevance.
        Each result: score, type, module, entity, file path, content snippet.

    Example:
        find_examples("confirm sale order and send email", "17.0", limit=3)
        → find_examples: "confirm sale order and send email" (17.0)
          Found 3 results
          #1 · score 0.82 · method · [sale] sale.order.action_confirm
             File: [odoo_17.0] addons/sale/models/sale_order.py:412
    """
    # #227: embed on the event loop (async, bounded, short timeout), then run
    # the blocking Neo4j/PG body in a worker thread so the loop stays free —
    # /health and other requests never freeze behind one slow embed.
    # Issue #255 (WI-8/B2): literal style queries skip pre-embed so the tool
    # works even when the embedder is down (the outage scenario in the issue).
    if not query.strip():
        return _srv._find_examples(query, odoo_version, limit, context_module,
                                   chunk_types, profile_name)
    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    # Replicate the style_only + literal detection from the sync body so the
    # async wrapper can decide whether to pre-embed.  We mirror the
    # selected_types filtering logic from _find_examples.
    _selected = [t for t in (chunk_types or []) if t in VALID_CHUNK_TYPES]
    _style_only = bool(_selected) and set(_selected) <= STYLE_CHUNK_TYPES
    _want_literal = _style_only and is_literal_token(query)

    query_vec: list[float] | None = None
    embedder = None
    _async_use_lexical = False
    if not _want_literal:
        # Standard NL path: pre-embed now on the event loop.
        try:
            embedder = _srv._get_embedder()
        except Exception:
            # Embedder unavailable — fall back to lexical keyword search.
            _async_use_lexical = True
        if not _async_use_lexical:
            try:
                instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
                query_vec = await _srv._embed_query(embedder, instruct, query)
            except _srv.EmbedOverloaded as e:
                # Overloaded is a transient server condition, not an outage —
                # return the clean message rather than a degraded lexical result
                # (retrying momentarily is better than lower-quality output).
                return f"find_examples: {e}\nFound 0 results\n"
            except Exception:
                # Embed failed (timeout, model not loaded, etc.) — fall back to
                # lexical keyword search so the agent still gets useful results.
                _async_use_lexical = True
    else:
        # Literal style token: best-effort embedder fetch for ANN backfill.
        # Failure here is non-fatal — sync body will use literal-only results.
        try:
            embedder = _srv._get_embedder()
        except Exception:
            embedder = None
    return await asyncio.to_thread(
        _srv._find_examples,
        query, odoo_version, limit, context_module, chunk_types, profile_name,
        _embedder=embedder, _query_vec=query_vec, _use_lexical=_async_use_lexical,
    )


def _compute_risk(view_count: int, method_count: int, js_count: int) -> str:
    """Risk thresholds v1 — validated 2026-05-11 against 25-case curated incident set.

    Dataset: tests/eval/impact_analysis_incidents.json (7 HIGH, 8 MEDIUM, 10 LOW cases).
    Macro-F1 = 1.0000 (perfect classification on all 25 cases).
    Sweep candidates: HIGH ∈ {7, 10, 12, 15} × MED ∈ {3, 4, 5, 6}.
    Current thresholds (HIGH>=10, MED>=4) are optimal vs all candidate pairs.
    (HIGH>=10, MED>=3 also achieves macro-F1=1.0 but MED=4 preserves the original
    "4-9 = module-scope review" semantics without information loss.)
    Re-validate: pytest tests/test_calibration_eval.py::test_risk_threshold_validation -v

    HIGH >= 10 affected entities, MEDIUM 4-9, LOW < 4.
    Rationale: <4 = isolated change, 4-9 = module-scope review needed,
    >=10 = cross-module impact requiring full regression.
    """
    total = view_count + method_count + js_count
    if total >= IMPACT_RISK_HIGH_THRESHOLD:
        return "HIGH"
    if total >= IMPACT_RISK_MED_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def _impact_analysis(
    entity_type: str,
    entity_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Return everything affected by changing the given entity. Risk-scored."""
    valid_types = ("field", "method", "model")
    if entity_type not in valid_types:
        return (
            f"Invalid entity_type '{entity_type}'. Use: field, method, model."
        )

    # ------------------------------------------------------------------ #
    # Parse entity_name per entity_type — validate before touching DB    #
    # ------------------------------------------------------------------ #
    if entity_type in ("field", "method"):
        if "." not in entity_name:
            return (
                f"Entity '{entity_name}' not found. "
                f"Expected format: '<model>.<{entity_type}>' "
                f"(e.g. 'sale.order.amount_total' for a field)."
            )
        # Split on LAST dot: model has dots, field/method does not
        last_dot = entity_name.rfind(".")
        model_name = entity_name[:last_dot]
        member_name = entity_name[last_dot + 1:]
    else:
        # entity_type == "model"
        model_name = entity_name
        member_name = None

    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        # ------------------------------------------------------------------ #
        # Query 1: verify entity exists                                        #
        # ------------------------------------------------------------------ #
        # G5 (#276): every heavy read below runs through _srv._data_bounded /
        # _srv._single_bounded, which wrap the Cypher in neo4j.Query(timeout=...) so a
        # runaway traversal (TARGETS_MODEL fan-out, DEPENDS_ON / BOUND_TO chains)
        # surfaces as a bounded OrmQueryTimeout instead of a zombie transaction.
        _label = f"impact analysis for '{entity_name}' (Odoo {odoo_version})"
        if entity_type == "field":
            exists = _srv._single_bounded(
                session,
                "MATCH (f:Field {name: $fn, model: $mn, odoo_version: $v}) "
                f"WHERE {_srv._scope_pred('f')} "
                "RETURN count(f) AS c",
                _label,
                fn=member_name, mn=model_name, v=odoo_version,
                **_srv._scope(profile_name),
            )["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )
        elif entity_type == "method":
            exists = _srv._single_bounded(
                session,
                "MATCH (mth:Method {name: $mn, model: $model, odoo_version: $v}) "
                f"WHERE {_srv._scope_pred('mth')} "
                "RETURN count(mth) AS c",
                _label,
                mn=member_name, model=model_name, v=odoo_version,
                **_srv._scope(profile_name),
            )["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )
        else:  # model
            exists = _srv._single_bounded(
                session,
                "MATCH (m:Model {name: $mn, odoo_version: $v}) "
                "WHERE coalesce(m.unresolved, false) = false "
                "AND m.module <> '__unresolved__' "
                f"AND {_srv._scope_pred('m')} "
                "RETURN count(m) AS c",
                _label,
                mn=model_name, v=odoo_version, **_srv._scope(profile_name),
            )["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )

        # ------------------------------------------------------------------ #
        # Query 2: views targeting model (DISTINCT to avoid TARGETS_MODEL fan-out)
        # ------------------------------------------------------------------ #
        views = _srv._data_bounded(session, f"""
            MATCH (m:Model {{name: $mn, odoo_version: $v}})<-[:{REL_TARGETS_MODEL}]-(view:View)
            WHERE ($own IS NULL OR (size(view.profile) > 0
                   AND all(__p IN view.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN DISTINCT view.xmlid AS xmlid, view.module AS module
            ORDER BY view.module, view.xmlid
        """, _label, mn=model_name, v=odoo_version, **_srv._scope(profile_name))

        # ------------------------------------------------------------------ #
        # Query 3: methods on this model (with super call filter for field;   #
        #          all overrides for method entity_type)                       #
        # ------------------------------------------------------------------ #
        if entity_type == "field":
            methods = _srv._data_bounded(session, """
                MATCH (mth:Method {model: $mn, odoo_version: $v})
                WHERE mth.has_super_call = true
                AND ($own IS NULL OR (size(mth.profile) > 0
                     AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module, mth.name
            """, _label, mn=model_name, v=odoo_version, **_srv._scope(profile_name))
        elif entity_type == "method":
            methods = _srv._data_bounded(session, """
                MATCH (mth:Method {name: $mn2, model: $mn, odoo_version: $v})
                WHERE ($own IS NULL OR (size(mth.profile) > 0
                       AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module
            """, _label, mn2=member_name, mn=model_name, v=odoo_version,
                **_srv._scope(profile_name))
        else:  # model
            methods = _srv._data_bounded(session, """
                MATCH (mth:Method {model: $mn, odoo_version: $v})
                WHERE ($own IS NULL OR (size(mth.profile) > 0
                       AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module, mth.name
            """, _label, mn=model_name, v=odoo_version, **_srv._scope(profile_name))

        # ------------------------------------------------------------------ #
        # Query 4: JS patches on components bound to this model               #
        # ------------------------------------------------------------------ #
        js_patches = _srv._data_bounded(session, """
            MATCH (m:Model {name: $mn, odoo_version: $v})<-[:BOUND_TO]-(comp:OWLComp)
                  <-[:PATCHES]-(jp:JSPatch)
            WHERE ($own IS NULL OR (size(jp.profile) > 0
                   AND all(__p IN jp.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN DISTINCT jp.target AS target, jp.patch_name AS patch_name,
                   jp.module AS module, jp.era AS era
            ORDER BY jp.module, jp.target
        """, _label, mn=model_name, v=odoo_version, **_srv._scope(profile_name))

        # ------------------------------------------------------------------ #
        # Query 5: dependent modules of all modules defining this model       #
        # ------------------------------------------------------------------ #
        dep_modules = _srv._data_bounded(session, f"""
            MATCH (m:Model {{name: $mn, odoo_version: $v}})-[:DEFINED_IN]->(defmod:Module)
                  <-[:{REL_DEPENDS_ON}]-(depmod:Module)
            WHERE ($own IS NULL OR (size(depmod.profile) > 0
                   AND all(__p IN depmod.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN DISTINCT depmod.name AS dep_name
            ORDER BY depmod.name
        """, _label, mn=model_name, v=odoo_version, **_srv._scope(profile_name))

        # For model entity_type: also collect defining modules as "extensions"
        if entity_type == "model":
            def_modules = _srv._data_bounded(session, """
                MATCH (m:Model {name: $mn, odoo_version: $v})-[:DEFINED_IN]->(mod:Module)
                WHERE ($own IS NULL OR (size(m.profile) > 0
                       AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT m.module AS module_name
                ORDER BY m.module
            """, _label, mn=model_name, v=odoo_version, **_srv._scope(profile_name))
        else:
            def_modules = []

        # ------------------------------------------------------------------ #
        # Query 6 (field only): methods that USES_FIELD / DEPENDS_ON_FIELD    #
        # Traverses A2d edges — populated after reindex; empty pre-reindex.   #
        # ------------------------------------------------------------------ #
        uses_field_methods: list[dict] = []
        depends_on_field_methods: list[dict] = []
        if entity_type == "field":
            uses_field_methods = _srv._data_bounded(
                session,
                f"""
                MATCH (mth:Method {{odoo_version: $v}})
                      -[:{REL_USES_FIELD}]->(f:Field {{name: $fn, model: $mn, odoo_version: $v}})
                WHERE ($own IS NULL OR (size(mth.profile) > 0
                       AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT mth.name AS name, mth.model AS model, mth.module AS module
                ORDER BY mth.module, mth.model, mth.name
                """,
                _label,
                fn=member_name, mn=model_name, v=odoo_version,
                **_srv._scope(profile_name),
            )
            depends_on_field_methods = _srv._data_bounded(
                session,
                f"""
                MATCH (mth:Method {{odoo_version: $v}})
                      -[:{REL_DEPENDS_ON_FIELD}]->(f:Field {{name: $fn, model: $mn,
                                                              odoo_version: $v}})
                WHERE ($own IS NULL OR (size(mth.profile) > 0
                       AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT mth.name AS name, mth.model AS model, mth.module AS module
                ORDER BY mth.module, mth.model, mth.name
                """,
                _label,
                fn=member_name, mn=model_name, v=odoo_version,
                **_srv._scope(profile_name),
            )

    # ---------------------------------------------------------------------- #
    # Build output tree — G1: all sections capped + disclosure (ADR-0023 §3) #
    # Risk score and counts in labels use the REAL total, not the cap.        #
    # ---------------------------------------------------------------------- #
    view_count = len(views)
    method_count = len(methods)
    js_count = len(js_patches)
    total = view_count + method_count + js_count
    risk = _compute_risk(view_count, method_count, js_count)

    # Helper: append a capped sub-list (items already formatted as strings)
    # Each sub-item is indented under its section header with tree connectors.
    def _append_capped_section(
        out: list[str],
        header: str,
        items: list,
        formatter,  # (item) -> str
        cap: int,
        total_count: int,
        more_hint: str,
    ) -> None:
        out.append(f"├─ {header}:")
        capped = _srv._render_capped(
            items[:cap], formatter,
            cap=cap, total=total_count,
            more_hint=more_hint,
        )
        # ADR-0023 §1.2: the shared helper attaches └─ to the LAST row, which
        # includes the "... and N more" disclosure row when total_count > cap.
        # Header was appended as a non-last child (├─) → prefix "│   ".
        out.extend(render_list_block(capped, prefix="│   "))

    lines = [f"impact_analysis({entity_type}, {entity_name}, {odoo_version})"]
    lines.append(f"├─ Risk: {risk} ({total} affected entities)")

    # --- Views section ---
    if views:
        _append_capped_section(
            lines,
            f"Views ({view_count})",
            views,
            lambda v_item: f"[{v_item['module']}] {v_item['xmlid']}",
            cap=LIST_PREVIEW_MAX_ITEMS,
            total_count=view_count,
            more_hint=(
                f"model_inspect(model='{model_name}', method='views'"
                f", odoo_version='{odoo_version}') for full view list"
            ),
        )
    else:
        lines.append("├─ Views: none")

    # --- Methods section ---
    if entity_type == "field":
        methods_label = (
            f"Methods on {model_name} with super() ({method_count})"
            f" — field-level filter not yet implemented (M5)"
        )
    elif entity_type == "method":
        methods_label = f"Override chain ({method_count})"
    else:
        methods_label = f"Methods ({method_count})"

    if entity_type == "field":
        # For field: capped list of super()-calling methods
        if methods:
            _append_capped_section(
                lines,
                methods_label,
                methods,
                lambda m_item: f"[{m_item['module']}] {m_item['name']}",
                cap=LIST_PREVIEW_MAX_ITEMS,
                total_count=method_count,
                more_hint=(
                    f"model_inspect(model='{model_name}', method='methods'"
                    f", odoo_version='{odoo_version}') for full method list"
                ),
            )
        else:
            lines.append(f"├─ {methods_label}: none")
        # B2: field-level blast radius from USES_FIELD / DEPENDS_ON_FIELD edges (A2d).
        # Omit sections entirely when empty (pre-reindex: edges not present yet).
        if uses_field_methods:
            uses_count = len(uses_field_methods)
            _append_capped_section(
                lines,
                f"Methods using this field ({uses_count})",
                uses_field_methods,
                lambda m_item: f"[{m_item['module']}] {m_item['model']}.{m_item['name']}",
                cap=LIST_PREVIEW_MAX_ITEMS,
                total_count=uses_count,
                more_hint=(
                    f"model_inspect(model='{model_name}', method='methods'"
                    f", odoo_version='{odoo_version}') for full method list"
                ),
            )
        if depends_on_field_methods:
            dep_count = len(depends_on_field_methods)
            _append_capped_section(
                lines,
                f"Compute-dependent methods ({dep_count})",
                depends_on_field_methods,
                lambda m_item: f"[{m_item['module']}] {m_item['model']}.{m_item['name']}",
                cap=LIST_PREVIEW_MAX_ITEMS,
                total_count=dep_count,
                more_hint=(
                    f"model_inspect(model='{model_name}', method='methods'"
                    f", odoo_version='{odoo_version}') for full method list"
                ),
            )
    elif methods:
        _append_capped_section(
            lines,
            methods_label,
            methods,
            lambda m_item: f"[{m_item['module']}] {m_item['name']}",
            cap=LIST_PREVIEW_MAX_ITEMS,
            total_count=method_count,
            more_hint=(
                f"model_inspect(model='{model_name}', method='methods'"
                f", odoo_version='{odoo_version}') for full method list"
            ),
        )
    else:
        lines.append(f"├─ {methods_label}: none")

    # --- JS patches section ---
    if js_patches:
        _append_capped_section(
            lines,
            f"JS patches ({js_count})",
            js_patches,
            lambda jp: (
                f"[{jp['module']}] {jp['target']}"
                f" via {jp['patch_name']} (era: {jp['era']})"
            ),
            cap=LIST_PREVIEW_MAX_ITEMS,
            total_count=js_count,
            more_hint=(
                f"model_inspect(model='{model_name}', method='summary'"
                f", odoo_version='{odoo_version}') for JS overview"
            ),
        )
    else:
        lines.append("├─ JS patches: none")

    # --- For model entity_type: extension modules section (capped) ---
    if entity_type == "model" and def_modules:
        def_count = len(def_modules)
        mod_names_preview = [d["module_name"] for d in def_modules[:LIST_PREVIEW_MAX_ITEMS]]
        preview_str = ", ".join(mod_names_preview)
        if def_count > LIST_PREVIEW_MAX_ITEMS:
            overflow = def_count - LIST_PREVIEW_MAX_ITEMS
            preview_str += (
                f", ... and {overflow} more"
                f" (use model_inspect(model='{model_name}', method='summary'"
                f", odoo_version='{odoo_version}') for full list)"
            )
        lines.append(f"├─ Defined/extended in ({def_count}): {preview_str}")

    # --- Dependent modules section (capped at IMPACT_MODULES_MAX) ---
    if dep_modules:
        dep_total = len(dep_modules)
        dep_names_preview = [d["dep_name"] for d in dep_modules[:IMPACT_MODULES_MAX]]
        preview_str = ", ".join(dep_names_preview)
        if dep_total > IMPACT_MODULES_MAX:
            overflow = dep_total - IMPACT_MODULES_MAX
            preview_str += (
                f", ... and {overflow} more"
                " (run with profile_name=<profile> to scope)"
            )
        lines.append(f"├─ Dependent modules ({dep_total}): {preview_str}")
    else:
        lines.append("├─ Dependent modules: none")

    # Wave 5: Next-step footer per ADR-0023 §4.
    if entity_type == "method":
        next_hints = [
            f"find_override_point(model='{model_name}', method='{member_name}'"
            f", odoo_version='{odoo_version}') for safe extension spot",
            f"find_deprecated_usage(odoo_version='{odoo_version}')"
            " to widen for deprecated calls",
        ]
    elif entity_type == "field":
        next_hints = [
            f"model_inspect(model='{model_name}', method='field', field='{member_name}'"
            f", odoo_version='{odoo_version}') for field detail",
            f"find_deprecated_usage(odoo_version='{odoo_version}')"
            " to widen for deprecated calls",
        ]
    else:  # model
        next_hints = [
            f"model_inspect(model='{model_name}', method='methods', odoo_version='{odoo_version}')"
            " for behavior surface",
            f"find_deprecated_usage(odoo_version='{odoo_version}')"
            " to widen for deprecated calls",
        ]
    lines.append(format_next_step(next_hints))
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_bounded_nonorm
def impact_analysis(
    entity_type: str,
    entity_name: str,
    odoo_version: RequiredOdooVersion,
    profile_name: str | None = None,
) -> str:
    """List everything affected by changing an entity. Risk-scored LOW/MEDIUM/HIGH.

    TRIGGER when: "what breaks if I change amount_total", "impact of modifying
    field X", "dependencies of method Y", "thay đổi field X ảnh hưởng đến gì",
    "rủi ro khi sửa method Y", "blast radius of removing field Z"
    PREFER over: manual grep — traces transitive dependencies (views, methods,
    JS patches, dependent modules) across all indexed repos automatically
    SKIP when: user wants to see who extends a model → use model_inspect(method='summary');
    user wants deprecation warnings → use find_deprecated_usage

    Args:
        entity_type: One of 'field', 'method', 'model'.
        entity_name: For field/method: '<model>.<name>' e.g.
            'sale.order.amount_total'. For model: '<model>' e.g. 'sale.order'.
        profile_name: Profile filter for all 5 sub-queries
            (Field/Method/View/JSPatch/Module). Default None = all profiles.

    Returns:
        Risk score (LOW/MEDIUM/HIGH) + breakdown of affected views, methods,
        JS patches across modules. Use BEFORE renaming or removing entities.

    Example:
        impact_analysis("field", "sale.order.amount_total", "17.0")
        → impact_analysis(field, sale.order.amount_total, 17.0)
          ├─ Risk: MEDIUM (7 affected entities)
          ├─ Views (3): ...
          ├─ Methods (4): ...
          └─ Dependent modules (2): viin_sale, to_sale_custom
    """
    return _srv._impact_analysis(entity_type, entity_name, odoo_version, profile_name)


def _suggest_pattern(
    intent: str,
    odoo_version: str = "auto",
    language: str = "python",
    limit: int = 5,
    *,
    _driver=None,
    _pg_conn=None,
    _embedder=None,
    _query_vec=None,
) -> str:
    """ANN-rank curated PatternExample chunks by intent string.

    Per ADR-0003: pgvector ANN over embeddings (chunk_type='pattern_example') →
    Neo4j batch fetch metadata via UNWIND on pattern_id list. Language filter
    via entity_name slug LIKE '<language>__%'.
    """
    if not intent.strip():
        return (
            "suggest_pattern: intent is required (empty input).\n"
            "Hint: pass a natural-language description, e.g. "
            "'computed field cross-model partner'."
        )
    if language not in _VALID_PATTERN_LANGUAGES:
        valid = ", ".join(_VALID_PATTERN_LANGUAGES)
        return (
            f"suggest_pattern: invalid language={language!r}. Valid: {valid}."
        )

    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    driver = _driver or _srv._get_driver()
    try:
        embedder = _embedder or _srv._get_embedder()
    except Exception:
        _srv.logger.warning("suggest_pattern: embedder unavailable", exc_info=True)
        return (
            "suggest_pattern: embedder unavailable.\n"
            "Hint: check Ollama is running (default: http://localhost:11434)."
        )

    with driver.session() as session:
        v = _srv._resolve_version(odoo_version, session)

    if _query_vec is not None:
        intent_vec = _query_vec
    else:
        try:
            # Cap intent to the token budget before INSTRUCT (#227, sync path).
            capped = _srv._cap_query_text(embedder, intent)
            instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
            intent_vec = embedder.embed([instruct + capped])[0]
        except Exception:
            _srv.logger.warning("suggest_pattern: embedding query failed", exc_info=True)
            return (
                "suggest_pattern: embedding query failed — try again shortly, "
                "or verify the embedder service is reachable."
            )

    # Use injected connection (test path) or check out from pool (production).
    # RLS note (WI-7 / ADR-0034 D3/A2 / FUFU-2): pattern catalogue chunks carry
    # the explicit profile_name = '__global__' sentinel (m13_021). The SELECT
    # filters on it directly so this read is immune to GUC state; the
    # embeddings_tenant RLS policy passes the sentinel unconditionally via the
    # "profile_name = '__global__'" branch — no GUC wiring needed here.
    _pg_ctx = nullcontext(_pg_conn) if _pg_conn is not None else _srv._checkout_pg()
    with _pg_ctx as pg:
        with pg.cursor() as cur:
            if language == "all":
                cur.execute(
                    """SELECT entity_name, file_path,
                              1 - (vec <=> %s::vector) AS cosine
                       FROM embeddings
                       WHERE chunk_type = 'pattern_example'
                         AND module = '__patterns__'
                         AND profile_name = %s
                       ORDER BY vec <=> %s::vector
                       LIMIT %s""",
                    [intent_vec, GLOBAL_PROFILE, intent_vec, limit],
                )
            else:
                cur.execute(
                    """SELECT entity_name, file_path,
                              1 - (vec <=> %s::vector) AS cosine
                       FROM embeddings
                       WHERE chunk_type = 'pattern_example'
                         AND module = '__patterns__'
                         AND profile_name = %s
                         AND entity_name LIKE %s
                       ORDER BY vec <=> %s::vector
                       LIMIT %s""",
                    [intent_vec, GLOBAL_PROFILE, f"{language}__%", intent_vec, limit],
                )
            ranked = cur.fetchall()

    if not ranked:
        next_line = format_next_step([
            f"find_examples(query='{intent}', odoo_version='{v}')"
            " for real-world variants",
        ])
        return (
            f"suggest_pattern({intent!r}, {v!r}, language={language})\n"
            "├─ No curated patterns available for this query. "
            "The pattern catalogue may not be populated for this version/profile.\n"
            + next_line
        )

    # Decode pattern_id from entity_name slug (<language>__<id>)
    pattern_ids = []
    score_map: dict[str, float] = {}
    for entity_name, _file, cosine in ranked:
        if "__" in entity_name:
            _lang, pid = entity_name.split("__", 1)
        else:
            pid = entity_name
        pattern_ids.append(pid)
        score_map[pid] = float(cosine)

    with driver.session() as session:
        records = session.run("""
            UNWIND $ids AS pid
            MATCH (p:PatternExample {pattern_id: pid})
            RETURN p.pattern_id AS id, p.intent_keywords AS kw,
                   p.file_ref AS fr, p.snippet_text AS sn,
                   p.gotchas AS g, p.language AS lang,
                   p.odoo_version_min AS vmin
        """, ids=pattern_ids).data()

    by_id = {r["id"]: r for r in records}
    return _format_suggest_pattern(
        ordered_ids=pattern_ids, by_id=by_id, score_map=score_map,
        intent=intent, version=v, language=language,
    )


def _format_suggest_pattern(
    *, ordered_ids: list[str], by_id: dict[str, dict],
    score_map: dict[str, float], intent: str, version: str, language: str,
) -> str:
    lines = [
        f"suggest_pattern({intent!r}, {version}, language={language}) "
        f"— {len(ordered_ids)} matches",
    ]
    # Wave 5: all pattern branches become ├─ so the Next: footer is the
    # final └─ (ADR-0023 §4).
    for i, pid in enumerate(ordered_ids):
        rec = by_id.get(pid)
        if not rec:
            continue
        connector = "├─"
        score = score_map.get(pid, 0.0)
        lines.append(f"{connector} #{i + 1} · score {score:.2f} · {pid}")
        prefix = "│   "
        lines.append(f"{prefix}├─ Language: {rec['lang']} (min v{rec['vmin']})")
        lines.append(f"{prefix}├─ File:     {rec['fr']}")
        snippet_lines = (rec.get("sn") or "").splitlines()
        if snippet_lines:
            lines.append(f"{prefix}├─ Snippet:")
            # Snippet is a non-last child → sublist indent is "│   " (4 chars).
            for sl in snippet_lines[:SNIPPET_PREVIEW_MAX_LINES]:
                lines.append(f"{prefix}│   {sl}")
            if len(snippet_lines) > SNIPPET_PREVIEW_MAX_LINES:
                extra = len(snippet_lines) - SNIPPET_PREVIEW_MAX_LINES
                # G7: add escape-hatch hint to odoo://pattern/{id} resource
                lines.append(
                    f"{prefix}│   ... ({extra} more lines"
                    f" — read full via odoo://{version}/pattern/{pid})"
                )
        gotchas = rec.get("g") or []
        if gotchas:
            lines.append(f"{prefix}└─ Gotchas:")
            # Gotchas is the last child → sublist indent is "    " (4 spaces).
            for g in gotchas:
                lines.append(f"{prefix}    • {g}")
    lines.append(format_next_step([
        f"find_examples(query='{intent}', odoo_version='{version}')"
        " for real-world variants",
    ]))
    return "\n".join(lines)


def _ee_confusion_live() -> dict[str, str | None]:
    """Build EE confusion map from live DB (cached 60 s by get_ee_modules).

    Falls back to static list when DB is unreachable — same as get_ee_modules().
    Called on every _check_module_exists invocation so admin CRUD changes
    propagate within one 60 s cache window (WI-R F-007 fix).
    """
    from src.data.ee_modules import get_ee_modules
    return {m["name"]: m["vt_equivalent"] for m in get_ee_modules()}


def _check_module_exists(
    name: str, odoo_version: str = "auto", *,
    profile_name: str | None = None,
    _driver=None,
) -> str:
    """Report whether `name` is indexed + flag EE-confusion (per ADR-0003 §2).

    Edition-first strategy: query Neo4j for indexed edition (OEEL-1 detected),
    fallback to DB-backed guard list if not indexed.  Both paths produce the same
    EE warning.  Guard list is read via get_ee_modules() (60 s cache) so that
    admin CRUD changes take effect within one cache window (WI-R F-007).
    """
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        v = _srv._resolve_version(odoo_version, session)
        rec = session.run("""
            MATCH (m:Module {name: $n, odoo_version: $v})
            WHERE ($own IS NULL OR (size(m.profile) > 0
                   AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN m.edition AS edition,
                   m.license AS license,
                   m.viindoo_equivalent_qname AS vvq,
                   m.repo AS repo
        """, n=name, v=v, **_srv._scope(profile_name)).single()

    indexed = rec is not None
    edition = rec["edition"] if rec else None
    license_val = rec["license"] if rec else None
    repo = rec.get("repo") if rec else None
    vvq_db = rec.get("vvq") if rec else None

    # Build live EE confusion map from DB (cached 60 s).  Falls back to static
    # list when DB is unreachable — transparent to callers (WI-R F-007 fix).
    confusion = _ee_confusion_live()

    # Edition-first: check Neo4j for 'enterprise' (from OEEL-1 detection at index time).
    # OPL-1 is NOT mapped to 'enterprise' by _detect_module_edition (it falls to 'custom'),
    # so it never trips the EE-confusion gate below — the indexed `edition` enum is the
    # sole signal (ADR-0036; #263 regression fix removed the prior license-based check).
    is_ee_confusion = False
    ee_source = ""  # track source for output messaging
    viindoo_equivalent = None

    # Gate EE-confusion on the indexed `edition` enum only (ADR-0036). OEEL-1
    # (Odoo S.A.'s OWN Enterprise license) is detected as edition="enterprise" at
    # index time, so the enum check covers it. OPL-1 is the Odoo Proprietary License
    # for third-party/proprietary apps (edition="viindoo"/"custom") and must NOT be
    # flagged as Odoo Enterprise — doing so mislabeled Viindoo OPL-1 addons such as
    # to_base / viin_hr (#263, regression from PR #165).
    if indexed and edition == "enterprise":
        is_ee_confusion = True
        ee_source = "indexed"
        viindoo_equivalent = vvq_db or confusion.get(name)
    elif name in confusion:
        # Not indexed (or not marked 'enterprise') but in guard list
        is_ee_confusion = True
        ee_source = "dict"
        viindoo_equivalent = confusion.get(name)

    return _format_check_module_exists(
        name=name, version=v, indexed=indexed, edition=edition,
        license_val=license_val, repo=repo,
        is_ee_confusion=is_ee_confusion, viindoo_equivalent=viindoo_equivalent,
        ee_source=ee_source,
    )


def _format_check_module_exists(
    *, name: str, version: str, indexed: bool, edition: str | None,
    license_val: str | None = None,
    repo: str | None, is_ee_confusion: bool, viindoo_equivalent: str | None,
    ee_source: str = "",
) -> str:
    lines = [f"check_module_exists({name!r}, {version})"]
    lines.append(f"├─ Indexed:         {'Yes' if indexed else 'No'}")
    if indexed and edition:
        repo_suffix = f" [{repo}]" if repo else ""
        # WG-5 T1: derive human-readable edition label from license (preferred)
        # or from indexed edition enum.
        ed_label = _srv._edition_label(edition, license_val)
        lines.append(f"├─ Edition:         {ed_label}{repo_suffix}")
    lines.append(
        f"├─ Is EE confusion: {'Yes' if is_ee_confusion else 'No'}"
    )
    if is_ee_confusion:
        if viindoo_equivalent:
            lines.append(f"├─ Viindoo equiv:   {viindoo_equivalent}")
        else:
            lines.append("├─ Viindoo equiv:   (none — feature not in Viindoo stack)")
        # Differentiate source for debugging
        source_hint = ""
        if ee_source == "indexed":
            source_hint = f" (license={license_val})" if license_val else ""
        elif ee_source == "dict":
            source_hint = " (legacy hardcoded dict)"
        # ADR-0023 §2: English-only tool output.
        lines.append(
            f"├─ ⚠ WARNING: this is an Odoo Enterprise module{source_hint}. "
            "Do NOT depend on it in a Viindoo Community stack — "
            "this violates the GPL/Enterprise license boundary."
        )
    elif not indexed:
        # ADR-0023 §4.4: terminal branch — module genuinely not found, no
        # operator-shell hint (agents cannot execute shell commands).
        lines.append(
            "└─ Not indexed in this profile. "
            "Verify the module name, or call list_available_profiles to see indexed scope."
        )
        return "\n".join(lines)
    # Wave 5: YES branch emits Next: footer (ADR-0023 §4).
    lines.append(format_next_step([
        f"describe_module(name='{name}', odoo_version='{version}')"
        " for full overview",
    ]))
    return "\n".join(lines)


def _anti_patterns_for_convention(kind: str) -> list[str]:
    """Return convention-specific anti-pattern hints for find_override_point."""
    if kind == "compute":
        return [
            "Calling super() in compute method — Odoo rebinds via @api.depends, "
            "super-chain semantically meaningless",
            "Forgetting @api.depends — silent stale data on field reads",
        ]
    if kind in ("inverse", "search", "default"):
        return [
            f"Calling super() in {kind} method — Odoo rebinds via decorator, "
            "super-chain has no effect",
        ]
    if kind == "action":
        return list(_ANTI_PATTERNS_BASE) + [
            "Returning bool/None instead of action_window dict — UI can't refresh",
        ]
    if kind == "crud":
        return list(_ANTI_PATTERNS_BASE) + [
            "Missing @api.model_create_multi on create() override — slow batch import",
            "Treating vals as single dict instead of vals_list — silent data loss",
        ]
    return list(_ANTI_PATTERNS_BASE)


def _fetch_method_for_diff(session, model: str, method: str, version: str) -> dict | None:
    """Fetch a single Method node's properties for cross-version diff.

    Returns a dict with keys: decorators, convention_kind, super_safety,
    has_super_call, signature. Returns None when no Method found.
    Aggregates across all modules (decorators union, super_call OR).
    """
    rows = session.run(f"""
        MATCH (mth:Method {{name: $method, model: $model, odoo_version: $v}})
        WHERE {_srv._scope_pred("mth")}
        RETURN mth.decorators AS decorators,
               mth.convention_kind AS ck,
               mth.super_safety AS ss,
               coalesce(mth.has_super_call, false) AS has_super,
               mth.signature AS signature
        ORDER BY mth.module
    """, method=method, model=model, v=version, **_srv._scope()).data()
    if not rows:
        return None
    # Merge across override chain: union decorators, OR has_super, first non-null sig
    all_decs: list[str] = []
    seen_decs: set[str] = set()
    has_super = False
    sig: str | None = None
    ck = rows[0]["ck"] or "private"
    ss = rows[0]["ss"] or "usually"
    for r in rows:
        for d in (r["decorators"] or []):
            if d not in seen_decs:
                seen_decs.add(d)
                all_decs.append(d)
        if r["has_super"]:
            has_super = True
        if sig is None and r["signature"] is not None:
            sig = r["signature"]
    return {
        "decorators": all_decs,
        "convention_kind": ck,
        "super_safety": ss,
        "has_super_call": has_super,
        "signature": sig,
    }


def _diff_method_across_versions(
    model: str, method: str, from_version: str, to_version: str,
    *, _driver=None,
) -> str:
    """Diff a method between two Odoo versions.

    Compares decorator set, convention_kind, super_safety, and signature
    between from_version and to_version. Returns tree-formatted string.
    """
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        from_data = _fetch_method_for_diff(session, model, method, from_version)
        to_data = _fetch_method_for_diff(session, model, method, to_version)

    header = f"Method version diff ({model}.{method}: {from_version} → {to_version})"
    lines = [header]

    # Presence
    if from_data and to_data:
        presence_label = "both versions present"
    elif from_data and not to_data:
        presence_label = f"deleted in {to_version} (not found)"
    elif not from_data and not to_data:
        presence_label = (
            f"absent in both {from_version} and {to_version}"
            " (model/method may not be indexed)"
        )
        lines.append(f"├─ Status:           {presence_label}")
        lines.append(format_next_step([
            f"model_inspect(model='{model}', method='methods', odoo_version='{to_version}')"
            " to verify the method name",
        ]))
        return "\n".join(lines)
    else:
        presence_label = f"added in {to_version} (not in {from_version})"
    lines.append(f"├─ Status:           {presence_label}")

    # Decorator diff
    from_decs = set(from_data["decorators"]) if from_data else set()
    to_decs = set(to_data["decorators"]) if to_data else set()
    removed = sorted(from_decs - to_decs)
    added = sorted(to_decs - from_decs)
    if removed or added:
        lines.append("├─ Decorator changes:")
        items = [f"Removed in {to_version}: {d}" for d in removed]
        items += [f"Added in {to_version}:   {d}" for d in added]
        last_idx = len(items) - 1
        for i, text in enumerate(items):
            connector = "└─" if i == last_idx else "├─"
            lines.append(f"│   {connector} {text}")
    else:
        lines.append("├─ Decorator changes: none")

    # Convention diff
    from_ck = from_data["convention_kind"] if from_data else "?"
    to_ck = to_data["convention_kind"] if to_data else "?"
    if from_ck != to_ck:
        lines.append(f"├─ Convention:        changed ({from_ck} → {to_ck})")
    else:
        lines.append(f"├─ Convention:        unchanged ({from_ck})")

    # Signature diff
    _NULL_HINT = "(signature not available for this version)"
    from_sig = from_data["signature"] if from_data else None
    to_sig = to_data["signature"] if to_data else None
    from_sig_str = from_sig if from_sig is not None else _NULL_HINT
    to_sig_str = to_sig if to_sig is not None else _NULL_HINT
    if from_sig is None or to_sig is None:
        lines.append(
            f"├─ Signature:         {from_version}={from_sig_str}"
            f" → {to_version}={to_sig_str}"
        )
    elif from_sig != to_sig:
        lines.append(
            f"├─ Signature:         {from_version}={from_sig}"
            f" → {to_version}={to_sig}"
        )
    else:
        lines.append(f"├─ Signature:         unchanged ({from_sig})")

    # Super safety
    from_ss = from_data["super_safety"] if from_data else "?"
    to_ss = to_data["super_safety"] if to_data else "?"
    if from_ss != to_ss:
        lines.append(f"├─ Super safety:      changed ({from_ss} → {to_ss})")
    else:
        lines.append(f"├─ Super safety:      unchanged ({from_ss})")

    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"model_inspect(model='{model}', method='method', method_name='{method}'"
        f", odoo_version='{to_version}') for full chain detail",
        f"find_examples(query='{method} override', odoo_version='{to_version}')"
        " for prior art",
    ]))
    return "\n".join(lines)


def _find_override_point(
    model: str, method: str, odoo_version: str = "auto",
    *, to_version: str = "", _driver=None,
) -> str:
    """Inspect Method override chain + surface convention hints + anti-patterns.

    When to_version is non-empty and differs from odoo_version, performs a
    cross-version diff instead of single-version inspection.
    """
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        v = _srv._resolve_version(odoo_version, session)

    # Cross-version diff mode
    if to_version and to_version != v:
        return _diff_method_across_versions(
            model, method, from_version=v, to_version=to_version, _driver=driver,
        )

    # Single-version mode (existing behaviour)
    # ADR-0034 WI-4 (R-09 fix): apply tenant boundary filter even though
    # find_override_point has no profile_name param — use None so admin is
    # unrestricted and tenant boundary is still enforced via _srv._effective_allowed.
    with driver.session() as session:
        records = session.run("""
            MATCH (mth:Method {name: $method, model: $model, odoo_version: $v})
            WHERE ($own IS NULL OR (size(mth.profile) > 0
                   AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
            OPTIONAL MATCH (mod:Module {name: mth.module, odoo_version: $v})
            RETURN mth.module AS module, mth.convention_kind AS ck,
                   mth.super_safety AS ss, mth.return_required AS rr,
                   coalesce(mth.has_super_call, false) AS has_super,
                   coalesce(mod.repo_url, mod.repo) AS repo, mod.edition AS edition
            ORDER BY mth.module
        """, method=method, model=model, v=v, **_srv._scope(None)).data()

    if not records:
        next_line = format_next_step([
            f"model_inspect(model='{model}', method='methods', odoo_version='{v}')"
            " to find the actual method name",
        ])
        return (
            f"find_override_point({model!r}, {method!r}, {v})\n"
            f"├─ method not found on model {model!r} in Odoo {v}\n"
            + next_line
        )

    convention_kind = records[0]["ck"] or "private"
    super_safety = records[0]["ss"] or "usually"
    return_required = bool(records[0]["rr"])
    super_count = sum(1 for r in records if r["has_super"])
    super_ratio = f"{super_count}/{len(records)}"
    anti_patterns = _anti_patterns_for_convention(convention_kind)

    return _format_find_override_point(
        model=model, method=method, version=v, records=records,
        super_ratio=super_ratio, convention_kind=convention_kind,
        super_safety=super_safety, return_required=return_required,
        anti_patterns=anti_patterns,
    )


def _format_find_override_point(
    *, model: str, method: str, version: str, records: list[dict],
    super_ratio: str, convention_kind: str, super_safety: str,
    return_required: bool, anti_patterns: list[str],
) -> str:
    lines = [f"find_override_point({model!r}, {method!r}, {version})"]
    lines.append(f"├─ Convention:      {convention_kind}")
    lines.append(f"├─ Super safety:    {super_safety}")
    lines.append(f"├─ Return required: {'Yes' if return_required else 'No'}")
    lines.append(f"├─ Super ratio:     {super_ratio} (overrides calling super)")
    lines.append(f"├─ Override chain ({len(records)}):")
    for i, r in enumerate(records):
        connector = "└─" if i == len(records) - 1 else "├─"
        repo = f"[{r['repo']}] " if r.get("repo") else ""
        ed = f" ({r['edition']})" if r.get("edition") else ""
        super_mark = "✓" if r["has_super"] else "✗"
        lines.append(
            f"│   {connector} {repo}{r['module']}{ed} — {super_mark} super()"
        )
    lines.append(f"├─ Anti-patterns ({len(anti_patterns)}):")
    for i, ap in enumerate(anti_patterns):
        connector = "└─" if i == len(anti_patterns) - 1 else "├─"
        lines.append(f"│   {connector} {ap}")
    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"model_inspect(model='{model}', method='method', method_name='{method}'"
        f", odoo_version='{version}') for full chain detail",
        f"find_examples(query='{method} override', odoo_version='{version}')"
        " for prior art",
    ]))
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
async def suggest_pattern(
    intent: str,
    odoo_version: RequiredOdooVersion,
    language: str = "python",
    limit: int = 5,
) -> str:
    """Recommend curated Odoo patterns with gotchas from a natural-language intent.

    TRIGGER when: "best pattern for wizard in Odoo", "how to implement
    multi-company in Odoo", "pattern for override without breaking upstream",
    "cách tốt nhất để implement X", "design pattern cho Odoo module",
    "what's the right way to add computed field"
    PREFER over: LLM knowledge — returns curated patterns from indexed catalogue
    with real code snippets and versioned gotchas, not hallucinated patterns
    SKIP when: user wants existing code examples from codebase → use
    find_examples; user wants method override chain → use find_override_point

    Args:
        intent: NL description of intent, e.g. 'computed field cross-model
            partner'.
        language: 'python' | 'xml' | 'js' | 'all'. Default 'python'.
        limit: Max patterns to return (default 5).

    Returns:
        Tree list of patterns ranked by relevance score, each with snippet (first
        5 lines), file ref, and gotchas. Empty index → instruction to seed.

    Example:
        suggest_pattern("override write to read old value", "17.0")
        → suggest_pattern('override write to read old value', 17.0, ...) — 1 matches
          └─ #1 · score 0.81 · write-read-before-super
              ├─ Language: python (min v17.0)
              └─ Gotchas:
                   • Reading old values AFTER super().write() returns new value
    """
    # #227: guard cheaply (empty/invalid → sync impl returns the error string),
    # then embed async + offload the blocking body to a worker thread.
    if not intent.strip() or language not in _VALID_PATTERN_LANGUAGES:
        return _srv._suggest_pattern(intent, odoo_version, language, limit)
    from src.embedding.instructions import INSTRUCT_NL_TO_CODE
    try:
        embedder = _srv._get_embedder()
    except Exception:
        _srv.logger.warning("suggest_pattern: embedder unavailable", exc_info=True)
        return (
            "suggest_pattern: embedder unavailable.\n"
            "Hint: check Ollama is running (default: http://localhost:11434)."
        )
    try:
        instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
        intent_vec = await _srv._embed_query(embedder, instruct, intent)
    except _srv.EmbedOverloaded as e:
        return f"suggest_pattern: {e}"
    except Exception:
        _srv.logger.warning("suggest_pattern: embedding query failed", exc_info=True)
        return (
            "suggest_pattern: embedding query failed — try again shortly, "
            "or verify the embedder service is reachable."
        )
    return await asyncio.to_thread(
        _srv._suggest_pattern,
        intent, odoo_version, language, limit,
        _embedder=embedder, _query_vec=intent_vec,
    )


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload
def check_module_exists(
    name: str,
    odoo_version: RequiredOdooVersion,
    profile_name: str | None = None,
) -> str:
    """Verify if a module is indexed and flag EE-confusion for Viindoo stack.

    TRIGGER when: "does module sale_management exist in Odoo 17", "is
    viin_sale available", "check if feature X is in standard Odoo", "module X
    có trong OCA không", "Odoo 17 có tính năng X chưa", "is helpdesk an EE
    module"
    PREFER over: searching manually — instant cross-version, cross-repo module
    existence check with Enterprise edition detection and Viindoo equivalent
    SKIP when: caller needs the module's contents (models, views, JS) — use
    describe_module instead, which returns a full architecture overview in
    one round-trip. user wants module field/method details → use model_inspect;
    user wants code examples from a module → use find_examples

    Args:
        name: Module technical name (e.g. 'sale', 'helpdesk', 'viin_helpdesk').
        profile_name: Optional inheritance-resolved profile filter. When set,
            narrows the check to modules visible in this profile (including
            parent profiles via the ancestor chain). Default None checks all.

    Returns:
        Tree text: Indexed yes/no, edition, EE-confusion flag, Viindoo
        equivalent (if any), and WARNING when name is an EE-only module.

    Example:
        check_module_exists('helpdesk', '17.0')
        → check_module_exists('helpdesk', 17.0)
          ├─ Indexed:         No
          ├─ Is EE confusion: Yes
          ├─ Viindoo equiv:   viin_helpdesk
          └─ ⚠ WARNING: this is an Odoo Enterprise module (legacy hardcoded dict).
    """
    return _srv._check_module_exists(name, odoo_version, profile_name=profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload
def find_override_point(
    model: str, method: str, odoo_version: RequiredOdooVersion, to_version: str = "",
) -> str:
    """Show override chain + super-call convention + anti-patterns for a method.

    TRIGGER when: "where should I override action_confirm in sale.order", "best
    override point for partner creation", "how to extend method X without
    breaking OCA", "override field X ở đâu là đúng", "điểm override phù hợp
    cho method Y", "is super() required for write override"
    PREFER over: model_inspect(method='method') — adds super() safety guidance
    and anti-patterns, not just the chain listing
    SKIP when: full override chain only → model_inspect(method='method');
    design pattern guidance → suggest_pattern

    Args:
        model: Odoo model dotted name (e.g. 'sale.order').
        method: Method name (e.g. 'action_confirm', '_compute_amount').
        odoo_version: From-version when in diff mode (see field schema for
            the required-version contract).
        to_version: Optional. When set, activates cross-version diff mode
            (e.g. '18.0' to diff 17.0 → 18.0). Default '' = single-version.

    Returns:
        Single-version: convention_kind, super_safety, return_required,
        super_ratio, override chain, and anti-patterns.
        Cross-version diff: presence, decorator changes, signature diff,
        convention and super safety change.

    Example:
        find_override_point('sale.order', 'action_confirm', '17.0')
        → find_override_point('sale.order', 'action_confirm', 17.0)
          ├─ Convention:      action
          ├─ Super safety:    always
          ├─ Return required: Yes
          ├─ Super ratio:     7/7
          └─ Anti-patterns (3): ...
    """
    return _srv._find_override_point(model, method, odoo_version, to_version=to_version)


# Bind the owning server module generation AFTER the tool functions are defined.
# sys.modules['src.mcp.server'] at THIS point is the generation that is importing
# this module (server.py imports this module from the very end of its own body,
# and that generation registered these tools onto its `mcp`). Binding at
# end-of-module — rather than via a top-level `from src.mcp import server`, which
# reads the stale `src.mcp` package attribute after a pop+reimport — makes `_srv`
# track the SAME generation as the tool objects defined above. That restores the
# pre-refactor bare-name behaviour: the impl bodies read the hub through
# `_srv.<name>` at call time so monkeypatch.setattr(srv, "_get_driver", ...),
# monkeypatch.setattr(srv, "_find_style_override", ...) and friends still take
# effect, and the pop + re-import fixtures see the impls re-exported on the fresh
# generation.
_srv = sys.modules["src.mcp.server"]
