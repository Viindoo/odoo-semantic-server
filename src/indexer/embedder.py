# SPDX-License-Identifier: AGPL-3.0-or-later
"""Embedding client protocol + implementations.

EmbedderClient        — structural Protocol; any object with .embed()/.embed_async()
                        and the read-only model/dim/num_ctx/chars_per_token attrs
                        satisfies it.
FakeEmbedder          — deterministic, seeded, no GPU. For CI and unit tests.
Qwen3Embedder         — Ollama HTTP client for Qwen3-Embedding (INSTRUCT prefix).
OpenAICompatEmbedder  — /v1/embeddings client (OpenAI / Voyage / TEI / vLLM / LiteLLM).

The two HTTP backends share all batch / retry / timeout / observability logic via
_BaseHttpEmbedder; only the wire format (endpoint path, payload shape, vector
extraction, query instruction, metric label) differs.

make_embedder() is the factory — pick the backend by EMBEDDER_BACKEND (or arg).

Token helpers (estimate_tokens / split_by_token_budget) are module-level so the
chunking layer (WI-B) can import and reuse the same conservative estimate.
"""
import asyncio
import logging
import math
import random
import threading
import time
from typing import Protocol, runtime_checkable

import httpx

from src.constants import (
    DEFAULT_EMBEDDER_DIM,
    DEFAULT_EMBEDDER_MODEL,
    EMBEDDER_BACKEND,
    EMBEDDER_CHARS_PER_TOKEN,
    EMBEDDER_MAX_BATCH,
    EMBEDDER_NUM_CTX,
    EMBEDDER_RETRY_BACKOFF_BASE,
    EMBEDDER_RETRY_BACKOFF_MAX,
    EMBEDDER_TRUNCATE_CHARS_PER_TOKEN,
    TIMEOUT_EMBEDDER_CONNECT,
    TIMEOUT_EMBEDDER_READ,
    TIMEOUT_EMBEDDER_READ_QUERY,
    TIMEOUT_EMBEDDER_WRITE,
)
from src.embedding.instructions import INSTRUCT_NL_TO_CODE
from src.metrics import embedder_batch_duration_seconds

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token helpers (shared with the chunking layer — WI-B)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str, chars_per_token: float = EMBEDDER_CHARS_PER_TOKEN) -> int:
    """Cheap, dependency-free token estimate: ceil(len(text) / chars_per_token).

    Deliberately *over*-estimates (low chars_per_token) so callers chunk/truncate
    conservatively rather than overflow the model context. Not a real tokenizer.
    """
    if not text:
        return 0
    return math.ceil(len(text) / chars_per_token)


def split_by_token_budget(
    text: str,
    budget: int,
    chars_per_token: float = EMBEDDER_CHARS_PER_TOKEN,
) -> list[str]:
    """Split ``text`` into pieces each estimated to be at most ``budget`` tokens.

    Fast-path: if the whole text already fits the budget, return ``[text]``
    unchanged. Otherwise slice on a fixed character threshold derived from the
    budget (``int(budget * chars_per_token)``) — even, deterministic chunks.
    """
    if budget <= 0:
        return [text]
    if estimate_tokens(text, chars_per_token) <= budget:
        return [text]
    char_limit = int(budget * chars_per_token)
    if char_limit <= 0:
        return [text]
    return [text[i : i + char_limit] for i in range(0, len(text), char_limit)]


# ---------------------------------------------------------------------------
# Settings-overlay resolvers (DB-only; class_default wins when no row exists)
# ---------------------------------------------------------------------------

def _resolved_max_batch(class_default: int) -> int:
    """Resolve the live embedder batch size (WI-9 / ADR-0042).

    Reads ``embedding.max_batch_size`` from the ``app_settings`` DB overlay
    **only** — ``class_default`` (typically :data:`EMBEDDER_MAX_BATCH` or a
    test-injected subclass attribute) wins when no DB row exists.  This
    preserves the long-standing test contract where
    ``class _SmallBatchEmbedder(Qwen3Embedder): _MAX_BATCH = 2`` truly forces
    sub-batching, regardless of whether the bootstrap inserted the catalogue
    default (50) into ``app_settings``.

    Implementation note (WI-R F-005): uses the public
    :func:`src.settings.get_overlay_only` helper to avoid coupling to the
    private resolver internals while keeping the DB-only behaviour
    (returns ``None`` when no row exists so class_default still wins).
    """
    try:
        from src.settings import get_overlay_only
        value = get_overlay_only("embedding.max_batch_size")
        if value is None:
            return class_default
        return int(value)
    except Exception:
        return class_default


