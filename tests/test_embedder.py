"""Tests for FakeEmbedder and Qwen3Embedder."""
import math
from unittest.mock import MagicMock, patch

import pytest

from src.indexer.embedder import EmbedderClient, FakeEmbedder, Qwen3Embedder


def _is_normalized(vec: list[float], tol: float = 1e-5) -> bool:
    return abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < tol


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


def _mock_ollama_response(embeddings: list[list[float]]):
    import json
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"embeddings": embeddings}).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def test_qwen3_embedder_calls_correct_url():
    raw = [[0.1] * 2048]
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(raw)) as mock_open:
        e = Qwen3Embedder(url="http://ollama:11434", model="qwen3-embedding:4b", dim=1024)
        e.embed(["test query"])
    call_args = mock_open.call_args[0][0]
    assert call_args.full_url == "http://ollama:11434/api/embed"


def test_qwen3_embedder_truncates_to_dim():
    raw = [[float(i) / 100 for i in range(2048)]]
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(raw)):
        e = Qwen3Embedder(dim=1024)
        result = e.embed(["x"])
    assert len(result[0]) == 1024


def test_qwen3_embedder_normalizes():
    raw = [[1.0, 2.0, 3.0]]
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(raw)):
        e = Qwen3Embedder(dim=3)
        result = e.embed(["x"])
    assert _is_normalized(result[0])


def test_qwen3_embedder_retries_on_error():
    import urllib.error
    side_effects = [urllib.error.URLError("timeout"), urllib.error.URLError("timeout")]
    raw = [[0.5, 0.5]]
    mock_resp = _mock_ollama_response(raw)

    call_count = [0]
    def fake_urlopen(req, timeout=None):
        if call_count[0] < len(side_effects):
            err = side_effects[call_count[0]]
            call_count[0] += 1
            raise err
        call_count[0] += 1
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        e = Qwen3Embedder(dim=2, retries=3)
        result = e.embed(["x"])
    assert len(result) == 1
    assert call_count[0] == 3


def test_qwen3_embedder_raises_after_exhausting_retries():
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        e = Qwen3Embedder(dim=4, retries=2)
        with pytest.raises(RuntimeError, match="failed after 2 attempts"):
            e.embed(["x"])
