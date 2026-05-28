# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Prometheus /metrics endpoint and embedder_batch_duration_seconds histogram.

Covers:
- Histogram is registered in the default Prometheus registry.
- Observing a value increments count and updates sum.
- /metrics endpoint returns 200 with Prometheus text format.
- /metrics body contains the histogram metric name.
- Qwen3Embedder.embed() observes the histogram on each _embed_one call.
"""
import contextlib
import math

import httpx
import pytest

from src.metrics import embedder_batch_duration_seconds

_LABEL = {"embedder_type": "qwen3"}


# ---------------------------------------------------------------------------
# Histogram unit tests — no server needed
# ---------------------------------------------------------------------------


def test_histogram_is_registered():
    """embedder_batch_duration_seconds must be importable and is a Histogram."""
    from prometheus_client import Histogram as _Histogram

    assert isinstance(embedder_batch_duration_seconds, _Histogram)


def test_histogram_observe_increments_count():
    """Observing a value must increment the sample count."""
    from prometheus_client import REGISTRY

    label = embedder_batch_duration_seconds.labels(embedder_type="qwen3")

    before = _get_histogram_count(REGISTRY, "embedder_batch_duration_seconds", _LABEL)
    label.observe(0.5)
    after = _get_histogram_count(REGISTRY, "embedder_batch_duration_seconds", _LABEL)

    assert after == before + 1


def test_histogram_observe_updates_sum():
    """Observing a value must increment the sample sum."""
    from prometheus_client import REGISTRY

    label = embedder_batch_duration_seconds.labels(embedder_type="qwen3")
    before_sum = _get_histogram_sum(
        REGISTRY, "embedder_batch_duration_seconds", _LABEL
    )
    label.observe(2.0)
    after_sum = _get_histogram_sum(
        REGISTRY, "embedder_batch_duration_seconds", _LABEL
    )

    assert math.isclose(after_sum - before_sum, 2.0, rel_tol=1e-6)


def test_histogram_has_correct_buckets():
    """Histogram must include the expected bucket boundaries."""
    from prometheus_client import REGISTRY

    for metric in REGISTRY.collect():
        if metric.name == "embedder_batch_duration_seconds":
            bucket_bounds = sorted(
                s.labels.get("le")
                for s in metric.samples
                if s.name.endswith("_bucket") and "embedder_type" in s.labels
            )
            # Remove the +Inf sentinel
            finite_bounds = [b for b in bucket_bounds if b != "+Inf"]
            expected = [
                "0.1", "0.25", "0.5", "1.0", "1.5",
                "2.5", "5.0", "10.0", "30.0", "60.0",
            ]
            for exp in expected:
                assert exp in finite_bounds, f"Missing bucket le={exp}"
            return
    pytest.fail("embedder_batch_duration_seconds not found in REGISTRY")


# ---------------------------------------------------------------------------
# Qwen3Embedder integration — histogram observed on embed()
# ---------------------------------------------------------------------------


def _mock_transport_ok(dim: int = 4):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.5] * dim]})

    return httpx.MockTransport(handler)


def test_qwen3_embed_observes_histogram():
    """Qwen3Embedder.embed() must observe the histogram for each _embed_one batch."""
    from prometheus_client import REGISTRY

    from src.indexer.embedder import Qwen3Embedder

    e = Qwen3Embedder(url="http://test", model="m", dim=4, transport=_mock_transport_ok())

    before = _get_histogram_count(
        REGISTRY, "embedder_batch_duration_seconds", _LABEL
    )
    e.embed(["hello", "world"])
    after = _get_histogram_count(
        REGISTRY, "embedder_batch_duration_seconds", _LABEL
    )

    assert after == before + 1, (
        f"Expected histogram count to increment by 1 after embed(); "
        f"before={before}, after={after}"
    )


def test_qwen3_large_batch_observes_once_per_subbatch():
    """When texts > _MAX_BATCH, each sub-batch triggers one histogram observation."""
    import json

    from prometheus_client import REGISTRY

    from src.indexer.embedder import Qwen3Embedder

    class _SmallBatchEmbedder(Qwen3Embedder):
        _MAX_BATCH = 2

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        n = len(body["input"])
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3, 0.4]] * n})

    e = _SmallBatchEmbedder(
        url="http://test", model="m", dim=4, transport=httpx.MockTransport(handler)
    )

    before = _get_histogram_count(
        REGISTRY, "embedder_batch_duration_seconds", _LABEL
    )
    # 5 texts, batched into ceil(5/2)=3 sub-batches
    e.embed(["a", "b", "c", "d", "e"])
    after = _get_histogram_count(
        REGISTRY, "embedder_batch_duration_seconds", _LABEL
    )

    assert after == before + 3, (
        f"Expected 3 histogram observations for 5 texts with _MAX_BATCH=2; "
        f"before={before}, after={after}"
    )


# ---------------------------------------------------------------------------
# /metrics HTTP endpoint tests
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _mcp_http_client():
    from asgi_lifespan import LifespanManager

    from src.mcp.server import mcp

    app = mcp.http_app(stateless_http=True, json_response=True)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client


@pytest.mark.asyncio
@pytest.mark.neo4j
@pytest.mark.postgres
async def test_metrics_endpoint_returns_200():
    """/metrics must return HTTP 200."""
    async with _mcp_http_client() as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    )


@pytest.mark.asyncio
@pytest.mark.neo4j
@pytest.mark.postgres
async def test_metrics_endpoint_content_type():
    """/metrics must return Prometheus text format content-type."""
    async with _mcp_http_client() as client:
        resp = await client.get("/metrics")
    # Prometheus CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    assert "text/plain" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
@pytest.mark.neo4j
@pytest.mark.postgres
async def test_metrics_endpoint_contains_histogram_name():
    """/metrics body must reference embedder_batch_duration_seconds."""
    async with _mcp_http_client() as client:
        resp = await client.get("/metrics")
    assert "embedder_batch_duration_seconds" in resp.text, (
        "Expected 'embedder_batch_duration_seconds' in /metrics output"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_histogram_count(registry, metric_name: str, label_filter: dict) -> float:
    """Return the _count sample for a Histogram from the registry.

    Returns 0.0 if the metric/label combination is not yet present.
    """
    for metric in registry.collect():
        if metric.name == metric_name:
            for sample in metric.samples:
                if sample.name == f"{metric_name}_count":
                    if all(sample.labels.get(k) == v for k, v in label_filter.items()):
                        return sample.value
    return 0.0


def _get_histogram_sum(registry, metric_name: str, label_filter: dict) -> float:
    """Return the _sum sample for a Histogram from the registry.

    Returns 0.0 if the metric/label combination is not yet present.
    """
    for metric in registry.collect():
        if metric.name == metric_name:
            for sample in metric.samples:
                if sample.name == f"{metric_name}_sum":
                    if all(sample.labels.get(k) == v for k, v in label_filter.items()):
                        return sample.value
    return 0.0
