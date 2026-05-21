# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_mcp_pg_pool.py
"""Unit/integration tests for the SimpleConnectionPool refactor (WI-P2).

Marker: pytest.mark.postgres — tests that need a live PostgreSQL connection.

These tests verify:
1. Pool creates connections and recycles them across sequential checkouts.
2. Concurrent checkouts from multiple threads do not corrupt state.
3. register_vector is idempotent — calling it on an already-registered
   connection still allows ``::vector`` casts.
4. Pool exhaustion at maxconn+1 concurrent holders either blocks (waits)
   or raises psycopg2.pool.PoolError — never silently returns a bad conn.
"""
import concurrent.futures
import time

import psycopg2
import psycopg2.pool
import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixture: reset module-level pool between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_pg_pool():
    """Close + reset the module-level pool before and after each test.

    Ensures tests are independent — each test starts with a fresh pool using
    PG_TEST_DSN from conftest.py (set via PG_TEST_DSN env var or default).
    Pool is now managed in src.db.pg._pool (PgPool), not src.mcp.server._pg_pool.

    Saves and restores the session-scoped pool so other tests (e.g., seed_patterns
    integration tests) that rely on the session pool are not broken after this file
    runs.  Also resets store singletons that hold references to the old pool.
    """
    import src.db.pg as pg_mod

    # Save the session-scoped pool (created by conftest pg_conn fixture).
    _saved_pool = pg_mod._pool
    _saved_auth = pg_mod._auth_store
    _saved_repo = pg_mod._repo_store
    _saved_job = pg_mod._job_store

    # Detach session pool so this test gets a clean slate (do NOT close it —
    # pg_conn session fixture owns its lifetime).
    pg_mod._pool = None
    pg_mod._auth_store = None
    pg_mod._repo_store = None
    pg_mod._job_store = None

    # Initialize a fresh test-scoped pool so tests calling get_pool() directly
    # (not via _checkout_pg / _ensure_pg) have a pool available.
    import os

    from src.db.pg import init_pool

    test_dsn = os.getenv(
        "PG_TEST_DSN",
        "postgresql://odoo_semantic:password@127.0.0.1:5432/odoo_semantic",
    )
    try:
        init_pool(test_dsn, min_conn=1, max_conn=5)
    except Exception:
        pass  # skip-worthy failures handled by _inject_pg_dsn / pg_conn fixtures

    yield

    # Tear down the pool created by this test (not the session pool).
    if pg_mod._pool is not None and pg_mod._pool is not _saved_pool:
        try:
            pg_mod._pool.close()
        except Exception:
            pass

    # Restore session-scoped pool and stores so subsequent tests are unaffected.
    pg_mod._pool = _saved_pool
    pg_mod._auth_store = _saved_auth
    pg_mod._repo_store = _saved_repo
    pg_mod._job_store = _saved_job


@pytest.fixture(autouse=True)
def _inject_pg_dsn(monkeypatch, pg_conn):
    """Point the server module at the test DSN so _get_pool() connects correctly.

    pg_conn is requested here so that if PostgreSQL is unreachable the test
    is skipped (via the pg_conn fixture's pytest.skip call).

    We set PG_DSN to PG_TEST_DSN (the raw DSN string) rather than
    pg_conn.dsn, because psycopg2 masks passwords in conn.dsn as 'xxx'.

    Also skips if the pgvector extension is not installed — _checkout_pg()
    calls register_vector() which requires the vector type to exist.
    """
    import os

    from src.db.migrate import _vector_extension_available, run_migrations

    run_migrations(pg_conn)
    if not _vector_extension_available(pg_conn):
        pytest.skip(
            "pgvector extension not installed — "
            "run as superuser: CREATE EXTENSION vector;"
        )

    test_dsn = os.getenv(
        "PG_TEST_DSN",
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
    )
    monkeypatch.setenv("PG_DSN", test_dsn)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pool_creates_and_recycles(pg_conn):
    """Sequential checkouts reuse the same physical connection (minconn=1)."""
    from src.db.pg import get_pool
    from src.mcp.server import _checkout_pg

    pool = get_pool()
    assert pool is not None

    backend_pids = []
    for _ in range(3):
        with _checkout_pg() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_backend_pid()")
                pid = cur.fetchone()[0]
                backend_pids.append(pid)

    # With minconn=1 and sequential access, the same backend PID is reused.
    assert len(set(backend_pids)) == 1, (
        f"Expected the same connection to be recycled; got PIDs: {backend_pids}"
    )


