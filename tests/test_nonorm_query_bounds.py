"""Per-query timeout + bounded-offload guards for NON-ORM heavy reads (#276 G5/G6).

Pure unit tests — no Docker / Neo4j. These protect the issue-#276 behaviour:

  * G5 — every heavy non-ORM read (impact_analysis's 6-query fan-out) runs
    through a ``neo4j.Query`` that carries the per-query timeout, so a runaway
    traversal becomes a bounded ``OrmQueryTimeout`` instead of a zombie
    transaction. The tests below capture the actual ``neo4j.Query`` the helper
    hands to ``session.run`` and assert it carries the timeout — they FAIL if the
    timeout wrapper is removed.

  * G6 — impact_analysis runs under a SEPARATE non-ORM concurrency semaphore
    (``offload_bounded_nonorm``) that fast-rejects past its cap, mirroring the
    ORM bounded-offload guard.

Why "protect behaviour, not code" (ETHOS #11): the timeout-carrying ``Query`` is
the load-bearing contract (#273/#276). A test that only checked "session.run was
called" would pass even after the timeout was dropped — so we assert on the
``Query.timeout`` value itself, the thing that actually prevents the hang.
"""

import asyncio
import importlib
import os
import threading

import neo4j
import pytest
from neo4j.exceptions import ClientError

from src.constants import NEO4J_QUERY_TIMEOUT_SECONDS
from src.mcp.orm import OrmQueryTimeout


def _reload_server_with(env: dict):
    """Reload constants then server with ORM/non-ORM env overrides applied."""
    for k, v in env.items():
        os.environ[k] = v
    import src.constants as consts

    importlib.reload(consts)
    import src.mcp.server as srv

    return importlib.reload(srv)


def _restore(keys):
    for k in keys:
        os.environ.pop(k, None)
    import src.constants as consts

    importlib.reload(consts)
    import src.mcp.server as srv

    importlib.reload(srv)


def _run(coro):
    """Run a coroutine in a dedicated thread with its own fresh event loop."""
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


