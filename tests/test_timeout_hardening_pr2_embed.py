# SPDX-License-Identifier: AGPL-3.0-or-later
"""PR-2 EMBED-then-offload + set_active_version timeout-hardening tests (no live DB).

The three EMBED tools (``suggest_pattern`` / ``find_examples`` /
``find_style_override``) are ``async def`` — they embed on the event loop, then
offload the blocking Neo4j/PG body via ``asyncio.to_thread``. Because there is NO
``@offload_neo4j`` backstop around the to_thread hop, the body must catch the
``OrmQueryTimeout`` INLINE, emit the non-ORM timeout metric exactly once, and
return the clean ADR-0023 string (never raise / escape as a protocol error).

``set_active_version`` (GAP-2) is a MUTATING ``@offload`` tool whose two Neo4j
sanity reads were previously swallowed by a catch-all ``except Exception`` with
no metric — its new ``except OrmQueryTimeout`` (placed BEFORE the catch-all) must
record the metric once and return the clean string.

These tests force the relevant Neo4j read to time out (``_TxTimeoutDriver``) while
the version resolution + PG path are stubbed, then assert:
  * the result is a CLEAN timeout string (not a raise, no Cypher leaked), and
  * ``nonorm_query_timeout_total{tool=...}`` incremented by exactly 1.

Tests are named after the business rule they protect, per ETHOS#11.
"""

from __future__ import annotations

import asyncio
import threading

from tests._timeout_harness import (
    TIMEOUT_TEST_VERSION,
    _TxTimeoutDriver,
    assert_clean_timeout_string,
)


def _run(coro):
    """Run *coro* in a dedicated thread with a fresh event loop.

    Mirrors test_timeout_hardening_pr1._run — a fresh thread always has a clean
    loop, so ``asyncio.run`` is safe even under pytest-asyncio mode=auto.
    """
    box: dict = {}

    def runner():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # propagate to the test thread
            box["error"] = exc

    t = threading.Thread(target=runner)
    t.start()
    t.join(timeout=60)
    assert not t.is_alive(), "coroutine did not finish within 60s"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _metric_value(tool: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "nonorm_query_timeout_total", {"tool": tool}
    ) or 0.0


# ---------------------------------------------------------------------------
# Minimal PG stubs — let the EMBED bodies reach their Neo4j read with rows in
# hand, without a real Postgres.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Cursor stub: ``execute`` is a no-op, ``fetchall`` returns canned rows."""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return None

    def fetchall(self):
        return self._rows


class _FakePgConn:
    """Connection stub yielding a cursor with canned rows."""

    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


class _StubEmbedder:
    """Embedder stub — never actually called when ``_query_vec`` is supplied."""

    query_instruction = ""

    def embed(self, texts):
        return [[0.0, 0.0, 0.0] for _ in texts]


# ---------------------------------------------------------------------------
# suggest_pattern — the PatternExample batch fetch must degrade cleanly.
# ---------------------------------------------------------------------------


