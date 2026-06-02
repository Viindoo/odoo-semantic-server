# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_jobs_route_tenant_scope.py
"""Regression guard + behavioural tests for IDOR fix #237.

Business rules protected:

  J1  Cross-tenant deny: non-admin T1 reads job owned by profile T2 → 404.
  J2  Owner allow: non-admin T1 reads job owned by profile T1 → 200, fields correct.
  J3  Shared profile (row exists, tenant_id IS NULL) → 200 for any authenticated user.
  J4  Admin bypass: admin reads any job → 200.
  J5  Unauthenticated → 401.
  J6  Not-found id → 404.
  J7  Bulk "all" job → 404 for non-admin, 200 for admin.
  J8  Orphan job (profile row deleted / never existed) → 404 for non-admin, 200 for admin.
  J9  Non-admin response NEVER contains error_msg or pid (redacted).

  C1  clone-status cross-tenant deny → 404.
  C2  clone-status owner allow → 200.
  C4  clone-status admin bypass → 200.

  G1  Static guard: every GET route in jobs.py + two repo GET routes carry at minimum
      AuthRequiredMiddleware coverage (covered by wave0 for 401 gate); additionally
      clone-status and core-symbol-counts must enforce tenant scope.
      Implemented as allowlist-based static scan: any new GET route in the sensitive
      route modules that returns user-scoped data must be listed in SCOPE_REVIEWED_ROUTES
      or the test fails.

All tests use httpx.AsyncClient + ASGI transport; PostgreSQL required (pytestmark postgres).
Cookie auth follows the bc47038 pattern: cookies on AsyncClient constructor.
"""
import os

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app
from src.web_ui.auth import hash_password

pytestmark = pytest.mark.postgres

os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-jobs-scope-237-32bytes!!")


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
    """Insert admin, T1 member, T2 member. Returns (admin_id, t1_id, t2_id)."""
    pw_hash = hash_password("TestPass123!")
    admin_hash = hash_password("AdminPass123!")
    cols = "(username, password_hash, email, email_verified, is_admin, is_active)"
    with pg_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('j237_admin', %s, 'j237_admin@test.invalid', TRUE, TRUE, TRUE)"
            " ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash,"
            " is_admin=TRUE RETURNING id",
            (admin_hash,),
        )
        admin_id = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('j237_t1', %s, 'j237_t1@test.invalid', TRUE, FALSE, TRUE)"
            " ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash,"
            " is_admin=FALSE RETURNING id",
            (pw_hash,),
        )
        t1_id = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('j237_t2', %s, 'j237_t2@test.invalid', TRUE, FALSE, TRUE)"
            " ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash,"
            " is_admin=FALSE RETURNING id",
            (pw_hash,),
        )
        t2_id = cur.fetchone()[0]
    pg_conn.commit()
    return admin_id, t1_id, t2_id


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
            " VALUES (%s, '17.0', 'j237 test', %s)"
            " ON CONFLICT (name) DO UPDATE SET odoo_version='17.0' RETURNING id",
            (name, tenant_id),
        )
        pid = cur.fetchone()[0]
    pg_conn.commit()
    return pid


def _seed_membership(pg_conn, user_id: int, tenant_id: int, role: str = "member") -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_members (user_id, tenant_id, role)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (user_id, tenant_id) DO UPDATE SET role=EXCLUDED.role",
            (user_id, tenant_id, role),
        )
    pg_conn.commit()


