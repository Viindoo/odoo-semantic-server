# SPDX-License-Identifier: AGPL-3.0-or-later
"""One staleness rule for unfinished Web UI indexer jobs (#381) - no DB.

The start-up sweep (``JobStore.mark_dead_jobs``) and the admin reset route
(``POST /api/jobs/{id}/reset``) decide with the same rule whether a
``queued`` / ``running`` job's process is gone:

* ``queued``, never started (``started_at`` NULL), older than
  ``INDEXER_JOB_QUEUED_TTL_SECONDS``: stale whatever its pid;
* a pid that no longer exists: stale;
* a pid that exists but is not the job's process - it started after the job
  did (pid reused), or the machine booted after the job started - is stale
  too, even when owned by another user (``PermissionError``). Before this a
  ``running`` row whose pid was reused after a reboot stayed ``running``
  forever and the reset route refused it as "still alive";
* when the process start / boot time cannot be read (not Linux), a live pid
  is left alone (the previous behaviour).
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from src import constants
from src.db import job_registry
from src.db.job_registry import JobStore

NOW = datetime.now(UTC)


class _Proc:
    """Fake /proc reader: epoch seconds, or None when unreadable."""

    def __init__(self, boot=None, starts=None):
        self.boot = boot
        self.starts = starts or {}

    def boot_time(self):
        return self.boot

    def start_time(self, pid):
        return self.starts.get(pid)


def _row(job_id, status, *, pid=None, age_s=0.0, started_s=None):
    return {
        "id": job_id, "profile_name": "p", "status": status, "pid": pid,
        "started_at": None if started_s is None else NOW - timedelta(seconds=started_s),
        "finished_at": None, "error_msg": None,
        "created_at": NOW - timedelta(seconds=age_s),
    }


class _Pool:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def checkout(self):
        yield object()

    def fetch_all(self, _conn, sql, params=()):
        return [dict(r) for r in self.rows if f"'{r['status']}'" in sql]


def _sweep(rows, proc, monkeypatch):
    monkeypatch.setattr(job_registry, "_PROC", proc, raising=False)
    store = JobStore(_Pool(rows))
    updates: list[dict] = []
    store.update_job = lambda job_id, **kw: updates.append({"id": job_id, **kw})
    return store.mark_dead_jobs(), {u["id"]: u for u in updates}


ME = os.getpid()


def test_a_running_job_whose_pid_now_belongs_to_a_newer_process_is_expired(monkeypatch):
    # started 1h ago; the live process with that pid started 10 min ago.
    proc = _Proc(boot=(NOW - timedelta(days=2)).timestamp(),
                 starts={ME: (NOW - timedelta(minutes=10)).timestamp()})
    count, updates = _sweep([_row(1, "running", pid=ME, age_s=3700, started_s=3600)], proc,
                            monkeypatch)
    assert count == 1
    assert updates[1]["status"] == "error"
    assert str(ME) in updates[1]["error_msg"]


def test_a_running_job_started_before_the_last_boot_is_expired(monkeypatch):
    proc = _Proc(boot=(NOW - timedelta(minutes=5)).timestamp())  # start time unreadable
    count, updates = _sweep([_row(2, "running", pid=ME, age_s=3700, started_s=3600)], proc,
                            monkeypatch)
    assert count == 1 and "boot" in updates[2]["error_msg"]


def test_a_reused_pid_owned_by_another_user_is_expired(monkeypatch):
    def kill(_pid, _sig):
        raise PermissionError

    monkeypatch.setattr(job_registry.os, "kill", kill)
    proc = _Proc(boot=(NOW - timedelta(days=2)).timestamp(),
                 starts={1: (NOW - timedelta(minutes=1)).timestamp()})
    count, updates = _sweep([_row(3, "running", pid=1, age_s=3700, started_s=3600)], proc,
                            monkeypatch)
    assert count == 1 and updates[3]["status"] == "error"


# GUARD: the job's own live process is never expired.
def test_the_jobs_own_live_process_is_left_alone(monkeypatch):
    proc = _Proc(boot=(NOW - timedelta(days=2)).timestamp(),
                 starts={ME: (NOW - timedelta(seconds=3601)).timestamp()})
    count, updates = _sweep([_row(4, "running", pid=ME, age_s=3700, started_s=3600)], proc,
                            monkeypatch)
    assert count == 0 and updates == {}


# GUARD: without /proc (not Linux) a live pid is left alone, as before.
def test_without_proc_information_a_live_pid_is_left_alone(monkeypatch):
    count, updates = _sweep([_row(5, "running", pid=ME, age_s=3700, started_s=3600)], _Proc(),
                            monkeypatch)
    assert count == 0 and updates == {}


def test_the_real_proc_reader_degrades_to_none_for_a_missing_pid():
    reader = job_registry.ProcInfo()
    assert reader.start_time(2**22 + 12345) is None
    # On Linux both are readable for our own process and ordered sanely.
    boot, mine = reader.boot_time(), reader.start_time(ME)
    if boot is not None and mine is not None:
        assert boot <= mine <= datetime.now(UTC).timestamp() + 5


# --- the reset route shares the rule --------------------------------------

def _reset(monkeypatch, job, proc):
    import asyncio

    import src.db.pg as pg_mod
    from src.web_ui.routes import jobs

    updates: list[dict] = []

    class _Store:
        def get_job(self, job_id):
            return job_registry._serialize_datetimes(dict(job))

        def update_job(self, job_id, **kw):
            updates.append({"id": job_id, **kw})

    monkeypatch.setattr(pg_mod, "job_store", lambda: _Store())
    monkeypatch.setattr(job_registry, "_PROC", proc, raising=False)
    resp = asyncio.run(jobs.reset_stuck_job.__wrapped__(None, job["id"], _user_id=1))
    return resp.status_code, json.loads(resp.body), updates


def test_reset_accepts_a_stale_queued_job_whose_pid_is_alive(monkeypatch):
    ttl = constants.INDEXER_JOB_QUEUED_TTL_SECONDS
    status, body, updates = _reset(
        monkeypatch, _row(6, "queued", pid=ME, age_s=ttl + 60), _Proc(),
    )
    assert status == 200, body
    assert updates and updates[0]["status"] == "error"


def test_reset_accepts_a_running_job_whose_pid_was_reused(monkeypatch):
    proc = _Proc(boot=(NOW - timedelta(days=2)).timestamp(),
                 starts={ME: (NOW - timedelta(minutes=10)).timestamp()})
    status, body, updates = _reset(
        monkeypatch, _row(7, "running", pid=ME, age_s=3700, started_s=3600), proc,
    )
    assert status == 200, body
    assert updates[0]["status"] == "error"


# GUARD: the reset route still refuses a job whose own process is alive.
def test_reset_refuses_a_job_whose_own_process_is_alive(monkeypatch):
    proc = _Proc(boot=(NOW - timedelta(days=2)).timestamp(),
                 starts={ME: (NOW - timedelta(seconds=3601)).timestamp()})
    status, body, updates = _reset(
        monkeypatch, _row(8, "running", pid=ME, age_s=3700, started_s=3600), proc,
    )
    assert status == 409, body
    assert updates == []


@pytest.fixture(autouse=True)
def _no_real_ttl_override(monkeypatch):
    monkeypatch.setattr(constants, "INDEXER_JOB_QUEUED_TTL_SECONDS", 600.0)
