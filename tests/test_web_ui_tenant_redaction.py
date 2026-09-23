# SPDX-License-Identifier: AGPL-3.0-or-later
"""A tenant member never receives server internals or other tenants' data from
the repo listing, the dashboard or a repo delete (F30 / F32 / F33, ADR-0034, #237).

Business rules protected (``is_admin`` always DB-sourced through a real login):

* ``GET /api/repos/profiles`` and ``GET /api/dashboard/stats`` for a non-admin:
  no raw indexer / clone error text (``error_msg`` becomes a fixed owner-facing
  category, ``clone_error_msg`` null, ``last_job.error_msg`` a category), no
  server checkout path (``local_path`` null), no ``lifecycle_attention`` text
  (it can name other tenants' repos; ``lifecycle_attention_at`` still says
  attention is needed), no profile of another tenant, and dashboard counts that
  cover only what the member can see (own API keys, embeddings of listed
  profiles). An admin sees everything, raw.
* A DB failure never hands a tenant member the raw exception text.
* ``DELETE /api/repos/repos/{id}`` by a tenant member never names another
  tenant's repo (basename or id) that blocks a module, nor exception text.
"""
from __future__ import annotations

import os
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from src.web_ui.app import create_app
from src.web_ui.auth import hash_password
from src.web_ui.routes.jobs import _JOB_ERROR_CATEGORIES, _JOB_ERROR_DEFAULT
from tests import _ledger_seed as ls

pytestmark = pytest.mark.postgres

os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-tenant-redaction-32bytes!!")

V = ls.V
CATEGORIES = {summary for _patterns, summary in _JOB_ERROR_CATEGORIES} | {_JOB_ERROR_DEFAULT}
AUTH_CATEGORY = _JOB_ERROR_CATEGORIES[0][1]

T1_INDEX_ERROR = (
    "Command '['git', '-C', '/srv/osm/clones/t1_profile_99/t1_secret_addons', 'fetch', "
    "'origin']' returned non-zero exit status 128. fatal: could not read from remote "
    "repository (permission denied)"
)
T1_CLONE_ERROR = (
    "ssh: connect to host git.t1-internal.example port 22 using /srv/osm/keys/t1_deploy "
    "(permission denied)"
)
T1_JOB_ERROR = "Traceback: FileNotFoundError: /srv/osm/clones/t1_profile_99/boom"
T1_ATTENTION = "module sale_t2_private is still claimed by t2_hidden_repo (id 4242)"


def _client(app, cookies=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://127.0.0.1", cookies=cookies)


@pytest.fixture(autouse=True)
def _real_auth(monkeypatch):
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)


@pytest.fixture
def pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _user(conn, username: str, *, admin: bool) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, email, email_verified, "
            "is_admin, is_active) VALUES (%s, %s, %s, TRUE, %s, TRUE) RETURNING id",
            (username, hash_password("Pass-1234567890!"), f"{username}@test.invalid", admin),
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
        resp = await c.post("/api/auth/login",
                            json={"username": username, "password": "Pass-1234567890!"})
    assert resp.status_code == 200, resp.text
    return dict(resp.cookies)


