# SPDX-License-Identifier: AGPL-3.0-or-later
"""An admin can tell from the Web UI what an Index click does, what state each
repository's indexing is in, why a job failed and what a running job is doing
(#403). Business rules protected:

* Index on a repository indexes its PROFILE. While a run of the profile is in
  progress, a click on a repository that run covers is informational (200,
  ``already_running``, the run named), never a second job; a repository added
  after the run read its repository list gets 409 naming the run.
* The repository listing attaches the run indexing the profile NOW
  (``index_run``), never the profile's last finished job; a repository added
  after the run started is marked not covered.
* Dead jobs are swept whenever the Web UI reads job state, with the same rule
  as the start-up sweep: a job whose process is gone is never shown running.
* Admins get the job history (every kind, raw error) and each job's log tail
  with its server path; a tenant member gets neither, and never the number of
  an ``all`` job (admin-only).
* A job log is written under ``INDEXER_JOB_LOG_DIR`` (never the temp dir),
  readable only by the service user, and a spawned index-repo runs verbose so
  the log names its steps.
"""
from __future__ import annotations

import os
import stat
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app
from src.web_ui.auth import hash_password
from tests._profile_lock import hold_profile_lock, profile_lock_session

pytestmark = pytest.mark.postgres

os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-index-job-visibility-32b!")

PASSWORD = "Pass-1234567890!"


@pytest.fixture(autouse=True)
def _real_auth(monkeypatch):
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)


@pytest.fixture
def pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _client(app, cookies=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://127.0.0.1", cookies=cookies)


def _user(conn, username: str, *, admin: bool) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, email, email_verified, "
            "is_admin, is_active) VALUES (%s, %s, %s, TRUE, %s, TRUE) RETURNING id",
            (username, hash_password(PASSWORD), f"{username}@test.invalid", admin),
        )
        return cur.fetchone()[0]


def _tenant(conn, name: str, member_id: int | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,))
        tid = cur.fetchone()[0]
        if member_id is not None:
            cur.execute("INSERT INTO tenant_members (user_id, tenant_id, role) "
                        "VALUES (%s, %s, 'member')", (member_id, tid))
    return tid


async def _login(app, username: str) -> dict:
    async with _client(app) as c:
        resp = await c.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return dict(resp.cookies)


def _profile(name: str, tenant_id: int | None = None) -> int:
    from src.db.pg import repo_store

    if tenant_id is None:
        return repo_store().add_profile(name=name, odoo_version="17.0", shared=True)
    return repo_store().add_profile(name=name, odoo_version="17.0", tenant_id=tenant_id)


def _repo(profile_id: int, slug: str) -> int:
    from src.db.pg import repo_store

    return repo_store().add_repo(
        profile_id=profile_id, url=f"https://example.invalid/{slug}.git", branch="17.0",
        local_path=f"/nonexistent/{slug}",
    )


def _age_job(conn, job_id: int, seconds: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE indexer_jobs SET created_at = now() - make_interval(secs => %s) "
            "WHERE id = %s", (seconds, job_id),
        )


def _job_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM indexer_jobs")
        return cur.fetchone()[0]


def _repos_by_id(body: dict) -> dict[int, dict]:
    return {r["id"]: r for p in body["profiles"] for r in p["repos"]}


@pytest.fixture
def world(pg):
    _user(pg, "ix_admin", admin=True)
    member = _user(pg, "ix_member", admin=False)
    t1 = _tenant(pg, "ix_t1", member)
    t2 = _tenant(pg, "ix_t2")
    return {"t1": t1, "t2": t2}


async def _as(username: str):
    app = create_app()
    cookies = await _login(app, username)
    return app, cookies


# --- 1. what an Index click does -------------------------------------------


@pytest.mark.asyncio
async def test_a_second_index_click_in_the_same_profile_names_the_running_job(world, pg):
    pid = _profile("ix_shop")
    first, second = _repo(pid, "a"), _repo(pid, "b")
    app, cookies = await _as("ix_admin")
    with mock.patch("subprocess.Popen") as popen:
        async with _client(app, cookies) as c:
            started = await c.post(f"/api/repos/repos/{first}/index", json={})
            again = await c.post(f"/api/repos/repos/{second}/index", json={})

    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    assert "every repository of profile ix_shop" in started.json()["message"]
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["already_running"] is True and body["job_id"] == job_id
    assert f"job #{job_id}" in body["message"]
    popen.assert_called_once()
    assert _job_count(pg) == 1


