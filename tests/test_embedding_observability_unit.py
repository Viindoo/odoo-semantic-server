# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_embedding_observability_unit.py
"""Pure-logic unit tests extracted from test_embedding_observability.py (WS-D / DD2 demote).

These two tests exercise ``FakeEmbedder.call_count`` (increment + thread-safety)
entirely in-process — they construct a ``FakeEmbedder`` and call ``embed()``
directly, never opening a Neo4j session or a Postgres connection and never
requesting the ``clean_neo4j`` / ``clean_pg`` fixtures.  The parent file carries a
module-level ``pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]`` (its other
tests index a real module end-to-end), which a per-test override cannot subtract;
so these pure tests live here in an unmarked module and now run in the fast unit
tier (``-m 'not neo4j and not postgres'``).

DD2 evidence: confirmed in-process counter assertions on ``FakeEmbedder`` —
no DB driver invoked.
"""
from src.indexer.embedder import FakeEmbedder

# ---------------------------------------------------------------------------
# Test 1: FakeEmbedder.call_count increments
# ---------------------------------------------------------------------------

def test_embedder_call_count_increments():
    """call_count starts at 0 and increments once per embed() call."""
    embedder = FakeEmbedder(dim=16)
    assert embedder.call_count == 0

    embedder.embed(["text one"])
    assert embedder.call_count == 1

    embedder.embed(["text two"])
    assert embedder.call_count == 2

    embedder.embed(["text three"])
    assert embedder.call_count == 3


# ---------------------------------------------------------------------------
# Test: call_count thread-safety
# ---------------------------------------------------------------------------

def test_embedder_call_count_thread_safe():
    """call_count must be exactly N*M when N threads each call embed() M times.

    Uses FakeEmbedder (no Docker needed) — validates the threading.Lock inside
    embed() prevents lost updates under concurrent access.
    """
    import threading

    N_THREADS = 8
    CALLS_PER_THREAD = 50

    embedder = FakeEmbedder(dim=16)

    errors: list[Exception] = []

    def _worker():
        try:
            for _ in range(CALLS_PER_THREAD):
                embedder.embed(["x"])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread(s) raised exceptions: {errors}"
    expected = N_THREADS * CALLS_PER_THREAD
    assert embedder.call_count == expected, (
        f"Expected call_count == {expected} after {N_THREADS} threads × {CALLS_PER_THREAD} calls; "
        f"got {embedder.call_count} (lost {expected - embedder.call_count} updates)"
    )
