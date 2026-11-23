# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hold a profile's indexer lock the way a real index run does, for tests.

An index run of profile P holds ``pg_advisory_lock(_profile_lock_id(P))`` on a
session it opened before reading P's repository list. ``hold_profile_lock``
opens such a session on the test database (``connect`` first, ``lock`` when
the caller says), so a test can place a repository's creation before or after
the run's start.
"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg2


@contextmanager
def profile_lock_session():
    """An autocommit session on the test database; yields ``lock(profile)``."""
    from src.db.pg import get_pool
    from src.indexer.pipeline import _profile_lock_id

    conn = psycopg2.connect(get_pool().dsn)
    conn.autocommit = True

    def lock(profile_name: str) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_profile_lock_id(profile_name),))
            assert cur.fetchone()[0], f"lock of {profile_name} already taken"

    try:
        yield lock
    finally:
        # Release explicitly before closing: a closed session's locks are only
        # freed when its backend exits, which a following peek may not see yet.
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock_all()")
        finally:
            conn.close()


@contextmanager
def hold_profile_lock(profile_name: str):
    """Hold *profile_name*'s indexer lock on a session started now."""
    with profile_lock_session() as lock:
        lock(profile_name)
        yield
