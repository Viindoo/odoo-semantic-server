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


def _run(coro):
    """Run a coroutine in a dedicated thread with its own fresh event loop.

    Under the full unit suite (pytest-asyncio mode=auto) an earlier test can
    leave a RUNNING loop on the main thread, making a bare ``asyncio.run()``
    raise "cannot be called from a running event loop". A fresh thread has no
    loop, so ``asyncio.run`` there is always safe and fully isolated.
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

        results = _run(drive())
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

        rejected = _run(drive())
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

        _run(drive())
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

        _run(drive())

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


def test_nonorm_timeout_records_separate_counter_not_orm():
    """A non-ORM heavy-read timeout must increment nonorm_query_timeout_total,
    NOT orm_query_timeout_total — ops rely on the label to tell the pools apart
    (#276 G5/G6). Protects the offload_bounded_nonorm timeout-metric wiring.
    """
    srv = _reload_server_with(
        {"NONORM_READ_MAX_CONCURRENCY": "2", "NONORM_SLOT_ACQUIRE_TIMEOUT": "5"}
    )
    try:
        from src import metrics

        def _count(counter):
            total = 0.0
            for m in counter.collect():
                for s in m.samples:
                    if s.name.endswith("_total"):
                        total += s.value
            return total

        nonorm_before = _count(metrics.nonorm_query_timeout_total)
        orm_before = _count(metrics.orm_query_timeout_total)

        @srv.offload_bounded_nonorm
        def timing_out(model, odoo_version="auto"):
            raise OrmQueryTimeout("non-ORM read timed out — narrow and retry.")

        async def drive():
            # The wrapper catches OrmQueryTimeout and returns the user message.
            return await timing_out("m", "99.0")

        result = _run(drive())
        assert "timed out" in result.lower()

        assert _count(metrics.nonorm_query_timeout_total) == nonorm_before + 1, (
            "non-ORM timeout must increment nonorm_query_timeout_total"
        )
        assert _count(metrics.orm_query_timeout_total) == orm_before, (
            "non-ORM timeout must NOT increment the ORM counter"
        )
    finally:
        _restore(["NONORM_READ_MAX_CONCURRENCY", "NONORM_SLOT_ACQUIRE_TIMEOUT"])


# ---------------------------------------------------------------------------
# (5) Env validation fail-fast: bad values -> SystemExit (HIGH #3 + #276 G6/G7).
# ---------------------------------------------------------------------------

def test_validate_orm_env_rejects_bad_values():
    import src.mcp.server as srv

    # Baseline good config must NOT raise.
    base = {
        "NEO4J_QUERY_TIMEOUT_SECONDS": "30",
        "ORM_QUERY_MAX_CONCURRENCY": "8",
        "ORM_SLOT_ACQUIRE_TIMEOUT": "5",
        "NONORM_READ_MAX_CONCURRENCY": "8",
        "NONORM_SLOT_ACQUIRE_TIMEOUT": "5",
        "EMBEDDER_SLOT_ACQUIRE_TIMEOUT": "5",
        "EMBEDDER_TIMEOUT_READ_QUERY": "30",
        "EMBEDDER_MAX_CONCURRENCY": "8",
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
        os.environ["ORM_SLOT_ACQUIRE_TIMEOUT"] = "5"

        # #276 G6: non-ORM cap 0 -> every non-ORM heavy read fast-rejects forever.
        os.environ["NONORM_READ_MAX_CONCURRENCY"] = "0"
        with pytest.raises(SystemExit):
            srv._validate_orm_env()
        os.environ["NONORM_READ_MAX_CONCURRENCY"] = "8"

        # #276 G6: non-ORM acquire timeout >= neo4j timeout -> reject not fast.
        os.environ["NONORM_SLOT_ACQUIRE_TIMEOUT"] = "30"
        with pytest.raises(SystemExit):
            srv._validate_orm_env()
        os.environ["NONORM_SLOT_ACQUIRE_TIMEOUT"] = "5"

        # #276 G7: embed acquire timeout >= query read timeout -> reject not fast.
        os.environ["EMBEDDER_SLOT_ACQUIRE_TIMEOUT"] = "30"
        with pytest.raises(SystemExit):
            srv._validate_orm_env()
        os.environ["EMBEDDER_SLOT_ACQUIRE_TIMEOUT"] = "5"

        # #276 G7: embed cap 0 -> BoundedSemaphore(0) can never be acquired ->
        # every query-embed fast-rejects forever (parity with ORM/non-ORM caps).
        os.environ["EMBEDDER_MAX_CONCURRENCY"] = "0"
        with pytest.raises(SystemExit):
            srv._validate_orm_env()
        os.environ["EMBEDDER_MAX_CONCURRENCY"] = "8"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# (6) #276 G7 — EMBED path slot is THREAD-held: cancelling the coroutine while
#     the worker thread is still embedding must NOT free the slot. The pre-fix
#     code (asyncio.Semaphore released in the coroutine `finally`) FAILS here.
# ---------------------------------------------------------------------------

def test_embed_slot_held_until_thread_exits_not_on_cancel():
    srv = _reload_server_with(
        {"EMBEDDER_MAX_CONCURRENCY": "2", "EMBEDDER_SLOT_ACQUIRE_TIMEOUT": "5"}
    )
    try:
        started = threading.Event()
        finish = threading.Event()
        exited = threading.Event()

        class _BlockingEmbedder:
            # Minimal embedder: sync embed() blocks until released, mirroring a
            # slow Ollama round-trip. No _embed_with_timeout -> exercises the
            # FakeEmbedder fallback path in _embed_sync_query.
            chars_per_token = 4.0

            def embed(self, texts):
                started.set()
                try:
                    finish.wait(5.0)
                    return [[0.1, 0.2, 0.3] for _ in texts]
                finally:
                    exited.set()

        embedder = _BlockingEmbedder()
        sem = srv._get_embed_semaphore()

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
            assert free_permits(2) == 2, "expected both embed permits free at start"
            task = asyncio.create_task(srv._embed_query(embedder, "", "hello world"))
            await asyncio.to_thread(started.wait, 5.0)
            assert started.is_set()
            # The worker thread now holds one embed slot.
            assert free_permits(2) == 1, "embed worker thread should hold one slot"

            # Cancel mid-embed (simulates client disconnect).
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # #276 G7 invariant: thread still embedding -> slot still held.
            assert not exited.is_set(), "embed thread should still be running"
            assert free_permits(2) == 1, (
                "cancel must NOT free the embed slot while the thread embeds"
            )

            # Release the embed; the slot must come back after the thread exits.
            finish.set()
            await asyncio.to_thread(exited.wait, 5.0)
            for _ in range(50):
                if free_permits(2) == 2:
                    break
                await asyncio.sleep(0.02)
            assert free_permits(2) == 2, "embed slot not reclaimed after thread exit"

        _run(drive())
    finally:
        finish.set()
        _restore(["EMBEDDER_MAX_CONCURRENCY", "EMBEDDER_SLOT_ACQUIRE_TIMEOUT"])


# ---------------------------------------------------------------------------
# (7) #276 G7 — embed fast-reject raises EmbedOverloaded from inside the worker
#     thread when the bounded embed semaphore is saturated.
# ---------------------------------------------------------------------------

def test_embed_fast_reject_when_saturated():
    srv = _reload_server_with(
        {"EMBEDDER_MAX_CONCURRENCY": "1", "EMBEDDER_SLOT_ACQUIRE_TIMEOUT": "0.1"}
    )
    try:
        gate = threading.Event()

        class _BlockingEmbedder:
            chars_per_token = 4.0

            def embed(self, texts):
                gate.wait(2.0)
                return [[0.1] for _ in texts]

        embedder = _BlockingEmbedder()

        async def drive():
            holder = asyncio.create_task(srv._embed_query(embedder, "", "first"))
            await asyncio.sleep(0.05)  # let the holder grab the only slot
            # Second embed cannot acquire within 0.1s -> EmbedOverloaded.
            with pytest.raises(srv.EmbedOverloaded):
                await srv._embed_query(embedder, "", "second")
            gate.set()
            await holder

        _run(drive())
    finally:
        gate.set()  # release any still-blocked embed thread
        _restore(["EMBEDDER_MAX_CONCURRENCY", "EMBEDDER_SLOT_ACQUIRE_TIMEOUT"])


# ---------------------------------------------------------------------------
# (8) #279 — the SAME cancel/overload/timeout behaviour must hold for BOTH the
#     ORM and non-ORM bounded-offload pools. After consolidating the two
#     decorators into one factory (_make_bounded_offload), this single
#     parametrized test guards BOTH paths at once: a future cancel-safety fix
#     that regresses one pool but not the other now fails here instead of
#     slipping through (the missed-fix class that bit the embed path in #275).
# ---------------------------------------------------------------------------

# (decorator attr, getter attr, cap env, slot-timeout env, busy-class attr)
_POOLS = [
    pytest.param(
        "offload_bounded",
        "_get_orm_semaphore",
        "ORM_QUERY_MAX_CONCURRENCY",
        "ORM_SLOT_ACQUIRE_TIMEOUT",
        id="orm_pool",
    ),
    pytest.param(
        "offload_bounded_nonorm",
        "_get_nonorm_semaphore",
        "NONORM_READ_MAX_CONCURRENCY",
        "NONORM_SLOT_ACQUIRE_TIMEOUT",
        id="nonorm_pool",
    ),
]


@pytest.mark.parametrize("decorator,getter,cap_env,timeout_env", _POOLS)
def test_cancel_safety_identical_across_pools(decorator, getter, cap_env, timeout_env):
    """Cancel-safety + fast-reject + in-thread timeout-metric are IDENTICAL for
    the ORM and non-ORM pools (the #279 consolidation invariant)."""
    srv = _reload_server_with({cap_env: "2", timeout_env: "5"})
    try:
        from src import metrics

        bound = getattr(srv, decorator)
        sem = getattr(srv, getter)()

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

        # --- (a) CANCELLATION: slot held until the thread exits, not on cancel.
        started = threading.Event()
        finish = threading.Event()
        exited = threading.Event()

        @bound
        def long_running(model, odoo_version="auto"):
            started.set()
            try:
                finish.wait(5.0)
                return "done"
            finally:
                exited.set()

        async def drive_cancel():
            assert free_permits(2) == 2
            task = asyncio.create_task(long_running("m", "99.0"))
            await asyncio.to_thread(started.wait, 5.0)
            assert free_permits(2) == 1, "worker thread should hold one slot"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Thread still running -> slot still held (the CRITICAL-2 invariant).
            assert not exited.is_set(), "thread should still be running"
            assert free_permits(2) == 1, (
                f"{getter}: cancel must NOT free the slot while the thread runs"
            )
            finish.set()
            await asyncio.to_thread(exited.wait, 5.0)
            for _ in range(50):
                if free_permits(2) == 2:
                    break
                await asyncio.sleep(0.02)
            assert free_permits(2) == 2, "slot not reclaimed after thread exit"

        _run(drive_cancel())

        # --- (b) FAST-REJECT past the cap returns a 'busy' STRING (ADR-0023).
        srv2 = _reload_server_with({cap_env: "1", timeout_env: "0.1"})
        bound2 = getattr(srv2, decorator)
        gate = threading.Event()

        @bound2
        def blocker(model, odoo_version="auto"):
            gate.wait(2.0)
            return "done"

        async def drive_reject():
            holder = asyncio.create_task(blocker("m", "99.0"))
            await asyncio.sleep(0.05)
            rejected = await blocker("m", "99.0")
            gate.set()
            await holder
            return rejected

        rejected = _run(drive_reject())
        assert isinstance(rejected, str), type(rejected)
        assert "busy" in rejected and "retry" in rejected, rejected

        # --- (c) TIMEOUT metric is recorded IN-THREAD even after a cancel, and
        #     ONLY on this pool's counter (never the other pool's).
        srv3 = _reload_server_with({cap_env: "2", timeout_env: "5"})
        bound3 = getattr(srv3, decorator)
        this_counter = (
            metrics.orm_query_timeout_total
            if decorator == "offload_bounded"
            else metrics.nonorm_query_timeout_total
        )
        other_counter = (
            metrics.nonorm_query_timeout_total
            if decorator == "offload_bounded"
            else metrics.orm_query_timeout_total
        )

        def _count(counter):
            total = 0.0
            for m in counter.collect():
                for s in m.samples:
                    if s.name.endswith("_total"):
                        total += s.value
            return total

        this_before = _count(this_counter)
        other_before = _count(other_counter)

        started3 = threading.Event()
        proceed3 = threading.Event()

        @bound3
        def timing_out(model, odoo_version="auto"):
            started3.set()
            proceed3.wait(5.0)
            raise OrmQueryTimeout("query timed out — narrow and retry.")

        async def drive_timeout():
            task = asyncio.create_task(timing_out("m", "99.0"))
            await asyncio.to_thread(started3.wait, 5.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            proceed3.set()

        _run(drive_timeout())

        import time

        deadline = time.time() + 5.0
        while time.time() < deadline and _count(this_counter) <= this_before:
            time.sleep(0.02)
        assert _count(this_counter) == this_before + 1, (
            f"{decorator}: timeout metric must increment in-thread even after cancel"
        )
        assert _count(other_counter) == other_before, (
            f"{decorator}: timeout must NOT touch the other pool's counter"
        )
    finally:
        finish.set()
        gate.set()
        proceed3.set()
        _restore([cap_env, timeout_env])
