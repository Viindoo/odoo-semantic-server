# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for init_pool_with_retry — backoff behaviour without live PG.

We mock out PgPool so we can drive the retry path deterministically (raise N
times, succeed once) and verify the backoff timing without sleeping for real.
"""
import psycopg2
import pytest


@pytest.fixture(autouse=True)
def _reset_module_pool():
    """Drop the module singleton between tests so each test starts clean."""
    import src.db.pg as pg_mod

    saved = pg_mod._pool
    pg_mod._pool = None
    yield
    pg_mod._pool = saved


def test_init_pool_with_retry_succeeds_first_try(monkeypatch):
    """Happy path — single attempt, no sleeps."""
    from src.db import pg as pg_mod

    constructed: list[dict] = []

    class _FakePool:
        def __init__(self, *args, **kwargs):
            constructed.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(pg_mod.psycopg2.pool, "SimpleConnectionPool", _FakePool)

    sleep_calls: list[float] = []
    monkeypatch.setattr(pg_mod.time, "sleep", lambda s: sleep_calls.append(s))

    pg_mod.init_pool_with_retry("postgresql://fake/db")

    assert len(constructed) == 1
    assert sleep_calls == []  # no retry → no sleep
    assert pg_mod.is_pool_initialized() is True


def test_init_pool_with_retry_recovers_on_third_attempt(monkeypatch):
    """Two failures then success — verify backoff schedule (1s, 2s)."""
    from src.db import pg as pg_mod

    attempts = {"n": 0}

    class _FlakyPool:
        def __init__(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise psycopg2.OperationalError("simulated PG unreachable")

    monkeypatch.setattr(pg_mod.psycopg2.pool, "SimpleConnectionPool", _FlakyPool)

    sleep_calls: list[float] = []
    monkeypatch.setattr(pg_mod.time, "sleep", lambda s: sleep_calls.append(s))

    pg_mod.init_pool_with_retry(
        "postgresql://fake/db",
        max_attempts=5,
        base_delay=1.0,
        max_delay=30.0,
    )

    assert attempts["n"] == 3
    # First retry waits base_delay (1.0); second retry waits 2.0 (exponential).
    assert sleep_calls == [1.0, 2.0]
    assert pg_mod.is_pool_initialized() is True


def test_init_pool_with_retry_raises_after_budget(monkeypatch):
    """All attempts fail — last exception propagates, sleeps respect cap."""
    from src.db import pg as pg_mod

    class _AlwaysFails:
        def __init__(self, *args, **kwargs):
            raise psycopg2.OperationalError("PG is dead")

    monkeypatch.setattr(pg_mod.psycopg2.pool, "SimpleConnectionPool", _AlwaysFails)

    sleep_calls: list[float] = []
    monkeypatch.setattr(pg_mod.time, "sleep", lambda s: sleep_calls.append(s))

    with pytest.raises(psycopg2.OperationalError, match="PG is dead"):
        pg_mod.init_pool_with_retry(
            "postgresql://fake/db",
            max_attempts=3,
            base_delay=1.0,
            max_delay=4.0,
        )

    # 3 attempts → 2 sleeps between them. Schedule: 1.0, 2.0 (capped well under 4.0).
    assert sleep_calls == [1.0, 2.0]
    assert pg_mod.is_pool_initialized() is False  # pool never came up


def test_init_pool_with_retry_respects_max_delay_cap(monkeypatch):
    """With a low max_delay, the exponential schedule plateaus instead of exploding."""
    from src.db import pg as pg_mod

    class _AlwaysFails:
        def __init__(self, *args, **kwargs):
            raise psycopg2.OperationalError("nope")

    monkeypatch.setattr(pg_mod.psycopg2.pool, "SimpleConnectionPool", _AlwaysFails)

    sleep_calls: list[float] = []
    monkeypatch.setattr(pg_mod.time, "sleep", lambda s: sleep_calls.append(s))

    with pytest.raises(psycopg2.OperationalError):
        pg_mod.init_pool_with_retry(
            "postgresql://fake/db",
            max_attempts=5,
            base_delay=1.0,
            max_delay=3.0,
        )

    # 5 attempts → 4 sleeps. Sequence: 1.0, 2.0, then capped to 3.0, 3.0.
    assert sleep_calls == [1.0, 2.0, 3.0, 3.0]


def test_is_pool_initialized_false_until_init(monkeypatch):
    """Sanity check for the new predicate exposed for the lifespan handler."""
    from src.db import pg as pg_mod

    assert pg_mod.is_pool_initialized() is False

    class _OkPool:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(pg_mod.psycopg2.pool, "SimpleConnectionPool", _OkPool)
    pg_mod.init_pool("postgresql://fake/db")
    assert pg_mod.is_pool_initialized() is True


def test_init_pool_forwards_connect_timeout(monkeypatch):
    """The new connect_timeout kwarg must reach psycopg2.connect via the pool."""
    from src.db import pg as pg_mod

    captured: dict = {}

    class _Spy:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(pg_mod.psycopg2.pool, "SimpleConnectionPool", _Spy)
    pg_mod.init_pool("postgresql://fake/db", connect_timeout=7)

    assert captured["kwargs"]["connect_timeout"] == 7


def test_get_pool_raises_typed_exception_when_not_initialized():
    """Issue #3 regression guard: get_pool() must raise PoolNotInitializedError
    (NOT a bare RuntimeError) so downstream `except` clauses can narrow."""
    import pytest as _pytest

    from src.db import pg as pg_mod
    from src.db.exceptions import PoolNotInitializedError

    # _reset_module_pool autouse fixture ensures pg_mod._pool is None.
    assert pg_mod._pool is None
    with _pytest.raises(PoolNotInitializedError):
        pg_mod.get_pool()
    # And the typed exception must still be a RuntimeError (backward compat).
    assert issubclass(PoolNotInitializedError, RuntimeError)