def _seed_job(pg_conn, profile_name: str, error_msg: str = "secret-error /private/path") -> int:
    """Insert an indexer_jobs row. Returns job id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO indexer_jobs (profile_name, status, error_msg)"
            " VALUES (%s, 'done', %s) RETURNING id",
            (profile_name, error_msg),
        )
        jid = cur.fetchone()[0]
    pg_conn.commit()
    return jid


def _seed_repo(pg_conn, profile_id: int, tenant_id: int | None = None) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repos"
            " (profile_id, url, branch, local_path, clone_status, tenant_id)"
            " VALUES (%s, 'https://example.com/j237-test.git', '17.0',"
            " '/tmp/j237_repo', 'cloned', %s)"
            " RETURNING id",
            (profile_id, tenant_id),
        )
        rid = cur.fetchone()[0]
    pg_conn.commit()
    return rid


async def _login(app, username: str, password: str) -> dict:
    async with _async_client(app) as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert resp.status_code == 200, f"Login failed for {username}: {resp.text}"
        return dict(resp.cookies)


# ---------------------------------------------------------------------------
# J1–J9: GET /api/jobs/{job_id}/status tenant-scope tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_j1_cross_tenant_deny(migrated_pg, monkeypatch):
    """J1: non-admin T1 reads job belonging to profile T2 → 404, no error_msg leak."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    tid1 = _seed_tenant(migrated_pg, "j237_tenant_one")
    tid2 = _seed_tenant(migrated_pg, "j237_tenant_two")
    _seed_profile(migrated_pg, "j237_prof_t2", tenant_id=tid2)
    _seed_membership(migrated_pg, t1_id, tid1)
    # T1 is NOT a member of tid2
    job_id = _seed_job(migrated_pg, "j237_prof_t2")

    cookies_t1 = await _login(app, "j237_t1", "TestPass123!")
    async with _async_client(app, cookies=cookies_t1) as client:
        resp = await client.get(f"/api/jobs/{job_id}/status")

    assert resp.status_code == 404, f"Expected 404 (cross-tenant), got {resp.status_code}"
    body = resp.json()
    assert "error_msg" not in body or body.get("error_msg") is None, (
        "error_msg must not be exposed in 404 body"
    )
    assert "secret-error" not in resp.text, "Raw error_msg leaked in 404 body"


@pytest.mark.asyncio
async def test_j2_owner_allow(migrated_pg, monkeypatch):
    """J2: non-admin T1 reads job for their own profile T1 → 200, basic fields present."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    tid1 = _seed_tenant(migrated_pg, "j237_tenant_j2")
    _seed_profile(migrated_pg, "j237_prof_t1_j2", tenant_id=tid1)
    _seed_membership(migrated_pg, t1_id, tid1)
    job_id = _seed_job(migrated_pg, "j237_prof_t1_j2", error_msg="owner-secret-err")

    cookies_t1 = await _login(app, "j237_t1", "TestPass123!")
    async with _async_client(app, cookies=cookies_t1) as client:
        resp = await client.get(f"/api/jobs/{job_id}/status")

    assert resp.status_code == 200, f"Expected 200 (owner), got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["id"] == job_id
    assert body["status"] == "done"
    # Non-admin must NOT see error_msg or pid
    assert body.get("error_msg") is None, "Non-admin should not see error_msg"
    assert body.get("pid") is None, "Non-admin should not see pid"


@pytest.mark.asyncio
async def test_j3_shared_profile_visible_all(migrated_pg, monkeypatch):
    """J3: shared profile (row exists, tenant_id IS NULL) job → 200 for any authenticated user."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    tid1 = _seed_tenant(migrated_pg, "j237_tenant_j3")
    _seed_profile(migrated_pg, "j237_prof_shared_j3", tenant_id=None)  # shared
    _seed_membership(migrated_pg, t1_id, tid1)
    job_id = _seed_job(migrated_pg, "j237_prof_shared_j3")

    cookies_t1 = await _login(app, "j237_t1", "TestPass123!")
    async with _async_client(app, cookies=cookies_t1) as client:
        resp = await client.get(f"/api/jobs/{job_id}/status")

    assert resp.status_code == 200, f"Expected 200 (shared), got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_j4_admin_bypass(migrated_pg, monkeypatch):
    """J4: admin reads job for any tenant → 200 with full fields."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    tid2 = _seed_tenant(migrated_pg, "j237_tenant_j4")
    _seed_profile(migrated_pg, "j237_prof_t2_j4", tenant_id=tid2)
    job_id = _seed_job(migrated_pg, "j237_prof_t2_j4", error_msg="admin-visible-err")

    cookies_admin = await _login(app, "j237_admin", "AdminPass123!")
    async with _async_client(app, cookies=cookies_admin) as client:
        resp = await client.get(f"/api/jobs/{job_id}/status")

    assert resp.status_code == 200, f"Expected 200 (admin), got {resp.status_code}: {resp.text}"
    body = resp.json()
    # Admin receives error_msg (may be None if job has none, but key present)
    assert "error_msg" in body, "Admin response should include error_msg field"
    assert body["error_msg"] == "admin-visible-err"


@pytest.mark.asyncio
async def test_j5_unauthenticated(migrated_pg, monkeypatch):
    """J5: unauthenticated request → 401."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()
    _seed_users(migrated_pg)
    tid2 = _seed_tenant(migrated_pg, "j237_tenant_j5")
    _seed_profile(migrated_pg, "j237_prof_j5", tenant_id=tid2)
    job_id = _seed_job(migrated_pg, "j237_prof_j5")

    async with _async_client(app) as client:
        resp = await client.get(f"/api/jobs/{job_id}/status")

    assert resp.status_code == 401, f"Expected 401 (unauth), got {resp.status_code}"


