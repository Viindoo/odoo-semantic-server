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
from unittest.mock import MagicMock

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
    """A FIRST-rerank-query (dependents) tx-timeout degrades cleanly + 1 metric.

    Protects: find_examples reranks via two Neo4j queries inside ONE try/except.
    This drives the all-runs-timeout harness, so the FIRST (dependents) query is
    the one that times out here. The second / VLP ``DEPENDS_ON*1..`` chain query
    (the #273-class fan-out) is covered separately by
    test_find_examples_vlp_chain_query_timeout below — the all-runs-timeout
    harness can never reach it because it dies on query #1. A timeout must be
    caught once, surfaced as a clean string — not escape, not double-count.
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


class _SecondRunTimeoutSession:
    """Session whose FIRST ``.run()`` succeeds (empty data) and SECOND times out.

    Lets a test drive the find_examples VLP ``DEPENDS_ON*1..`` chain query (the
    second rerank query) to timeout while the first/dependents query passes — the
    exact #273-class fan-out path the all-runs-timeout harness can never reach
    because it dies on query #1.
    """

    def __init__(self):
        self._calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, *a, **k):
        self._calls += 1
        if self._calls == 1:
            result = MagicMock()
            result.data.return_value = []
            return result
        from neo4j.exceptions import ClientError

        exc = ClientError("transaction timed out")
        exc.code = "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"
        raise exc


class _SecondRunTimeoutDriver:
    """Driver whose single session times out only on the SECOND ``.run()``."""

    def __init__(self):
        self._session = _SecondRunTimeoutSession()

    def session(self, *a, **k):
        return self._session


def test_find_examples_vlp_chain_query_timeout_returns_clean_string_and_counts_once(
    monkeypatch,
):
    """A tx-timeout on the VLP DEPENDS_ON chain (2nd rerank query) degrades cleanly.

    Protects: the #273-class ``DEPENDS_ON*1..`` chain query is the actual fan-out
    vector #287 bounds. Here the first/dependents query SUCCEEDS (empty) and only
    the chain query times out — so this test reaches the chain path the
    all-runs-timeout harness cannot (it raises on query #1). The timeout must be
    caught once and surfaced as a clean string.
    """
    import src.mcp.server as srv
    from src.mcp.tools import discovery

    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)
    monkeypatch.setattr(srv, "_effective_allowed", lambda p: None)
    monkeypatch.setattr(srv, "_set_iterative_scan", lambda cur: None)

    class _NullTx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(srv, "_rls_read_tx", lambda pg, allowed: _NullTx())

    ann_row = (
        "method", "sale", "sale.order.write", None,
        "addons/sale/models/sale_order.py", 0, "def write(self, vals): ...",
        0.88, 10, None, None,
    )
    pg = _FakePgConn([ann_row])

    before = _metric_value("find_examples")
    # context_module set so the SECOND (VLP chain) query runs; the driver passes
    # query #1 (dependents) and times out only on that chain query.
    result = discovery._find_examples(
        "write override",
        TIMEOUT_TEST_VERSION,
        5,
        context_module="account",
        _driver=_SecondRunTimeoutDriver(),
        _pg_conn=pg,
        _embedder=_StubEmbedder(),
        _query_vec=[0.1, 0.2, 0.3],
    )
    after = _metric_value("find_examples")

    assert_clean_timeout_string(result)
    assert after == before + 1, (
        f"find_examples VLP chain must count the timeout once; "
        f"before={before} after={after}"
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


# ---------------------------------------------------------------------------
# Version-resolution timeout (#287 review) — the 3 EMBED tools resolve the Odoo
# version via a bounded Neo4j read (Tier-3 _latest_version) that can itself time
# out. That call runs OUTSIDE the main fetch/rerank catch, so each tool now wraps
# it in its own inline catch — a resolve timeout must degrade cleanly, not escape
# the async body as a protocol error.
# ---------------------------------------------------------------------------


def _raise_resolve_timeout(*_a, **_k):
    from src.mcp.orm import OrmQueryTimeout

    raise OrmQueryTimeout(
        "Query timed out after 30s while resolving the latest indexed Odoo version."
    )


def test_suggest_pattern_version_resolution_timeout_returns_clean_string(monkeypatch):
    """A tx-timeout during version resolution degrades cleanly + 1 metric."""
    import src.mcp.server as srv
    from src.mcp.tools import guidance

    monkeypatch.setattr(srv, "_resolve_version", _raise_resolve_timeout)

    before = _metric_value("suggest_pattern")
    result = guidance._suggest_pattern(
        "override write", "auto", "python", 5,
        _driver=_TxTimeoutDriver(),
        _embedder=_StubEmbedder(),
        _query_vec=[0.1, 0.2, 0.3],
    )
    after = _metric_value("suggest_pattern")

    assert_clean_timeout_string(result)
    assert after == before + 1


def test_find_examples_version_resolution_timeout_returns_clean_string(monkeypatch):
    """A tx-timeout during version resolution degrades cleanly + 1 metric."""
    import src.mcp.server as srv
    from src.mcp.tools import discovery

    monkeypatch.setattr(srv, "_resolve_version", _raise_resolve_timeout)

    before = _metric_value("find_examples")
    # odoo_version='auto' so the gated _resolve_version call fires.
    result = discovery._find_examples(
        "write override", "auto", 5,
        _driver=_TxTimeoutDriver(),
        _embedder=_StubEmbedder(),
        _query_vec=[0.1, 0.2, 0.3],
    )
    after = _metric_value("find_examples")

    assert_clean_timeout_string(result)
    assert after == before + 1


def test_find_style_override_version_resolution_timeout_returns_clean_string(monkeypatch):
    """A tx-timeout during version resolution degrades cleanly + 1 metric."""
    import src.mcp.server as srv
    from src.mcp.tools import stylesheet

    monkeypatch.setattr(srv, "_resolve_version", _raise_resolve_timeout)

    before = _metric_value("find_style_override")
    result = stylesheet._find_style_override(
        ".o_list_view", "auto", 5,
        _driver=_TxTimeoutDriver(),
        _embedder=_StubEmbedder(),
        _query_vec=[0.1, 0.2, 0.3],
    )
    after = _metric_value("find_style_override")

    assert_clean_timeout_string(result)
    assert after == before + 1


# ---------------------------------------------------------------------------
# Version-resolution Tier-3 END-TO-END (#287 review, Hướng B). The STUB tests
# above replace _resolve_version with a raiser — they prove "the tool catches
# when resolve raises", but never run the real 3-tier resolver. These tests run
# Tier-3 FOR REAL: version='auto' (a sentinel, so Tier-1 does NOT short-circuit)
# + a no-pin session (Tier-2 returns None for the non-numeric 'default' api key)
# drives resolve_version_v2 into Tier-3 _latest_version(), whose bounded Neo4j
# read runs on the _TxTimeoutDriver and times out — exactly the production path
# the harness's EXPLICIT-version + stubbed-resolver design left in a blind spot.
# _resolve_version is NOT monkeypatched here; only the driver and (defensively)
# the Tier-2 session-state lookup are. Kept ALONGSIDE the stub tests above: stub
# = unit (catch wiring), this = integration-of-resolution (real Tier-3).
# ---------------------------------------------------------------------------


def _stub_no_pin(monkeypatch, srv):
    """Force Tier-2 to report no session pin so resolution falls to Tier-3.

    In the unit lane ``_get_api_key_id()`` already returns the non-numeric
    ``'default'`` sentinel (so ``get_session_state`` returns ``None`` anyway),
    but we pin it to ``None`` explicitly so the test never depends on a pin a
    prior test may have left in the in-memory store.
    """
    monkeypatch.setattr(srv._session, "get_session_state", lambda *a, **k: None)


def test_suggest_pattern_version_resolution_tier3_timeout_degrades_cleanly(monkeypatch):
    """suggest_pattern: a REAL Tier-3 resolve timeout (auto + no pin) degrades cleanly.

    Protects: the version-resolution Neo4j read (Tier-3 _latest_version) is the
    path the original timeout harness never exercised. With no resolver stub the
    async body's inline catch must convert the OrmQueryTimeout to a clean string
    and count it once — a regression that strips the inline catch lets it escape.
    """
    import src.mcp.server as srv
    from src.mcp.tools import guidance

    _stub_no_pin(monkeypatch, srv)

    before = _metric_value("suggest_pattern")
    result = guidance._suggest_pattern(
        "override write", "auto", "python", 5,
        _driver=_TxTimeoutDriver(),
        _embedder=_StubEmbedder(),
        _query_vec=[0.1, 0.2, 0.3],
    )
    after = _metric_value("suggest_pattern")

    assert_clean_timeout_string(result)
    assert after == before + 1, (
        f"suggest_pattern Tier-3 resolve timeout must count once; "
        f"before={before} after={after}"
    )


def test_find_examples_version_resolution_tier3_timeout_degrades_cleanly(monkeypatch):
    """find_examples: a REAL Tier-3 resolve timeout (auto + no pin) degrades cleanly."""
    import src.mcp.server as srv
    from src.mcp.tools import discovery

    _stub_no_pin(monkeypatch, srv)

    before = _metric_value("find_examples")
    result = discovery._find_examples(
        "write override", "auto", 5,
        _driver=_TxTimeoutDriver(),
        _embedder=_StubEmbedder(),
        _query_vec=[0.1, 0.2, 0.3],
    )
    after = _metric_value("find_examples")

    assert_clean_timeout_string(result)
    assert after == before + 1, (
        f"find_examples Tier-3 resolve timeout must count once; "
        f"before={before} after={after}"
    )


def test_find_style_override_version_resolution_tier3_timeout_degrades_cleanly(
    monkeypatch,
):
    """find_style_override: a REAL Tier-3 resolve timeout (auto + no pin) degrades cleanly."""
    import src.mcp.server as srv
    from src.mcp.tools import stylesheet

    _stub_no_pin(monkeypatch, srv)

    before = _metric_value("find_style_override")
    result = stylesheet._find_style_override(
        ".o_list_view", "auto", 5,
        _driver=_TxTimeoutDriver(),
        _embedder=_StubEmbedder(),
        _query_vec=[0.1, 0.2, 0.3],
    )
    after = _metric_value("find_style_override")

    assert_clean_timeout_string(result)
    assert after == before + 1, (
        f"find_style_override Tier-3 resolve timeout must count once; "
        f"before={before} after={after}"
    )


def test_lookup_core_api_version_resolution_tier3_timeout_caught_by_decorator(
    monkeypatch,
):
    """lookup_core_api: a REAL Tier-3 resolve timeout is caught by @offload_neo4j.

    Protects (gap §2.2): the PURE (sync) tools rely on the @offload_neo4j body
    backstop to cover the resolution read — but no prior test ran the resolution
    path FOR REAL through a decorated wrapper. Here version='auto' + no pin drives
    Tier-3 _latest_version() on the timing-out driver, INSIDE the sync body the
    decorator wraps; the wrapper must surface a clean string, not an escape.
    Driven through ``.fn`` (the @offload_neo4j async wrapper) so the body-level
    catch is the one under test.
    """
    import src.mcp.server as srv

    _stub_no_pin(monkeypatch, srv)
    # The PURE impl uses _srv._get_driver() (no _driver kwarg), so route the hub
    # driver to the timing-out stub.
    monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())

    before = _metric_value("lookup_core_api")
    result = _run(srv.lookup_core_api.fn(name="safe_eval", odoo_version="auto"))
    after = _metric_value("lookup_core_api")

    text = result if isinstance(result, str) else result.content[0].text
    assert_clean_timeout_string(text)
    assert after == before + 1, (
        f"lookup_core_api Tier-3 resolve timeout must count once via the "
        f"@offload_neo4j backstop; before={before} after={after}"
    )