@pytest.mark.asyncio
async def test_a_running_web_job_holding_the_lock_covers_the_repos_it_listed(world, pg):
    from datetime import UTC, datetime

    from src.db.pg import job_store

    pid = _profile("ix_lock")
    rid = _repo(pid, "listed")
    job = job_store().create_job("ix_lock")
    job_store().update_job(job, status="running", pid=os.getpid(),
                           started_at=datetime.now(UTC))
    app, cookies = await _as("ix_admin")
    with hold_profile_lock("ix_lock"), mock.patch("subprocess.Popen") as popen:
        async with _client(app, cookies) as c:
            resp = await c.post(f"/api/repos/repos/{rid}/index", json={})
            listing = (await c.get("/api/repos/profiles")).json()

    assert resp.status_code == 200 and resp.json()["job_id"] == job
    run = _repos_by_id(listing)[rid]["index_run"]
    assert run["job_id"] == job and run["covers_repo"] is True
    popen.assert_not_called()


# --- 2. the state of each repository ---------------------------------------


@pytest.mark.asyncio
async def test_a_repo_added_after_the_run_started_is_not_shown_as_covered(world, pg):
    pid = _profile("ix_late")
    before = _repo(pid, "before")
    app, cookies = await _as("ix_admin")
    with profile_lock_session() as lock:  # the run's session starts here
        after = _repo(pid, "after")
        lock("ix_late")
        async with _client(app, cookies) as c:
            repos = _repos_by_id((await c.get("/api/repos/profiles")).json())
            late_click = await c.post(f"/api/repos/repos/{after}/index", json={})

    assert repos[before]["index_run"]["covers_repo"] is True
    assert repos[after]["index_run"]["covers_repo"] is False
    assert repos[before]["index_run"]["job_id"] is None  # started outside the Web UI
    assert late_click.status_code == 409
    assert "Index again when it finishes" in late_click.json()["error"]


@pytest.mark.asyncio
async def test_listing_sweeps_a_dead_queued_job_instead_of_showing_it_running(world, pg):
    from src.constants import INDEXER_JOB_QUEUED_TTL_SECONDS
    from src.db.pg import job_store

    pid = _profile("ix_dead")
    rid = _repo(pid, "r")
    job = job_store().create_job("ix_dead")
    _age_job(pg, job, INDEXER_JOB_QUEUED_TTL_SECONDS + 60)
    app, cookies = await _as("ix_admin")
    async with _client(app, cookies) as c:
        repos = _repos_by_id((await c.get("/api/repos/profiles")).json())

    assert repos[rid]["index_run"] is None
    row = job_store().get_job(job)
    assert row["status"] == "error"
    assert row["error_msg"].endswith("(found when the Web UI listed repositories)")


@pytest.mark.asyncio
async def test_job_status_sweeps_a_dead_job(world, pg):
    from src.constants import INDEXER_JOB_QUEUED_TTL_SECONDS
    from src.db.pg import job_store

    _profile("ix_dead2")
    job = job_store().create_job("ix_dead2")
    _age_job(pg, job, INDEXER_JOB_QUEUED_TTL_SECONDS + 60)
    app, cookies = await _as("ix_admin")
    async with _client(app, cookies) as c:
        body = (await c.get(f"/api/jobs/{job}/status")).json()

    assert body["status"] == "error" and "stayed queued" in body["error_msg"]


# --- tenant cases -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tenant_member_is_told_about_an_all_job_without_its_number(world, pg):
    from src.db.pg import job_store

    pid = _profile("ix_t1_profile", tenant_id=world["t1"])
    rid = _repo(pid, "mine")
    all_job = job_store().create_job("all")
    app, member = await _as("ix_member")
    with mock.patch("subprocess.Popen") as popen:
        async with _client(app, member) as c:
            click = await c.post(f"/api/repos/repos/{rid}/index", json={})
            mine = _repos_by_id((await c.get("/api/repos/profiles")).json())[rid]
    admin_app, admin = await _as("ix_admin")
    async with _client(admin_app, admin) as c:
        theirs = _repos_by_id((await c.get("/api/repos/profiles")).json())[rid]

    assert click.status_code == 200 and click.json()["already_running"] is True
    assert click.json()["job_id"] is None
    assert f"#{all_job}" not in click.text and "all profiles" in click.text
    assert mine["index_run"]["job_id"] is None and mine["index_run"]["covers_repo"] is True
    assert theirs["index_run"]["job_id"] == all_job  # GUARD: admins see the job
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_a_tenant_member_cannot_index_another_tenants_repo(world, pg):
    pid = _profile("ix_t2_profile", tenant_id=world["t2"])
    rid = _repo(pid, "theirs")
    app, member = await _as("ix_member")
    with mock.patch("subprocess.Popen") as popen:
        async with _client(app, member) as c:
            resp = await c.post(f"/api/repos/repos/{rid}/index", json={})

    assert resp.status_code == 403
    popen.assert_not_called()
    assert _job_count(pg) == 0