@pytest.mark.asyncio
async def test_j6_not_found(migrated_pg, monkeypatch):
    """J6: non-existent job id → 404 for any caller."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    cookies_admin = await _login(app, "j237_admin", "AdminPass123!")

    async with _async_client(app, cookies=cookies_admin) as client:
        resp = await client.get("/api/jobs/99999999/status")

    assert resp.status_code == 404, f"Expected 404 (not found), got {resp.status_code}"


@pytest.mark.asyncio
async def test_j7_bulk_all_job_admin_only(migrated_pg, monkeypatch):
    """J7: job with profile_name='all' → 404 non-admin, 200 admin."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    tid1 = _seed_tenant(migrated_pg, "j237_tenant_j7")
    _seed_membership(migrated_pg, t1_id, tid1)
    job_id = _seed_job(migrated_pg, "all")

    cookies_t1 = await _login(app, "j237_t1", "TestPass123!")
    async with _async_client(app, cookies=cookies_t1) as client:
        resp_nonadmin = await client.get(f"/api/jobs/{job_id}/status")

    assert resp_nonadmin.status_code == 404, (
        f"Non-admin should get 404 for 'all' job, got {resp_nonadmin.status_code}"
    )

    cookies_admin = await _login(app, "j237_admin", "AdminPass123!")
    async with _async_client(app, cookies=cookies_admin) as client:
        resp_admin = await client.get(f"/api/jobs/{job_id}/status")

    assert resp_admin.status_code == 200, (
        f"Admin should get 200 for 'all' job, got {resp_admin.status_code}"
    )


