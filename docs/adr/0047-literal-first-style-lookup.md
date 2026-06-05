# ADR-0047 — Literal-first style/example lookup + HNSW recall mitigation (#255)

**Status:** Accepted
**Date:** 2026-06-04
**Authors:** Engineering team
**Related:** ADR-0023 (tool output tree grammar — `match:` tag fits it), ADR-0025
  (CSS/SCSS stylesheet indexing), ADR-0034 (multi-tenant pooled isolation — tenant
  choke preserved), ADR-0044/0045/0046 (embedding stack)

---

## Context

Issue #255: `find_style_override(".o_list_view","17.0")` and
`find_examples(".o_list_view", chunk_types=["css","scss","less"])` returned **0 results**
while a natural-language query with the same filter returned 3 — with a healthy embedder,
reproducible 100% on prod. The tool failed its own docstring example.

### Root cause (verified at code level + prod index-scan counter)

Both tools run pure pgvector ANN over the HNSW index `idx_embeddings_vec`
(`USING hnsw (vec vector_cosine_ops) WITH (m=16, ef_construction=200)`), with
`chunk_type IN ('css','scss','less')` as a **post-filter** and **no cosine threshold**:

```sql
... ORDER BY vec <=> %s::vector
WHERE odoo_version=%s AND chunk_type IN ('css','scss','less') [AND profile_name = ANY(%s)]
LIMIT min(limit, FIND_EXAMPLES_ANN_LIMIT)
```

Every query string is wrapped with `INSTRUCT_NL_TO_CODE` before embedding
(`src/embedding/instructions.py`). A literal selector `.o_list_view` becomes a code-ish
out-of-distribution vector → HNSW's top-`ef_search`(=40) candidates contain 0 css/scss rows
→ post-filter empties the pool → "Found 0". This is textbook **HNSW post-filter recall
collapse** (prod ground truth: `idx_embeddings_vec.idx_scan` +2 over 2 calls; query runs
through HNSW, not bitmap+exact-sort).

### Schema fact shaping the fix

(`src/indexer/writer_pgvector.py:343/375/414`, `parser_scss.py`)

| chunk_type | `entity_name` stored | variable name location |
|---|---|---|
| css | raw — `.o_list_view` | n/a |
| scss/less | `{kind}:{name}` — `selector:.o_list_view`, `mixin:o-flex` | — |
| scss/less variable block | `variable:{file_stem}:variables` | **only in `content`** |

So `.`/`#` selectors are in `entity_name`; `$`/`@` variables are only in `content`.

---

## Decision

1. **Literal-first lookup.** A verbatim CSS token (selector / variable / mixin) is matched
   by a deterministic substring ILIKE **before** ANN, in a shared helper
   `_literal_style_lookup`. The column is **routed by token shape**: selectors
   (`.`/`#`/`[`/`&`/bare-ident) → `entity_name`; variables (`$`/`@`) → `content` (the only
   place the variable name lives). This is plan-independent and embedder-independent.
