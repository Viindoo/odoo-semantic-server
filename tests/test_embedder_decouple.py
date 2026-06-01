# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the provider-decoupled embedder (WI-A).

Covers:
  * make_embedder() factory dispatch (fake / openai / env default)
  * OpenAICompatEmbedder parsing of the /v1/embeddings response shape
  * Bug B: vector-count mismatch raises a clear RuntimeError
  * Choke-point truncation safety-net preserves vector count + call_count
  * Token helpers estimate_tokens / split_by_token_budget
"""
import asyncio
import json
import math

import httpx
import pytest

from src.indexer.embedder import (
    EmbedderClient,
    FakeEmbedder,
    OpenAICompatEmbedder,
    Qwen3Embedder,
    estimate_tokens,
    make_embedder,
    split_by_token_budget,
)


def _is_normalized(vec: list[float], tol: float = 1e-5) -> bool:
    return abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < tol


# ---------------------------------------------------------------------------
# (a) Factory dispatch
# ---------------------------------------------------------------------------

def test_make_embedder_fake_returns_fake():
    e = make_embedder("fake")
    assert isinstance(e, FakeEmbedder)


def test_make_embedder_openai_returns_openai_compat():
    e = make_embedder("openai", url="http://test", model="m", dim=4)
    assert isinstance(e, OpenAICompatEmbedder)


def test_make_embedder_tei_returns_openai_compat():
    e = make_embedder("tei", url="http://test", model="m", dim=4)
    assert isinstance(e, OpenAICompatEmbedder)


def test_make_embedder_ollama_returns_qwen3():
    e = make_embedder("ollama", url="http://test", model="m", dim=4)
    assert isinstance(e, Qwen3Embedder)


def test_make_embedder_default_follows_env(monkeypatch):
    """With no explicit backend, make_embedder() honours EMBEDDER_BACKEND."""
    import src.indexer.embedder as emb
    monkeypatch.setattr(emb, "EMBEDDER_BACKEND", "openai")
    e = make_embedder(url="http://test", model="m", dim=4)
    assert isinstance(e, OpenAICompatEmbedder)


def test_make_embedder_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown EMBEDDER_BACKEND"):
        make_embedder("nope")


def test_made_embedders_satisfy_protocol():
    for backend, kwargs in (
        ("fake", {}),
        ("openai", {"url": "http://t", "model": "m", "dim": 4}),
        ("ollama", {"url": "http://t", "model": "m", "dim": 4}),
    ):
        assert isinstance(make_embedder(backend, **kwargs), EmbedderClient)


# ---------------------------------------------------------------------------
# (a') Capability descriptors exposed
# ---------------------------------------------------------------------------

def test_capability_attrs_present():
    e = make_embedder("openai", url="http://t", model="voyage-3", dim=512)
    assert e.model == "voyage-3"
    assert e.dim == 512
    assert isinstance(e.num_ctx, int)
    assert isinstance(e.chars_per_token, float)


# ---------------------------------------------------------------------------
# (b) OpenAICompatEmbedder parses {"data":[{"embedding":[...]}]}
# ---------------------------------------------------------------------------

def _openai_transport(dim: int = 4):
    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["input"])
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.3] * dim} for _ in range(n)]},
        )

    return httpx.MockTransport(handler)


def test_openai_compat_parses_data_embedding_shape():
    e = OpenAICompatEmbedder(
        url="http://test", model="m", dim=4, transport=_openai_transport(dim=8)
    )
    result = e.embed(["alpha", "beta"])
    assert len(result) == 2
    for vec in result:
        assert len(vec) == 4  # MRL truncated to dim
        assert _is_normalized(vec)


def test_openai_compat_hits_v1_embeddings_path():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        n = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"data": [{"embedding": [0.5, 0.5]} for _ in range(n)]})

    e = OpenAICompatEmbedder(
        url="http://test", model="m", dim=2, transport=httpx.MockTransport(handler)
    )
    e.embed(["x"])
    assert captured[0].url.path == "/v1/embeddings"


def test_openai_compat_has_no_query_instruction():
    assert OpenAICompatEmbedder.query_instruction == ""


def test_qwen3_has_qwen_query_instruction():
    assert "Instruct:" in Qwen3Embedder.query_instruction
    assert Qwen3Embedder.query_instruction.endswith("Query: ")


# ---------------------------------------------------------------------------
# (c) Bug B: vector-count mismatch raises a clear RuntimeError
# ---------------------------------------------------------------------------

def _truncating_transport(drop: int = 1):
    """Return one fewer vector than the request asked for (Qwen wire shape)."""

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"embeddings": [[0.5, 0.5]] * max(n - drop, 0)})

    return httpx.MockTransport(handler)


def test_bug_b_qwen_vector_count_mismatch_raises():
    e = Qwen3Embedder(
        url="http://test", model="m", dim=2, transport=_truncating_transport(drop=1)
    )
    with pytest.raises(RuntimeError, match=r"returned 2 vectors for 3 inputs"):
        e.embed(["a", "b", "c"])


def test_bug_b_openai_vector_count_mismatch_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["input"])
        # Drop one
        return httpx.Response(
            200, json={"data": [{"embedding": [0.5, 0.5]} for _ in range(n - 1)]}
        )

    e = OpenAICompatEmbedder(
        url="http://test", model="m", dim=2, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RuntimeError, match=r"returned 1 vectors for 2 inputs"):
        e.embed(["a", "b"])


# ---------------------------------------------------------------------------
# (d) Choke-point truncation safety-net
# ---------------------------------------------------------------------------

def test_truncation_worst_case_ratio_guarantees_ctx_fit():
    """_truncate_to_ctx must use the worst-case (minimum) chars-per-token ratio.

    When chars_per_token=3.0 (the standard estimation ratio) and
    EMBEDDER_TRUNCATE_CHARS_PER_TOKEN=2.0 (the conservative safety-net floor),
    the char limit must be num_ctx * 2.0 (not num_ctx * 3.0).

    This ensures that even token-dense code (~2 chars/token) fits within
    num_ctx after truncation.
    """
    captured_lengths: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        captured_lengths.extend(len(t) for t in inputs)
        return httpx.Response(
            200, json={"embeddings": [[0.5, 0.5] for _ in inputs]}
        )

    # num_ctx=100, chars_per_token=3.0 (standard ratio)
    # EMBEDDER_TRUNCATE_CHARS_PER_TOKEN=2.0 (worst-case floor)
    # conservative char_limit = 100 * min(3.0, 2.0) = 100 * 2.0 = 200
    # If the ratio were used naively: 100 * 3.0 = 300 chars -> 150 tokens @ 2 chars/tok > 100 ctx
    e = Qwen3Embedder(
        url="http://test",
        model="m",
        dim=2,
        num_ctx=100,
        chars_per_token=3.0,
        transport=httpx.MockTransport(handler),
    )
    long_text = "x" * 500
    result = e.embed([long_text])
    assert len(result) == 1  # no split, one vector per input
    # char_limit = 100 * min(3.0, 2.0) = 200
    assert captured_lengths[0] <= 200
    # Verify: at 2 chars/token (worst-case code density), estimated tokens <= num_ctx
    import math as _math
    estimated_tokens = _math.ceil(captured_lengths[0] / 2.0)
    assert estimated_tokens <= 100, (
        f"After truncation, worst-case token estimate {estimated_tokens} > num_ctx=100"
    )


def test_truncation_preserves_vector_count_and_call_count():
    """A text longer than num_ctx*chars_per_token is truncated, not split.

    Vector count stays == input count and call_count increments exactly once.
    """
    captured_lengths: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        captured_lengths.extend(len(t) for t in inputs)
        return httpx.Response(
            200, json={"embeddings": [[0.5, 0.5] for _ in inputs]}
        )

    # num_ctx=10, chars_per_token=2.0 -> char_limit=20
    e = Qwen3Embedder(
        url="http://test",
        model="m",
        dim=2,
        num_ctx=10,
        chars_per_token=2.0,
        transport=httpx.MockTransport(handler),
    )
    long_text = "x" * 500
    before = e.call_count
    result = e.embed([long_text, "short"])

    assert len(result) == 2  # one vector per input — no split
    assert e.call_count == before + 1  # exactly one embed() call
    # The long text was clamped to char_limit (20); "short" untouched.
    assert max(captured_lengths) <= 20


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def test_estimate_tokens_overestimates_conservatively():
    # 30 chars at 3.0 chars/token -> 10 tokens
    assert estimate_tokens("x" * 30, chars_per_token=3.0) == 10


def test_estimate_tokens_empty_is_zero():
    assert estimate_tokens("") == 0


def test_split_by_token_budget_fast_path_returns_single():
    assert split_by_token_budget("short", budget=1000) == ["short"]


def test_split_by_token_budget_chunks_evenly():
    # budget=10 tokens, chars_per_token=3.0 -> 30-char slices
    text = "a" * 95
    parts = split_by_token_budget(text, budget=10, chars_per_token=3.0)
    assert "".join(parts) == text
    assert all(len(p) <= 30 for p in parts)
    assert len(parts) == math.ceil(95 / 30)


# ---------------------------------------------------------------------------
# Async path
# ---------------------------------------------------------------------------

def test_embed_async_fake_returns_vectors():
    e = FakeEmbedder(dim=8)
    result = asyncio.run(e.embed_async(["a", "b"]))
    assert len(result) == 2
    assert all(len(v) == 8 for v in result)


def test_embed_async_http_default_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"embeddings": [[0.5, 0.5] for _ in range(n)]})

    e = Qwen3Embedder(
        url="http://test", model="m", dim=2, transport=httpx.MockTransport(handler)
    )
    result = asyncio.run(e.embed_async(["x"]))
    assert len(result) == 1
    assert e.call_count == 1


def test_embed_async_query_timeout_path():
    """Passing a short read_timeout still returns correct vectors + call_count."""
    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"embeddings": [[0.5, 0.5] for _ in range(n)]})

    e = Qwen3Embedder(
        url="http://test", model="m", dim=2, transport=httpx.MockTransport(handler)
    )
    result = asyncio.run(e.embed_async(["x"], read_timeout=5))
    assert len(result) == 1
    assert e.call_count == 1