@pytest.mark.asyncio
async def test_j8_orphan_job_non_admin_denied(migrated_pg, monkeypatch):
    """J8: job whose profile row doesn't exist → non-admin 404, admin 200."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    tid1 = _seed_tenant(migrated_pg, "j237_tenant_j8")
    _seed_membership(migrated_pg, t1_id, tid1)
    # Intentionally do NOT create a profile row for "orphan_profile_j8"
    job_id = _seed_job(migrated_pg, "orphan_profile_j8")

    cookies_t1 = await _login(app, "j237_t1", "TestPass123!")
    async with _async_client(app, cookies=cookies_t1) as client:
        resp_nonadmin = await client.get(f"/api/jobs/{job_id}/status")

    assert resp_nonadmin.status_code == 404, (
        f"Non-admin should get 404 for orphan job, got {resp_nonadmin.status_code}"
    )

    cookies_admin = await _login(app, "j237_admin", "AdminPass123!")
    async with _async_client(app, cookies=cookies_admin) as client:
        resp_admin = await client.get(f"/api/jobs/{job_id}/status")

    assert resp_admin.status_code == 200, (
        f"Admin should get 200 for orphan job, got {resp_admin.status_code}"
    )


@pytest.mark.asyncio
async def test_j9_no_sensitive_fields_for_nonadmin(migrated_pg, monkeypatch):
    """J9: non-admin response body never contains error_msg or pid values."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    tid1 = _seed_tenant(migrated_pg, "j237_tenant_j9")
    _seed_profile(migrated_pg, "j237_prof_t1_j9", tenant_id=tid1)
    _seed_membership(migrated_pg, t1_id, tid1)
    job_id = _seed_job(
        migrated_pg, "j237_prof_t1_j9", error_msg="INTERNAL ERROR: /etc/passwd line 42"
    )

    cookies_t1 = await _login(app, "j237_t1", "TestPass123!")
    async with _async_client(app, cookies=cookies_t1) as client:
        resp = await client.get(f"/api/jobs/{job_id}/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("error_msg") is None, "Non-admin must not see error_msg"
    assert body.get("pid") is None, "Non-admin must not see pid"
    assert "/etc/passwd" not in resp.text, "Sensitive path must not appear in response"
    assert "INTERNAL ERROR" not in resp.text, "Raw error text must not appear in response"


# ---------------------------------------------------------------------------
# C1/C2/C4: GET /api/repos/repos/{repo_id}/clone-status tenant-scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c1_clone_status_cross_tenant_deny(migrated_pg, monkeypatch):
    """C1: non-admin T1 reads clone-status for repo owned by T2 → 404."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    tid1 = _seed_tenant(migrated_pg, "j237_tenant_c1_t1")
    tid2 = _seed_tenant(migrated_pg, "j237_tenant_c1_t2")
    prof_id = _seed_profile(migrated_pg, "j237_prof_c1_t2", tenant_id=tid2)
    _seed_membership(migrated_pg, t1_id, tid1)
    repo_id = _seed_repo(migrated_pg, prof_id, tenant_id=tid2)

    cookies_t1 = await _login(app, "j237_t1", "TestPass123!")
    async with _async_client(app, cookies=cookies_t1) as client:
        resp = await client.get(f"/api/repos/repos/{repo_id}/clone-status")

    assert resp.status_code == 404, (
        f"Expected 404 (cross-tenant clone-status), got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_c2_clone_status_owner_allow(migrated_pg, monkeypatch):
    """C2: non-admin T1 reads clone-status for their own repo → 200, error_msg hidden."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    tid1 = _seed_tenant(migrated_pg, "j237_tenant_c2")
    prof_id = _seed_profile(migrated_pg, "j237_prof_c2_t1", tenant_id=tid1)
    _seed_membership(migrated_pg, t1_id, tid1)
    repo_id = _seed_repo(migrated_pg, prof_id, tenant_id=tid1)

    cookies_t1 = await _login(app, "j237_t1", "TestPass123!")
    async with _async_client(app, cookies=cookies_t1) as client:
        resp = await client.get(f"/api/repos/repos/{repo_id}/clone-status")

    assert resp.status_code == 200, (
        f"Expected 200 (owner clone-status), got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "id" in body
    # Non-admin: error_msg should be None / absent
    assert body.get("error_msg") is None


@pytest.mark.asyncio
async def test_c4_clone_status_admin_bypass(migrated_pg, monkeypatch):
    """C4: admin reads clone-status for any tenant's repo → 200."""
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    app = create_app()

    admin_id, t1_id, t2_id = _seed_users(migrated_pg)
    tid2 = _seed_tenant(migrated_pg, "j237_tenant_c4")
    prof_id = _seed_profile(migrated_pg, "j237_prof_c4_t2", tenant_id=tid2)
    repo_id = _seed_repo(migrated_pg, prof_id, tenant_id=tid2)

    cookies_admin = await _login(app, "j237_admin", "AdminPass123!")
    async with _async_client(app, cookies=cookies_admin) as client:
        resp = await client.get(f"/api/repos/repos/{repo_id}/clone-status")

    assert resp.status_code == 200, (
        f"Expected 200 (admin clone-status), got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# G1: Static allowlist guard — GET routes returning user-scoped data
# ---------------------------------------------------------------------------


class TestSensitiveGetRouteAllowlist:
    """Anti-recurrence guard for IDOR-class gaps on GET routes.

    Strategy: allowlist-based.  Every GET route in the three sensitive modules
    (jobs, repos, operations) that can return user-scoped data must appear in
    SCOPE_REVIEWED_ROUTES below.  A new GET route added to these modules that
    is NOT in the allowlist → test fails → forces a reviewer to consciously
    decide: "does this route need tenant-scope / admin-only protection?"

    Routes in SCOPE_REVIEWED_ROUTES carry one of these disposition tags:
      - admin_only  : Depends(require_admin) or Depends(require_admin_with_fresh_mfa)
      - tenant_scope: explicit is_in_scope + resolve_tenant_scope_web check
      - public      : intentionally open (e.g., health, site-config)
      - read_own    : scoped to the authenticated user's own data (no cross-tenant risk)

    Format: (exact_route_path_after_prefix, disposition)
    exact_route_path = as seen in app.routes after prefix mounting.
    """

    SCOPE_REVIEWED_ROUTES: list[tuple[str, str]] = [
        # --- jobs router (/api/jobs) ---
        ("/api/jobs/{job_id}/status",                      "tenant_scope"),

        # --- repos router (/api/repos) ---
        ("/api/repos/profiles",                            "tenant_scope"),
        ("/api/repos/repos/{repo_id}/clone-status",       "tenant_scope"),
        ("/api/repos/repos/{repo_id}/core-symbol-counts", "tenant_scope"),
        ("/api/repos/versions",                            "public"),
        ("/api/repos/worker-counts",                       "admin_only"),
        # SSH keys are globally shared (no per-tenant rows); list only exposes
        # id+name, no secrets. Requires authentication (added in #237 sweep).
        ("/api/repos/ssh-keys-list",                       "read_own"),

        # --- operations router (/api/operations) ---
        ("/api/operations/diagnose",                       "admin_only"),
        ("/api/operations/backup/{job_id}/stream",         "admin_only"),
        ("/api/operations/backup/{job_id}/status",         "admin_only"),
        # Static PRESETS dict — no user data, no tenant context.
        ("/api/operations/presets",                        "public"),
    ]

    _SENSITIVE_ROUTE_PREFIXES = {"/api/jobs", "/api/repos", "/api/operations"}

    def test_no_unreviewed_get_routes(self, migrated_pg):
        """Fail if a new GET route appears in the sensitive modules without a review entry.

        Walk the mounted app routes.  For every GET APIRoute whose path starts
        with one of the sensitive prefixes, assert it appears in SCOPE_REVIEWED_ROUTES.
        """
        from fastapi.routing import APIRoute

        app = create_app()
        reviewed_paths = {path for path, _ in self.SCOPE_REVIEWED_ROUTES}

        unreviewed: list[str] = []
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            if "GET" not in route.methods:
                continue
            if not any(route.path.startswith(p) for p in self._SENSITIVE_ROUTE_PREFIXES):
                continue
            if route.path not in reviewed_paths:
                unreviewed.append(route.path)

        assert not unreviewed, (
            "New GET route(s) in sensitive modules not reviewed for IDOR safety.\n"
            "Add to SCOPE_REVIEWED_ROUTES with the appropriate disposition tag:\n"
            + "\n".join(f"  {p}" for p in sorted(unreviewed))
        )
