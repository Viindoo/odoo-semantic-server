# SPDX-License-Identifier: AGPL-3.0-or-later
"""A SIGTERM-ed index run leaves its Web UI job in error (#381 F6) - no DB.

The run's main connection may be inside a ``_write_scope`` transaction
(autocommit off) when SIGTERM lands; an update written there is rolled back
when the process exits, so the job stayed ``running`` with a dead pid. The
handler must write the job's error on a connection of its own that commits.
"""
from __future__ import annotations

import os
import signal

import pytest

import src.indexer.__main__ as main_mod

_DURABLE: list[dict] = []


class _FakeConn:
    """Autocommit writes are durable; others wait for commit, and are lost
    on rollback or close (what PG does to an open transaction at exit)."""

    def __init__(self):
        self.autocommit = True
        self.pending: list[dict] = []
        self.closed = False

    def write(self, row):
        if self.autocommit:
            _DURABLE.append(row)
        else:
            self.pending.append(row)

    def commit(self):
        _DURABLE.extend(self.pending)
        self.pending.clear()

    def rollback(self):
        self.pending.clear()

    def close(self):
        self.pending.clear()
        self.closed = True


class _NoCliHandler(Exception):
    pass


def _no_cli_handler(signum, frame):
    raise _NoCliHandler("the CLI installed no SIGTERM handler")


@pytest.fixture
def run(monkeypatch):
    _DURABLE.clear()
    opened: list[_FakeConn] = []

    def fake_open():
        conn = _FakeConn()
        opened.append(conn)
        return conn

    monkeypatch.setattr(main_mod, "open_production_pg", fake_open)
    monkeypatch.setattr(
        main_mod.job_registry, "update_job",
        lambda conn, job_id, **kw: conn.write({"job_id": job_id, **kw}),
    )
    previous = signal.getsignal(signal.SIGTERM)
    # Stand-in until the CLI installs its own handler: a run that installs
    # none fails the test instead of killing pytest.
    signal.signal(signal.SIGTERM, _no_cli_handler)
    yield opened
    signal.signal(signal.SIGTERM, previous)


def _sigterm_inside_a_write_transaction(opened):
    main_pg = opened[0]
    main_pg.autocommit = False  # what _write_scope does
    os.kill(os.getpid(), signal.SIGTERM)


def test_sigterm_during_index_repo_commits_the_job_error(run, monkeypatch):
    opened = run

    def fake_index_profile(pg, **_kw):
        _sigterm_inside_a_write_transaction(opened)
        return {}

    monkeypatch.setattr(main_mod, "index_profile", fake_index_profile)

    with pytest.raises(SystemExit):
        main_mod.main(["index-repo", "--profile", "p", "--no-embed", "--job-id", "9"])

    statuses = [(r["job_id"], r.get("status")) for r in _DURABLE]
    assert (9, "error") in statuses, statuses
    err = next(r for r in _DURABLE if r.get("status") == "error")
    assert "SIGTERM" in err["error_msg"]
    assert all(c.closed for c in opened), "the handler's connection must be closed"


def test_sigterm_during_index_core_commits_the_job_error(run, monkeypatch):
    opened = run

    def fake_run_index_core(**_kw):
        _sigterm_inside_a_write_transaction(opened)

    monkeypatch.setattr(main_mod, "_run_index_core", fake_run_index_core)

    with pytest.raises(SystemExit):
        main_mod.main([
            "index-core", "--source", "/s", "--version", "17.0", "--job-id", "4",
        ])

    assert (4, "error") in [(r["job_id"], r.get("status")) for r in _DURABLE]
