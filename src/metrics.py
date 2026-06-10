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
from prometheus_client import Counter, Histogram

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

# ---------------------------------------------------------------------------
# Auth — forgot-password background-task counters (WFIX-1, ADR-0023 style)
# ---------------------------------------------------------------------------

forgot_password_db_failure_total = Counter(
    "forgot_password_db_failure_total",
    "Number of DB errors (lookup or INSERT) in the forgot-password background task.",
)

forgot_password_email_send_failure_total = Counter(
    "forgot_password_email_send_failure_total",
    "Number of SMTP send failures in the forgot-password background task.",
)

forgot_password_success_total = Counter(
    "forgot_password_success_total",
    "Number of password-reset tokens successfully inserted and emailed.",
)

# ---------------------------------------------------------------------------
# ORM-validation tools — timeout + overload counters (#273, ADR-0046 style)
# ---------------------------------------------------------------------------
# These surface the two NEW failure modes introduced by the #273 fix so ops
# can distinguish "dense-graph query hit the Neo4j timeout" (data regression /
# mesh re-grown) from "a fan-out burst saturated the ORM semaphore". The
# `tool` label is the ORM tool name (resolve_orm_chain / validate_domain /
# validate_depends / validate_relation).

orm_query_timeout_total = Counter(
    "orm_query_timeout_total",
    "Number of ORM-validation tool calls whose Neo4j query hit the per-query"
    " timeout (#273).",
    labelnames=["tool"],
)

orm_overloaded_total = Counter(
    "orm_overloaded_total",
    "Number of ORM-validation tool calls fast-rejected because the bounded ORM"
    " concurrency semaphore was full (#273).",
    labelnames=["tool"],
)

__all__ = [
    "embedder_batch_duration_seconds",
    "forgot_password_db_failure_total",
    "forgot_password_email_send_failure_total",
    "forgot_password_success_total",
    "orm_overloaded_total",
    "orm_query_timeout_total",
    "prometheus_client",
]
