# SPDX-License-Identifier: AGPL-3.0-or-later
"""``indexer_is_running`` tells whether an index run of a profile is in
progress without ever getting in a starting run's way (#403).

Business rules protected:

* True while any session holds the profile's lock (the lock ``_indexer_lock``
  takes), its own session included; False when nobody does.
* The peek takes no lock: an index run that tries its lock at ANY moment of
  the peek gets it. A take-and-release peek held the lock for the length of the
  peek, and ``_indexer_lock`` does not wait: such a run failed with "Indexer
  already running" although nothing was indexing. The Web UI peeks before
  reset-embed, index-all and profile delete.
* The Web UI's "which run indexes this profile" view (``helpers.index_runs``)
  reads the same lock state.
"""
from __future__ import annotations

import psycopg2
import pytest

from src.indexer.pipeline import _profile_lock_id, indexer_is_running
from tests._profile_lock import hold_profile_lock

pytestmark = pytest.mark.postgres

PROFILE = "peek_profile"


def _session():
    from src.db.pg import get_pool

    conn = psycopg2.connect(get_pool().dsn)
    conn.autocommit = True
    return conn


class _StarterProbe:
    """Wraps the peeking connection: after every statement the peek runs, an
    index run starting in another session tries the profile lock (and lets go
    at once). Records whether each attempt got it."""

    def __init__(self, conn, starter, lock_id: int):
        self._conn, self._starter, self._lock_id = conn, starter, lock_id
        self.attempts: list[bool] = []

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def cursor(self, *args, **kwargs):
        probe = self
        real = self._conn.cursor(*args, **kwargs)

        class _Cursor:
            def __enter__(self):
                real.__enter__()
                return self

            def __exit__(self, *exc):
                return real.__exit__(*exc)

            def execute(self, *a, **kw):
                real.execute(*a, **kw)
                probe._start_run()

            def __getattr__(self, name):
                return getattr(real, name)

        return _Cursor()

    def _start_run(self) -> None:
        with self._starter.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (self._lock_id,))
            got = cur.fetchone()[0]
            if got:
                cur.execute("SELECT pg_advisory_unlock(%s)", (self._lock_id,))
        self.attempts.append(got)


def test_a_run_starting_during_the_peek_always_gets_its_lock(pg_conn):
    peeker, starter = _session(), _session()
    try:
        probe = _StarterProbe(peeker, starter, _profile_lock_id(PROFILE))
        assert indexer_is_running(probe, PROFILE) is False
        assert probe.attempts, "positive control: the starter tried during the peek"
        assert all(probe.attempts), (
            "an index run starting during the peek found its lock busy: "
            f"{probe.attempts}")
    finally:
        peeker.close()
        starter.close()


def test_a_held_lock_is_running_and_a_free_one_is_not(pg_conn):
    conn = _session()
    try:
        assert indexer_is_running(conn, PROFILE) is False
        with hold_profile_lock(PROFILE):
            assert indexer_is_running(conn, PROFILE) is True
            assert indexer_is_running(conn, "another_profile") is False
        assert indexer_is_running(conn, PROFILE) is False
    finally:
        conn.close()


def test_a_lock_held_by_the_peeking_session_itself_is_running(pg_conn):
    conn = _session()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_profile_lock_id(PROFILE),))
        assert indexer_is_running(conn, PROFILE) is True
    finally:
        with conn.cursor() as cur:  # release before close: backend exit is async
            cur.execute("SELECT pg_advisory_unlock_all()")
        conn.close()


def test_a_lock_of_another_database_is_not_this_profiles_run(pg_conn):
    """Advisory locks are per database and pg_locks lists the whole cluster:
    a run of the same profile name on another database of the cluster (a
    staging copy, another test database) is not a run of this deployment."""
    import os

    admin_dsn = os.environ.get("PG_ADMIN_DSN")
    if not admin_dsn:
        pytest.skip("PG_ADMIN_DSN not set - no second database to hold the lock on")
    other = psycopg2.connect(admin_dsn)
    other.autocommit = True
    conn = _session()
    try:
        with other.cursor() as cur:
            cur.execute("SELECT current_database() <> %s", (conn.info.dbname,))
            assert cur.fetchone()[0], "the admin DSN must name another database"
            cur.execute("SELECT pg_advisory_lock(%s)", (_profile_lock_id(PROFILE),))
        assert indexer_is_running(conn, PROFILE) is False
    finally:
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock_all()")
        other.close()
        conn.close()


def test_the_index_run_view_reads_the_same_lock_state(pg_conn):
    from src.web_ui.helpers.index_runs import profile_runs

    conn = _session()
    try:
        assert profile_runs(conn, [PROFILE], []) == {}
        with hold_profile_lock(PROFILE):
            run = profile_runs(conn, [PROFILE], [])[PROFILE]
            assert run.external and indexer_is_running(conn, PROFILE)
            assert run.repos_listed_by is not None  # the holder session's start
    finally:
        conn.close()