def _resolved_timeout_read(class_default: int) -> int:
    """Resolve the live embedder read-timeout (WI-9 / ADR-0042).

    Same DB-only resolution shape as :func:`_resolved_max_batch` — falls back
    to ``class_default`` (typically :data:`TIMEOUT_EMBEDDER_READ`) when no row
    exists.  This keeps unit tests that pin a specific timeout (no pool
    available) deterministic.

    WI-R F-005: uses public :func:`src.settings.get_overlay_only`.
    """
    try:
        from src.settings import get_overlay_only
        value = get_overlay_only("embedding.timeout_read_seconds")
        if value is None:
            return class_default
        return int(value)
    except Exception:
        return class_default


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class EmbedderClient(Protocol):
    # Read-only capability descriptors (impls set these as instance attrs).
    model: str
    dim: int
    num_ctx: int
    chars_per_token: float

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return L2-normalized embedding vectors for each text."""
        ...

    async def embed_async(
        self, texts: list[str], *, read_timeout: int | None = None
    ) -> list[list[float]]:
        """Async wrapper over embed() (runs in a worker thread)."""
        ...


# ---------------------------------------------------------------------------
# FakeEmbedder
# ---------------------------------------------------------------------------

class FakeEmbedder:
    """Deterministic embedder for CI — no GPU, no network.

    Uses a seeded RNG so the same text always gets the same vector within a
    test session (seed is global, not per-text, which is intentional — tests
    only need non-zero distinct-ish vectors, not true semantic similarity).

    call_count is incremented on each successful embed() call (thread-safe).
    Mirror of the real embedder shape — lets tests assert observability
    invariants without a real Ollama instance.
    """

    def __init__(
        self,
        dim: int = 1024,
        seed: int = 42,
        *,
        model: str = "fake",
        num_ctx: int = EMBEDDER_NUM_CTX,
        chars_per_token: float = EMBEDDER_CHARS_PER_TOKEN,
    ):
        self._dim = dim
        self._seed = seed
        self._lock = threading.Lock()
        self.call_count: int = 0
        # Capability descriptors (Protocol attrs).
        self.model = model
        self.dim = dim
        self.num_ctx = num_ctx
        self.chars_per_token = chars_per_token

    def embed(self, texts: list[str]) -> list[list[float]]:
        rng = random.Random(self._seed)
        result = []
        for _ in texts:
            vec = [rng.gauss(0, 1) for _ in range(self._dim)]
            result.append(_normalize(vec))
        with self._lock:
            self.call_count += 1
        return result

    async def embed_async(
        self, texts: list[str], *, read_timeout: int | None = None
    ) -> list[list[float]]:
        # read_timeout is irrelevant for the in-process fake but accepted for
        # Protocol parity.
        return await asyncio.to_thread(self.embed, texts)


# ---------------------------------------------------------------------------
# Shared HTTP base
# ---------------------------------------------------------------------------

class _BaseHttpEmbedder:
    """Common batch / retry / timeout / observability machinery for HTTP backends.

    Subclasses customise the wire format by overriding:
      * ``endpoint_path``       — appended to the base url
      * ``query_instruction``   — prefix prepended to query text (or "")
      * ``_embedder_type``      — value of the ``embedder_type`` metric label
      * ``_build_payload(texts)`` — request JSON body
      * ``_extract_vectors(data)`` — pull list[list[float]] out of the response

    Truncation safety-net, MRL ``dim`` truncation, L2-normalisation, the
    sub-batch loop, the retry/backoff loop and the histogram/call_count
    observability invariants all live here so the two backends stay DRY.
    """

    # Cap per-request batch so a single big module doesn't push past either the
    # in-process timeout or any reverse proxy's `proxy_read_timeout`.
    # Empirical (production): a 50-text batch takes ~10-56s on qwen3-embedding-q5km.
    _MAX_BATCH = EMBEDDER_MAX_BATCH

    endpoint_path = "/api/embed"
    query_instruction = ""
    _embedder_type = "http"

    def __init__(
        self,
        url: str = "http://localhost:11434",
        model: str = DEFAULT_EMBEDDER_MODEL,
        dim: int = DEFAULT_EMBEDDER_DIM,
        retries: int = 3,
        auth_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        retry_backoff_base: float = EMBEDDER_RETRY_BACKOFF_BASE,
        retry_backoff_max: float = EMBEDDER_RETRY_BACKOFF_MAX,
        num_ctx: int = EMBEDDER_NUM_CTX,
        chars_per_token: float = EMBEDDER_CHARS_PER_TOKEN,
    ):
        self._url = url.rstrip("/") + self.endpoint_path
        self._model = model
        self._dim = dim
        self._retries = retries
        self._retry_backoff_base = retry_backoff_base
        self._retry_backoff_max = retry_backoff_max
        self._auth_token = auth_token
        self._transport = transport
        self._lock = threading.Lock()
        self.call_count: int = 0

        # Capability descriptors (Protocol attrs).
        self.model = model
        self.dim = dim
        self.num_ctx = num_ctx
        self.chars_per_token = chars_per_token

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        self._headers = headers

        # WI-9 (ADR-0042): read timeout resolved through the settings overlay
        # so an operator can lengthen the window for slow embedder boxes
        # without redeploying.  The constant is kept as the fallback when no
        # DB row exists (e.g. unit tests with no pool).  Connect/write stay
        # hard-coded — they protect the embedder process from a wedged TCP
        # connection, not the slow per-request inference path.
        read_timeout = _resolved_timeout_read(TIMEOUT_EMBEDDER_READ)
        self._http = httpx.Client(
            timeout=httpx.Timeout(
                connect=TIMEOUT_EMBEDDER_CONNECT,
                read=read_timeout,
                write=TIMEOUT_EMBEDDER_WRITE,
                pool=5.0,
            ),
            headers=headers,
            transport=transport,
        )
        # Short-timeout client for the online query path (embed_async with
        # read_timeout != None). Built once here so the timeout is always
        # TIMEOUT_EMBEDDER_READ_QUERY regardless of what callers pass.
        # Shares the same transport so tests that inject a MockTransport work.
        self._query_http: httpx.Client = httpx.Client(
            timeout=httpx.Timeout(
                connect=TIMEOUT_EMBEDDER_CONNECT,
                read=TIMEOUT_EMBEDDER_READ_QUERY,
                write=TIMEOUT_EMBEDDER_WRITE,
                pool=5.0,
            ),
            headers=headers,
            transport=transport,
        )

    # --- wire-format hooks (override in subclasses) ---

    def _build_payload(self, texts: list[str]) -> dict:
        return {"model": self._model, "input": texts}

    def _extract_vectors(self, data: dict) -> list[list[float]]:
        return data["embeddings"]

    # --- truncation safety-net ---

    def _truncate_to_ctx(self, texts: list[str]) -> list[str]:
        """Final safety-net: clamp any text past num_ctx chars (worst-case estimate).

        This is the LAST line of defence — the real, token-aware cap lives in
        the chunking layer (WI-B). We never split into extra vectors and never
        pool: ``len(out) == len(texts)`` is preserved so call_count and the
        per-sub-batch histogram invariants hold.

        The char limit uses the *minimum* of ``self.chars_per_token`` and the
        module-level constant :data:`EMBEDDER_TRUNCATE_CHARS_PER_TOKEN`
        (default 2.0).  This worst-case floor guarantees that even code with
        very dense tokens (e.g. minified JS, identifiers packed 1–2 chars/token)
        still fits within ``num_ctx`` after truncation, regardless of which
        chars_per_token was configured for estimation elsewhere.
        """
        conservative_cpt = min(self.chars_per_token, EMBEDDER_TRUNCATE_CHARS_PER_TOKEN)
        char_limit = int(self.num_ctx * conservative_cpt)
        if char_limit <= 0:
            return texts
        out = []
        for t in texts:
            if len(t) > char_limit:
                _logger.warning(
                    "embedder truncating text from %d to %d chars (ctx safety-net)",
                    len(t), char_limit,
                )
                out.append(t[:char_limit])
            else:
                out.append(t)
        return out

    # --- core embed ---

    def embed(self, texts: list[str]) -> list[list[float]]:
        # ADR-0010 D7: the two observability signals measure different things
        # and therefore have different cardinality, deliberately:
        #
        #   * `_hist.observe(duration)` is recorded ONCE PER _embed_one round-trip
        #     — i.e. once for the single-batch path, and once per sub-batch on the
        #     large-batch path. This keeps the histogram a faithful per-network-
        #     -call latency distribution (a 250-text embed() should contribute the
        #     latency of each of its sub-batches, not one blended number).
        #   * `call_count` is incremented ONCE PER embed() call, regardless of how
        #     many sub-batches it fanned into (matches the EmbedderClient docstring).
        #
        # Both writes happen under `self._lock`. They are co-located in the same
        # critical section ONLY on the single-batch path (below); on the large-
        # batch path each observe() takes the lock per sub-batch and the single
        # `call_count += 1` takes it once after the loop. prometheus_client is
        # itself thread-safe, so no datum is torn; the lock here only serialises
        # this class's own `call_count` mutation.
        return self._run_batch(texts)

    def _run_batch(
        self, texts: list[str], *, client: httpx.Client | None = None
    ) -> list[list[float]]:
        """Shared loop body for embed() and the query-timeout path.

        Handles truncation, sub-batch splitting, histogram observations, and the
        call_count increment.  ``client`` overrides the default ``self._http``
        (used by the query path to route through a shorter-timeout client).

        Observability invariants (ADR-0010 D7):
          * histogram observed once per ``_embed_one`` call (one per sub-batch)
          * ``call_count`` incremented exactly once per ``_run_batch`` call
        """
        texts = self._truncate_to_ctx(texts)
        _hist = embedder_batch_duration_seconds.labels(embedder_type=self._embedder_type)
        # WI-9: resolve the live batch ceiling once per call so the loop
        # sub-slicing stays consistent if the setting is rotated mid-run.
        # Class-attr self._MAX_BATCH is the fallback when the overlay errors.
        max_batch = _resolved_max_batch(self._MAX_BATCH)
        if len(texts) > max_batch:
            out: list[list[float]] = []
            for i in range(0, len(texts), max_batch):
                batch = texts[i : i + max_batch]
                start = time.monotonic()
                out.extend(self._embed_one(batch, client=client))
                duration = time.monotonic() - start
                with self._lock:
                    _hist.observe(duration)
                _logger.debug("embed batch n=%d duration=%.2fs", len(batch), duration)
            with self._lock:
                self.call_count += 1
            return out
        start = time.monotonic()
        result = self._embed_one(texts, client=client)
        duration = time.monotonic() - start
        with self._lock:
            _hist.observe(duration)
            self.call_count += 1
        _logger.debug("embed batch n=%d duration=%.2fs", len(texts), duration)
        return result

    def _embed_one(
        self, texts: list[str], *, client: httpx.Client | None = None
    ) -> list[list[float]]:
        http = client or self._http
        payload = self._build_payload(texts)
        last_err: Exception | None = None
        for i in range(self._retries):
            try:
                resp = http.post(self._url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                embs = self._extract_vectors(data)
                if len(embs) != len(texts):
                    # Bug B guard: a backend that drops/duplicates a vector would
                    # silently misalign every downstream chunk→vector mapping.
                    raise RuntimeError(
                        f"embedder returned {len(embs)} vectors for {len(texts)} inputs"
                    )
                return [_normalize(v[: self._dim]) for v in embs]
            except httpx.HTTPError as e:
                last_err = e
                if i < self._retries - 1:
                    delay = min(self._retry_backoff_base * (2 ** i), self._retry_backoff_max)
                    _logger.warning(
                        "embed attempt %d/%d failed (%s) — retrying in %.1fs",
                        i + 1, self._retries, last_err, delay,
                    )
                    time.sleep(delay)
        raise RuntimeError(
            f"{type(self).__name__} failed after {self._retries} attempts: {last_err}"
        )

    # --- async path ---

    async def embed_async(
        self, texts: list[str], *, read_timeout: int | None = None
    ) -> list[list[float]]:
        """Run the embedding in a worker thread (httpx stays sync — simplest correct path).

        When ``read_timeout`` is ``None`` the default long-timeout batch client
        (``self._http``) is used.  When ``read_timeout`` is the sentinel
        ``"query"`` or an int, the dedicated short-timeout query client
        (``self._query_http``, built once in ``__init__`` with
        :data:`TIMEOUT_EMBEDDER_READ_QUERY`) is used instead — ``read_timeout``
        values other than ``"query"`` are accepted for API compatibility but the
        query client's timeout is always ``TIMEOUT_EMBEDDER_READ_QUERY``.
        """
        if read_timeout is None:
            return await asyncio.to_thread(self._run_batch, texts)
        # Any non-None read_timeout routes through the fixed query client.
        return await asyncio.to_thread(self._run_batch, texts, client=self._query_http)

    def _embed_with_timeout(self, texts: list[str], read_timeout: int) -> list[list[float]]:  # noqa: ARG002
        """Backward-compat shim: embed() variant that routes through the query client.

        ``read_timeout`` is accepted but ignored — the query client timeout is
        fixed at :data:`TIMEOUT_EMBEDDER_READ_QUERY` (set once in ``__init__``).
        Callers should prefer ``embed_async(read_timeout=...)`` directly.
        """
        return self._run_batch(texts, client=self._query_http)

    def close(self) -> None:
        """Close the underlying HTTP client(s) and release connections."""
        self._http.close()
        self._query_http.close()


# ---------------------------------------------------------------------------
# Qwen3 (Ollama) backend
# ---------------------------------------------------------------------------

class Qwen3Embedder(_BaseHttpEmbedder):
    """Ollama HTTP client for Qwen3-Embedding-4B (or any compatible model).

    Expects Ollama /api/embed endpoint. Truncates to `dim` dimensions and
    L2-normalises — supports MRL (Matryoshka Representation Learning).

    Qwen uses an asymmetric INSTRUCT prefix on *query* text only (documents are
    embedded raw); ``query_instruction`` exposes that prefix for the search path.

    auth_token: optional Bearer token sent as `Authorization: Bearer <token>`.
    Set when Ollama sits behind an authenticated reverse proxy.

    transport: optional httpx.BaseTransport for testing (inject MockTransport).

    Wire format is the Ollama default (``{model, input}`` payload,
    ``data["embeddings"]`` extraction) which is already implemented by the
    ``_BaseHttpEmbedder`` defaults — no overrides needed.
    """

    endpoint_path = "/api/embed"
    query_instruction = INSTRUCT_NL_TO_CODE
    _embedder_type = "qwen3"


# ---------------------------------------------------------------------------
# OpenAI-compatible backend
# ---------------------------------------------------------------------------

class OpenAICompatEmbedder(_BaseHttpEmbedder):
    """/v1/embeddings client for OpenAI / Voyage / TEI / vLLM / LiteLLM.

    Same batch/retry/timeout/observability machinery as Qwen3Embedder, but the
    OpenAI wire format: POST {model, input} to /v1/embeddings, response
    ``{"data": [{"embedding": [...]}, ...]}``. No INSTRUCT prefix (symmetric
    models). Still applies MRL ``dim`` truncation + L2-normalisation so a
    downstream pgvector column of a fixed width keeps working across backends.
    """

    endpoint_path = "/v1/embeddings"
    query_instruction = ""
    _embedder_type = "openai"

    def _build_payload(self, texts: list[str]) -> dict:
        # Pin encoding_format=float: real OpenAI may return base64-packed
        # embeddings otherwise, which _extract_vectors does not decode. TEI/
        # vLLM/Voyage ignore the field, so it is safe across all targets.
        return {"model": self._model, "input": texts, "encoding_format": "float"}

    def _extract_vectors(self, data: dict) -> list[list[float]]:
        return [d["embedding"] for d in data["data"]]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_embedder(backend: str | None = None, **kwargs) -> EmbedderClient:
    """Construct an embedder for ``backend`` (defaults to :data:`EMBEDDER_BACKEND`).

    backend mapping:
      * ``ollama``           -> Qwen3Embedder
      * ``openai`` / ``tei`` -> OpenAICompatEmbedder
      * ``fake``             -> FakeEmbedder

    ``**kwargs`` are forwarded to the chosen constructor (url / model / dim /
    auth_token / transport / retries / ...). Callers that read url/model/dim/
    auth from their own config (see src/indexer/__main__.py, src/mcp/server.py,
    src/indexer/seed_patterns.py) pass them through here.
    """
    chosen = (backend or EMBEDDER_BACKEND or "ollama").strip().lower()
    if chosen in ("fake", "test"):
        # FakeEmbedder has a narrower constructor — forward only what it accepts.
        fake_kwargs = {
            k: v
            for k, v in kwargs.items()
            if k in ("dim", "seed", "model", "num_ctx", "chars_per_token")
        }
        return FakeEmbedder(**fake_kwargs)
    if chosen in ("openai", "tei", "voyage", "vllm", "litellm", "openai-compat"):
        return OpenAICompatEmbedder(**kwargs)
    if chosen in ("ollama", "qwen", "qwen3"):
        return Qwen3Embedder(**kwargs)
    raise ValueError(
        f"Unknown EMBEDDER_BACKEND {chosen!r} — expected one of "
        "ollama | openai | tei | fake"
    )
