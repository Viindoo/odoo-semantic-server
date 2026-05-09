"""Embedding client protocol + implementations.

EmbedderClient — structural Protocol, any object with .embed() satisfies it.
FakeEmbedder   — deterministic, seeded, no GPU. For CI and unit tests.
Qwen3Embedder  — Ollama HTTP client for Qwen3-Embedding-4B Q5_K_M.
"""
import json
import math
import random
import urllib.request
from typing import Protocol, runtime_checkable


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


class FakeEmbedder:
    """Deterministic embedder for CI — no GPU, no network.

    Uses a seeded RNG so the same text always gets the same vector within a
    test session (seed is global, not per-text, which is intentional — tests
    only need non-zero distinct-ish vectors, not true semantic similarity).
    """

    def __init__(self, dim: int = 1024, seed: int = 42):
        self._dim = dim
        self._seed = seed

    def embed(self, texts: list[str]) -> list[list[float]]:
        rng = random.Random(self._seed)
        result = []
        for _ in texts:
            vec = [rng.gauss(0, 1) for _ in range(self._dim)]
            result.append(_normalize(vec))
        return result


class Qwen3Embedder:
    """Ollama HTTP client for Qwen3-Embedding-4B (or any compatible model).

    Expects Ollama /api/embed endpoint. Truncates to `dim` dimensions and
    L2-normalises — supports MRL (Matryoshka Representation Learning).
    """

    def __init__(
        self,
        url: str = "http://localhost:11434",
        model: str = "qwen3-embedding-q5km",
        dim: int = 1024,
        retries: int = 3,
    ):
        self._url = url.rstrip("/") + "/api/embed"
        self._model = model
        self._dim = dim
        self._retries = retries

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self._model, "input": texts}).encode()
        last_err: Exception | None = None
        for _ in range(self._retries):
            try:
                req = urllib.request.Request(
                    self._url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                return [_normalize(v[: self._dim]) for v in data["embeddings"]]
            except Exception as e:
                last_err = e
        raise RuntimeError(
            f"Qwen3Embedder failed after {self._retries} attempts: {last_err}"
        )