def test_suggest_pattern_pattern_fetch_timeout_returns_clean_string_and_counts_once(
    monkeypatch,
):
    """A PatternExample-fetch tx-timeout degrades to a clean string + 1 metric.

    Protects: suggest_pattern must NEVER let a Neo4j timeout escape as a protocol
    error — the async body has no decorator backstop, so the inline catch is the
    only guard (ADR-0023 raw-text contract).
    """
    import src.mcp.server as srv
    from src.mcp.tools import guidance

    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)

    # One ranked row from PG so the body reaches the PatternExample Neo4j query.
    # entity_name slug "<language>__<id>" → decoded to pattern_id "p1".
    pg = _FakePgConn([("python__p1", "patterns/p1.py", 0.91)])

    before = _metric_value("suggest_pattern")
    result = guidance._suggest_pattern(
        "override write to read old value",
        TIMEOUT_TEST_VERSION,
        "python",
        5,
        _driver=_TxTimeoutDriver(),
        _pg_conn=pg,
        _embedder=_StubEmbedder(),
        _query_vec=[0.1, 0.2, 0.3],
    )
    after = _metric_value("suggest_pattern")

    assert_clean_timeout_string(result)
    assert after == before + 1, (
        f"suggest_pattern must count the timeout once; before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# find_examples — the rerank queries (incl. the VLP DEPENDS_ON chain) must
# degrade cleanly.
# ---------------------------------------------------------------------------


def test_find_examples_rerank_timeout_returns_clean_string_and_counts_once(monkeypatch):
    """A rerank-query tx-timeout degrades to a clean string + 1 metric.

    Protects: find_examples reranks via two Neo4j queries (one is the #273-class
    ``DEPENDS_ON*1..`` VLP). A timeout on either must be caught once and surfaced
    as a clean string — not escape, not double-count.
    """
    import src.mcp.server as srv
    from src.mcp.tools import discovery

    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)
    # Stub the server-hub PG-side helpers so the ANN path is inert (no real DB):
    # None = admin/no tenant filter; null RLS tx; no-op iterative-scan flag.
    monkeypatch.setattr(srv, "_effective_allowed", lambda p: None)
    monkeypatch.setattr(srv, "_set_iterative_scan", lambda cur: None)

    class _NullTx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(srv, "_rls_read_tx", lambda pg, allowed: _NullTx())

    # One ANN row from PG (non-style path) so the body reaches the Neo4j rerank
    # with a non-empty module set. Columns mirror the find_examples ANN SELECT:
    # chunk_type, module, entity_name, model_name, file_path, chunk_idx, content,
    # cosine, line_start, repo, repo_id.
    ann_row = (
        "method", "sale", "sale.order.write", None,
        "addons/sale/models/sale_order.py", 0, "def write(self, vals): ...",
        0.88, 10, None, None,
    )
    pg = _FakePgConn([ann_row])

    before = _metric_value("find_examples")
    # context_module set so BOTH rerank queries run (dependents + DEPENDS_ON chain).
    result = discovery._find_examples(
        "write override",
        TIMEOUT_TEST_VERSION,
        5,
        context_module="account",
        _driver=_TxTimeoutDriver(),
        _pg_conn=pg,
        _embedder=_StubEmbedder(),
        _query_vec=[0.1, 0.2, 0.3],
    )
    after = _metric_value("find_examples")

    assert_clean_timeout_string(result)
    assert after == before + 1, (
        f"find_examples must count the timeout once; before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# find_style_override — the per-result importer BFS must degrade cleanly.
# ---------------------------------------------------------------------------


def test_find_style_override_importer_timeout_returns_clean_string_and_counts_once(
    monkeypatch,
):
    """An importer-BFS tx-timeout in the render loop degrades cleanly + 1 metric.

    Protects: find_style_override runs a per-result importer BFS inside its render
    loop. A timeout on any row must be caught around the whole loop (one metric,
    one clean string) rather than escaping mid-render.
    """
    import src.mcp.server as srv
    from src.mcp.tools import stylesheet

    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)
    # The body calls _effective_allowed / _rls_read_tx / _set_iterative_scan on
    # the server hub — stub them so the PG path is inert (None = admin/no filter).
    monkeypatch.setattr(srv, "_effective_allowed", lambda p: None)

    class _NullTx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(srv, "_rls_read_tx", lambda pg, allowed: _NullTx())
    monkeypatch.setattr(srv, "_set_iterative_scan", lambda cur: None)

    # One ANN style row from PG so the render loop runs (and hits the importer
    # BFS on the timing-out driver). Columns mirror the find_style_override ANN
    # SELECT: chunk_type, module, entity_name, file_path, chunk_idx, content, cosine.
    ann_row = (
        "scss", "web", "selector:.o_list_view",
        "addons/web/static/src/scss/list_view.scss", 0,
        ".o_list_view { display: flex; }", 0.77,
    )
    pg = _FakePgConn([ann_row])

    before = _metric_value("find_style_override")
    # ".o_list_view" is a literal token → literal-first path; literal_rows are
    # returned from the same stub cursor, the importer BFS then times out.
    result = stylesheet._find_style_override(
        ".o_list_view",
        TIMEOUT_TEST_VERSION,
        5,
        _driver=_TxTimeoutDriver(),
        _pg_conn=pg,
        _embedder=_StubEmbedder(),
        _query_vec=[0.1, 0.2, 0.3],
    )
    after = _metric_value("find_style_override")

    assert_clean_timeout_string(result)
    assert after == before + 1, (
        f"find_style_override must count the timeout once; before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# set_active_version (GAP-2) — the sanity read must no longer be silently
# swallowed by the catch-all.
# ---------------------------------------------------------------------------


def test_set_active_version_sanity_read_timeout_returns_clean_string_and_counts_once(
    monkeypatch,
):
    """A sanity-read tx-timeout returns a clean string + 1 metric (not swallowed).

    Protects: set_active_version pins a version only after a Neo4j sanity read.
    Pre-#287 a tx-timeout was caught by the catch-all ``except Exception`` with no
    metric, indistinguishable from a real config error. The new
    ``except OrmQueryTimeout`` (before the catch-all) must surface it cleanly and
    count it exactly once.
    """
    import src.mcp.server as srv
    from src.mcp.tools import session_tools

    monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())

    before = _metric_value("set_active_version")
    # @offload wraps the sync body in an async def; .fn is that async wrapper, so
    # drive it through a fresh event loop. The sanity read times out → the new
    # except OrmQueryTimeout returns exc.user_message (a clean str).
    result = _run(session_tools.set_active_version.fn("17.0"))
    after = _metric_value("set_active_version")

    # @offload returns the sync body's value; on the OrmQueryTimeout branch that
    # value is the clean string (exc.user_message), not a ToolResult.
    text = result if isinstance(result, str) else result.content[0].text
    assert_clean_timeout_string(text)
    assert after == before + 1, (
        f"set_active_version must count the timeout once; before={before} after={after}"
    )
