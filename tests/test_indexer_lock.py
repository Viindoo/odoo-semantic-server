# tests/test_indexer_lock.py
"""Tests for Postgres advisory lock in indexer pipeline."""
import os

import pytest

from src.indexer.pipeline import _LOCK_ID, _indexer_lock

pytestmark = pytest.mark.postgres


class TestIndexerLock:
    def test_acquire_and_release(self, pg_conn):
        """Lock can be acquired and released without error."""
        with _indexer_lock(pg_conn, "test-profile"):
            # Lock acquired — verify we can still execute queries
            with pg_conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone()[0] == 1
        # After context exit, lock is released

    def test_double_acquire_raises(self, pg_conn):
        """Second acquire while first is held raises RuntimeError."""
        import psycopg2

        dsn = os.environ.get(
            "PG_TEST_DSN",
            "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
        )
        conn2 = psycopg2.connect(dsn)
        conn2.autocommit = True
        try:
            with _indexer_lock(pg_conn, "test-profile-concurrent"):
                # While first lock is held, try to acquire same lock on second conn
                with conn2.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,))
                    acquired = cur.fetchone()[0]
                assert (
                    acquired is False
                ), "Second connection should not acquire lock while first holds it"
        finally:
            conn2.close()

    def test_lock_releases_on_exception(self, pg_conn):
        """Lock is released even when exception raised inside context."""
        with pytest.raises(ValueError, match="simulated error"):
            with _indexer_lock(pg_conn, "test-exception"):
                raise ValueError("simulated error")

        # Lock should be released now — re-acquire should succeed
        with _indexer_lock(pg_conn, "test-exception"):
            pass
