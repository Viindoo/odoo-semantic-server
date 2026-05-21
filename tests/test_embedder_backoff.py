# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for exponential backoff in Qwen3Embedder._embed_one retry loop.

Verifies:
- sleep is called between failed attempts (not after the final failure)
- sleep delays follow min(base * 2**i, max) formula
- no sleep on the final (exhausted) failure
"""
from unittest.mock import patch

import httpx
import pytest

from src.indexer.embedder import Qwen3Embedder


def _make_embedder(**kwargs) -> Qwen3Embedder:
    """Helper: embedder with fast default backoff for tests (overridden per-test)."""
    defaults = dict(url="http://test", model="m", dim=2)
    defaults.update(kwargs)
    return Qwen3Embedder(**defaults)


def _fail_then_succeed(fail_count: int, embeddings: list[list[float]]):
    """Return an httpx.MockTransport handler that fails `fail_count` times then succeeds."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] <= fail_count:
            raise httpx.ConnectError("Connection timed out")
        return httpx.Response(200, json={"embeddings": embeddings})

    return httpx.MockTransport(handler)


def _always_fail():
    """Return an httpx.MockTransport handler that always raises ConnectError."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection timed out")

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Test 1: two failures then success → sleep called exactly 2 times with
# correct delays (base=2.0: delay[0]=2.0, delay[1]=4.0)
# ---------------------------------------------------------------------------

def test_backoff_sleep_called_between_attempts():
    """Two failures then success: time.sleep called exactly 2 times."""
    transport = _fail_then_succeed(fail_count=2, embeddings=[[0.5, 0.5]])
    embedder = _make_embedder(
        retries=3,
        retry_backoff_base=2.0,
        retry_backoff_max=30.0,
        transport=transport,
    )

    with patch("src.indexer.embedder.time.sleep") as mock_sleep:
        result = embedder.embed(["x"])

    assert len(result) == 1
    assert mock_sleep.call_count == 2
    delays = [call.args[0] for call in mock_sleep.call_args_list]
    assert delays[0] == pytest.approx(2.0)   # base * 2**0 = 2.0
    assert delays[1] == pytest.approx(4.0)   # base * 2**1 = 4.0


# ---------------------------------------------------------------------------
# Test 2: all 3 attempts fail → RuntimeError raised → sleep called exactly 2
# times (no sleep after the final failure)
# ---------------------------------------------------------------------------

def test_backoff_no_sleep_after_final_failure():
    """All retries exhausted: RuntimeError raised, sleep NOT called after last attempt."""
    embedder = _make_embedder(
        retries=3,
        retry_backoff_base=2.0,
        retry_backoff_max=30.0,
        transport=_always_fail(),
    )

    with patch("src.indexer.embedder.time.sleep") as mock_sleep:
        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            embedder.embed(["x"])

    # 3 attempts → 2 sleeps (between attempt 1→2 and 2→3, NOT after 3)
    assert mock_sleep.call_count == 2


# ---------------------------------------------------------------------------
# Test 3: backoff_max cap — base=10, max=15 → delay[0]=10.0, delay[1]=15.0
# (20.0 would exceed max, so capped at 15.0)
# ---------------------------------------------------------------------------

def test_backoff_respects_max_cap():
    """Delay is capped at retry_backoff_max."""
    transport = _fail_then_succeed(fail_count=2, embeddings=[[0.1, 0.2]])
    embedder = _make_embedder(
        retries=3,
        retry_backoff_base=10.0,
        retry_backoff_max=15.0,
        transport=transport,
    )

    with patch("src.indexer.embedder.time.sleep") as mock_sleep:
        embedder.embed(["y"])

    delays = [call.args[0] for call in mock_sleep.call_args_list]
    assert delays[0] == pytest.approx(10.0)   # 10 * 2**0 = 10, within max
    assert delays[1] == pytest.approx(15.0)   # 10 * 2**1 = 20 → capped at 15


# ---------------------------------------------------------------------------
# Test 4: retries=1 → single attempt, never sleeps (no room for backoff)
# ---------------------------------------------------------------------------

def test_backoff_no_sleep_when_retries_one():
    """With retries=1, a single failure raises RuntimeError without any sleep."""
    embedder = _make_embedder(
        retries=1,
        retry_backoff_base=2.0,
        retry_backoff_max=30.0,
        transport=_always_fail(),
    )

    with patch("src.indexer.embedder.time.sleep") as mock_sleep:
        with pytest.raises(RuntimeError, match="failed after 1 attempts"):
            embedder.embed(["z"])

    assert mock_sleep.call_count == 0