@pytest.fixture
def world(pg, tmp_path):
    """t1 member + admin; profiles of t1, shared, and t2; t1's repo has every
    kind of raw error text; API keys and embeddings spread over the tenants."""
    from src.db.pg import auth_store, job_store, repo_store

    _user(pg, "red_admin", admin=True)
    member_id = _user(pg, "red_member", admin=False)
    other_id = _user(pg, "red_other", admin=False)
    t1 = _tenant(pg, "red_t1", member_id)
    t2 = _tenant(pg, "red_t2", other_id)

    p1 = ls.add_profile("t1_profile_99", tenant_id=t1)
    ps = ls.add_profile("shared_profile_99")
    p2 = ls.add_profile("t2_profile_99", tenant_id=t2)
    r1 = ls.add_repo(p1, tmp_path / "t1_profile_99" / "t1_secret_addons", tenant_id=t1)
    ls.add_repo(ps, tmp_path / "shared_profile_99" / "shared_addons")
    ls.add_repo(p2, tmp_path / "t2_profile_99" / "t2_hidden_repo", tenant_id=t2)

    repo_store().update_repo_status(r1, "error", T1_INDEX_ERROR)
    repo_store().set_clone_status(r1, "error", T1_CLONE_ERROR)
    job = job_store().create_job("t1_profile_99")
    job_store().update_job(job, status="error", error_msg=T1_JOB_ERROR)
    ls.presence_store().set_lifecycle_attention(r1, T1_ATTENTION)

    auth_store().create_api_key("member-laptop", user_id=member_id, tenant_id=t1)
    auth_store().create_api_key("other-laptop", user_id=other_id, tenant_id=t2)
    auth_store().create_api_key("cli-admin")

    vec = _vector_extension_available(pg)
    if vec:
        with pg.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (V,))
        ls.seed_embedding("t1_mod", "t1_profile_99")
        ls.seed_embedding("shared_mod", "shared_profile_99")
        for i in range(3):  # one write per module: a re-write replaces its rows
            ls.seed_embedding(f"t2_mod{i}", "t2_profile_99")
    yield {"r1": r1, "vec": vec, "t1": t1}
    if vec:
        with pg.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (V,))


def _t1_repo(body: dict, r1: int) -> dict:
    [repo] = [r for p in body["profiles"] for r in p["repos"] if r["id"] == r1]
    return repo


async def _as_member(path: str) -> httpx.Response:
    app = create_app()
    cookies = await _login(app, "red_member")
    async with _client(app, cookies) as c:
        resp = await c.get(path)
    assert resp.status_code == 200, resp.text
    return resp


BOTH = pytest.mark.parametrize("path", ["/api/repos/profiles", "/api/dashboard/stats"])


@pytest.mark.asyncio
@BOTH
async def test_a_tenant_member_gets_error_categories_not_raw_indexer_or_clone_text(world, path):
    resp = await _as_member(path)
    for marker in ("/srv/osm", "t1-internal", "t1_deploy", "Traceback"):
        assert marker not in resp.text, f"{marker!r} leaked to a tenant member on {path}"
    repo = _t1_repo(resp.json(), world["r1"])
    assert repo["error_msg"] == AUTH_CATEGORY
    assert repo["clone_error_msg"] is None
    if repo.get("last_job") is not None:
        assert repo["last_job"]["error_msg"] in CATEGORIES


@pytest.mark.asyncio
@BOTH
async def test_a_tenant_member_never_receives_server_checkout_paths(world, path):
    body = (await _as_member(path)).json()
    repos = [r for p in body["profiles"] for r in p["repos"]]
    assert repos, "positive control"
    assert all(r["local_path"] is None for r in repos)


@pytest.mark.asyncio
@BOTH
async def test_a_tenant_member_never_sees_another_tenants_profiles(world, path):
    body = (await _as_member(path)).json()
    names = {p["name"] for p in body["profiles"]}
    assert {"t1_profile_99", "shared_profile_99"} <= names
    assert "t2_profile_99" not in names
    assert {p["tenant_id"] for p in body["profiles"]} <= {world["t1"], None}


@pytest.mark.asyncio
@BOTH
async def test_a_tenant_member_never_reads_lifecycle_attention_text(world, path):
    """The attention text can name other tenants' repos (ADR-0034): a member only
    learns THAT attention is needed (the timestamp), never the text."""
    resp = await _as_member(path)
    assert "t2_hidden_repo" not in resp.text, f"attention text leaked on {path}"
    repo = _t1_repo(resp.json(), world["r1"])
    assert repo.get("lifecycle_attention") is None
    assert repo.get("lifecycle_attention_at") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/repos/profiles", "/api/dashboard/stats"])
async def test_an_admin_still_sees_every_profile_and_the_raw_detail(world, path):
    # GUARD: pre-existing behaviour (admins always saw raw detail); keeps the
    # redaction from being "fixed" by hiding data from admins too.
    app = create_app()
    cookies = await _login(app, "red_admin")
    async with _client(app, cookies) as c:
        body = (await c.get(path)).json()

    assert {"t1_profile_99", "shared_profile_99", "t2_profile_99"} <= {
        p["name"] for p in body["profiles"]}
    repo = _t1_repo(body, world["r1"])
    assert repo["error_msg"] == T1_INDEX_ERROR
    assert repo["clone_error_msg"] == T1_CLONE_ERROR
    assert repo["local_path"].endswith("/t1_profile_99/t1_secret_addons")
    if path == "/api/repos/profiles":
        assert repo["last_job"]["error_msg"] == T1_JOB_ERROR
        assert repo["lifecycle_attention"] == T1_ATTENTION


