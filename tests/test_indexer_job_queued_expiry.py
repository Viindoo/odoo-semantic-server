# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Web UI indexer job can never stay ``queued`` forever (#381 F1) - no DB.

A job whose child died before its first ``running`` report (argparse exit,
import error, OOM at start-up) used to stay ``queued`` with no pid: the
start-up sweep skipped rows without a pid and the reset route accepted only
``running`` rows, so nothing could ever clear it (prod had three).

* the spawn helper records the child's pid on the job row right away;
* the start-up sweep expires a ``queued`` row with no pid once it is older
  than ``INDEXER_JOB_QUEUED_TTL_SECONDS``, and still leaves a fresh one alone;
* the admin reset route accepts a ``queued`` job.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from src import constants
from src.db.job_registry import JobStore


class _FakePool:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def checkout(self):
        yield object()

    def fetch_all(self, _conn, sql, params=()):
        # The sweep reads the unfinished jobs; serve every row whose status the
        # query names.
        return [dict(r) for r in self.rows if f"'{r['status']}'" in sql]


def _row(job_id, status, *, pid=None, age_s=0.0):
    return {
        "id": job_id, "profile_name": "p", "status": status, "pid": pid,
        "started_at": None, "finished_at": None, "error_msg": None,
        "created_at": datetime.now(UTC) - timedelta(seconds=age_s),
    }


def _sweep(rows):
    store = JobStore(_FakePool(rows))
    updates: list[dict] = []
    store.update_job = lambda job_id, **kw: updates.append({"id": job_id, **kw})
    count = store.mark_dead_jobs()
    return count, {u["id"]: u for u in updates}


def test_a_stale_queued_job_without_pid_is_expired_to_error():
    ttl = constants.INDEXER_JOB_QUEUED_TTL_SECONDS
    count, updates = _sweep([_row(3, "queued", age_s=ttl + 60)])

    assert count == 1
    assert updates[3]["status"] == "error"
    assert updates[3]["finished_at"] is not None
    assert "queued" in updates[3]["error_msg"] and "pid" in updates[3]["error_msg"]


def test_a_fresh_queued_job_without_pid_is_left_alone():
    # The parent is still between the INSERT and recording the pid.
    count, updates = _sweep([_row(9, "queued", age_s=1)])
    assert count == 0
    assert updates == {}


def test_a_shorter_ttl_expires_a_younger_job(monkeypatch):
    monkeypatch.setattr(constants, "INDEXER_JOB_QUEUED_TTL_SECONDS", 5.0)
    count, updates = _sweep([_row(4, "queued", age_s=30)])
    assert count == 1 and updates[4]["status"] == "error"


# GUARD: pre-existing behaviour - a dead pid is expired whatever the age.
def test_a_queued_job_whose_pid_is_dead_is_expired():
    count, updates = _sweep([_row(5, "queued", pid=2**22 + 12345, age_s=1)])
    assert count == 1
    assert updates[5]["status"] == "error"


# GUARD: a live process is never touched.
def test_a_live_job_is_left_alone():
    import os

    count, updates = _sweep([_row(6, "running", pid=os.getpid(), age_s=10**6)])
    assert count == 0 and updates == {}


def test_spawn_records_the_child_pid_on_the_job(monkeypatch):
    from src.web_ui.helpers import subprocess_runner

    updates: list[dict] = []

    class _Store:
        def create_job(self, _label):
            return 11

        def update_job(self, job_id, **kw):
            updates.append({"id": job_id, **kw})

    class _Proc:
        pid = 4242

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess_runner, "job_store", lambda: _Store())
    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", lambda *a, **k: _Proc())

    subprocess_runner.spawn_indexer_subcommand(["index-repo", "--profile", "p"], "p")

    assert updates == [{"id": 11, "pid": 4242}]


@pytest.mark.asyncio
async def test_the_admin_can_reset_a_queued_job(monkeypatch):
    import src.db.pg as pg_mod
    from src.web_ui.routes import jobs

    updates: list[dict] = []

    class _Store:
        def get_job(self, job_id):
            return {"id": job_id, "status": "queued", "pid": None}

        def update_job(self, job_id, **kw):
            updates.append({"id": job_id, **kw})

    monkeypatch.setattr(pg_mod, "job_store", lambda: _Store())

    resp = await jobs.reset_stuck_job.__wrapped__(None, 3, _user_id=1)

    assert resp.status_code == 200
    assert updates and updates[0]["id"] == 3 and updates[0]["status"] == "error"
