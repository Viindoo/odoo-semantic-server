# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prometheus metrics registry for the Odoo Semantic MCP project.

Single module that owns all metric definitions so they are registered once
and importable from anywhere in the process.

Layered at ``src/`` (shared) — NOT under ``src/mcp/`` — so that the indexer
pipeline (``src/indexer/embedder.py``) can record batch-embed durations
without the indexer layer importing the server (mcp) layer. Both the indexer
and the MCP server depend *downward* onto this shared module; the pipeline
direction stays one-way (CLAUDE.md "Pipeline — Không Cross-Import Ngang Hàng").

Cross-process caveat
--------------------
The MCP server (:8002) and the indexer batch job run as **separate OS
processes**.  prometheus_client uses an in-process registry by default
(not multiprocess-mode / pushgateway).  Therefore:

- ``/metrics`` on the MCP server reflects only embeddings done *in that
  process* (i.e. query-embed calls from find_examples / find_style_override).
- Batch-index embed calls (src/indexer pipeline) are NOT visible on
  ``/metrics`` of the MCP process.

If cross-process aggregation is needed in the future, adopt
``prometheus_client`` multiprocess mode (PROMETHEUS_MULTIPROC_DIR env var)
or a Pushgateway.  For now, in-process is the right trade-off: the MCP
server embed path is the latency-sensitive one; batch can be logged via
the existing `_logger.debug` lines.
"""
import prometheus_client
from prometheus_client import Histogram

# Bucket boundaries chosen for Qwen3-Embedding batches up to EMBEDDER_MAX_BATCH=50.
# Empirical: ~22s per 100 texts → ~11s per 50-text batch.
# Buckets cover: sub-second (query embed), 1-10s typical batch, up to 60s timeout.
_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1.0, 1.5, 2.5, 5.0, 10.0, 30.0, 60.0)

embedder_batch_duration_seconds = Histogram(
    "embedder_batch_duration_seconds",
    "Duration in seconds of a single Qwen3Embedder._embed_one() batch call.",
    labelnames=["embedder_type"],
    buckets=_DURATION_BUCKETS,
)

__all__ = [
    "embedder_batch_duration_seconds",
    "prometheus_client",
]
