"""Embedding client protocol + implementations.

EmbedderClient — structural Protocol, any object with .embed() satisfies it.
FakeEmbedder   — deterministic, seeded, no GPU. For CI and unit tests.
Qwen3Embedder  — Ollama HTTP client for Qwen3-Embedding-4B Q5_K_M.
"""
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
    EMBEDDER_MAX_BATCH,
    TIMEOUT_EMBEDDER_CONNECT,
    TIMEOUT_EMBEDDER_READ,
    TIMEOUT_EMBEDDER_WRITE,
)


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


@runtime_checkable
class EmbedderClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return L2-normalized embedding vectors for each text."""
        ...


_logger = logging.getLogger(__name__)


class FakeEmbedder:
    """Deterministic embedder for CI — no GPU, no network.

    Uses a seeded RNG so the same text always gets the same vector within a
    test session (seed is global, not per-text, which is intentional — tests
    only need non-zero distinct-ish vectors, not true semantic similarity).

    call_count is incremented on each successful embed() call (thread-safe).
    Mirror of Qwen3Embedder shape — lets tests assert observability invariants
    without a real Ollama instance.
    """

    def __init__(self, dim: int = 1024, seed: int = 42):
        self._dim = dim
        self._seed = seed
        self._lock = threading.Lock()
        self.call_count: int = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        rng = random.Random(self._seed)
        result = []
        for _ in texts:
            vec = [rng.gauss(0, 1) for _ in range(self._dim)]
            result.append(_normalize(vec))
        with self._lock:
            self.call_count += 1
        return result


class Qwen3Embedder:
    """Ollama HTTP client for Qwen3-Embedding-4B (or any compatible model).

    Expects Ollama /api/embed endpoint. Truncates to `dim` dimensions and
    L2-normalises — supports MRL (Matryoshka Representation Learning).

    auth_token: optional Bearer token sent as `Authorization: Bearer <token>`.
    Set when Ollama sits behind an authenticated reverse proxy.

    transport: optional httpx.BaseTransport for testing (inject MockTransport).
    """

    # Cap per-request batch so a single big module doesn't push past either the
    # in-process timeout or any reverse proxy's `proxy_read_timeout` (typical 120s).
    # Empirical: ~22s per 100 texts on qwen3-embedding-q5km, so 50 stays well under.
    _MAX_BATCH = EMBEDDER_MAX_BATCH

    def __init__(
        self,
        url: str = "http://localhost:11434",
        model: str = DEFAULT_EMBEDDER_MODEL,
        dim: int = DEFAULT_EMBEDDER_DIM,
        retries: int = 3,
        auth_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self._url = url.rstrip("/") + "/api/embed"
        self._model = model
        self._dim = dim
        self._retries = retries
        self._auth_token = auth_token
        self._lock = threading.Lock()
        self.call_count: int = 0

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        self._http = httpx.Client(
            timeout=httpx.Timeout(
                connect=TIMEOUT_EMBEDDER_CONNECT,
                read=TIMEOUT_EMBEDDER_READ,
                write=TIMEOUT_EMBEDDER_WRITE,
                pool=5.0,
            ),
            headers=headers,
            transport=transport,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if len(texts) > self._MAX_BATCH:
            out: list[list[float]] = []
            for i in range(0, len(texts), self._MAX_BATCH):
                batch = texts[i : i + self._MAX_BATCH]
                start = time.monotonic()
                out.extend(self._embed_one(batch))
                _logger.debug(
                    "embed batch n=%d duration=%.2fs", len(batch), time.monotonic() - start
                )
            with self._lock:
                self.call_count += 1
            return out
        start = time.monotonic()
        result = self._embed_one(texts)
        _logger.debug(
            "embed batch n=%d duration=%.2fs", len(texts), time.monotonic() - start
        )
        with self._lock:
            self.call_count += 1
        return result

    def _embed_one(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": texts}
        last_err: Exception | None = None
        for _ in range(self._retries):
            try:
                resp = self._http.post(self._url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return [_normalize(v[: self._dim]) for v in data["embeddings"]]
            except httpx.HTTPError as e:
                last_err = e
        raise RuntimeError(
            f"Qwen3Embedder failed after {self._retries} attempts: {last_err}"
        )

    def close(self) -> None:
        """Close the underlying HTTP client and release connections."""
        self._http.close()
