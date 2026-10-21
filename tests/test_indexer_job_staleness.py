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


# --- the status route reports liveness by the same rule ----------------------

def _status(monkeypatch, job, proc):
    import asyncio

    import src.db.pg as pg_mod
    from src.web_ui.routes import jobs

    class _Store:
        def get_job(self, job_id):
            return job_registry._serialize_datetimes(dict(job))

    monkeypatch.setattr(pg_mod, "job_store", lambda: _Store())
    monkeypatch.setattr(jobs, "resolve_read_scope", lambda _req: (True, None))
    monkeypatch.setattr(job_registry, "_PROC", proc, raising=False)
    resp = asyncio.run(jobs.job_status(None, job["id"]))
    return json.loads(resp.body)


def test_status_reports_a_reused_pid_as_not_alive(monkeypatch):
    proc = _Proc(boot=(NOW - timedelta(days=2)).timestamp(),
                 starts={ME: (NOW - timedelta(minutes=10)).timestamp()})
    body = _status(monkeypatch, _row(9, "running", pid=ME, age_s=3700, started_s=3600), proc)
    assert body["is_alive"] is False


# GUARD: the job's own live process reads alive.
def test_status_reports_the_jobs_own_process_as_alive(monkeypatch):
    proc = _Proc(boot=(NOW - timedelta(days=2)).timestamp(),
                 starts={ME: (NOW - timedelta(seconds=3601)).timestamp()})
    body = _status(monkeypatch, _row(10, "running", pid=ME, age_s=3700, started_s=3600), proc)
    assert body["is_alive"] is True


# --- a wall-clock step does not expire a live job ----------------------------

def test_a_small_forward_clock_step_does_not_expire_a_live_job(monkeypatch):
    """btime and the pid's start time are derived from the current wall clock,
    started_at from the clock when the job reported: a forward step of the
    clock after the job started (NTP step, 30s) shifts both by the step. A
    live job must survive such a step."""
    step = 30
    proc = _Proc(boot=(NOW - timedelta(days=2) + timedelta(seconds=step)).timestamp(),
                 starts={ME: (NOW - timedelta(seconds=3601 - step)).timestamp()})
    count, updates = _sweep([_row(11, "running", pid=ME, age_s=3700, started_s=3600)], proc,
                            monkeypatch)
    assert count == 0 and updates == {}


# --- /proc/<pid>/stat parsing ---------------------------------------------

def _stat(comm: str, starttime: int) -> str:
    # fields 3.. after "(comm)": state then 18 numbers, field 22 = starttime.
    rest = ["S"] + [str(i) for i in range(4, 22)] + [str(starttime), "999", "0"]
    return f"4242 ({comm}) " + " ".join(rest)


def test_parse_starttime_reads_field_22():
    assert job_registry._parse_starttime(_stat("python", 123456)) == 123456


def test_parse_starttime_splits_after_the_last_paren():
    # A process may name itself anything, spaces and parens included.
    assert job_registry._parse_starttime(_stat("x) (y z) 1 2", 777)) == 777


def test_parse_starttime_of_garbage_is_none():
    assert job_registry._parse_starttime("") is None
    assert job_registry._parse_starttime("4242 (python) S 1 2") is None


# --- exact process identity (#383 item 4) -----------------------------------
#
# A row whose child wrote proc_start_ticks + boot_id with its 'running' report
# is judged by equality on them: immune to wall-clock steps, and a pid reused
# a few seconds after the job started is still caught. Legacy rows (either
# column NULL) keep the 120 s heuristic above.

BOOT = "bbc6c547-1620-4bd1-861e-7570428ced48"
TICKS = 87_285_701


class _IdProc(_Proc):
    """Fake /proc that also answers the boot id and per-pid start ticks."""

    def __init__(self, boot=None, starts=None, boot_id=BOOT, ticks=None):
        super().__init__(boot, starts)
        self._boot_id = boot_id
        self.ticks = ticks or {}

    def boot_id(self):
        return self._boot_id

    def start_ticks(self, pid):
        return self.ticks.get(pid)


def _id_row(job_id, *, ticks=TICKS, boot_id=BOOT):
    return {**_row(job_id, "running", pid=ME, age_s=3700, started_s=3600),
            "proc_start_ticks": ticks, "boot_id": boot_id}


def test_a_live_job_survives_a_large_forward_clock_step_when_its_identity_matches(monkeypatch):
    # The wall clock jumped 10 min forward after the job started: the legacy
    # heuristic sees the pid "start" 10 min after started_at and expires it.
    step = 600
    proc = _IdProc(boot=(NOW - timedelta(days=2) + timedelta(seconds=step)).timestamp(),
                   starts={ME: (NOW - timedelta(seconds=3601 - step)).timestamp()},
                   ticks={ME: TICKS})
    count, updates = _sweep([_id_row(20)], proc, monkeypatch)
    assert count == 0 and updates == {}
    # The same row without the identity (legacy) is expired by the heuristic:
    legacy = _row(21, "running", pid=ME, age_s=3700, started_s=3600)
    count, _ = _sweep([legacy], proc, monkeypatch)
    assert count == 1