# --- 3. why a job failed: the job history ------------------------------------


@pytest.mark.asyncio
async def test_admins_get_the_job_history_with_raw_errors_newest_first(world, pg):
    from src.db.pg import job_store

    older = job_store().create_job("core:17.0")
    job_store().update_job(older, status="error", error_msg="Traceback: /srv/osm/boom")
    newer = job_store().create_job("patterns")
    job_store().update_job(newer, status="done")
    app, cookies = await _as("ix_admin")
    async with _client(app, cookies) as c:
        jobs = (await c.get("/api/jobs")).json()["jobs"]
        one = (await c.get("/api/jobs?limit=1")).json()["jobs"]
        too_many = await c.get("/api/jobs?limit=501")

    assert [j["id"] for j in jobs] == [newer, older]
    assert jobs[1]["error_msg"] == "Traceback: /srv/osm/boom"
    assert jobs[1]["profile_name"] == "core:17.0"
    assert [j["id"] for j in one] == [newer]
    assert too_many.status_code == 422


@pytest.mark.asyncio
async def test_a_tenant_member_gets_no_job_history(world, pg):
    from src.db.pg import job_store

    job_store().create_job("ix_any")
    app, member = await _as("ix_member")
    async with _client(app, member) as c:
        resp = await c.get("/api/jobs")
    assert resp.status_code == 403
    assert "jobs" not in resp.text


# --- 5. what a running job is doing: its log ---------------------------------


@pytest.mark.asyncio
async def test_admins_read_the_job_log_tail_and_a_member_does_not(world, pg):
    from src.web_ui.helpers.job_logs import job_log_path

    pid = _profile("ix_log", tenant_id=world["t1"])
    rid = _repo(pid, "logged")
    app, admin = await _as("ix_admin")
    with mock.patch("subprocess.Popen") as popen:
        async with _client(app, admin) as c:
            job = (await c.post(f"/api/repos/repos/{rid}/index", json={})).json()["job_id"]
    log_file = popen.call_args.kwargs["stdout"]
    assert log_file.name == str(job_log_path(job))
    with open(job_log_path(job), "a", encoding="utf-8") as fh:
        fh.write("INFO Repo https://example.invalid/logged.git (id=1): indexing started\n")

    async with _client(app, admin) as c:
        seen = (await c.get(f"/api/jobs/{job}/status")).json()
    member_app, member = await _as("ix_member")
    async with _client(member_app, member) as c:
        hidden = await c.get(f"/api/jobs/{job}/status")

    assert seen["log"]["path"] == str(job_log_path(job))
    assert seen["log"]["tail"].endswith("indexing started\n")
    assert seen["log"]["truncated"] is False
    assert hidden.status_code == 200  # the member's own profile: in scope
    assert hidden.json()["log"] is None
    assert "indexing started" not in hidden.text


def test_a_spawned_job_logs_privately_in_the_job_log_dir_and_runs_verbose(pg, tmp_path,
                                                                           monkeypatch):
    from src.web_ui.helpers.job_logs import job_log_path
    from src.web_ui.helpers.subprocess_runner import spawn_indexer_subcommand

    monkeypatch.setenv("INDEXER_JOB_LOG_DIR", str(tmp_path / "logs"))
    with mock.patch("subprocess.Popen") as popen:
        repo_job = spawn_indexer_subcommand(["index-repo", "--profile", "p"], job_label="p")
        repo_argv = popen.call_args.args[0]
        core_job = spawn_indexer_subcommand(
            ["index-core", "--source", "/s", "--version", "17.0"], job_label="core:17.0",
        )
        core_argv = popen.call_args.args[0]

    path = job_log_path(repo_job)
    assert path.parent == tmp_path / "logs" and path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert repo_argv[-3:] == ["--verbose", "--job-id", str(repo_job)]
    assert "--verbose" not in core_argv  # index-core does not declare it
    assert core_argv[-2:] == ["--job-id", str(core_job)]