@pytest.mark.asyncio
async def test_dashboard_counts_cover_only_what_a_tenant_member_can_see(world):
    app = create_app()
    member = await _login(app, "red_member")
    admin = await _login(app, "red_admin")
    async with _client(app, member) as c:
        mine = (await c.get("/api/dashboard/stats")).json()
    async with _client(app, admin) as c:
        everything = (await c.get("/api/dashboard/stats")).json()

    assert mine["api_key_count"] == 1
    assert everything["api_key_count"] == 3
    if world["vec"]:
        assert mine["embeddings_total"] == 2  # t1 + shared, not t2's 3 rows
        assert everything["embeddings_total"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/repos/profiles", "/api/dashboard/stats"])
async def test_a_db_failure_never_hands_a_tenant_member_the_exception_text(world, path):
    secret = "could not connect to server at /var/run/pg-secret-socket"
    app = create_app()
    member = await _login(app, "red_member")
    admin = await _login(app, "red_admin")

    from src.db.repo_registry import RepoStore
    with mock.patch.object(RepoStore, "list_profiles", side_effect=RuntimeError(secret)):
        async with _client(app, member) as c:
            as_member = await c.get(path)
        async with _client(app, admin) as c:
            as_admin = await c.get(path)

    assert secret not in as_member.text
    assert as_member.json()["error"]
    assert secret in as_admin.json()["error"]


@pytest.mark.asyncio
@pytest.mark.neo4j
async def test_a_tenant_members_repo_delete_never_names_another_tenants_blocking_repo(
    pg, clean_neo4j, tmp_path,
):
    """t1's repo ships viin_ai; t2 has a cloned-but-never-indexed repo at the same
    version that may ship it too, so the name is undecidable. The member's delete
    response keeps the undecidable name but not t2's repo name or id; the admin
    view of the same situation does name it (positive control)."""
    member_id = _user(pg, "red_member", admin=False)
    _user(pg, "red_admin", admin=True)
    t1 = _tenant(pg, "red_t1", member_id)
    t2 = _tenant(pg, "red_t2")
    app = create_app()

    async def _delete(cookies_user: str, suffix: str) -> tuple[httpx.Response, int]:
        p1 = ls.add_profile(f"t1_{suffix}_99", tenant_id=t1)
        p2 = ls.add_profile(f"t2_{suffix}_99", tenant_id=t2)
        r1 = ls.add_repo(p1, ls.write_repo(tmp_path, f"t1_{suffix}_99", "t1_addons",
                                           modules=("viin_ai",)), tenant_id=t1)
        ls.observe(r1, f"t1_{suffix}_99", ("viin_ai",), head=f"h-{suffix}")
        r2 = ls.add_repo(p2, ls.write_repo(tmp_path, f"t2_{suffix}_99", f"t2_hidden_{suffix}",
                                           modules=("viin_ai",)), tenant_id=t2)
        cookies = await _login(app, cookies_user)
        async with _client(app, cookies) as c:
            resp = await c.delete(f"/api/repos/repos/{r1}")
        assert resp.status_code == 200, resp.text
        with pg.cursor() as cur:  # the next scenario must not see this one's blocker
            cur.execute("DELETE FROM repos WHERE id = %s", (r2,))
        return resp, r2

    as_admin, _ = await _delete("red_admin", "adm")
    assert "t2_hidden_adm" in as_admin.text, "positive control: the blocker is reported"

    as_member, r2_member = await _delete("red_member", "mem")
    undecidable = as_member.json()["lifecycle"]["versions"][V]["undecidable"]
    assert undecidable == {"viin_ai": []}
    assert "t2_hidden_mem" not in as_member.text
    summary = as_member.json()["lifecycle"]
    assert str(r2_member) not in str(summary["versions"][V]["undecidable"])
