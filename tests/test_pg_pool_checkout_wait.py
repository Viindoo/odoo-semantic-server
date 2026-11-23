# SPDX-License-Identifier: AGPL-3.0-or-later
"""A pool checkout queues for a returned connection instead of failing.

Parallel index workers (up to ``--max-workers 8``) and request threads share
one ``PgPool``. psycopg2's pool raises ``PoolError`` the instant every
connection is in use, which failed a repo of an index run whenever its
workers happened to write embeddings at the same moment. Business rules:

* more concurrent checkouts than connections all succeed - each waits for a
  connection another thread returns;
* a checkout that no returned connection can serve within the timeout fails
  with ``PoolError`` (bounded, never a hang);
* a block that raises still returns its connection.
"""
from __future__ import annotations

import os
import threading
import time

import psycopg2.pool
import pytest

import src.db.pg as pg_mod


class _FakeConn:
    autocommit = False

    def rollback(self):
        pass


class _FakeThreadedPool:
    """psycopg2 semantics: getconn raises PoolError once max connections are out."""

    def __init__(self, minconn, maxconn, *args, **kwargs):
        self.maxconn = maxconn
        self.out = 0
        self.peak = 0
        self._lock = threading.Lock()

    def getconn(self):
        with self._lock:
            if self.out >= self.maxconn:
                raise psycopg2.pool.PoolError("connection pool exhausted")
            self.out += 1
            self.peak = max(self.peak, self.out)
            return _FakeConn()

    def putconn(self, conn):
        with self._lock:
            self.out -= 1

    def closeall(self):
        pass


@pytest.fixture
def fake_pool_class(monkeypatch):
    monkeypatch.setattr(pg_mod.psycopg2.pool, "ThreadedConnectionPool", _FakeThreadedPool)


def _run_threads(n, target):
    errors: list[BaseException] = []

    def wrapped():
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 - collected for the assert
            errors.append(exc)

    threads = [threading.Thread(target=wrapped) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return errors


def test_more_concurrent_checkouts_than_connections_all_succeed(fake_pool_class):
    pool = pg_mod.PgPool("postgresql://fake/db", min_conn=1, max_conn=2, checkout_timeout=5)

    def work():
        with pool.checkout():
            time.sleep(0.05)

    errors = _run_threads(8, work)

    assert errors == []
    assert pool._pool.peak == 2


def test_checkout_fails_when_no_connection_comes_back_in_time(fake_pool_class):
    pool = pg_mod.PgPool("postgresql://fake/db", min_conn=1, max_conn=1, checkout_timeout=0.2)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with pool.checkout():
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(5)
    started = time.monotonic()
    try:
        with pytest.raises(psycopg2.pool.PoolError), pool.checkout_vec():
            pass
    finally:
        release.set()
        holder.join(5)
    assert 0.2 <= time.monotonic() - started < 2


def test_a_failing_block_returns_its_connection(fake_pool_class):
    pool = pg_mod.PgPool("postgresql://fake/db", min_conn=1, max_conn=1, checkout_timeout=0.2)

    with pytest.raises(RuntimeError), pool.checkout():
        raise RuntimeError("boom")

    with pool.checkout():  # would time out if the slot or the connection leaked
        pass
    assert pool._pool.out == 0


@pytest.mark.postgres
def test_real_pool_serves_more_threads_than_connections(_ephemeral_pg_db):
    dsn = _ephemeral_pg_db or os.environ.get("PG_TEST_DSN")
    pool = pg_mod.PgPool(dsn, min_conn=1, max_conn=2, checkout_timeout=10)
    try:
        def work():
            with pool.checkout() as conn, conn.cursor() as cur:
                cur.execute("SELECT pg_sleep(0.1)")

        assert _run_threads(6, work) == []
    finally:
        pool.close()
