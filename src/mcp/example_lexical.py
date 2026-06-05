# SPDX-License-Identifier: AGPL-3.0-or-later
"""Embedder-free lexical fallback for find_examples (issue #264, WI-9).

When the embedder is unavailable, an NL query that normally goes through ANN
search can fall back to ILIKE keyword matching against the embeddings table.
Quality is lower than semantic search (match: lexical vs match: semantic) but
degraded-but-useful is better than zero results.

Design (ADR-0047 extension note):
- Token strategy: whitespace-split query, drop tokens < 3 chars + English
  stopwords; OR of surviving tokens against entity_name column ILIKE.
- Column: entity_name primary (precise: matches action_confirm, sale.order,
  _compute_amount_total); content is not searched (avoids spam over-match).
- Ordering: length(entity_name) ASC, module ASC, chunk_idx ASC — shortest
  entity name = most specific match, mirrors _literal_style_lookup.
- RLS: caller must pass the same allowed list from _effective_allowed(); the
  tenant choke (ADR-0034) is preserved — never bypassed.
- match tag: 'lexical' (distinguishes from 'literal' css path and 'semantic').
- Chunk types: respects the caller's selected_types filter; default = all types.

Caller is server.py._find_examples only.  suggest_pattern is out of scope
(small curated catalogue — see ADR-0047 note, #264 acceptance).
"""
import re

# Short English stopwords to drop before ILIKE matching.  Kept minimal —
# only truly content-free words that produce useless matches in code corpora.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "for", "on", "with",
    "is", "are", "was", "be", "by", "at", "as", "it", "its", "from", "how",
    "can", "do", "use", "get", "set", "has",
})

# Tokenise on any non-alphanumeric-underscore-dot sequence.
_TOKEN_RE = re.compile(r"[^\w.]+")


def _extract_keywords(query: str) -> list[str]:
    """Split query into searchable tokens: keep >=3 chars, not a stopword.

    Returns deduplicated list preserving order.  Order matters for the ILIKE
    alternation — earlier (longer) tokens are typically more specific.

    Args:
        query: Raw user query string.

    Returns:
        Non-empty list of lowercase keyword strings, or [] if nothing survives.
    """
    seen: set[str] = set()
    result: list[str] = []
    for tok in _TOKEN_RE.split(query.lower()):
        tok = tok.strip(".")
        if len(tok) >= 3 and tok not in _STOPWORDS and tok not in seen:
            seen.add(tok)
            result.append(tok)
    return result


def lexical_example_lookup(
    cur,
    query: str,
    odoo_version: str,
    allowed: list[str] | None,
    limit: int,
    selected_types: list[str],
    extra_cols: list[str] | None = None,
) -> list[dict]:
    """ILIKE keyword search against embeddings.entity_name (degraded fallback).

    Runs entirely in SQL without the embedder — suitable for use when the
    embedder is down or overloaded.  Results are labelled match='lexical' so
    the caller can communicate degraded quality to the agent.

    Args:
        cur:           Open psycopg2 cursor (inside _rls_read_tx context).
        query:         Raw NL query string to tokenise and search.
        odoo_version:  Resolved Odoo version string.
        allowed:       Tenant-filter list from _effective_allowed(), or None
                       (None = admin/unrestricted; [] = deny-all).
        limit:         Row cap (caller applies min(user_limit, ANN_LIMIT)).
        selected_types: Chunk type filter; empty list = all types.
        extra_cols:    Additional SQL columns to SELECT (model_name, line_start,
                       repo, repo_id — same set as _literal_style_lookup extra_cols).

    Returns:
        List of dicts with keys matching the ANN row shape:
        chunk_type, module, entity_name, file_path, chunk_idx, content,
        cosine (None), match ('lexical').
        Extra columns included verbatim when requested.
        Returns [] when no keywords survive tokenisation or no rows match.

    Behaviour when tenant choke applies:
        allowed=None  -> no profile_name filter (admin/unrestricted).
        allowed=[]    -> AND profile_name = ANY('{}') -> matches nothing (deny-all).
        allowed=[...] -> AND profile_name = ANY(%s) with the list.
    """
    keywords = _extract_keywords(query)
    if not keywords:
        return []

    # Validate extra_cols against a fixed allowlist (same as _literal_style_lookup).
    _ALLOWED_EXTRA_COLS = frozenset({"model_name", "line_start", "repo", "repo_id"})
    if extra_cols:
        bad = [c for c in extra_cols if c not in _ALLOWED_EXTRA_COLS]
        if bad:
            raise ValueError(f"disallowed extra_cols: {bad}")

    base_cols = "chunk_type, module, entity_name, file_path, chunk_idx, content"
    extra_sql = (", " + ", ".join(extra_cols)) if extra_cols else ""

    # Build ILIKE OR conditions on entity_name.
    # Using ESCAPE '\\' so underscores in names like action_confirm are literal.
    # Each keyword becomes '%<kw>%' with metacharacters escaped.
    def _escape(tok: str) -> str:
        return tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    ilike_clauses = " OR ".join(
        "entity_name ILIKE %s ESCAPE '\\'" for _ in keywords
    )
    ilike_params = [f"%{_escape(k)}%" for k in keywords]

    # chunk_type filter: if selected_types non-empty, restrict; else all types.
    type_sql = ""
    type_params: list = []
    if selected_types:
        ph = ", ".join(["%s"] * len(selected_types))
        type_sql = f" AND chunk_type IN ({ph})"
        type_params = list(selected_types)

    # Tenant/RLS filter.
    prof_sql = "" if allowed is None else " AND profile_name = ANY(%s)"
    prof_params: list = [] if allowed is None else [allowed]

    params: list = [odoo_version] + ilike_params + type_params + prof_params + [limit]

    cur.execute(
        f"""SELECT {base_cols}{extra_sql}
            FROM embeddings
            WHERE odoo_version = %s
              AND ({ilike_clauses}){type_sql}{prof_sql}
            ORDER BY length(entity_name) ASC, module ASC, chunk_idx ASC
            LIMIT %s""",
        params,
    )
    rows = cur.fetchall()
    n_base = 6  # chunk_type, module, entity_name, file_path, chunk_idx, content
    result: list[dict] = []
    for r in rows:
        d: dict = dict(
            chunk_type=r[0], module=r[1], entity_name=r[2],
            file_path=r[3], chunk_idx=r[4], content=r[5],
            cosine=None, match="lexical",
        )
        if extra_cols:
            for idx, ecol in enumerate(extra_cols, n_base):
                d[ecol] = r[idx]
        result.append(d)
    return result
