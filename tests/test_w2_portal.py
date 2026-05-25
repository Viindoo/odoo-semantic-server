# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_w2_portal.py
"""Wave 2 — Customer self-service portal tests (ADR-0038 W2).

Business rules protected by this suite:
  R1  Read scope: non-admin member T1 sees profile T1 + shared(null), NOT profile T2.
  R2  tenant_id present in GET /api/repos/profiles response.
  W1  Write allow: non-admin T1 can add repo to profile T1 -> 200, repo inherits tenant_id=T1.
  W2  Write allow: non-admin T1 can trigger index on repo T1 -> 200.
  W3  Write allow: non-admin T1 can patch repo T1 -> 200.
  W4  Write allow: non-admin T1 can delete repo T1 -> 200.
  D1  Write deny cross-tenant: non-admin T1 add/index/patch/delete on profile/repo T2 -> 403.
  D2  Write deny shared: non-admin T1 add repo to profile shared(null) -> 403.
  D3  Write deny shared: non-admin T1 index/patch/delete repo shared(null) -> 403.
  D4  No-membership deny-all: non-admin with no membership -> write 403 everywhere.
  D5  No-membership read: GET profiles shows only shared profiles (null), not tenant-owned.
  A1  Admin bypass: admin can add/index/delete on any tenant + shared -> OK.
  N1  404: repo/profile not found -> 404 (not 403).
  G1  W0/W1 regression: non-admin profile CRUD / tenant CRUD / operations -> 403.
  G2  Unauthenticated -> 401.
  T1  GET /api/account/tenants: non-admin sees own membership; admin sees all.

All tests drive the real FastAPI app via httpx.AsyncClient (ASGI transport).
PostgreSQL is required (pytestmark = pytest.mark.postgres).
Cookies set on client constructor (NOT per-request) per bc47038/httpx 0.28 pattern.
"""
import os

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app
from src.web_ui.auth import hash_password

pytestmark = pytest.mark.postgres

# Session secret must be set before app creation
os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-wave2-portal-32bytes!!!")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _async_client(app, cookies=None):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", cookies=cookies)


def _seed_users(pg_conn):
    """Insert admin, non-admin T1 member, non-admin T2 member, no-membership user.

    Returns (admin_id, user_t1_id, user_t2_id, user_nomember_id).
    """
    admin_hash = hash_password("AdminPass123!")
    user_hash = hash_password("UserPass123!")
    cols = "(username, password_hash, email, email_verified, is_admin, is_active)"
    with pg_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('w2_admin', %s, 'w2_admin@test.invalid', TRUE, TRUE, TRUE)"
            " ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash,"
            " is_admin=TRUE RETURNING id",
            (admin_hash,),
        )
        admin_id = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('w2_user_t1', %s, 'w2_user_t1@test.invalid', TRUE, FALSE, TRUE)"
            " ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash,"
            " is_admin=FALSE RETURNING id",
            (user_hash,),
        )
        user_t1_id = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('w2_user_t2', %s, 'w2_user_t2@test.invalid', TRUE, FALSE, TRUE)"
            " ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash,"
            " is_admin=FALSE RETURNING id",
            (user_hash,),
        )
        user_t2_id = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('w2_user_nomem', %s, 'w2_nomem@test.invalid', TRUE, FALSE, TRUE)"
            " ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash,"
            " is_admin=FALSE RETURNING id",
            (user_hash,),
        )
        nomember_id = cur.fetchone()[0]
    pg_conn.commit()
    return admin_id, user_t1_id, user_t2_id, nomember_id


def _seed_tenant(pg_conn, name: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (name) VALUES (%s)"
            " ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id",
            (name,),
        )
        tid = cur.fetchone()[0]
    pg_conn.commit()
    return tid


def _seed_profile(pg_conn, name: str, tenant_id: int | None = None) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, description, tenant_id)"
            " VALUES (%s, '17.0', 'w2 test', %s)"
            " ON CONFLICT (name) DO UPDATE SET odoo_version='17.0' RETURNING id",
            (name, tenant_id),
        )
        pid = cur.fetchone()[0]
    pg_conn.commit()
    return pid