2. **Detection** lives in a new pure helper `src/mcp/style_literal.py`
   (`is_literal_token` / `literal_column` / `ilike_pattern`, imports only `re` — no
   pipeline cross-import). NL phrases (whitespace, plain words) and known at-rule keywords
   (`@media`/`@import`/`@supports`/`@keyframes`/`@font-face`) are excluded to avoid floods.
   LIKE metacharacters (`%`, `_`, `\`) are escaped and applied with `ESCAPE '\'`.
3. **Merge.** Literal rows run first; ANN backfills remaining slots; results are merged and
   deduped on `(chunk_type, module, file_path, entity_name, chunk_idx)`, literal ranked
   above semantic. The ANN path is byte-for-byte unchanged for NL queries (zero regression).
   In `find_examples`'s Neo4j rerank, literal rows (`cosine=None`) get a descending floor
   `LITERAL_RANK_FLOOR + (n-i)*eps` so they sort on top while preserving SQL order
   (deterministic tiebreak per the project ORDER BY rule).
4. **Scope = both tools.** `find_style_override` and `find_examples` (only when
   `chunk_types ⊆ {css,scss,less}` AND the query is literal-shaped) share the helper.
5. **HNSW recall mitigation (general, flag-gated).** `_set_iterative_scan` issues
   `SET LOCAL hnsw.iterative_scan = 'relaxed_order'` (pgvector ≥0.8) inside the existing
   `_rls_read_tx` transaction before each ANN execute, gated by the constant
   `HNSW_ITERATIVE_SCAN` (set to `''` to revert with no code change). This lets filtered
   semantic (NL) queries keep scanning past the post-filter until LIMIT is met. Literal-first
   does **not** depend on it.
6. **Provenance.** Each rendered hit keeps a score-shaped token and appends
   `· match: literal|semantic` (ADR-0023 grammar preserved); the header notes the split.
7. **Embedder-outage robustness.** A literal style query never fetches/embeds on the hot
   path; if the embedder is down (the VRAM-contention ops scenario in the issue), literal
   lookups still serve. Applies to both sync bodies and both async wrappers (the wrapper is
   where the original blocking pre-embed lived).
8. **Deferred:** `pg_trgm` GIN index on `entity_name` — current rowset (~5k css/scss rows,
   narrowed by `idx_embeddings_filter`) makes ILIKE sub-millisecond; add the index only at
   hundreds of thousands of rows (a code comment marks the trigger).

### Accepted trade-off — `relaxed_order`

`relaxed_order` may return filtered-semantic rows in slightly non-exact distance order vs
the prior default (`off`). This can perturb the ranking of NL `find_examples`/
`find_style_override` results (not literal results, which are deterministic). We accept this
minor reordering: it is gated by `HNSW_ITERATIVE_SCAN` (one-line revert) and the recall gain
(returning results where the baseline collapsed to 0) outweighs exact-order stability for
filtered semantic queries.

---

## Consequences

- AC1–AC4 satisfied deterministically; AC5 (iterative_scan) shipped as flag-gated mitigation
  (behavioral recall-collapse reproduction in a testcontainer is not deterministic at small
  row counts, so no behavioral test claim — code shipped, no xfail-as-evidence).
- **Tool count stays 24** (helpers are internal; no new `@mcp.tool`). **No migration**
  (iterative_scan is a runtime GUC; ILIKE is a query). Tool output stays English-only.
- Tenant choke `profile_name = ANY(%s)` (ADR-0034) preserved in the literal SQL.
- The `HNSW_ITERATIVE_SCAN` kill-switch is **env-gated** (`os.getenv`, default
  `'relaxed_order'`): ops can set `HNSW_ITERATIVE_SCAN=''` to disable at runtime with no
  code change. `_literal_style_lookup` asserts its interpolated `col`/`extra_cols` against
  closed allowlists as a permanent SQL-injection barrier (defense-in-depth — inputs are
  already constants today).

## Known limitations (accepted / deferred — PR #257 review)

`is_literal_token` is a deliberately conservative heuristic. The following are accepted; in
every case the query still returns correct results via ANN backfill (literal-first never
makes a query *worse* than the prior ANN-only behaviour):

- **Bare element selectors** (`button`, `form`, `div`, `table`) are **not** literal-routed —
  the bare-ident branch requires a `-`/`_` separator so plain English words don't hijack NL
  search. Element selectors fall through to ANN. Low value to special-case; deferred.
- **At-rule with no space** (`@media(max-width:600px)`) bypasses the exact-keyword flood
  guard and is treated literal → `content` ILIKE usually returns 0, then ANN backfills. The
  result is not wrong; only the `match:` provenance label can momentarily mislead.
- **Hyphenated NL token** (`primary-color`) is treated literal → may mis-rank slightly, but
  ANN backfill keeps the result set non-empty and relevant.

These are heuristic edges, not correctness bugs; revisit only if telemetry shows real
mis-routing. The async wrapper / sync body literal gate (`style_only + is_literal_token`)
is intentionally duplicated across the two tools rather than abstracted — kept inline for
readability; a `_should_literal()` helper is a possible future dedup if a third caller appears.

---

## Amendment 2026-06-05 (PR #266 WI-9 — lexical fallback for `find_examples` when embedder is down, #264)

**Scope:** Extends Decision 7 (embedder-outage robustness) to cover the general `find_examples` corpus (not only style chunks), via a new standalone helper.

### What changed

Issue #264: when the Ollama embedder is unreachable, `find_examples(query="computed field pattern")` raised a `RuntimeError` and returned no output, rather than a degraded-but-useful result.

PR #266 WI-9 introduces `src/mcp/example_lexical.py` — a pure Python lexical search helper (no embedder dependency, no pipeline cross-import) used as a fallback for `find_examples` when embedding fails:

1. **Lexical token extraction (`_tokenize`):** splits the query on non-word chars (`[^\w.]+`), drops stop-words and length-1 tokens, returns up to `LEXICAL_MAX_TOKENS=8` tokens. The tokenizer regex is linear (single negated class; no catastrophic backtracking — verified by `test_pathological_input_completes_fast`).
2. **Entity-name-first ILIKE search:** runs `entity_name ILIKE %token%` against the embeddings table for each token, union-deduped by `(chunk_type, module, file_path, entity_name, chunk_idx)`, filtered by `odoo_version` and the ADR-0034 tenant choke (`profile_name = ANY(%s)`). Matches are labeled `match: lexical` (ADR-0023 grammar).
3. **Graceful zero-result disclosure:** when the lexical search also returns 0 rows, the tool emits a structured banner: `"Found 0 results (lexical search returned nothing for tokens: ...)"`, distinguishing "search worked but no match" from "tool errored". `EmbedOverloaded` (controlled "server busy" string) continues to pass through as-is.
4. **Scope:** `find_examples` only, for ALL chunk types (not limited to `css/scss/less`). `find_style_override` already has its own literal-first path (D7 above); the two paths are independent.
5. **`/ready` not gated on embedder:** ADR-0046 decision confirmed: `/ready` probes Postgres reachability + embedding row count only. A failed embed call does not affect `/ready`. The lexical fallback means `find_examples` remains partially functional even when `/ready` would say "ready" but the embedder is actually down.
