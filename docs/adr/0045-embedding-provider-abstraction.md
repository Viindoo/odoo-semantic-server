# ADR-0045 — Embedding Provider Abstraction

**Status:** Accepted
**Date:** 2026-06-01
**Authors:** Engineering team
**Related:** ADR-0010 (embedding observability), ADR-0044 (token-bounded embedding),
  ADR-0046 (MCP embed concurrency / anti-hang)

---

## Context

Before this wave, the codebase had one concrete embedder: `Qwen3Embedder`, hard-coupled
to the Ollama `/api/embed` endpoint and the Qwen3-Embedding wire format. Four coupling
points made switching providers painful:

1. **Endpoint + payload format** baked into the class (Ollama `/api/embed`,
   `{"model": ..., "input": [...]}`).
2. **Vector extraction** assumed `data["embeddings"]` (Ollama shape).
3. **Vector dimension** hard-coded as 1024 in the `DEFAULT_EMBEDDER_DIM` constant and
   in the `embeddings` table schema (`vector(1024)`).
4. **Query instruction** (Qwen asymmetric INSTRUCT prefix) was implicit — the same
   prefix was silently prepended to all query embeddings, which is wrong for symmetric
   OpenAI-style models.

Operators running OSM with OpenAI, Voyage, Text Embeddings Inference (TEI), vLLM, or
LiteLLM could not swap the embedder without modifying source code. The fixed dimension
also prevented running two providers side-by-side (e.g. Ollama for one profile, OpenAI
for another) because existing vectors would be invalidated silently.

---

## Decision

### D1 — `EmbedderClient` structural Protocol

```python
@runtime_checkable
class EmbedderClient(Protocol):
    model: str            # model identifier string (written to embeddings.embedding_model)
    dim: int              # vector dimension (written to embeddings.embedding_dim)
    num_ctx: int          # model context window (tokens)
    chars_per_token: float  # heuristic chars-per-token for token helpers

    def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_async(self, texts: list[str], *, read_timeout: int | None = None) -> list[list[float]]: ...
```

Any class that exposes these four read-only attributes and the two methods satisfies
the Protocol. `isinstance(x, EmbedderClient)` returns `True` at runtime (via
`@runtime_checkable`) so tests can assert the contract without importing the concrete
class.

### D2 — `_BaseHttpEmbedder` shared machinery

All HTTP backends share batch/retry/timeout/observability logic in `_BaseHttpEmbedder`.
Subclasses customise only the wire format by overriding four hooks:

| Hook | Responsibility |
|---|---|
| `endpoint_path` | URL suffix appended to the base URL |
| `query_instruction` | Prefix for asymmetric query embedding (`""` for symmetric models) |
| `_build_payload(texts)` | Build request JSON body |
| `_extract_vectors(data)` | Pull `list[list[float]]` from response JSON |

Everything else (truncation safety-net, MRL dim truncation, L2-normalisation,
sub-batch loop, retry/backoff, histogram/call_count observability, async off-thread
path) lives in the base class and is inherited unchanged.

### D3 — `Qwen3Embedder` and `OpenAICompatEmbedder`

| Class | Endpoint | Payload | Extraction | Instruction |
|---|---|---|---|---|
| `Qwen3Embedder` | `/api/embed` | `{model, input}` | `data["embeddings"]` | `INSTRUCT_NL_TO_CODE` |
| `OpenAICompatEmbedder` | `/v1/embeddings` | `{model, input}` | `data["data"][i]["embedding"]` | `""` |

`OpenAICompatEmbedder` covers OpenAI, Voyage AI, Hugging Face TEI, vLLM
(`/v1/embeddings`), and LiteLLM proxy — any service that implements the OpenAI
embeddings wire format.

### D4 — `make_embedder(backend, **kwargs)` factory

```python
EMBEDDER_BACKEND: str = os.getenv("EMBEDDER_BACKEND", "ollama")

def make_embedder(backend: str | None = None, **kwargs) -> EmbedderClient:
    chosen = (backend or EMBEDDER_BACKEND or "ollama").strip().lower()
    if chosen in ("fake", "test"):
        return FakeEmbedder(...)
    if chosen in ("openai", "tei", "voyage", "vllm", "litellm", "openai-compat"):
        return OpenAICompatEmbedder(**kwargs)
    if chosen in ("ollama", "qwen", "qwen3"):
        return Qwen3Embedder(**kwargs)
    raise ValueError(...)
```

All callers (indexer `__main__`, MCP `server.py`, `seed_patterns.py`) go through the
factory. Backend selection is a single env-var flip; no code change required to switch
providers.

### D5 — Per-backend capability attributes (`model`, `dim`, `num_ctx`, `chars_per_token`)

