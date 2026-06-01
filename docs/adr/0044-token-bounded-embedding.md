# ADR-0044 — Token-Bounded Embedding (fix #226)

**Status:** Accepted
**Date:** 2026-06-01
**Authors:** Engineering team
**Related:** ADR-0010 (embedding observability), ADR-0003 (pattern storage), ADR-0045 (provider abstraction)

---

## Context

### The problem — char-based chunking does not bound tokens

Before this fix, the chunking layer (writer_pgvector `_sliding`) split text by a fixed
character window (`_WINDOW_CHARS = 2048`) and a fixed overlap (`_OVERLAP_CHARS = 256`).
Character count is not a reliable proxy for token count: code-dense text, XML
attributes, and comment blocks tokenize at very different char-per-token ratios.

Two concrete sources of oversized payloads were identified:

**Pattern chunks (`make_pattern_chunks`):** `patterns.json` examples are unbounded by
size. A large pattern (code snippet + gotchas + usage notes) can produce a
`chunk_type='pattern_example'` row whose `content` is substantially longer than the
embedder model's context window (`num_ctx`, default 4096 tokens). The whole entry was
sent as a single text.

**MCP query path (`_cap_query_text` / `_embed_query`):** A user can paste kilobytes of
code into the `query` / `intent` / `selector` arguments of `find_examples`,
`suggest_pattern`, or `find_style_override`. The full paste was forwarded to the
embedder, wasting context and slowing every query.

Neither path had a token-aware cap; both relied on the embedder to truncate silently or
raise — neither behavior is acceptable (silent truncation corrupts meaning; raising
fails the request).

### Why not a real tokenizer?

Adding a real tokenizer (tiktoken / sentencepiece) would introduce a model-specific
runtime dependency. The fix uses a deliberately conservative chars-per-token estimate
(`EMBEDDER_CHARS_PER_TOKEN`, default 3.0) that over-estimates token count. This means
the system splits or truncates slightly more aggressively than strictly necessary, which
is the safe direction: a short chunk is always embeddable; an oversized one may overflow
the model context and produce a truncated or errored vector.

---

## Decision

### D1 — Module-level token helpers (`estimate_tokens` / `split_by_token_budget`)

Two helpers live in `src/indexer/embedder.py` (imported by the chunking layer):

```python
def estimate_tokens(text: str, chars_per_token: float = EMBEDDER_CHARS_PER_TOKEN) -> int:
    """Cheap heuristic: ceil(len(text) / chars_per_token). Intentionally over-estimates."""

def split_by_token_budget(text: str, budget: int, chars_per_token: float = ...) -> list[str]:
    """Split text into pieces each estimated <= budget tokens. Fast-path if already fits."""
```

Both are dependency-free and deterministic. The char-per-token ratio is tunable via
`EMBEDDER_CHARS_PER_TOKEN` (env var, default 3.0). Shared between the indexer writer
(WI-B) and the MCP query path so both use the same estimate.

### D2 — Token cap in `_sliding` (build-time chunking)

`_sliding` gains an inner `_token_split_window` call: after each char window is sliced,
if `estimate_tokens(window) > EMBEDDER_TOKEN_BUDGET`, the window is further split by
`split_by_token_budget`. The sub-chunks inherit the same `chunk_idx` offset sequence,
preserving the pgvector `ux_embeddings_chunk` unique key invariant. `EMBEDDER_TOKEN_BUDGET`
defaults to 3500 tokens — a margin below `EMBEDDER_NUM_CTX` (default 4096) to leave
headroom for the instruction prefix and tokenizer drift.

The same pattern applies in `make_pattern_chunks`, `make_view_chunks`,
`make_js_chunks`, and `make_style_chunks`: each helper calls `split_by_token_budget`
before emitting an `EmbeddingChunk`, so no individual chunk content exceeds the budget.

### D3 — Truncation choke-point in `_BaseHttpEmbedder._truncate_to_ctx`

