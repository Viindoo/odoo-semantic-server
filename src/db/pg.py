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
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2.extras import RealDictCursor, execute_values

from src.db._types import PgConn

if TYPE_CHECKING:
    from src.db.auth_registry import AuthStore
    from src.db.job_registry import JobStore
    from src.db.repo_registry import RepoStore


class PgPool:
    """Centralizes psycopg2 connection pool + safe parameterized-query helpers."""

    def __init__(self, dsn: str, *, min_conn: int = 2, max_conn: int = 10) -> None:
        self._pool = psycopg2.pool.SimpleConnectionPool(min_conn, max_conn, dsn)

    # ------------------------------------------------------------------
    # Connection checkout
    # ------------------------------------------------------------------

    @contextmanager
    def checkout(self) -> Generator[PgConn, None, None]:
        """Checkout a connection from the pool. Auto-returns on context exit."""
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)

    @contextmanager
    def checkout_vec(self) -> Generator[PgConn, None, None]:
        """Checkout + register pgvector (register_vector). For embedding queries."""
        conn = self._pool.getconn()
        try:
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


def init_pool(dsn: str, *, min_conn: int = 2, max_conn: int = 10) -> None:
    """Initialize module-level pool singleton. Call once at app startup."""
    global _pool
    _pool = PgPool(dsn, min_conn=min_conn, max_conn=max_conn)


def get_pool() -> PgPool:
    """Return the initialized pool. Raises RuntimeError if init_pool() not called."""
    if _pool is None:
        raise RuntimeError(
            "PostgreSQL pool is not initialized. Call init_pool(dsn) at startup."
        )
    return _pool


# ---------------------------------------------------------------------------
# Lazy store accessors
# ---------------------------------------------------------------------------

_auth_store: "AuthStore | None" = None
_repo_store: "RepoStore | None" = None
_job_store: "JobStore | None" = None


def auth_store() -> "AuthStore":
    """Return module-level AuthStore singleton (lazy init after init_pool)."""
    global _auth_store
    if _auth_store is None:
        from src.db.auth_registry import AuthStore  # noqa: PLC0415
        _auth_store = AuthStore(get_pool())
    return _auth_store


def repo_store() -> "RepoStore":
    """Return module-level RepoStore singleton (lazy init after init_pool)."""
    global _repo_store
    if _repo_store is None:
        from src.db.repo_registry import RepoStore  # noqa: PLC0415
        _repo_store = RepoStore(get_pool())
    return _repo_store


def job_store() -> "JobStore":
    """Return module-level JobStore singleton (lazy init after init_pool)."""
    global _job_store
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
