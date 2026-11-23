# SPDX-License-Identifier: AGPL-3.0-or-later
"""A repository clone the Web UI spawns logs where an operator and an admin can
read it, like an indexer job (#403, same PrivateTmp defect class).

Business rules protected:

* Both clone spawns (``POST /api/repos/repos`` with an SSH URL, and
  ``POST /api/repos/profiles/{id}/clone-all``) write the cloner's output to
  ``osm-clone-<repo id>.log`` under ``INDEXER_JOB_LOG_DIR`` (never the temp
  dir), readable only by the service user (``0600``).
* ``GET /api/repos/repos/{id}/clone-status`` gives the log path and tail to
  admins only; a tenant member who may read the repo gets ``log: null``.
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

pytestmark = pytest.mark.postgres

os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-clone-logs-403-32-bytes!")

PASSWORD = "Pass-1234567890!"


@pytest.fixture(autouse=True)
def _real_auth(monkeypatch):
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)


@pytest.fixture
def world(clean_pg, tmp_path, monkeypatch):
    monkeypatch.setenv("INDEXER_JOB_LOG_DIR", str(tmp_path / "logs"))
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        for name, admin in (("cl_admin", True), ("cl_member", False)):
            cur.execute(
                "INSERT INTO webui_users (username, password_hash, email, email_verified, "
                "is_admin, is_active) VALUES (%s, %s, %s, TRUE, %s, TRUE) RETURNING id",
                (name, hash_password(PASSWORD), f"{name}@test.invalid", admin),
            )
        member = cur.fetchone()[0]
        cur.execute("INSERT INTO tenants (name) VALUES ('cl_t1') RETURNING id")
        tenant = cur.fetchone()[0]
        cur.execute("INSERT INTO tenant_members (user_id, tenant_id, role) "
                    "VALUES (%s, %s, 'member')", (member, tenant))
    return {"tenant": tenant}


def _client(app, cookies=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://127.0.0.1", cookies=cookies)


async def _login(app, username: str) -> dict:
    async with _client(app) as c:
        resp = await c.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return dict(resp.cookies)


def _assert_private_clone_log(popen, repo_id: int) -> None:
    from src.web_ui.helpers.job_logs import clone_log_path, job_log_dir

    log = popen.call_args.kwargs["stdout"]
    path = clone_log_path(repo_id)
    assert log.name == str(path) and path.parent == job_log_dir()
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert popen.call_args.args[0][1:] == ["-m", "src.cloner", "--repo-id", str(repo_id)]


@pytest.mark.asyncio
async def test_an_ssh_repo_clone_logs_privately_in_the_log_dir(world):
    from src.db.pg import auth_store, repo_store

    repo_store().add_profile(name="cl_ssh", odoo_version="17.0", shared=True)
    key_id = auth_store().save_ssh_key("deploy-key", "ssh-ed25519 AAAA", "enc_privkey")
    app = create_app()
    cookies = await _login(app, "cl_admin")
    with mock.patch("subprocess.Popen") as popen:
        async with _client(app, cookies) as c:
            resp = await c.post("/api/repos/repos", json={
                "profile": "cl_ssh", "url": "git@github.com:org/cl.git", "branch": "17.0",
                "ssh_key_id": str(key_id),
            })
    assert resp.status_code == 200, resp.text
    _assert_private_clone_log(popen, resp.json()["repo_id"])


@pytest.mark.asyncio
async def test_clone_all_logs_each_clone_privately_in_the_log_dir(world):
    from src.db.pg import repo_store

    pid = repo_store().add_profile(name="cl_all", odoo_version="17.0", shared=True)
    rid = repo_store().add_repo(profile_id=pid, url="https://example.invalid/cl.git",
                                branch="17.0", local_path="/nonexistent/cl")
    app = create_app()
    cookies = await _login(app, "cl_admin")
    with mock.patch("subprocess.Popen") as popen:
        async with _client(app, cookies) as c:
            resp = await c.post(f"/api/repos/profiles/{pid}/clone-all", json={})
    assert resp.status_code == 200 and resp.json()["spawned"] == 1, resp.text
    _assert_private_clone_log(popen, rid)


@pytest.mark.asyncio
async def test_admins_read_the_clone_log_and_a_member_does_not(world):
    from src.db.pg import repo_store
    from src.web_ui.helpers.job_logs import clone_log_path

    pid = repo_store().add_profile(name="cl_status", odoo_version="17.0",
                                   tenant_id=world["tenant"])
    rid = repo_store().add_repo(profile_id=pid, url="git@github.com:org/st.git",
                                branch="17.0", local_path="/nonexistent/st")
    path = clone_log_path(rid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Cloning into '/srv/osm/clones/st'...\n"
                    "git@github.com: Permission denied (publickey).\n")

    app = create_app()
    admin = await _login(app, "cl_admin")
    member = await _login(app, "cl_member")
    async with _client(app, admin) as c:
        seen = (await c.get(f"/api/repos/repos/{rid}/clone-status")).json()
    async with _client(app, member) as c:
        hidden = await c.get(f"/api/repos/repos/{rid}/clone-status")

    assert seen["log"]["path"] == str(path)
    assert seen["log"]["tail"].endswith("Permission denied (publickey).\n")
    assert hidden.status_code == 200  # the member's own repo: in scope
    assert hidden.json()["log"] is None
    assert "/srv/osm" not in hidden.text
