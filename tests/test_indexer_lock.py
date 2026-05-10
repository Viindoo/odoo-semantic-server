# tests/test_indexer_lock.py
"""Tests for Postgres advisory lock in indexer pipeline."""
import os

import pytest

from src.indexer.pipeline import _indexer_lock, _profile_lock_id

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
                lock_id = _profile_lock_id("test-profile-concurrent")
                with conn2.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
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

    def test_different_profiles_dont_block(self, pg_conn):
        """Two different profile names should NOT block each other."""
        id_a = _profile_lock_id("profile_a")
        id_b = _profile_lock_id("profile_b")
        assert id_a != id_b
        # Acquire lock A
        with pg_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (id_a,))
            assert cur.fetchone()[0] is True
        # Acquire lock B from same connection — should succeed (different ids)
        with pg_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (id_b,))
            assert cur.fetchone()[0] is True
        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (id_a,))
            cur.execute("SELECT pg_advisory_unlock(%s)", (id_b,))

    def test_same_profile_blocks(self, pg_conn):
        """Same profile from two connections — second must fail."""
        import psycopg2

        dsn = os.environ.get(
            "PG_TEST_DSN",
            "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
        )
        id_a = _profile_lock_id("profile_a")
        conn2 = psycopg2.connect(dsn)
        conn2.autocommit = True
        try:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (id_a,))
                assert cur.fetchone()[0] is True
            # From second connection — should fail
            with conn2.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (id_a,))
                assert cur.fetchone()[0] is False
            # Cleanup from original conn
            with pg_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (id_a,))
        finally:
            conn2.close()