As a last line of defence — catching anything the chunking layer missed — the HTTP
embedder truncates each text to `num_ctx * chars_per_token` characters before the HTTP
call. This is a single-vector safety-net: it never splits into extra vectors (length
invariant `len(out) == len(texts)` is preserved), only clamps. A `WARNING` log line
records the truncation with before/after char counts.

```
embedder truncating text from 18234 to 12288 chars (ctx safety-net)
```

The choke-point fires only when the chunking layer produces an oversized chunk (e.g.
a new parser extension that forgets to call `split_by_token_budget`). Under normal
operation with the updated writers it should never trigger.

### D4 — MCP query cap (`_cap_query_text`)

`server.py` calls `_cap_query_text(embedder, text)` before embedding any user-supplied
query string:

```python
def _cap_query_text(embedder, text: str) -> str:
    chars_per_token = getattr(embedder, "chars_per_token", None) or 4.0
    return split_by_token_budget(text, EMBEDDER_TOKEN_BUDGET, chars_per_token)[0]
```

Only the first chunk (the leading `EMBEDDER_TOKEN_BUDGET` tokens) is used — no
multi-chunk query embedding is performed. This is intentional: a query is a search
intent, not a document; embedding only the leading context is correct and fast.

### D5 — Bug B: length-guard in `_embed_one`

If an embedding backend returns a different number of vectors than input texts, every
downstream chunk-to-vector mapping is silently misaligned. A length-guard raises
immediately:

```python
if len(embs) != len(texts):
    raise RuntimeError(
        f"embedder returned {len(embs)} vectors for {len(texts)} inputs"
    )
```

This guard fires before any vector is stored — the batch fails cleanly rather than
silently corrupting the `embeddings` table.

### D6 — Resilient skip-log in `_embed_chunks_resilient`

The build-time embed path (writer `write_module_embeddings`) now uses
`_embed_chunks_resilient` instead of a bare `embedder.embed()` call:

1. Happy path: embed all chunks in one batch call.
2. If the batch fails (any exception): degrade to per-chunk embedding.
3. Any individual chunk that fails on the degraded path is logged as a `WARNING` and
   **skipped** (not re-raised). The surviving chunks and their vectors are written.

This means a single malformed chunk cannot abort the indexing of an entire module.
Operators see the warning and can investigate without a full reindex.

### D7 — Observability preserved (ADR-0010 contract unchanged)

The ADR-0010 invariants are maintained:
- `call_count` increments once per `embed()` call (not per sub-batch).
- `embedder_batch_duration_seconds` histogram records once per HTTP round-trip.
- `FakeEmbedder` mirrors the Protocol (`call_count`, `model`, `dim`, `num_ctx`,
  `chars_per_token`) so tests remain accurate.

No vector pooling or merging was introduced: doing so would violate the existing
histogram-per-call contract and obscure latency signals.

---

## Consequences

### Positive

- Pattern chunks and MCP query text are now reliably within the model context window.
- A rogue parser extension that produces oversized chunks degrades gracefully (choke-point
  truncation + warning) rather than silently corrupting vectors.
- `Bug B`: misaligned backend responses now raise immediately, preventing silent
  pgvector corruption.
- Partial module embed failures no longer abort the entire module write.

### Negative / Trade-offs

- `estimate_tokens` is a heuristic (not a real tokenizer). The 3.0 chars/token ratio
  may over-split for prose-heavy content. Operators can tune `EMBEDDER_CHARS_PER_TOKEN`
  up (less splitting, closer to real token density) or down (more conservative).
- Pattern chunks that are split into multiple rows change the `chunk_idx` sequence for
  large patterns. A full reindex is recommended after deploying this change to ensure
  chunk layout is consistent; however, the `ON CONFLICT DO UPDATE` clause in the INSERT
  path makes partial re-runs safe.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDER_NUM_CTX` | `4096` | Model context window (tokens); mirrors `Modelfile num_ctx` |
| `EMBEDDER_TOKEN_BUDGET` | `3500` | Per-chunk target (tokens); leave headroom below `num_ctx` |
| `EMBEDDER_CHARS_PER_TOKEN` | `3.0` | Conservative chars-per-token estimate (low = safe over-split) |