def test_a_pid_reused_seconds_after_the_job_started_is_expired(monkeypatch):
    # The new process started 5 s after started_at: inside the 120 s slack, so
    # the heuristic misses it; the ticks do not.
    proc = _IdProc(boot=(NOW - timedelta(days=2)).timestamp(),
                   starts={ME: (NOW - timedelta(seconds=3595)).timestamp()},
                   ticks={ME: TICKS + 500})
    count, updates = _sweep([_id_row(22)], proc, monkeypatch)
    assert count == 1
    assert "start ticks differ" in updates[22]["error_msg"]


def test_a_reboot_is_detected_by_the_boot_id(monkeypatch):
    proc = _IdProc(boot=(NOW - timedelta(days=2)).timestamp(), boot_id="another-boot",
                   ticks={ME: TICKS})
    count, updates = _sweep([_id_row(23)], proc, monkeypatch)
    assert count == 1 and "rebooted" in updates[23]["error_msg"]


# GUARD: unreadable current identity (hidepid, not Linux) is not a mismatch.
def test_unreadable_current_identity_gives_the_benefit_of_the_doubt(monkeypatch):
    count, updates = _sweep([_id_row(24)], _IdProc(boot_id=None), monkeypatch)
    assert count == 0 and updates == {}


# GUARD: only one identity column set -> legacy heuristic, as specified.
def test_a_row_with_ticks_but_no_boot_id_keeps_the_heuristic(monkeypatch):
    proc = _IdProc(boot=(NOW - timedelta(days=2)).timestamp(),
                   starts={ME: (NOW - timedelta(minutes=10)).timestamp()},
                   ticks={ME: TICKS})
    count, _ = _sweep([_id_row(25, boot_id=None)], proc, monkeypatch)
    assert count == 1  # heuristic: the pid's process started after the job


def test_the_real_proc_reader_returns_our_own_identity():
    try:
        with open("/proc/self/stat", encoding="utf-8") as fh:
            expected = job_registry._parse_starttime(fh.read())
    except OSError:
        pytest.skip("no /proc on this platform")
    ident = job_registry.own_process_identity()
    assert ident["proc_start_ticks"] == expected
    assert ident == job_registry.own_process_identity()  # stable
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
        assert ident["boot_id"] == fh.read().strip()


def test_identity_left_out_when_proc_is_unreadable(monkeypatch):
    monkeypatch.setattr(job_registry, "_PROC", _IdProc(boot_id=None), raising=False)
    assert job_registry.own_process_identity() == {}


@pytest.mark.parametrize("argv", [
    ["index-repo", "--profile", "p1", "--no-embed", "--job-id", "42"],
    ["index-core", "--source", "/nonexistent", "--version", "17.0", "--job-id", "42"],
])
def test_the_indexer_child_reports_its_identity_with_running(monkeypatch, argv):
    from unittest.mock import MagicMock

    import src.indexer.__main__ as main_mod

    fake = _IdProc(ticks={ME: TICKS})
    monkeypatch.setattr(job_registry, "_PROC", fake, raising=False)
    calls = []
    monkeypatch.setattr(main_mod, "open_production_pg", lambda: MagicMock())
    monkeypatch.setattr(main_mod, "index_profile", lambda pg, **kw: {"modules": 0})
    monkeypatch.setattr(main_mod, "_run_index_core", lambda **kw: None)
    monkeypatch.setattr(main_mod.job_registry, "update_job",
                        lambda conn, job_id, **kw: calls.append(kw))
    monkeypatch.setattr(main_mod, "_install_sigterm_handler", lambda job_id: None)
    assert main_mod.main(argv) == 0
    running = calls[0]
    assert running["status"] == "running" and running["pid"] == ME
    assert running["proc_start_ticks"] == TICKS and running["boot_id"] == BOOT


class _RaisingCursor:
    """Fake cursor: the identity columns do not exist (pre-0004 schema)."""

    def __init__(self, log):
        self.log = log
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        import psycopg2.errors

        self.log.append((sql, params))
        if params and TICKS in params:
            raise psycopg2.errors.UndefinedColumn("column proc_start_ticks does not exist")


@pytest.mark.parametrize("autocommit", [True, False])
def test_update_job_without_the_columns_still_writes_running(autocommit):
    """Code deployed before migration 0004: 'running' must still be recorded."""
    log: list = []

    class _Conn:
        def __init__(self):
            self.autocommit = autocommit

        def cursor(self):
            return _RaisingCursor(log)

    job_registry.update_job(_Conn(), 7, status="running", pid=ME,
                            proc_start_ticks=TICKS, boot_id=BOOT)
    _final_sql, final_params = log[-1]
    assert final_params == ["running", ME, 7]
    savepoints = [s for s, _ in log if isinstance(s, str)]
    if autocommit:
        assert savepoints == []
    else:
        assert savepoints == ["SAVEPOINT job_identity", "ROLLBACK TO SAVEPOINT job_identity"]


def test_non_admins_never_see_a_jobs_process_identity():
    from src.web_ui.routes.repos import _redact_repo_rows

    def rows():
        return [{"id": 1, "last_job": {
            "id": 5, "status": "running", "error_msg": None,
            "pid": 4242, "proc_start_ticks": TICKS, "boot_id": BOOT,
        }}]

    job = _redact_repo_rows(rows(), is_admin=False)[0]["last_job"]
    assert job["pid"] is None and job["proc_start_ticks"] is None and job["boot_id"] is None
    assert job["id"] == 5 and job["status"] == "running"
    admin = _redact_repo_rows(rows(), is_admin=True)[0]["last_job"]
    assert admin["pid"] == 4242 and admin["boot_id"] == BOOT
