"""Bounded-offload semaphore tests for the 4 ORM-validation tools.

Pure asyncio — no Docker / Neo4j. These guard the PR #275 round-3 CRITICAL-2
fix: the ORM concurrency slot must be tied to the WORKER THREAD, not the caller
coroutine, so that cancelling the coroutine (client disconnect) does NOT free
the slot while the worker thread is still pinning a Neo4j connection.

The pre-fix implementation (loop-bound asyncio.Semaphore released in the
coroutine's ``finally``) freed the slot the instant the coroutine was cancelled
— test (3) below reproduces exactly that and fails against the old code.
"""

import asyncio
import importlib
import os
import threading

import pytest

from src.mcp.orm import OrmQueryTimeout


def _reload_server_with(env: dict):
    """Reload src.mcp.server with the given ORM env overrides applied.

    The ORM knobs are now the SSOT in src.constants (PR #275 review LOW SSOT),
    so constants must be reloaded FIRST for the env override to take effect, then
    server (which re-imports them). Returns the freshly-reloaded server module.
    """
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


# ---------------------------------------------------------------------------
# (1) Concurrency cap holds — never more than ORM_QUERY_MAX_CONCURRENCY run.
# ---------------------------------------------------------------------------

def test_cap_concurrency_holds():
    srv = _reload_server_with(
        {"ORM_QUERY_MAX_CONCURRENCY": "2", "ORM_SLOT_ACQUIRE_TIMEOUT": "0.2"}
    )
    try:
        peak = 0
        current = 0
        lock = threading.Lock()

        @srv.offload_bounded
        def slow(model, odoo_version="auto"):
            nonlocal peak, current
            with lock:
                current += 1
                peak = max(peak, current)
            threading.Event().wait(0.4)  # hold the slot
            with lock:
                current -= 1
            return "done"

        async def drive():
            tasks = [asyncio.create_task(slow("m", "99.0")) for _ in range(4)]
            return await asyncio.gather(*tasks, return_exceptions=True)

        results = asyncio.run(drive())
        assert peak <= 2, f"cap breached: peak={peak}"
        # 2 served, 2 fast-rejected (acquire timeout 0.2s < the 0.4s hold).
        served = [r for r in results if r == "done"]
        assert len(served) == 2, results
    finally:
        _restore(["ORM_QUERY_MAX_CONCURRENCY", "ORM_SLOT_ACQUIRE_TIMEOUT"])


# ---------------------------------------------------------------------------
# (2) Fast-reject returns a 'busy' STRING (not an exception escaping the
#     wrapper) — uniform with the embed path + ADR-0023 raw-text posture.
# ---------------------------------------------------------------------------

def test_fast_reject_returns_busy_string():
    srv = _reload_server_with(
        {"ORM_QUERY_MAX_CONCURRENCY": "1", "ORM_SLOT_ACQUIRE_TIMEOUT": "0.1"}
    )
    try:
        gate = threading.Event()

        @srv.offload_bounded
        def blocker(model, odoo_version="auto"):
            gate.wait(2.0)
            return "done"

        async def drive():
            holder = asyncio.create_task(blocker("m", "99.0"))
            await asyncio.sleep(0.05)  # let the holder grab the only slot
            # This one cannot acquire within 0.1s -> fast-reject as a string.
            rejected = await blocker("m", "99.0")
            gate.set()
            await holder
            return rejected

        rejected = asyncio.run(drive())
        assert isinstance(rejected, str), type(rejected)
        assert "busy" in rejected and "retry" in rejected, rejected
        assert not isinstance(rejected, srv.OrmOverloaded)
    finally:
        _restore(["ORM_QUERY_MAX_CONCURRENCY", "ORM_SLOT_ACQUIRE_TIMEOUT"])


# ---------------------------------------------------------------------------
# (3) CANCELLATION: slot stays HELD while the worker thread is still running,
#     and is only released after the thread exits. The pre-fix code FAILS here
#     (it released on coroutine cancel while the thread kept running).
# ---------------------------------------------------------------------------

