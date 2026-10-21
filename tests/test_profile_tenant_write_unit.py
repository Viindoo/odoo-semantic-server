# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_profile_tenant_write_unit.py
"""Write-side tenant fail-closed for profiles and repos (#385).

No database: the REAL ``RepoStore`` runs against an in-memory fake of the few
SQL statements it issues (``_FakeDB``), and the REAL FastAPI routes run in an
ASGI client with the store swapped in. What is protected is the behaviour an
admin or the CLI observes:

- creating a profile without an owner is refused (422) - a forgotten field can
  never publish a profile to every key; ``shared=true`` must be explicit;
- a child of a private profile inherits that tenant; a mismatching tenant or
  ``shared`` under a private parent is refused;
- the visibility invariant holds on every write path: create, re-parent,
  tenant change (parent AND children checked), and repos follow the profile;
- making a profile shared needs an explicit confirmation;
- a refused write leaves no partial row behind.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager

import httpx
import psycopg2.errors
import pytest

from src.db.exceptions import (
    ProfileInputError,
    ProfileTenantRequiredError,
    ProfileVisibilityError,
)
from src.db.repo_registry import (
    RepoStore,
    check_child_visibility,
    resolve_new_profile_tenant,
    validate_profile_identity,
)

pytestmark = pytest.mark.http

T_VIINDOO, T_ACME = 67, 2


# ---------------------------------------------------------------------------
# In-memory fake of the RepoStore SQL surface used here
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self):
        self.tenants = {T_VIINDOO, T_ACME}
        self.profiles: dict[int, dict] = {}
        self.repos: dict[int, dict] = {}
        self._next = 100

    def nid(self) -> int:
        self._next += 1
        return self._next

    def profile(self, name, version="20.0", tenant=None, parent=None) -> int:
        pid = self.nid()
        self.profiles[pid] = {"name": name, "odoo_version": version,
                              "tenant_id": tenant, "parent_profile_id": parent}
        return pid

    def repo(self, profile_id, tenant=None) -> int:
        rid = self.nid()
        self.repos[rid] = {"profile_id": profile_id, "tenant_id": tenant}
        return rid

    def snapshot(self):
        return copy.deepcopy((self.profiles, self.repos, self._next))

    def restore(self, snap):
        self.profiles, self.repos, self._next = copy.deepcopy(snap)


class _Cursor:
    def __init__(self, db: _FakeDB):
        self.db = db
        self._rows: list[tuple] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def execute(self, sql, params=()):  # noqa: C901 - one branch per statement
        db, q = self.db, " ".join(sql.split())
        p = db.profiles
        self._rows, self.rowcount = [], 0
        if q.startswith("SELECT odoo_version, tenant_id FROM profiles WHERE id = %s"):
            row = p.get(params[0])
            self._rows = [(row["odoo_version"], row["tenant_id"])] if row else []
        elif q.startswith("INSERT INTO profiles"):
            name, version, desc, parent, tenant = params
            if any(r["name"] == name for r in p.values()):
                raise psycopg2.errors.UniqueViolation()
            if tenant is not None and tenant not in db.tenants:
                raise psycopg2.errors.ForeignKeyViolation()
            pid = db.nid()
            p[pid] = {"name": name, "odoo_version": version, "tenant_id": tenant,
                      "parent_profile_id": parent}
            self._rows = [(pid,)]
        elif q.startswith("SELECT name, tenant_id, parent_profile_id FROM profiles"):
            row = p.get(params[0])
            self._rows = [(row["name"], row["tenant_id"], row["parent_profile_id"])] if row else []
        elif q.startswith("SELECT name, tenant_id FROM profiles WHERE id = %s"):
            row = p[params[0]]
            self._rows = [(row["name"], row["tenant_id"])]
        elif q.startswith("SELECT name, tenant_id FROM profiles WHERE parent_profile_id"):
            self._rows = sorted(
                (r["name"], r["tenant_id"]) for r in p.values()
                if r["parent_profile_id"] == params[0]
            )
        elif q.startswith("UPDATE profiles SET tenant_id"):
            if params[0] is not None and params[0] not in db.tenants:
                raise psycopg2.errors.ForeignKeyViolation()
            p[params[1]]["tenant_id"] = params[0]
            self.rowcount = 1
        elif q.startswith("UPDATE repos SET tenant_id = %s WHERE profile_id"):
            for r in db.repos.values():
                if r["profile_id"] == params[1] and r["tenant_id"] != params[0]:
                    r["tenant_id"] = params[0]
                    self.rowcount += 1
        elif q.startswith("SELECT id FROM profiles WHERE id = ANY"):
            self._rows = [(i,) for i in params[0] if i in p]
        elif q.startswith("SELECT odoo_version, tenant_id, name FROM profiles"):
            row = p.get(params[0])
            self._rows = [(row["odoo_version"], row["tenant_id"], row["name"])] if row else []
        elif q.startswith("WITH RECURSIVE ancestors"):
            start, target = params
            node, seen = start, set()
            while node is not None and node not in seen:
                if node == target:
                    self._rows = [(node,)]
                    break
                seen.add(node)
                node = p[node]["parent_profile_id"]
        elif q.startswith("UPDATE profiles SET parent_profile_id"):
            parent, pid, _ = params
            if p[pid]["parent_profile_id"] != parent:
                p[pid]["parent_profile_id"] = parent
                self.rowcount = 1
        elif q.startswith("SELECT tenant_id, name FROM profiles WHERE id = %s"):
            row = p.get(params[0])
            self._rows = [(row["tenant_id"], row["name"])] if row else []
        elif q.startswith("SELECT profile_id FROM repos WHERE id = %s"):
            repo = db.repos.get(params[0])
            self._rows = [(repo["profile_id"],)] if repo else []
        elif q.startswith("UPDATE repos SET tenant_id = %s WHERE id = %s AND profile_id = %s"):
            repo = db.repos.get(params[1])
            if repo is not None and repo["profile_id"] == params[2]:
                repo["tenant_id"] = params[0]
                self.rowcount = 1
        elif q.startswith("INSERT INTO repos"):
            rid = db.nid()
            db.repos[rid] = {"profile_id": params[0], "tenant_id": params[6]}
            self._rows = [(rid,)]
        else:  # pragma: no cover - a new statement must be modelled explicitly
            raise AssertionError(f"unmodelled SQL: {q}")


class _Conn:
    def __init__(self, db: _FakeDB):
        self.db = db
        self._autocommit = True
        self._snap = None

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        if not value and self._autocommit:
            self._snap = self.db.snapshot()
        self._autocommit = value

    def cursor(self):
        return _Cursor(self.db)

    def commit(self):
        self._snap = self.db.snapshot()

    def rollback(self):
        if self._snap is not None:
            self.db.restore(self._snap)


class _Pool:
    def __init__(self, db: _FakeDB):
        self.db = db

    @contextmanager
    def checkout(self):
        yield _Conn(self.db)

    def fetch_all(self, _conn, sql, params=()):  # list_profiles (versions route)
        assert "FROM profiles" in sql, sql
        return [{"id": i, **r} for i, r in sorted(self.db.profiles.items())]


@pytest.fixture
def db():
    return _FakeDB()


@pytest.fixture
def store(db):
    return RepoStore(_Pool(db))


# ---------------------------------------------------------------------------
# The rule itself (pure)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("child,parent,allowed", [
    (None, None, True),          # shared under shared (odoo_20 -> oca_20)
    (T_ACME, None, True),        # private under shared (odoo_20 -> acme_20)
    (T_ACME, T_ACME, True),      # same tenant chain
    (None, T_VIINDOO, False),    # shared under private: the 322 shape
    (T_ACME, T_VIINDOO, False),  # another tenant extends a private chain
])
def test_child_is_never_more_visible_than_its_parent(child, parent, allowed):
    if allowed:
        check_child_visibility(child, parent)
    else:
        with pytest.raises(ProfileVisibilityError):
            check_child_visibility(child, parent)


@pytest.mark.parametrize("kwargs,expected", [
    ({"has_parent": False, "parent_tenant_id": None, "tenant_id": T_ACME, "shared": False}, T_ACME),
    ({"has_parent": False, "parent_tenant_id": None, "tenant_id": None, "shared": True}, None),
    ({"has_parent": True, "parent_tenant_id": T_VIINDOO, "tenant_id": None, "shared": False},
     T_VIINDOO),
    ({"has_parent": True, "parent_tenant_id": None, "tenant_id": T_ACME, "shared": False}, T_ACME),
])
def test_new_profile_tenant_resolution(kwargs, expected):
    assert resolve_new_profile_tenant(**kwargs) == expected


@pytest.mark.parametrize("kwargs,error", [
    ({"has_parent": False, "parent_tenant_id": None, "tenant_id": None, "shared": False},
     ProfileTenantRequiredError),
    ({"has_parent": True, "parent_tenant_id": None, "tenant_id": None, "shared": False},
     ProfileTenantRequiredError),
    ({"has_parent": True, "parent_tenant_id": T_VIINDOO, "tenant_id": None, "shared": True},
     ProfileVisibilityError),
    ({"has_parent": True, "parent_tenant_id": T_VIINDOO, "tenant_id": T_ACME, "shared": False},
     ProfileVisibilityError),
    ({"has_parent": False, "parent_tenant_id": None, "tenant_id": T_ACME, "shared": True},
     ProfileInputError),
])
def test_new_profile_tenant_refusals(kwargs, error):
    with pytest.raises(error):
        resolve_new_profile_tenant(**kwargs)


@pytest.mark.parametrize("name,version,ok", [
    ("viindoo_internal_20", "20.0", True),
    ("odoo_8", "8.0", True),
    ("bad name!", "17.0", False),
    ("x" * 51, "17.0", False),
    ("ok_name", "latest", False),
    ("ok_name", "17", False),
    # `$` would accept a trailing newline: "foo\n" would become a Neo4j
    # profile[] value and an RLS GUC list element
    ("foo\n", "17.0", False),
    ("ok_name", "17.0\n", False),
])
def test_profile_name_and_version_format_is_shared_with_the_cli(name, version, ok):
    from src.manager.__main__ import _PROFILE_NAME_RE, _VERSION_RE

    if ok:
        validate_profile_identity(name, version)
    else:
        with pytest.raises(ProfileInputError):
            validate_profile_identity(name, version)
    # SSOT: the CLI pre-check is the very same pattern objects.
    assert bool(_PROFILE_NAME_RE.match(name) and _VERSION_RE.match(version)) is ok


# ---------------------------------------------------------------------------
# RepoStore write paths
# ---------------------------------------------------------------------------

def test_add_profile_without_owner_is_refused_and_writes_nothing(store, db):
    with pytest.raises(ProfileTenantRequiredError):
        store.add_profile("standard_viindoo_21", "21.0")
    assert db.profiles == {}


def test_add_profile_under_private_parent_inherits_its_tenant(store, db):
    parent = db.profile("standard_viindoo_20", tenant=T_VIINDOO)
    pid = store.add_profile("viindoo_internal_20", "20.0", parent_id=parent)
    assert db.profiles[pid]["tenant_id"] == T_VIINDOO


def test_add_profile_shared_or_foreign_under_private_parent_is_refused(store, db):
    parent = db.profile("standard_viindoo_20", tenant=T_VIINDOO)
    with pytest.raises(ProfileVisibilityError):
        store.add_profile("viindoo_internal_20", "20.0", parent_id=parent, shared=True)
    with pytest.raises(ProfileVisibilityError):
        store.add_profile("acme_20", "20.0", parent_id=parent, tenant_id=T_ACME)
    assert [r["name"] for r in db.profiles.values()] == ["standard_viindoo_20"]


def test_add_profile_unknown_tenant_is_an_input_error(store):
    with pytest.raises(ProfileInputError, match="not found"):
        store.add_profile("acme_20", "20.0", tenant_id=999)


def test_reparent_under_a_private_parent_requires_the_same_tenant(store, db):
    private = db.profile("standard_viindoo_20", tenant=T_VIINDOO)
    shared_child = db.profile("public_20")
    foreign_child = db.profile("acme_20", tenant=T_ACME)
    own_child = db.profile("viindoo_internal_20", tenant=T_VIINDOO)
    for child in (shared_child, foreign_child):
        with pytest.raises(ProfileVisibilityError):
            store.set_profile_parent(child, private)
        assert db.profiles[child]["parent_profile_id"] is None
    assert store.set_profile_parent(own_child, private) is True


def test_making_a_profile_shared_needs_confirmation(store, db):
    pid = db.profile("standard_viindoo_20", tenant=T_VIINDOO)
    with pytest.raises(ProfileTenantRequiredError):
        store.set_profile_tenant(pid, None)
    assert db.profiles[pid]["tenant_id"] == T_VIINDOO
    assert store.set_profile_tenant(pid, None, confirm_shared=True)["changed"] is True


def test_tenant_change_respects_parent_and_children(store, db):
    root = db.profile("odoo_20")
    private = db.profile("standard_viindoo_20", tenant=T_VIINDOO, parent=root)
    child = db.profile("viindoo_internal_20", tenant=T_VIINDOO, parent=private)
    # child cannot leave its private parent's tenant, nor become shared
    with pytest.raises(ProfileVisibilityError):
        store.set_profile_tenant(child, T_ACME)
    with pytest.raises(ProfileVisibilityError):
        store.set_profile_tenant(child, None, confirm_shared=True)
    # the parent cannot move to another tenant while its child stays behind
    with pytest.raises(ProfileVisibilityError, match="viindoo_internal_20"):
        store.set_profile_tenant(private, T_ACME)
    # the shared root may become private only if no child would be more visible
    shared_kid = db.profile("public_child_20", parent=root)
    with pytest.raises(ProfileVisibilityError, match="public_child_20"):
        store.set_profile_tenant(root, T_VIINDOO)
    assert db.profiles[root]["tenant_id"] is None
    assert db.profiles[shared_kid]["tenant_id"] is None


def test_tenant_change_moves_the_profiles_repos(store, db):
    pid = db.profile("standard_viindoo_20")
    r1 = db.repo(pid)
    r2 = db.repo(pid)
    other = db.repo(db.profile("odoo_20"))
    result = store.set_profile_tenant(pid, T_VIINDOO)
    assert result == {"changed": True, "repos_updated": 2}
    assert db.repos[r1]["tenant_id"] == db.repos[r2]["tenant_id"] == T_VIINDOO
    assert db.repos[other]["tenant_id"] is None


def test_new_repo_inherits_its_profiles_tenant(store, db):
    pid = db.profile("standard_viindoo_20", tenant=T_VIINDOO)
    rid = store.add_repo(pid, "git@github.com:Viindoo/x.git", "20.0", "/tmp/x")
    assert db.repos[rid]["tenant_id"] == T_VIINDOO
    with pytest.raises(ProfileVisibilityError):
        store.add_repo(pid, "git@github.com:Viindoo/y.git", "20.0", "/tmp/y", tenant_id=None)


def test_repo_tenant_can_only_follow_its_profile(store, db):
    pid = db.profile("standard_viindoo_20", tenant=T_VIINDOO)
    rid = db.repo(pid, tenant=None)  # legacy drift
    with pytest.raises(ProfileVisibilityError):
        store.set_repo_tenant(rid, T_ACME)
    assert store.set_repo_tenant(rid, T_VIINDOO) is True
    assert db.repos[rid]["tenant_id"] == T_VIINDOO
    assert store.set_repo_tenant(99999, T_VIINDOO) is False


# ---------------------------------------------------------------------------
# Routes (admin, auth bypass) with the fake store
# ---------------------------------------------------------------------------

@pytest.fixture
async def client(store, monkeypatch):
    from fastapi import FastAPI

    from src.web_ui.routes import repos as repos_routes
    from src.web_ui.routes import tenants as tenant_routes
    from src.web_ui.routes import versions as version_routes

    monkeypatch.setenv("WEBUI_AUTH_DISABLED", "1")
    monkeypatch.setattr("src.db.pg.repo_store", lambda: store)
    audit: list[tuple] = []
    monkeypatch.setattr("src.db.audit.write_audit_log",
                        lambda *a, **k: audit.append((a, k)))
    invalidations: list[object] = []
    monkeypatch.setattr("src.mcp.session.invalidate_allowed_profiles",
                        lambda *a: invalidations.append(a))

    class _Tenants:
        def get_tenant_by_id(self, tid):
            return {"id": tid} if tid in (T_VIINDOO, T_ACME) else None

    monkeypatch.setattr("src.db.pg.auth_store", lambda: _Tenants())
    app = FastAPI()
    app.include_router(repos_routes.router)
    app.include_router(tenant_routes.router)
    app.include_router(version_routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        c.invalidations = invalidations
        yield c


async def test_create_without_tenant_or_shared_is_422(client, db):
    resp = await client.post("/api/repos/profiles", json={"name": "x_20", "version": "20.0"})
    assert resp.status_code == 422, resp.text
    assert "tenant_id is required" in resp.json()["detail"]
    assert db.profiles == {}
    assert client.invalidations == []


async def test_create_under_private_parent_inherits_tenant(client, db):
    parent = db.profile("standard_viindoo_20", tenant=T_VIINDOO)
    resp = await client.post("/api/repos/profiles", json={
        "name": "viindoo_internal_20", "version": "20.0", "parent_id": parent,
    })
    assert resp.status_code == 200, resp.text
    pid = resp.json()["profile_id"]
    assert db.profiles[pid]["tenant_id"] == T_VIINDOO
    assert client.invalidations, "a new profile must drop the scope cache"


async def test_create_shared_needs_the_explicit_flag(client, db):
    resp = await client.post("/api/repos/profiles", json={
        "name": "odoo_21", "version": "21.0", "shared": True,
    })
    assert resp.status_code == 200, resp.text
    assert db.profiles[resp.json()["profile_id"]]["tenant_id"] is None


async def test_create_rejects_a_malformed_name_or_version(client):
    bad = await client.post("/api/repos/profiles", json={
        "name": "bad name", "version": "20.0", "shared": True,
    })
    assert bad.status_code == 422
    bad_v = await client.post("/api/repos/profiles", json={
        "name": "ok_20", "version": "twenty", "shared": True,
    })
    assert bad_v.status_code == 422


async def test_patch_tenant_null_without_confirm_is_422(client, db):
    pid = db.profile("standard_viindoo_20", tenant=T_VIINDOO)
    resp = await client.patch(f"/api/profiles/{pid}/tenant", json={"tenant_id": None})
    assert resp.status_code == 422
    assert db.profiles[pid]["tenant_id"] == T_VIINDOO
    ok = await client.patch(f"/api/profiles/{pid}/tenant",
                            json={"tenant_id": None, "confirm_shared": True})
    assert ok.status_code == 200 and db.profiles[pid]["tenant_id"] is None


async def test_patch_tenant_reports_moved_repos_and_refuses_a_stranded_child(client, db):
    root = db.profile("standard_viindoo_20")
    db.profile("viindoo_internal_20", parent=root)  # shared child
    rid = db.repo(root)
    refused = await client.patch(f"/api/profiles/{root}/tenant", json={"tenant_id": T_VIINDOO})
    assert refused.status_code == 422, refused.text
    assert db.repos[rid]["tenant_id"] is None, "a refused change moves no repo"


async def test_patch_repo_tenant_must_follow_profile(client, db):
    pid = db.profile("standard_viindoo_20", tenant=T_VIINDOO)
    rid = db.repo(pid, tenant=T_VIINDOO)
    resp = await client.patch(f"/api/repos/{rid}/tenant", json={"tenant_id": None})
    assert resp.status_code == 422
    assert db.repos[rid]["tenant_id"] == T_VIINDOO


async def test_add_repo_refuses_when_the_profile_moved_tenant_after_the_check(
    client, db, store, monkeypatch,
):
    """TOCTOU: the write-scope check approved tenant A; an admin then moved the
    profile to tenant B. The repo must not be inserted into B's profile."""
    pid = db.profile("acme_20", tenant=T_ACME)
    real_list = store.list_profiles

    def stale_list():
        rows = real_list()
        db.profiles[pid]["tenant_id"] = T_VIINDOO  # the concurrent admin move commits now
        return [dict(r, tenant_id=T_ACME) if r["id"] == pid else r for r in rows]

    monkeypatch.setattr(store, "list_profiles", stale_list)
    resp = await client.post("/api/repos/repos", json={
        "profile": "acme_20", "url": "https://github.com/acme/addons.git", "branch": "20.0",
    })
    assert resp.status_code == 409, resp.text
    assert "tenant changed" in resp.json()["error"]
    assert db.repos == {}, "nothing was inserted into the other tenant's profile"


async def test_versions_come_from_the_profile_registry(client, db):
    """20.0 is selectable once odoo_20 exists - not gated by the Bootstrap table."""
    for name, version in (("odoo_9", "9.0"), ("odoo_10", "10.0"), ("odoo_20", "20.0")):
        db.profile(name, version=version)
    db.profile("weird", version="not-a-version")
    resp = await client.get("/api/versions")
    assert resp.status_code == 200
    assert resp.json()["versions"] == ["9.0", "10.0", "20.0"]


def test_registered_versions_hide_other_tenants_private_series():
    from src.web_ui.routes.versions import registered_versions

    rows = [
        {"odoo_version": "17.0", "tenant_id": None},
        {"odoo_version": "16.0", "tenant_id": T_ACME},
        {"odoo_version": "18.0", "tenant_id": T_VIINDOO},
    ]
    assert registered_versions(rows, {T_VIINDOO}) == ["17.0", "18.0"]

