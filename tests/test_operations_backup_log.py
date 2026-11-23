# SPDX-License-Identifier: AGPL-3.0-or-later
"""A backup started from the Operations page logs where an operator and an
admin can read it (#403, same PrivateTmp defect class as the job logs).

Business rules protected:

* The backup's output goes to ``osm-backup-<job uuid>.log`` under
  ``INDEXER_JOB_LOG_DIR`` (never the temp dir, which the shipped unit makes
  private and empties on restart), readable only by the service user (``0600``).
* The progress stream still shows the backup's output, from that file, and the
  status gives the log path and tail.
* Only admins reach the stream and the status.
* A job id from the URL never names a file: a non-uuid id has no log.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import uuid
from unittest import mock

import httpx
import pytest

from src.web_ui.routes import operations as ops

PASSWORD = "Pass-1234567890!"


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    monkeypatch.setenv("INDEXER_JOB_LOG_DIR", str(directory))
    return directory


@pytest.fixture
def bypass_client(monkeypatch):
    from fastapi import FastAPI

    monkeypatch.setenv("WEBUI_AUTH_DISABLED", "1")
    app = FastAPI()
    app.include_router(ops.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _run_backup(job_id: str, output: str) -> None:
    """Run the real spawn path with a fake CLI that prints two lines."""
    with ops._backup_jobs_lock:
        ops._backup_jobs[job_id] = {"job_id": job_id, "status": "pending", "output": output,
                                    "started_at": None, "finished_at": None,
                                    "exit_code": None, "created_at": None}
    done = threading.Event()

    def fake_popen(argv, stdout, stderr, shell):
        stdout.write(b"Dumping Postgres...\nBackup written\n")
        stdout.flush()
        proc = mock.MagicMock()
        proc.wait.return_value = 0
        return proc

    real_thread = threading.Thread

    def thread(target, daemon):
        def run():
            try:
                target()
            finally:
                done.set()
        return real_thread(target=run, daemon=daemon)

    with mock.patch.object(ops.subprocess, "Popen", side_effect=fake_popen), \
            mock.patch.object(ops.threading, "Thread", side_effect=thread):
        ops._spawn_backup_subprocess(job_id, output, "")
        assert done.wait(10)


@pytest.mark.http
async def test_a_backup_logs_privately_in_the_log_dir_and_streams_from_it(log_dir, tmp_path,
                                                                          bypass_client):
    job_id = str(uuid.uuid4())
    _run_backup(job_id, str(tmp_path / "out.tar.gz"))
    try:
        path = log_dir / f"osm-backup-{job_id}.log"
        assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
        async with bypass_client as c:
            stream = await c.get(f"/api/operations/backup/{job_id}/stream")
            status = (await c.get(f"/api/operations/backup/{job_id}/status")).json()
        events = [json.loads(line[6:]) for line in stream.text.splitlines()
                  if line.startswith("data: ")]
        assert {"line": "Backup written"} in events
        assert events[-1] == {"done": True, "status": "done", "exit_code": 0}
        assert status["log"]["path"] == str(path)
        assert status["log"]["tail"].endswith("Backup written\n")
    finally:
        with ops._backup_jobs_lock:
            ops._backup_jobs.pop(job_id, None)


@pytest.mark.http
async def test_a_job_id_that_is_not_a_uuid_names_no_log_file(log_dir, bypass_client):
    job_id = "../../etc/passwd"
    with ops._backup_jobs_lock:
        ops._backup_jobs[job_id] = {"job_id": job_id, "status": "done", "exit_code": 0}
    try:
        async with bypass_client as c:
            status = (await c.get("/api/operations/backup/x/status")).json()
        assert status == {"error": "job not found"}
        assert ops._backup_log_path(job_id) is None
    finally:
        with ops._backup_jobs_lock:
            ops._backup_jobs.pop(job_id, None)


@pytest.mark.postgres
async def test_only_admins_reach_the_backup_log(clean_pg, monkeypatch, log_dir):
    from src.db.migrate import run_migrations
    from src.web_ui.app import create_app
    from src.web_ui.auth import hash_password

    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-backup-log-403-32-bytes!")
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, email, email_verified, "
            "is_admin, is_active) VALUES ('bk_member', %s, 'bk@test.invalid', TRUE, FALSE, "
            "TRUE)", (hash_password(PASSWORD),),
        )
    job_id = str(uuid.uuid4())
    with ops._backup_jobs_lock:
        ops._backup_jobs[job_id] = {"job_id": job_id, "status": "done", "exit_code": 0}
    app = create_app()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as c:
            login = await c.post("/api/auth/login",
                                 json={"username": "bk_member", "password": PASSWORD})
        assert login.status_code == 200, login.text
        # Cookies on the client constructor (the session cookie is Secure).
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1",
                                     cookies=dict(login.cookies)) as c:
            status = await c.get(f"/api/operations/backup/{job_id}/status")
            stream = await c.get(f"/api/operations/backup/{job_id}/stream")
        assert status.status_code == 403 and stream.status_code == 403
    finally:
        with ops._backup_jobs_lock:
            ops._backup_jobs.pop(job_id, None)
