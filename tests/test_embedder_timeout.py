"""Verify that httpx.ReadTimeout is raised (and eventually surfaced as RuntimeError)
when the Ollama server is too slow to respond within the configured read timeout.

Note: httpx.MockTransport runs synchronously in-process — socket-level timeouts
do not fire. We simulate the timeout by having the handler raise httpx.ReadTimeout
directly, which is exactly what httpx raises when the real server is too slow.
This tests the _embed_one retry/raise path without a real network server.
"""
import httpx
import pytest

from src.indexer.embedder import Qwen3Embedder


def test_slow_server_raises_timeout():
    """A ReadTimeout from the server must bubble up as RuntimeError after retries."""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        # Simulate what httpx raises when server doesn't respond within read timeout
        raise httpx.ReadTimeout("mock read timeout", request=request)

    transport = httpx.MockTransport(timeout_handler)
    client = Qwen3Embedder(url="http://test", model="m", dim=1024, retries=1, transport=transport)
    with pytest.raises(RuntimeError, match="failed after"):
        client.embed(["hello"])


def test_connect_timeout_raises_runtime_error():
    """A ConnectTimeout must also be retried and surface as RuntimeError."""

    def connect_timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("mock connect timeout", request=request)

    transport = httpx.MockTransport(connect_timeout_handler)
    client = Qwen3Embedder(url="http://test", model="m", dim=1024, retries=2, transport=transport)
    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        client.embed(["hello"])