def test_slot_held_until_thread_exits_not_on_cancel():
    srv = _reload_server_with(
        {"ORM_QUERY_MAX_CONCURRENCY": "2", "ORM_SLOT_ACQUIRE_TIMEOUT": "5"}
    )
    try:
        started = threading.Event()
        finish = threading.Event()
        exited = threading.Event()

        @srv.offload_bounded
        def long_running(model, odoo_version="auto"):
            started.set()
            try:
                finish.wait(5.0)  # held open until the test releases it
                return "done"
            finally:
                exited.set()

        sem = srv._get_orm_semaphore()
        # Probe available permits without consuming any. A threading semaphore
        # has no public count; acquire-then-release with a non-blocking probe
        # tells us whether at least one permit is free.
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
            assert free_permits(2) == 2, "expected both permits free at start"
            task = asyncio.create_task(long_running("m", "99.0"))
            # Wait until the worker thread has actually entered the body and
            # taken its slot.
            await asyncio.to_thread(started.wait, 5.0)
            assert started.is_set()
            # One slot is now held by the running thread.
            assert free_permits(2) == 1, "worker thread should hold one slot"

            # Cancel the coroutine mid-thread (simulates client disconnect).
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # CRITICAL-2 invariant: the worker thread is STILL running, so the
            # slot must STILL be held. Pre-fix code released it on cancel here.
            assert not exited.is_set(), "thread should still be running"
            assert free_permits(2) == 1, (
                "cancellation must NOT free the slot while the thread runs"
            )

            # Now let the thread finish; the slot must come back.
            finish.set()
            await asyncio.to_thread(exited.wait, 5.0)
            # Give the thread's finally (sem.release) a beat to run.
            for _ in range(50):
                if free_permits(2) == 2:
                    break
                await asyncio.sleep(0.02)
            assert free_permits(2) == 2, "slot not reclaimed after thread exit"

        asyncio.run(drive())
    finally:
        finish.set()
        _restore(["ORM_QUERY_MAX_CONCURRENCY", "ORM_SLOT_ACQUIRE_TIMEOUT"])


# ---------------------------------------------------------------------------
# (4) Timeout metric is incremented IN-THREAD even when the coroutine is
#     cancelled — the cancel-path observability blind spot (MED).
# ---------------------------------------------------------------------------

def test_timeout_metric_recorded_even_when_coroutine_cancelled():
    srv = _reload_server_with(
        {"ORM_QUERY_MAX_CONCURRENCY": "2", "ORM_SLOT_ACQUIRE_TIMEOUT": "5"}
    )
    try:
        from src import metrics

        def _count():
            # Sum the counter across whatever 'tool' label samples exist.
            total = 0.0
            for m in metrics.orm_query_timeout_total.collect():
                for s in m.samples:
                    if s.name.endswith("_total"):
                        total += s.value
            return total

        before = _count()

        started = threading.Event()
        proceed = threading.Event()

        @srv.offload_bounded
        def timing_out(model, odoo_version="auto"):
            started.set()
            proceed.wait(5.0)
            # Simulate the Neo4j query timeout firing AFTER the coroutine was
            # cancelled — the metric/log must still be recorded in-thread.
            raise OrmQueryTimeout("ORM query timed out — narrow and retry.")

        async def drive():
            task = asyncio.create_task(timing_out("m", "99.0"))
            await asyncio.to_thread(started.wait, 5.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # The thread is parked on proceed.wait; release it so it reaches the
            # OrmQueryTimeout raise + the in-thread metric increment.
            proceed.set()

        asyncio.run(drive())

        # Poll: the thread runs detached after cancellation; wait for the metric.
        import time

        deadline = time.time() + 5.0
        while time.time() < deadline and _count() <= before:
            time.sleep(0.02)
        assert _count() == before + 1, (
            "orm_query_timeout_total must increment in-thread even after cancel"
        )
    finally:
        proceed.set()
        _restore(["ORM_QUERY_MAX_CONCURRENCY", "ORM_SLOT_ACQUIRE_TIMEOUT"])


# ---------------------------------------------------------------------------
# (5) Env validation fail-fast: bad values -> SystemExit (HIGH #3).
# ---------------------------------------------------------------------------

def test_validate_orm_env_rejects_bad_values():
    import src.mcp.server as srv

    # Baseline good config must NOT raise.
    base = {
        "NEO4J_QUERY_TIMEOUT_SECONDS": "30",
        "ORM_QUERY_MAX_CONCURRENCY": "8",
        "ORM_SLOT_ACQUIRE_TIMEOUT": "5",
    }
    saved = {k: os.environ.get(k) for k in base}
    try:
        os.environ.update(base)
        srv._validate_orm_env()  # no raise

        # neo4j timeout 0 -> driver no-timeout -> reverts #273.
        os.environ["NEO4J_QUERY_TIMEOUT_SECONDS"] = "0"
        with pytest.raises(SystemExit):
            srv._validate_orm_env()
        os.environ["NEO4J_QUERY_TIMEOUT_SECONDS"] = "30"

        # ORM cap 0 -> every call fast-rejects forever.
        os.environ["ORM_QUERY_MAX_CONCURRENCY"] = "0"
        with pytest.raises(SystemExit):
            srv._validate_orm_env()
        os.environ["ORM_QUERY_MAX_CONCURRENCY"] = "8"

        # acquire timeout >= neo4j timeout -> reject is no longer fast.
        os.environ["ORM_SLOT_ACQUIRE_TIMEOUT"] = "30"
        with pytest.raises(SystemExit):
            srv._validate_orm_env()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