Each embedder instance carries its capability attributes, set in `__init__`. These are
the source of truth for:
- Which model string to write to `embeddings.embedding_model`.
- What dimension to write to `embeddings.embedding_dim` and to pass to the dim
  fail-fast guard.
- What context window size to use for truncation.
- What chars-per-token ratio to use for token estimation.

`FakeEmbedder` mirrors all four so tests that inject a fake still exercise the full
Protocol path.

### D6 — `embedding_model` + `embedding_dim` columns (migration m13_018)

Two new columns on the `embeddings` table:

```sql
ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS embedding_dim   INT;
```

Backfill: existing rows (all indexed with Qwen3-embedding-q5km at dim=1024 before this
migration) receive `embedding_model = 'qwen3-embedding-q5km'` and `embedding_dim = 1024`.

The writer stamps every new row:
```python
c.as_tuple(vecs[i], embedder.model, embedder.dim)
```

The `ON CONFLICT DO UPDATE` clause also updates both columns so a re-index with a
different model overwrites the old provenance.

A partial index on `embedding_model` accelerates the fail-fast guard and any future
per-model cosine search filter:

```sql
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings (embedding_model)
    WHERE embedding_model IS NOT NULL;
```

### D7 — Fail-fast dim mismatch guard (`embedding_guard.py`)

`assert_dim_matches(conn, configured_dim)` is called once per `write_module_embeddings`
batch (after acquiring the PG connection, before the DELETE/INSERT):

```python
from src.db.embedding_guard import assert_dim_matches, EmbedderDimMismatch

assert_dim_matches(pg_conn, configured_dim=embedder.dim)
```

If the stored `embedding_dim` differs from the configured dim, `EmbedderDimMismatch` is
raised with a clear message including both values and the stored model name. The indexer
and MCP server should treat this as a `SystemExit(1)` — operating with mixed vector
spaces silently corrupts cosine-similarity results.

**WARNING:** Switching to a provider with a different embedding dimension requires a
full reindex of all profiles:

```bash
python -m src.indexer --full --profile <name>
```

Running the migration alone is not sufficient — the guard will reject writes with the
new dim until the old rows are cleared by the full reindex.

### D8 — Vector dimension un-hardcoded in pgvector schema

The `embeddings.vec` column type (`vector(N)`) is the only place where the dimension
must be hard-coded in SQL. The default (`1024`) is kept as the initial schema dimension
and as `DEFAULT_EMBEDDER_DIM`. If a different dimension is needed, the operator must:
1. Create a new migration that alters the column: `ALTER TABLE embeddings ALTER COLUMN vec TYPE vector(<new_dim>)`.
2. Run a full reindex for all profiles.
3. Update `DEFAULT_EMBEDDER_DIM` and `.env` / systemd to set `EMBEDDER_BACKEND` + any
   dim-related flags for the new model.

This is intentionally a manual operation — automatic dimension migration would risk
silently mixing incompatible vector spaces.

---

## Consequences

### Positive

- Operators can switch embedding providers by changing `EMBEDDER_BACKEND` (and related
  URL / model / dim env vars) without modifying source code.
- `embedding_model` + `embedding_dim` columns provide provenance for every vector,
  enabling future per-model cosine search, model-drift detection, and re-embed targeting.
- The fail-fast guard prevents silent vector space corruption when the operator changes
  the provider without reindexing.
- `FakeEmbedder` now exposes all Protocol attributes, keeping CI tests isolated from
  real embedder infrastructure.

### Negative / Trade-offs

- Changing embedding dimension requires a full reindex — this can take hours on large
  codebases (production: 16h for v8-v19 full reindex). Plan dimension changes carefully.
- The `embedding_model` / `embedding_dim` backfill in m13_018 assumes all pre-migration
  rows were indexed with `qwen3-embedding-q5km` at dim=1024. Operators who already
  changed the model before m13_018 must manually correct the backfill.
- `OpenAICompatEmbedder` uses no query instruction (symmetric embedding). If the chosen
  model requires an asymmetric instruction prefix, a custom subclass is needed.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDER_BACKEND` | `ollama` | Provider selection: `ollama` / `openai` / `tei` / `fake` |
| `EMBEDDER_NUM_CTX` | `4096` | Model context window (tokens) |
| `EMBEDDER_CHARS_PER_TOKEN` | `3.0` | Conservative chars-per-token estimate |

### Migration

The `embeddings.embedding_model` / `embedding_dim` provenance columns (originally shipped
as `m13_018`) are now folded into the squashed baseline `migrations/0001_initial.sql`
(commit `cc7687b`, 2026-06-14) - the standalone `m13_018_*.sql` file no longer exists.
Apply via `python -m src.db.migrate`; existing pre-squash deployments already have it.
