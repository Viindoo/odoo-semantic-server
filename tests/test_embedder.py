"""Tests for FakeEmbedder and Qwen3Embedder."""
import math

import httpx
import pytest

from src.indexer.embedder import EmbedderClient, FakeEmbedder, Qwen3Embedder


def _is_normalized(vec: list[float], tol: float = 1e-5) -> bool:
    return abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < tol


def _mock_transport(embeddings: list[list[float]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": embeddings})
    return httpx.MockTransport(handler)


# --- FakeEmbedder ---

def test_fake_embedder_satisfies_protocol():
    assert isinstance(FakeEmbedder(), EmbedderClient)


def test_fake_embedder_returns_correct_count():
    e = FakeEmbedder(dim=16)
    result = e.embed(["a", "b", "c"])
    assert len(result) == 3


def test_fake_embedder_returns_correct_dim():
    e = FakeEmbedder(dim=64)
    vecs = e.embed(["hello"])
    assert len(vecs[0]) == 64


def test_fake_embedder_is_normalized():
    e = FakeEmbedder(dim=32)
    for vec in e.embed(["x", "y", "z"]):
        assert _is_normalized(vec)


def test_fake_embedder_deterministic():
    e1 = FakeEmbedder(dim=16, seed=7)
    e2 = FakeEmbedder(dim=16, seed=7)
    assert e1.embed(["hello"]) == e2.embed(["hello"])


def test_fake_embedder_different_seeds_differ():
    v1 = FakeEmbedder(dim=16, seed=1).embed(["x"])[0]
    v2 = FakeEmbedder(dim=16, seed=2).embed(["x"])[0]
    assert v1 != v2


# --- Qwen3Embedder ---

def test_qwen3_embedder_satisfies_protocol():
    assert isinstance(Qwen3Embedder(), EmbedderClient)


def test_qwen3_embedder_protocol_conformance():
    """embed() must return list[list[float]] with one vector per input text."""
    raw = [[0.1] * 2048, [0.2] * 2048]
    e = Qwen3Embedder(url="http://test", model="m", dim=1024, transport=_mock_transport(raw))
    result = e.embed(["text one", "text two"])
    assert isinstance(result, list)
    assert len(result) == 2
    for vec in result:
        assert isinstance(vec, list)
        assert all(isinstance(v, float) for v in vec)


def test_qwen3_embedder_truncates_to_dim():
    """Vectors longer than dim must be sliced to exactly dim elements."""
    raw = [[float(i) / 100 for i in range(2048)]]
    e = Qwen3Embedder(url="http://test", model="m", dim=1024, transport=_mock_transport(raw))
    result = e.embed(["x"])
    assert len(result[0]) == 1024


def test_qwen3_embedder_normalizes():
    """Each returned vector must be L2-normalised."""
    raw = [[1.0, 2.0, 3.0]]
    e = Qwen3Embedder(url="http://test", model="m", dim=3, transport=_mock_transport(raw))
    result = e.embed(["x"])
    assert _is_normalized(result[0])


def test_qwen3_embedder_with_token_sends_bearer_header():
    """Authorization: Bearer <token> must appear in the request when auth_token is set."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"embeddings": [[0.5, 0.5]]})

    e = Qwen3Embedder(
        url="http://test", model="m", dim=2, auth_token="abc123",
        transport=httpx.MockTransport(handler),
    )
    e.embed(["hello"])
    assert len(captured) == 1
    assert captured[0].headers.get("authorization") == "Bearer abc123"


def test_qwen3_embedder_no_token_sends_no_auth_header():
    """Without auth_token, Authorization header must NOT be present."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    e = Qwen3Embedder(
        url="http://test", model="m", dim=2,
        transport=httpx.MockTransport(handler),
    )
    e.embed(["hello"])
    assert len(captured) == 1
    assert "authorization" not in captured[0].headers


def test_qwen3_embedder_retries_on_error():
    """Two HTTP errors then success: total call count == 3, result returned."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] < 3:
            raise httpx.ConnectError("transient")
        return httpx.Response(200, json={"embeddings": [[0.5, 0.5]]})

    e = Qwen3Embedder(
        url="http://test", model="m", dim=2, retries=3,
        transport=httpx.MockTransport(handler),
    )
    result = e.embed(["x"])
    assert len(result) == 1
    assert call_count[0] == 3


def test_qwen3_embedder_raises_after_exhausting_retries():
    """After retries exhausted, RuntimeError must be raised with attempt count."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    e = Qwen3Embedder(
        url="http://test", model="m", dim=4, retries=2,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        e.embed(["x"])