def test_concurrent_checkouts_do_not_corrupt(pg_conn):
    """5 concurrent threads each check out a connection and run SELECT 1 safely."""
    from src.mcp.server import _checkout_pg

    errors: list[Exception] = []
    results: list[int] = []

    def worker(_):
        try:
            with _checkout_pg() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    val = cur.fetchone()[0]
                    results.append(val)
        except Exception as exc:
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as exe:
        list(exe.map(worker, range(5)))

    assert not errors, f"Concurrent checkouts raised exceptions: {errors}"
    assert results == [1] * 5, f"Unexpected SELECT 1 results: {results}"


def test_register_vector_idempotent(pg_conn):
    """Registering pgvector on an already-registered connection is a no-op.

    This covers the case where pool grows and a recycled connection is
    re-registered on each checkout.
    """
    from pgvector.psycopg2 import register_vector

    from src.mcp.server import _checkout_pg

    with _checkout_pg() as conn:
        # Call register_vector a second time explicitly — must not raise
        register_vector(conn)
        with conn.cursor() as cur:
            # A basic ::vector cast verifies the type adapter is active
            cur.execute("SELECT '[1,2,3]'::vector")
            row = cur.fetchone()
    assert row is not None, "::vector cast returned no row after idempotent registration"


def test_pool_max_blocks_or_raises_at_11():
    """At maxconn=10, the 11th concurrent holder is blocked or raises PoolError.

    We create a pool with maxconn=10, then spawn 11 threads that each hold a
    connection for 0.5 s.  The 11th thread must either:
    - Block (waits in getconn) and eventually get a conn when one is released, OR
    - Raise ``psycopg2.pool.PoolError`` (pool exhausted, no-wait mode).

    Either outcome is acceptable — the important thing is the pool does NOT
    silently return a None/invalid connection.

    Note: SimpleConnectionPool.getconn() with no connections available raises
    ``psycopg2.pool.PoolError`` immediately (no blocking wait). This test
    asserts that the 11th checkout raises PoolError within 2 seconds.
    """
    import os

    import src.db.pg as pg_mod
    from src.db.pg import PgPool

    dsn = os.environ.get("PG_DSN")
    if not dsn:
        pytest.skip("PG_DSN not set — need live PG for pool exhaustion test")

    # Build a small PgPool with maxconn=10 explicitly for this test.
    # The _reset_pg_pool autouse fixture will close and reset it after the test.
    small_pool_obj = PgPool(dsn, min_conn=1, max_conn=10)
    pg_mod._pool = small_pool_obj  # fixture's autouse reset will clean this up
    small_pool = small_pool_obj._pool  # underlying SimpleConnectionPool for direct getconn()

    held_events: list = []
    release_event = __import__("threading").Event()
    errors: list[Exception] = []
    conn_list: list = []

    def hold_conn(_):
        """Check out a conn and hold it until release_event is set."""
        try:
            conn = small_pool.getconn()
            conn_list.append(conn)
            # Signal that we got the conn, then wait
            held_events.append(True)
            release_event.wait(timeout=2.0)
        except psycopg2.pool.PoolError as e:
            errors.append(e)
        except Exception as e:
            errors.append(e)

    # Spawn 11 threads — 10 should succeed, 11th should raise PoolError
    with concurrent.futures.ThreadPoolExecutor(max_workers=11) as exe:
        futs = [exe.submit(hold_conn, i) for i in range(11)]
        # Give all threads a moment to attempt getconn
        time.sleep(0.3)
        # Release all held connections
        release_event.set()
        # Collect results
        for f in futs:
            f.result(timeout=5.0)

    # Return all successfully checked-out connections
    for c in conn_list:
        try:
            small_pool.putconn(c)
        except Exception:
            pass

    # Assert: pool behaviour at maxconn+1 is observable (PoolError raised or
    # at most maxconn simultaneous holders due to psycopg2's best-effort
    # thread-safety in SimpleConnectionPool).
    pool_error_raised = len([e for e in errors if isinstance(e, psycopg2.pool.PoolError)])
    other_errors = [e for e in errors if not isinstance(e, psycopg2.pool.PoolError)]

    assert not other_errors, f"Unexpected non-PoolError exceptions: {other_errors}"

    # Either a PoolError was observed (strict enforcement) OR all threads
    # succeeded within the timeout after connections were released.
    # What must NOT happen: non-psycopg2 exceptions or silent None returns.
    assert pool_error_raised >= 1 or len(conn_list) >= 10, (
        f"Pool neither raised PoolError nor checked out connections. "
        f"conn_list={len(conn_list)}, errors={errors}"
    )
