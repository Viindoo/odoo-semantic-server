# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mark a tool/resource body as degraded so it is never cached.

A renderer that swallows a dependency failure and still returns a body (for
example "Lifecycle: unavailable (ledger unreachable)") calls
:func:`mark_degraded`. :meth:`src.mcp.resources.ResourceCache.get_or_compute`
runs the renderer inside :func:`collect_degradation` and skips the cache put
when anything was marked, so the next read retries instead of serving the
degraded body for the whole TTL.

Outside a collection scope :func:`mark_degraded` is a no-op (tool calls are not
cached). The marker lives in a :class:`contextvars.ContextVar`, so concurrent
renders on other threads or tasks never see each other's marks.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_SINK: ContextVar[list[str] | None] = ContextVar("osm_degraded_body", default=None)


def mark_degraded(reason: str) -> None:
    """Record that the body being rendered is missing data because of *reason*."""
    sink = _SINK.get()
    if sink is not None:
        sink.append(reason)


@contextmanager
def collect_degradation() -> Iterator[list[str]]:
    """Collect :func:`mark_degraded` reasons raised while the block runs."""
    sink: list[str] = []
    token = _SINK.set(sink)
    try:
        yield sink
    finally:
        _SINK.reset(token)