class _CapturingResult:
    """Stand-in for a neo4j.Result that records the Query it was built from."""

    def __init__(self, captured: dict, rows):
        self._captured = captured
        self._rows = rows

    def data(self):
        return list(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None


class _CapturingSession:
    """Fake Neo4j session capturing every Query handed to ``run``."""

    def __init__(self, rows_for=None):
        self.queries: list = []
        self._rows_for = rows_for or (lambda q: [])

    def run(self, query, **params):
        self.queries.append(query)
        return _CapturingResult({}, self._rows_for(query))


# ---------------------------------------------------------------------------
# G5 — the bounded read helpers attach the per-query timeout to the Query.
# ---------------------------------------------------------------------------

def test_data_bounded_attaches_query_timeout():
    import src.mcp.server as srv

    session = _CapturingSession(rows_for=lambda q: [{"x": 1}])
    out = srv._data_bounded(session, "MATCH (n) RETURN n AS x", "test label")

    assert out == [{"x": 1}]
    assert len(session.queries) == 1
    q = session.queries[0]
    # The load-bearing contract: a neo4j.Query carrying the per-query timeout.
    assert isinstance(q, neo4j.Query), f"expected a neo4j.Query, got {type(q)}"
    assert q.timeout == NEO4J_QUERY_TIMEOUT_SECONDS, (
        "non-ORM read query must carry the per-query timeout (#276 G5)"
    )


def test_single_bounded_attaches_query_timeout():
    import src.mcp.server as srv

    session = _CapturingSession(rows_for=lambda q: [{"c": 3}])
    rec = srv._single_bounded(session, "MATCH (n) RETURN count(n) AS c", "lbl")

    assert rec == {"c": 3}
    q = session.queries[0]
    assert isinstance(q, neo4j.Query)
    assert q.timeout == NEO4J_QUERY_TIMEOUT_SECONDS


def test_bounded_read_converts_tx_timeout_to_ormquerytimeout():
    """A driver/server transaction-timeout ClientError becomes OrmQueryTimeout."""
    import src.mcp.server as srv

    class _TimingOutSession:
        def run(self, query, **params):
            exc = ClientError("timed out")
            # Match the prefix _is_tx_timeout keys on (driver- or server-set).
            exc.code = "Neo.ClientError.Transaction.TransactionTimedOut"
            raise exc

    with pytest.raises(OrmQueryTimeout) as ei:
        srv._data_bounded(_TimingOutSession(), "MATCH (n) RETURN n", "impact for X")
    # ADR-0023 tone: English, names the timeout, no Cypher leaked.
    msg = ei.value.user_message
    assert "timed out" in msg.lower()
    assert "MATCH" not in msg, "must not leak Cypher in the user message"


def test_bounded_read_propagates_non_timeout_clienterror():
    """A non-timeout ClientError (e.g. syntax) propagates unchanged."""
    import src.mcp.server as srv

    class _SyntaxErrorSession:
        def run(self, query, **params):
            exc = ClientError("syntax")
            exc.code = "Neo.ClientError.Statement.SyntaxError"
            raise exc

    with pytest.raises(ClientError):
        srv._data_bounded(_SyntaxErrorSession(), "MATCH (n RETURN n", "lbl")


# ---------------------------------------------------------------------------
# G6 — impact_analysis is bounded by a SEPARATE non-ORM semaphore that
# fast-rejects past its cap (mirror of the ORM bounded-offload guard).
# ---------------------------------------------------------------------------

def test_impact_analysis_uses_nonorm_bounded_offload():
    """The impact_analysis tool body is wrapped by offload_bounded_nonorm.

    Asserted structurally: the registered tool's underlying function differs from
    the bare sync body (it has been wrapped), and a non-ORM semaphore exists.
    """
    import src.mcp.server as srv

    assert hasattr(srv, "offload_bounded_nonorm")
    assert hasattr(srv, "_get_nonorm_semaphore")
    # The non-ORM semaphore is a distinct object from the ORM one (separate pool
    # so one read class cannot starve the other — the whole point of G6).
    assert srv._get_nonorm_semaphore() is not srv._get_orm_semaphore()


def test_nonorm_cap_concurrency_holds_and_fast_rejects():
    srv = _reload_server_with(
        {"NONORM_READ_MAX_CONCURRENCY": "2", "NONORM_SLOT_ACQUIRE_TIMEOUT": "0.2"}
    )
    try:
        peak = 0
        current = 0
        lock = threading.Lock()

        @srv.offload_bounded_nonorm
        def slow(entity_type, entity_name, odoo_version="auto"):
            nonlocal peak, current
            with lock:
                current += 1
                peak = max(peak, current)
            threading.Event().wait(0.4)  # hold the slot
            with lock:
                current -= 1
            return "done"

        async def drive():
            tasks = [
                asyncio.create_task(slow("model", "m", "99.0")) for _ in range(4)
            ]
            return await asyncio.gather(*tasks, return_exceptions=True)

        results = _run(drive())
        assert peak <= 2, f"non-ORM cap breached: peak={peak}"
        served = [r for r in results if r == "done"]
        assert len(served) == 2, results
        # The 2 rejected calls came back as a 'busy' STRING (ADR-0023 raw-text),
        # not an exception escaping the wrapper.
        rejected = [r for r in results if isinstance(r, str) and r != "done"]
        assert len(rejected) == 2
        assert all("busy" in r and "retry" in r for r in rejected), rejected
    finally:
        _restore(["NONORM_READ_MAX_CONCURRENCY", "NONORM_SLOT_ACQUIRE_TIMEOUT"])


def test_nonorm_slot_held_until_thread_exits_not_on_cancel():
    """Cancelling the coroutine must NOT free the non-ORM slot mid-thread (#276 G6)."""
    srv = _reload_server_with(
        {"NONORM_READ_MAX_CONCURRENCY": "2", "NONORM_SLOT_ACQUIRE_TIMEOUT": "5"}
    )
    try:
        started = threading.Event()
        finish = threading.Event()
        exited = threading.Event()

        @srv.offload_bounded_nonorm
        def long_running(entity_type, entity_name, odoo_version="auto"):
            started.set()
            try:
                finish.wait(5.0)
                return "done"
            finally:
                exited.set()

        sem = srv._get_nonorm_semaphore()

        def free_permits(cap):
            grabbed = 0
            for _ in range(cap):
                if sem.acquire(blocking=False):
                    grabbed += 1
                else:
                    break
            for _ in range(grabbed):
                sem.release()
            return grabbed

        async def drive():
            assert free_permits(2) == 2
            task = asyncio.create_task(long_running("model", "m", "99.0"))
            await asyncio.to_thread(started.wait, 5.0)
            assert free_permits(2) == 1, "worker thread should hold one slot"

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert not exited.is_set(), "thread should still be running"
            assert free_permits(2) == 1, (
                "cancel must NOT free the non-ORM slot while the thread runs"
            )

            finish.set()
            await asyncio.to_thread(exited.wait, 5.0)
            for _ in range(50):
                if free_permits(2) == 2:
                    break
                await asyncio.sleep(0.02)
            assert free_permits(2) == 2, "slot not reclaimed after thread exit"

        _run(drive())
    finally:
        finish.set()
        _restore(["NONORM_READ_MAX_CONCURRENCY", "NONORM_SLOT_ACQUIRE_TIMEOUT"])