def _seed_repo(pg_conn, profile_id: int, tenant_id: int | None = None) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path, clone_status, tenant_id)"
            " VALUES (%s, 'https://example.com/w2-test.git', '17.0', '/tmp/w2_repo', 'manual', %s)"
            " RETURNING id",
            (profile_id, tenant_id),
        )
        rid = cur.fetchone()[0]
    pg_conn.commit()
    return rid


def _seed_membership(pg_conn, user_id: int, tenant_id: int, role: str = "member") -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_members (user_id, tenant_id, role)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (user_id, tenant_id) DO UPDATE SET role=EXCLUDED.role",
            (user_id, tenant_id, role),
        )
    pg_conn.commit()


async def _login(app, username: str, password: str) -> dict:
    async with _async_client(app) as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert resp.status_code == 200, f"Login failed for {username}: {resp.text}"
        return dict(resp.cookies)


# ---------------------------------------------------------------------------
# R1 + R2: Read scope — non-admin sees correct profiles + tenant_id present
# ---------------------------------------------------------------------------


class TestReadScope:
    """R1/R2: GET /api/repos/profiles is filtered to tenant scope for non-admin."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_non_admin_t1_sees_t1_and_shared_not_t2(self, migrated_pg):
        """R1: Non-admin T1 member sees profile T1 + shared(null), NOT profile T2."""
        admin_id, user_t1_id, user_t2_id, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_r1_t1")
        t2_id = _seed_tenant(migrated_pg, "w2_r1_t2")
        _seed_profile(migrated_pg, "w2_r1_profile_t1", tenant_id=t1_id)
        _seed_profile(migrated_pg, "w2_r1_profile_t2", tenant_id=t2_id)
        _seed_profile(migrated_pg, "w2_r1_profile_shared", tenant_id=None)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.get("/api/repos/profiles")
        assert resp.status_code == 200
        data = resp.json()
        names = [p["name"] for p in data["profiles"]]
        assert "w2_r1_profile_t1" in names, "T1 member must see T1 profile"
        assert "w2_r1_profile_shared" in names, "T1 member must see shared(null) profile"
        assert "w2_r1_profile_t2" not in names, "T1 member must NOT see T2 profile"

    @pytest.mark.asyncio
    async def test_tenant_id_present_in_profile_response(self, migrated_pg):
        """R2: tenant_id field is included in every profile in the GET profiles response."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_r2_t1")
        _seed_profile(migrated_pg, "w2_r2_profile_t1", tenant_id=t1_id)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.get("/api/repos/profiles")
        assert resp.status_code == 200
        data = resp.json()
        p = next((p for p in data["profiles"] if p["name"] == "w2_r2_profile_t1"), None)
        assert p is not None
        assert "tenant_id" in p, "tenant_id must be present in profile response"
        assert p["tenant_id"] == t1_id


# ---------------------------------------------------------------------------
# W1-W4: Write allow — non-admin T1 member can CRUD repos in T1
# ---------------------------------------------------------------------------


