# SPDX-License-Identifier: AGPL-3.0-or-later
"""Centralised PostgreSQL connection-pool management and safe SQL helpers.

Usage
-----
At application startup (once)::

    from src.db.pg import init_pool
    init_pool(dsn="postgresql://...")

Anywhere else::

    from src.db.pg import get_pool
    pool = get_pool()
    with pool.checkout() as conn:
        row = pool.fetch_one(conn, "SELECT id FROM profiles WHERE name = %s", (name,))
"""
import logging
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2.extras import RealDictCursor, execute_values

from src.constants import PG_CONNECT_TIMEOUT_SECONDS
from src.db._types import PgConn

if TYPE_CHECKING:
    from src.db.auth_registry import AuthStore
    from src.db.job_registry import JobStore
    from src.db.repo_registry import RepoStore


_log = logging.getLogger(__name__)


class PgPool:
    """Centralizes psycopg2 connection pool + safe parameterized-query helpers."""

    def __init__(
        self,
        dsn: str,
        *,
        min_conn: int = 2,
        max_conn: int = 10,
        connect_timeout: int = PG_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        # connect_timeout is forwarded to psycopg2.connect() so an unreachable
        # PG fails fast (within connect_timeout seconds) instead of hanging
        # the caller. SimpleConnectionPool eagerly opens min_conn connections
        # at construction, so a missing PG raises here — callers that want to
        # tolerate a transient outage should use init_pool_with_retry().
        self._pool = psycopg2.pool.SimpleConnectionPool(
            min_conn, max_conn, dsn, connect_timeout=connect_timeout,
        )

    # ------------------------------------------------------------------
    # Connection checkout
    # ------------------------------------------------------------------

    @contextmanager
    def checkout(self) -> Generator[PgConn, None, None]:
        """Checkout a connection from the pool. Auto-returns on context exit.

        Rolls back any pending transaction and sets autocommit=True before
        yielding so callers always receive a clean connection regardless of
        what the previous checkout left behind.
        """
        conn = self._pool.getconn()
        try:
            conn.rollback()       # clear any pending transaction
            conn.autocommit = True  # safe now; callers manage transactions explicitly
            yield conn
        finally:
            self._pool.putconn(conn)

    @contextmanager
    def checkout_vec(self) -> Generator[PgConn, None, None]:
        """Checkout + register pgvector (register_vector). For embedding queries."""
        conn = self._pool.getconn()
        try:
            conn.rollback()
            conn.autocommit = True
            from pgvector.psycopg2 import register_vector
            register_vector(conn)
            yield conn
        finally:
            self._pool.putconn(conn)

    # ------------------------------------------------------------------
    # SQL helpers
    # ------------------------------------------------------------------

    def execute(self, conn: PgConn, sql: str, params: tuple = ()) -> int:
        """Execute parameterized SQL. Returns rowcount. Commits if not autocommit.

        sql MUST be a static string literal — never an f-string or concatenation.
        params MUST be passed separately — never interpolated into sql.
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rowcount = cur.rowcount
        if not conn.autocommit:
            conn.commit()
        return rowcount

    def fetch_one(self, conn: PgConn, sql: str, params: tuple = ()) -> dict | None:
        """Execute + fetchone. Returns dict (column→value) or None if no row.

        Uses RealDictCursor internally.
        """
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return dict(row) if row is not None else None

    def fetch_all(self, conn: PgConn, sql: str, params: tuple = ()) -> list[dict]:
        """Execute + fetchall. Returns list of dicts. Uses RealDictCursor."""
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def execute_batch(self, conn: PgConn, sql: str, values: list[tuple]) -> None:
        """Bulk insert via psycopg2.extras.execute_values. Commits if not autocommit."""
        with conn.cursor() as cur:
            execute_values(cur, sql, values)
        if not conn.autocommit:
            conn.commit()

    def close(self) -> None:
        """Close all connections in pool."""
        self._pool.closeall()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_pool: PgPool | None = None


def init_pool(
    dsn: str,
    *,
    min_conn: int = 2,
    max_conn: int = 10,
    connect_timeout: int = PG_CONNECT_TIMEOUT_SECONDS,
) -> None:
    """Initialize module-level pool singleton. Call once at app startup."""
    global _pool
    _pool = PgPool(
        dsn, min_conn=min_conn, max_conn=max_conn, connect_timeout=connect_timeout,
    )


def is_pool_initialized() -> bool:
    """Predicate for the degraded-mode startup path. True once init_pool() succeeded."""
    return _pool is not None


def init_pool_with_retry(
    dsn: str,
    *,
    min_conn: int = 2,
    max_conn: int = 10,
    connect_timeout: int = PG_CONNECT_TIMEOUT_SECONDS,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> None:
    """Initialize pool with exponential backoff retry.

    For callers that need a working pool before doing useful work (CLI
    subcommands, indexer, migration). MCP server lifespan uses a different
    pattern (try once → schedule background retry → enter degraded mode)
    so startup is not blocked by an unreachable DB tier.

    Raises the last exception when `max_attempts` are exhausted.
    """
    delay = base_delay
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            init_pool(
                dsn, min_conn=min_conn, max_conn=max_conn,
                connect_timeout=connect_timeout,
            )
            if attempt > 1:
                _log.info("PG pool init succeeded on attempt %d/%d", attempt, max_attempts)
            return
        except Exception as e:  # noqa: BLE001 — re-raised below if budget exhausted
            last_error = e
            _log.warning(
                "PG pool init attempt %d/%d failed: %s (retry in %.1fs)",
                attempt, max_attempts, str(e)[:200], delay,
            )
            if attempt == max_attempts:
                break
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    # Re-raise the most recent error so callers see the actual psycopg2/network cause.
    assert last_error is not None  # invariant: loop ran ≥1 attempt
    raise last_error


def get_pool() -> PgPool:
    """Return the initialized pool.

    Raises `PoolNotInitializedError` (which IS-A RuntimeError, so legacy
    `except RuntimeError` callers are unaffected) when the module-level pool
    singleton has not been created yet. Use the typed exception in new code
    so handlers can distinguish "pool down" from unrelated RuntimeErrors.
    """
    if _pool is None:
        from src.db.exceptions import PoolNotInitializedError  # noqa: PLC0415

        raise PoolNotInitializedError(
            "PostgreSQL pool is not initialized. Call init_pool(dsn) at startup."
        )
    return _pool


# ---------------------------------------------------------------------------
# Lazy store accessors
# ---------------------------------------------------------------------------

_auth_store: "AuthStore | None" = None
_repo_store: "RepoStore | None" = None
_job_store: "JobStore | None" = None
_store_lock = threading.Lock()  # guards auth_store / repo_store / job_store lazy init


def auth_store() -> "AuthStore":
    """Return module-level AuthStore singleton (lazy init after init_pool)."""
    global _auth_store
    if _auth_store is not None:  # fast path — no lock overhead on hot calls
        return _auth_store
    with _store_lock:
        if _auth_store is None:  # re-check after acquiring lock
            from src.db.auth_registry import AuthStore  # noqa: PLC0415
            _auth_store = AuthStore(get_pool())
    return _auth_store


def repo_store() -> "RepoStore":
    """Return module-level RepoStore singleton (lazy init after init_pool)."""
    global _repo_store
    if _repo_store is not None:
        return _repo_store
    with _store_lock:
        if _repo_store is None:
            from src.db.repo_registry import RepoStore  # noqa: PLC0415
            _repo_store = RepoStore(get_pool())
    return _repo_store


def job_store() -> "JobStore":
    """Return module-level JobStore singleton (lazy init after init_pool)."""
    global _job_store
    if _job_store is not None:
        return _job_store
    with _store_lock:
        if _job_store is None:
            from src.db.job_registry import JobStore  # noqa: PLC0415
            _job_store = JobStore(get_pool())
    return _job_store


# ---------------------------------------------------------------------------
# Advisory lock context manager
# ---------------------------------------------------------------------------

@contextmanager
def advisory_lock(conn: PgConn, lock_id: int) -> Generator[bool, None, None]:
    """Attempt pg_try_advisory_lock(lock_id). Yields True if acquired, False if not.

    Always releases the lock on exit (only if it was acquired).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
        acquired = cur.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