class TestWriteAllow:
    """W1-W4: Non-admin T1 member can add/index/patch/delete repos in T1."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_non_admin_can_add_repo_to_t1_profile(self, migrated_pg):
        """W1: Non-admin T1 member can add repo to T1 profile; new repo has tenant_id=T1."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_w1_t1")
        _seed_profile(migrated_pg, "w2_w1_profile_t1", tenant_id=t1_id)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/repos/repos",
                json={
                    "profile": "w2_w1_profile_t1",
                    "url": "https://github.com/example/w2-w1-repo.git",
                    "branch": "17.0",
                },
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json().get("ok") is True

        # Verify repo has correct tenant_id in DB
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT r.tenant_id FROM repos r JOIN profiles p ON r.profile_id = p.id"
                " WHERE p.name = 'w2_w1_profile_t1'"
                " ORDER BY r.id DESC LIMIT 1"
            )
            row = cur.fetchone()
        assert row is not None, "New repo must exist in DB"
        assert row[0] == t1_id, f"New repo must have tenant_id={t1_id}, got {row[0]}"

    @pytest.mark.asyncio
    async def test_non_admin_can_index_repo_in_t1(self, migrated_pg):
        """W2: Non-admin T1 member can trigger index for repo in T1."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_w2_t1")
        pid = _seed_profile(migrated_pg, "w2_w2_profile_t1", tenant_id=t1_id)
        repo_id = _seed_repo(migrated_pg, pid, tenant_id=t1_id)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                f"/api/repos/repos/{repo_id}/index",
                json={"max_workers": "1"},
            )
        # 200 (job_id returned) or 409 (indexer already running) are acceptable
        assert resp.status_code in (200, 409), (
            f"Expected 200 or 409 from index, got {resp.status_code}: {resp.text}"
        )
        # Must NOT be 403 (access denied) or 401 (unauthenticated)
        assert resp.status_code != 403, "Non-admin T1 member must NOT get 403 on T1 repo index"

    @pytest.mark.asyncio
    async def test_non_admin_can_patch_repo_in_t1(self, migrated_pg):
        """W3: Non-admin T1 member can patch repo in T1."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_w3_t1")
        pid = _seed_profile(migrated_pg, "w2_w3_profile_t1", tenant_id=t1_id)
        repo_id = _seed_repo(migrated_pg, pid, tenant_id=t1_id)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.patch(
                f"/api/repos/repos/{repo_id}",
                json={"branch": "16.0"},
            )
        assert resp.status_code == 200, (
            f"Non-admin T1 member must be able to patch T1 repo, "
            f"got {resp.status_code}: {resp.text}"
        )
        assert resp.json().get("ok") is True

    @pytest.mark.asyncio
    async def test_non_admin_can_delete_repo_in_t1(self, migrated_pg):
        """W4: Non-admin T1 member can delete repo in T1."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_w4_t1")
        pid = _seed_profile(migrated_pg, "w2_w4_profile_t1", tenant_id=t1_id)
        repo_id = _seed_repo(migrated_pg, pid, tenant_id=t1_id)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.delete(f"/api/repos/repos/{repo_id}")
        assert resp.status_code == 200, (
            f"Non-admin T1 member must be able to delete T1 repo, "
            f"got {resp.status_code}: {resp.text}"
        )
        assert resp.json().get("ok") is True


# ---------------------------------------------------------------------------
# D1: Write deny cross-tenant
# ---------------------------------------------------------------------------


class TestWriteDenyCrossTenant:
    """D1: Non-admin T1 member cannot write to T2 profiles/repos -> 403."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_cross_tenant_add_repo_403(self, migrated_pg):
        """D1a: Non-admin T1 adding repo to T2 profile -> 403."""
        _, user_t1_id, user_t2_id, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_d1a_t1")
        t2_id = _seed_tenant(migrated_pg, "w2_d1a_t2")
        _seed_profile(migrated_pg, "w2_d1a_profile_t1", tenant_id=t1_id)
        _seed_profile(migrated_pg, "w2_d1a_profile_t2", tenant_id=t2_id)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/repos/repos",
                json={
                    "profile": "w2_d1a_profile_t2",
                    "url": "https://github.com/example/cross-tenant.git",
                    "branch": "17.0",
                },
            )
        assert resp.status_code == 403, (
            f"Cross-tenant add repo must be 403, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_cross_tenant_index_403(self, migrated_pg):
        """D1b: Non-admin T1 indexing T2 repo -> 403."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_d1b_t1")
        t2_id = _seed_tenant(migrated_pg, "w2_d1b_t2")
        pid_t2 = _seed_profile(migrated_pg, "w2_d1b_profile_t2", tenant_id=t2_id)
        repo_t2_id = _seed_repo(migrated_pg, pid_t2, tenant_id=t2_id)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                f"/api/repos/repos/{repo_t2_id}/index",
                json={"max_workers": "1"},
            )
        assert resp.status_code == 403, (
            f"Cross-tenant index must be 403, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_cross_tenant_patch_403(self, migrated_pg):
        """D1c: Non-admin T1 patching T2 repo -> 403."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_d1c_t1")
        t2_id = _seed_tenant(migrated_pg, "w2_d1c_t2")
        pid_t2 = _seed_profile(migrated_pg, "w2_d1c_profile_t2", tenant_id=t2_id)
        repo_t2_id = _seed_repo(migrated_pg, pid_t2, tenant_id=t2_id)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.patch(
                f"/api/repos/repos/{repo_t2_id}",
                json={"branch": "16.0"},
            )
        assert resp.status_code == 403, (
            f"Cross-tenant patch must be 403, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_cross_tenant_delete_403(self, migrated_pg):
        """D1d: Non-admin T1 deleting T2 repo -> 403."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_d1d_t1")
        t2_id = _seed_tenant(migrated_pg, "w2_d1d_t2")
        pid_t2 = _seed_profile(migrated_pg, "w2_d1d_profile_t2", tenant_id=t2_id)
        repo_t2_id = _seed_repo(migrated_pg, pid_t2, tenant_id=t2_id)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.delete(f"/api/repos/repos/{repo_t2_id}")
        assert resp.status_code == 403, (
            f"Cross-tenant delete must be 403, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# D2-D3: Write deny shared (null tenant_id) — critical invariant
# ---------------------------------------------------------------------------


class TestWriteDenyShared:
    """D2-D3: Non-admin cannot write to shared (tenant_id=NULL) resources.

    This is the most critical invariant: shared = global admin-only for writes.
    A bug here would let any non-admin mutate spec data / base profiles.
    """

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_non_admin_add_repo_to_shared_profile_403(self, migrated_pg):
        """D2: Non-admin adding repo to shared(null) profile -> 403 (critical invariant)."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_d2_t1")
        _seed_profile(migrated_pg, "w2_d2_profile_shared", tenant_id=None)  # shared
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/repos/repos",
                json={
                    "profile": "w2_d2_profile_shared",
                    "url": "https://github.com/example/shared-repo.git",
                    "branch": "17.0",
                },
            )
        assert resp.status_code == 403, (
            f"Non-admin adding repo to shared profile must be 403 (critical invariant), "
            f"got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_non_admin_index_shared_repo_403(self, migrated_pg):
        """D3a: Non-admin indexing shared(null) repo -> 403."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_d3a_t1")
        pid_shared = _seed_profile(migrated_pg, "w2_d3a_shared", tenant_id=None)
        repo_shared_id = _seed_repo(migrated_pg, pid_shared, tenant_id=None)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                f"/api/repos/repos/{repo_shared_id}/index",
                json={"max_workers": "1"},
            )
        assert resp.status_code == 403, (
            f"Non-admin index on shared repo must be 403, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_non_admin_delete_shared_repo_403(self, migrated_pg):
        """D3b: Non-admin deleting shared(null) repo -> 403."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_d3b_t1")
        pid_shared = _seed_profile(migrated_pg, "w2_d3b_shared", tenant_id=None)
        repo_shared_id = _seed_repo(migrated_pg, pid_shared, tenant_id=None)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.delete(f"/api/repos/repos/{repo_shared_id}")
        assert resp.status_code == 403, (
            f"Non-admin delete on shared repo must be 403, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_non_admin_patch_shared_repo_403(self, migrated_pg):
        """D3c: Non-admin patching shared(null) repo -> 403."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_d3c_t1")
        pid_shared = _seed_profile(migrated_pg, "w2_d3c_shared", tenant_id=None)
        repo_shared_id = _seed_repo(migrated_pg, pid_shared, tenant_id=None)
        _seed_membership(migrated_pg, user_t1_id, t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.patch(
                f"/api/repos/repos/{repo_shared_id}",
                json={"branch": "16.0"},
            )
        assert resp.status_code == 403, (
            f"Non-admin patch on shared repo must be 403, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# D4-D5: No-membership deny-all
# ---------------------------------------------------------------------------


class TestNoMembershipDenyAll:
    """D4-D5: Non-admin user with no membership: writes 403, reads only shared."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_no_membership_add_repo_403(self, migrated_pg):
        """D4: Non-admin with no membership trying to add repo -> 403."""
        _, _, _, nomember_id = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_d4_t1")
        _seed_profile(migrated_pg, "w2_d4_profile_t1", tenant_id=t1_id)

        app = create_app()
        cookies = await _login(app, "w2_user_nomem", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/repos/repos",
                json={
                    "profile": "w2_d4_profile_t1",
                    "url": "https://github.com/example/nomem.git",
                    "branch": "17.0",
                },
            )
        assert resp.status_code == 403, (
            f"No-membership user must get 403 on add repo, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_no_membership_read_shows_only_shared(self, migrated_pg):
        """D5: Non-admin with no membership sees only shared profiles in GET profiles."""
        _, _, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_d5_t1")
        _seed_profile(migrated_pg, "w2_d5_profile_t1", tenant_id=t1_id)
        _seed_profile(migrated_pg, "w2_d5_profile_shared", tenant_id=None)

        app = create_app()
        cookies = await _login(app, "w2_user_nomem", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.get("/api/repos/profiles")
        assert resp.status_code == 200
        data = resp.json()
        names = [p["name"] for p in data["profiles"]]
        assert "w2_d5_profile_t1" not in names, "No-membership user must NOT see T1 profile"
        assert "w2_d5_profile_shared" in names, "No-membership user must see shared profile"


# ---------------------------------------------------------------------------
# A1: Admin bypass
# ---------------------------------------------------------------------------


class TestAdminBypass:
    """A1: Admin can add/index/delete on any tenant + shared."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_admin_can_add_repo_to_shared_profile(self, migrated_pg):
        """A1a: Admin can add repo to shared (null) profile."""
        _seed_users(migrated_pg)
        _seed_profile(migrated_pg, "w2_a1_profile_shared", tenant_id=None)

        app = create_app()
        cookies = await _login(app, "w2_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/repos/repos",
                json={
                    "profile": "w2_a1_profile_shared",
                    "url": "https://github.com/example/admin-shared.git",
                    "branch": "17.0",
                },
            )
        assert resp.status_code == 200, (
            f"Admin must be able to add repo to shared profile, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_admin_can_delete_any_tenant_repo(self, migrated_pg):
        """A1b: Admin can delete repo from any tenant."""
        _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_a1b_t1")
        pid = _seed_profile(migrated_pg, "w2_a1b_profile", tenant_id=t1_id)
        repo_id = _seed_repo(migrated_pg, pid, tenant_id=t1_id)

        app = create_app()
        cookies = await _login(app, "w2_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.delete(f"/api/repos/repos/{repo_id}")
        assert resp.status_code == 200, (
            f"Admin must be able to delete any tenant repo, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# N1: 404 for non-existent resources
# ---------------------------------------------------------------------------


class TestNotFound:
    """N1: Non-existent repo/profile -> 404."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_index_nonexistent_repo_404(self, migrated_pg):
        """N1a: Trigger index on non-existent repo -> 404."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login(app, "w2_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/repos/repos/999999/index",
                json={"max_workers": "1"},
            )
        assert resp.status_code == 404, (
            f"Index on missing repo must be 404, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_delete_nonexistent_repo_404(self, migrated_pg):
        """N1b: Delete non-existent repo -> 404."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login(app, "w2_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.delete("/api/repos/repos/999999")
        assert resp.status_code == 404, (
            f"Delete on missing repo must be 404, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_add_repo_nonexistent_profile_404(self, migrated_pg):
        """N1c: Add repo to non-existent profile -> 404."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login(app, "w2_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/repos/repos",
                json={
                    "profile": "absolutely_nonexistent_w2_profile",
                    "url": "https://github.com/example/x.git",
                    "branch": "17.0",
                },
            )
        assert resp.status_code == 404, (
            f"Add repo with missing profile must be 404, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# G1: W0/W1 regression — admin-only gates still intact
# ---------------------------------------------------------------------------


class TestAdminGateRegression:
    """G1: Non-admin must still get 403 on admin-only routes (W0/W1 gates intact)."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    _ADMIN_ONLY_ROUTES = [
        ("POST", "/api/repos/profiles", {"name": "x", "version": "17.0"}),
        ("POST", "/api/tenants", {"name": "evil_tenant_w2"}),
        ("PATCH", "/api/tenants/1", {"name": "evil"}),
        ("DELETE", "/api/tenants/1", None),
        ("POST", "/api/tenants/1/members", {"user_id": 1, "role": "member"}),
        ("DELETE", "/api/tenants/1/members/1", None),
        ("PATCH", "/api/profiles/1/tenant", {"tenant_id": 1}),
        ("PATCH", "/api/repos/1/tenant", {"tenant_id": 1}),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,path,body", _ADMIN_ONLY_ROUTES)
    async def test_non_admin_still_gets_403_on_admin_routes(self, migrated_pg, method, path, body):
        """G1: W0/W1 admin-only routes still return 403 for non-admin."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.request(method, path, json=body or {})
        assert resp.status_code == 403, (
            f"{method} {path}: W0/W1 gate must still return 403 for non-admin, "
            f"got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# G2: Unauthenticated -> 401
# ---------------------------------------------------------------------------


class TestUnauthenticatedW2:
    """G2: Session-less callers get 401 on all W2 routes."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    _W2_ROUTES = [
        ("GET", "/api/repos/profiles"),
        ("POST", "/api/repos/repos"),
        ("POST", "/api/repos/repos/1/index"),
        ("PATCH", "/api/repos/repos/1"),
        ("DELETE", "/api/repos/repos/1"),
        ("GET", "/api/account/tenants"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,path", _W2_ROUTES)
    async def test_unauthenticated_returns_401(self, migrated_pg, method, path):
        """G2: Unauthenticated call to W2 route returns 401."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.request(method, path, json={})
        assert resp.status_code == 401, (
            f"{method} {path} must return 401 without session, "
            f"got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# T1: GET /api/account/tenants
# ---------------------------------------------------------------------------


class TestAccountTenants:
    """T1: GET /api/account/tenants returns correct membership info."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_non_admin_sees_own_tenants(self, migrated_pg):
        """T1a: Non-admin sees only their own tenant memberships."""
        _, user_t1_id, _, _ = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_t1a_t1")
        t2_id = _seed_tenant(migrated_pg, "w2_t1a_t2")
        _seed_membership(migrated_pg, user_t1_id, t1_id, role="member")

        app = create_app()
        cookies = await _login(app, "w2_user_t1", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.get("/api/account/tenants")
        assert resp.status_code == 200
        data = resp.json()
        assert "tenants" in data
        tenant_ids = [t["tenant_id"] for t in data["tenants"]]
        assert t1_id in tenant_ids, "User must see T1 in their memberships"
        assert t2_id not in tenant_ids, "User must NOT see T2 (no membership)"

        # Verify role is returned
        t1_entry = next(t for t in data["tenants"] if t["tenant_id"] == t1_id)
        assert t1_entry["role"] == "member"
        assert "name" in t1_entry

    @pytest.mark.asyncio
    async def test_admin_sees_all_tenants(self, migrated_pg):
        """T1b: Admin sees all tenants with role='admin'."""
        _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w2_t1b_t1")
        t2_id = _seed_tenant(migrated_pg, "w2_t1b_t2")

        app = create_app()
        cookies = await _login(app, "w2_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.get("/api/account/tenants")
        assert resp.status_code == 200
        data = resp.json()
        tenant_ids = [t["tenant_id"] for t in data["tenants"]]
        assert t1_id in tenant_ids
        assert t2_id in tenant_ids
        for entry in data["tenants"]:
            assert entry["role"] == "admin"

    @pytest.mark.asyncio
    async def test_unauthenticated_account_tenants_401(self, migrated_pg):
        """T1c: Unauthenticated GET /api/account/tenants -> 401."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/account/tenants")
        assert resp.status_code == 401
