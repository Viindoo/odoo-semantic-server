# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_w1_tenant_rbac.py
"""Wave 1 — Tenant RBAC web-UI write-side tests (ADR-0038).

Business intent (14 cases):
  T1  Migration m13_005 applies cleanly: tenant_members exists, password_hash nullable,
      profiles_name_no_comma constraint exists.
  T2  OAuth-only user (password_hash=NULL) can be inserted after migration (fold #176).
  T3  Profile name containing ',' is rejected at DB level (GUC-delimiter guard).
  T4  resolve_tenant_scope_web: admin session -> ALL_TENANTS sentinel.
  T5  resolve_tenant_scope_web: non-admin session with membership -> correct set.
  T6  resolve_tenant_scope_web: unauthenticated -> empty set (fail-closed).
  T6b resolve_tenant_scope_web: DB error during lookup -> empty set (fail-closed).
  T7  Admin can create tenant + add member via API.
  T8  Cross-tenant write block: is_in_scope rejects tenant_id not in user scope.
  T9  Admin can assign profile to tenant; invalidate_allowed_profiles called.
  T10 Bug (i) HTTPS: add repo to non-existent profile -> 404 (not {"ok":true}).
  T11 Bug (i) SSH: add SSH repo to non-existent profile -> 404 (not 500).
  T12 W0 gate preserved: non-admin POST to mutating routes -> 403 (including /api/tenants).
  T13 Non-admin GET /api/tenants -> 403 (admin-only endpoint).
  T14 Delete tenant with resources -> 409 (D8 — blocked when resources remain).

All tests use httpx.AsyncClient with ASGI transport (no real server).
PostgreSQL is required (pytestmark postgres) for DB-layer route handlers.
"""
import os

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app
from src.web_ui.auth import hash_password

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Session secret must be set before app creation
# ---------------------------------------------------------------------------
os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-wave1-tenant-rbac-32bytes!!")


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


def _seed_users(pg_conn) -> tuple[int, int]:
    """Insert one admin user and one non-admin user. Return (admin_id, nonadmin_id)."""
    admin_hash = hash_password("AdminPass123!")
    nonadmin_hash = hash_password("UserPass123!")
    cols = "(username, password_hash, email, email_verified, is_admin, is_active)"
    with pg_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('w1_admin', %s, 'w1_admin@test.invalid', TRUE, TRUE, TRUE)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET password_hash=EXCLUDED.password_hash, is_admin=TRUE RETURNING id",
            (admin_hash,),
        )
        admin_id = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('w1_user', %s, 'w1_user@test.invalid', TRUE, FALSE, TRUE)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET password_hash=EXCLUDED.password_hash, is_admin=FALSE RETURNING id",
            (nonadmin_hash,),
        )
        nonadmin_id = cur.fetchone()[0]
    pg_conn.commit()
    return admin_id, nonadmin_id


async def _login_session(app, username: str, password: str) -> dict:
    async with _async_client(app) as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert resp.status_code == 200, f"Login failed for {username}: {resp.text}"
        return dict(resp.cookies)


def _seed_tenant(pg_conn, name: str = "test_tenant_w1") -> int:
    """Insert a test tenant. Return tenant id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (name) VALUES (%s)"
            " ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id",
            (name,),
        )
        tid = cur.fetchone()[0]
    pg_conn.commit()
    return tid


def _seed_profile(pg_conn, name: str = "w1_test_profile", tenant_id: int | None = None) -> int:
    """Insert a test profile. Return profile id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, description, tenant_id)"
            " VALUES (%s, '17.0', 'w1 test', %s)"
            " ON CONFLICT (name) DO UPDATE SET odoo_version='17.0' RETURNING id",
            (name, tenant_id),
        )
        pid = cur.fetchone()[0]
    pg_conn.commit()
    return pid


def _seed_repo(pg_conn, profile_id: int, tenant_id: int | None = None) -> int:
    """Insert a test repo. Return repo id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path, clone_status, tenant_id)"
            " VALUES (%s, 'https://example.com/w1-test.git', '17.0', '/tmp/w1_repo', 'manual', %s)"
            " RETURNING id",
            (profile_id, tenant_id),
        )
        rid = cur.fetchone()[0]
    pg_conn.commit()
    return rid


# ---------------------------------------------------------------------------
# T1: Migration m13_005 applies cleanly
# ---------------------------------------------------------------------------


class TestMigrationM13005:
    """T1: Migration m13_005 applies cleanly and creates expected schema objects."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    def test_tenant_members_table_exists(self, migrated_pg):
        """T1a: tenant_members table is created by m13_005."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables"
                " WHERE table_name = 'tenant_members'",
            )
            row = cur.fetchone()
        assert row is not None, "tenant_members table must exist after m13_005"

    def test_tenant_members_primary_key(self, migrated_pg):
        """T1b: tenant_members has PK on (user_id, tenant_id)."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.table_constraints"
                " WHERE table_name = 'tenant_members' AND constraint_type = 'PRIMARY KEY'",
            )
            count = cur.fetchone()[0]
        assert count == 1, "tenant_members must have a PRIMARY KEY constraint"

    def test_password_hash_nullable(self, migrated_pg):
        """T1c: webui_users.password_hash is nullable after m13_005 (fold #176)."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = 'webui_users' AND column_name = 'password_hash'",
            )
            row = cur.fetchone()
        assert row is not None, "password_hash column must exist"
        assert row[0] == "YES", "password_hash must be nullable after m13_005"

    def test_profiles_name_no_comma_constraint(self, migrated_pg):
        """T1d: profiles_name_no_comma CHECK constraint exists after m13_005."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_constraint"
                " WHERE conname = 'profiles_name_no_comma'"
                "   AND conrelid = 'profiles'::regclass",
            )
            row = cur.fetchone()
        assert row is not None, "profiles_name_no_comma CHECK constraint must exist"


# ---------------------------------------------------------------------------
# T2: OAuth-only user (password_hash=NULL) insert works after migration
# ---------------------------------------------------------------------------


class TestPasswordHashNullable:
    """T2: OAuth-only users (password_hash=NULL) can be inserted after m13_005."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    def test_insert_null_password_hash(self, migrated_pg):
        """T2: Inserting a user with password_hash=NULL succeeds after m13_005."""
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users"
                " (username, password_hash, email, email_verified, is_admin, is_active)"
                " VALUES ('w1_oauth_user', NULL, 'oauth@test.invalid', TRUE, FALSE, TRUE)"
                " ON CONFLICT (username) DO NOTHING",
            )
        migrated_pg.commit()
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT password_hash FROM webui_users WHERE username = 'w1_oauth_user'",
            )
            row = cur.fetchone()
        assert row is not None, "OAuth user must be in DB"
        assert row[0] is None, "password_hash must be NULL for OAuth-only user"


# ---------------------------------------------------------------------------
# T3: Profile name with ',' is rejected (GUC-delimiter guard)
# ---------------------------------------------------------------------------


class TestGucDelimiterGuard:
    """T3: profiles.name containing ',' is rejected by CHECK constraint."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    def test_comma_in_profile_name_rejected(self, migrated_pg):
        """T3: INSERT profile name with ',' raises CheckViolation."""
        with pytest.raises(Exception) as exc_info:
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO profiles (name, odoo_version) VALUES ('a,b', '17.0')",
                )
            migrated_pg.commit()
        migrated_pg.rollback()
        err_str = str(exc_info.value).lower()
        assert "check" in err_str or "constraint" in err_str or "violation" in err_str, (
            f"Expected CheckViolation, got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# T4, T5, T6: resolve_tenant_scope_web helper
# ---------------------------------------------------------------------------


def _make_request(session: dict):
    """Build a minimal Starlette Request carrying the given session dict.

    resolve_tenant_scope_web reads only request.session (via current_user_id /
    is_admin_session), so a bare ASGI scope with a 'session' key is enough to
    drive the real helper through all of its branches — admin, non-admin, and
    fail-closed. Tests must exercise the function itself, not its sub-helpers.
    """
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "session": session,
        }
    )


class TestResolveTenantScopeWeb:
    """T4/T5/T6: resolve_tenant_scope_web returns the correct scope per session type.

    These drive the real helper (not its sub-dependencies), so an inverted admin
    branch or a missing fail-closed `except` would turn the test red.
    """

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    def test_admin_returns_all_tenants(self, migrated_pg):
        """T4: Admin session -> ALL_TENANTS sentinel (scope bypass)."""
        from src.web_ui.auth import ALL_TENANTS, resolve_tenant_scope_web

        admin_id, _ = _seed_users(migrated_pg)
        scope = resolve_tenant_scope_web(_make_request({"user_id": admin_id}))
        assert scope is ALL_TENANTS, (
            f"Admin session must resolve to the ALL_TENANTS sentinel, got {scope!r}"
        )

    def test_non_admin_with_membership_returns_tenant_set(self, migrated_pg):
        """T5: Non-admin with membership in T1 -> exactly {T1}, never T2."""
        from src.web_ui.auth import ALL_TENANTS, resolve_tenant_scope_web

        _, nonadmin_id = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "w1_tenant_1")
        t2_id = _seed_tenant(migrated_pg, "w1_tenant_2")
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO tenant_members (user_id, tenant_id, role)"
                " VALUES (%s, %s, 'member')"
                " ON CONFLICT (user_id, tenant_id) DO NOTHING",
                (nonadmin_id, t1_id),
            )
        migrated_pg.commit()

        scope = resolve_tenant_scope_web(_make_request({"user_id": nonadmin_id}))
        assert scope is not ALL_TENANTS, "Non-admin must NOT get the admin bypass"
        assert scope == {t1_id}, f"Non-admin scope must be exactly {{T1}}, got {scope!r}"
        assert t2_id not in scope, "Non-admin must NOT see tenant T2 (no membership)"

    def test_unauthenticated_returns_empty_set(self, migrated_pg):
        """T6: Unauthenticated session -> empty set (fail-closed deny-all)."""
        from src.web_ui.auth import resolve_tenant_scope_web

        scope = resolve_tenant_scope_web(_make_request({}))
        assert scope == set(), (
            f"Unauthenticated session must fail closed to empty set, got {scope!r}"
        )

    def test_db_error_fails_closed(self, migrated_pg, monkeypatch):
        """T6b: A DB error while resolving a non-admin's tenants fails closed.

        The most security-load-bearing branch: if the membership lookup raises,
        resolve_tenant_scope_web must deny-all (empty set), never leak scope.
        """
        from src.db import pg
        from src.web_ui.auth import resolve_tenant_scope_web

        _, nonadmin_id = _seed_users(migrated_pg)

        def _boom(_uid):
            raise RuntimeError("simulated DB outage during membership lookup")

        # Patch the singleton instance method used by the non-admin branch; the
        # admin check (get_user_field) stays intact so the branch is reached.
        monkeypatch.setattr(pg.auth_store(), "list_tenant_ids_for_user", _boom)
        scope = resolve_tenant_scope_web(_make_request({"user_id": nonadmin_id}))
        assert scope == set(), (
            f"A DB error during scope resolution must fail closed (deny-all), got {scope!r}"
        )

    def test_is_in_scope_all_tenants(self):
        """T4 extension: is_in_scope with ALL_TENANTS sentinel always returns True."""
        from src.web_ui.auth import ALL_TENANTS, is_in_scope
        assert is_in_scope(ALL_TENANTS, 999) is True
        assert is_in_scope(ALL_TENANTS, None) is True

    def test_is_in_scope_member_check(self):
        """T5 extension: is_in_scope checks membership correctly."""
        from src.web_ui.auth import is_in_scope
        scope = {1, 2}
        assert is_in_scope(scope, 1) is True
        assert is_in_scope(scope, 2) is True
        assert is_in_scope(scope, 3) is False
        assert is_in_scope(scope, None) is True   # shared/global visible to all

    def test_is_in_scope_empty_set(self):
        """T6 extension: is_in_scope with empty set (unauthenticated) denies all tenant-specific."""
        from src.web_ui.auth import is_in_scope
        scope: set = set()
        assert is_in_scope(scope, 1) is False
        assert is_in_scope(scope, None) is True    # shared still visible (read-side)


# ---------------------------------------------------------------------------
# T7: Admin can create tenant and add member via API
# ---------------------------------------------------------------------------


class TestAdminTenantCrud:
    """T7: Admin can create tenant + add member via /api/tenants routes."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_admin_creates_tenant_and_adds_member(self, migrated_pg):
        """T7: POST /api/tenants + POST /api/tenants/{id}/members succeed for admin."""
        admin_id, nonadmin_id = _seed_users(migrated_pg)
        app = create_app()
        admin_cookies = await _login_session(app, "w1_admin", "AdminPass123!")

        async with _async_client(app, cookies=admin_cookies) as client:
            # Create tenant
            resp = await client.post(
                "/api/tenants",
                json={"name": "T7_tenant"},
            )
            assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
            data = resp.json()
            assert data.get("ok") is True
            tenant_id = data["tenant_id"]
            assert isinstance(tenant_id, int)

            # Add non-admin as member
            resp = await client.post(
                f"/api/tenants/{tenant_id}/members",
                json={"user_id": nonadmin_id, "role": "member"},
            )
            assert resp.status_code == 200, f"Add member failed: {resp.status_code}: {resp.text}"
            assert resp.json().get("ok") is True

            # Verify membership in DB
            from src.db.pg import auth_store
            assert auth_store().user_is_member_of(nonadmin_id, tenant_id), (
                "Non-admin must be a member of the created tenant"
            )

    @pytest.mark.asyncio
    async def test_admin_list_tenants(self, migrated_pg):
        """T7 extension: GET /api/tenants returns tenant list for admin."""
        _seed_users(migrated_pg)
        _seed_tenant(migrated_pg, "T7_list_tenant")
        app = create_app()
        admin_cookies = await _login_session(app, "w1_admin", "AdminPass123!")

        async with _async_client(app, cookies=admin_cookies) as client:
            resp = await client.get("/api/tenants")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "tenants" in data
        names = [t["name"] for t in data["tenants"]]
        assert "T7_list_tenant" in names


# ---------------------------------------------------------------------------
# T8: Cross-tenant write block via is_in_scope helper
# ---------------------------------------------------------------------------


class TestCrossTenantWriteBlock:
    """T8: is_in_scope correctly blocks cross-tenant write attempts."""

    def test_cross_tenant_denied_at_helper_level(self, migrated_pg):
        """T8: User scope for T1 cannot reach T2 resources."""
        _, nonadmin_id = _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "T8_tenant_1")
        t2_id = _seed_tenant(migrated_pg, "T8_tenant_2")

        # User has scope for T1 only
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO tenant_members (user_id, tenant_id, role)"
                " VALUES (%s, %s, 'member') ON CONFLICT DO NOTHING",
                (nonadmin_id, t1_id),
            )
        migrated_pg.commit()

        from src.db.pg import auth_store
        from src.web_ui.auth import is_in_scope
        scope = set(auth_store().list_tenant_ids_for_user(nonadmin_id))

        # T1 is in scope (allowed)
        assert is_in_scope(scope, t1_id) is True
        # T2 is NOT in scope (403 path)
        assert is_in_scope(scope, t2_id) is False


# ---------------------------------------------------------------------------
# T9: Admin can assign profile to tenant; invalidate_allowed_profiles called
# ---------------------------------------------------------------------------


class TestAssignProfileTenant:
    """T9: PATCH /api/profiles/{id}/tenant assigns tenant and invalidates cache."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_admin_assigns_profile_to_tenant(self, migrated_pg):
        """T9: PATCH /api/profiles/{profile_id}/tenant sets tenant_id in DB.

        invalidate_allowed_profiles is called inside the route handler (same
        pattern as repos.py:88/129/218/291) to keep the read-side RLS cache
        consistent. The route calls it via a local import from src.mcp.session,
        so the DB-level assertion is the primary verification here.
        """
        _seed_users(migrated_pg)
        t1_id = _seed_tenant(migrated_pg, "T9_tenant")
        profile_id = _seed_profile(migrated_pg, "T9_profile")

        app = create_app()
        admin_cookies = await _login_session(app, "w1_admin", "AdminPass123!")

        async with _async_client(app, cookies=admin_cookies) as client:
            resp = await client.patch(
                f"/api/profiles/{profile_id}/tenant",
                json={"tenant_id": t1_id},
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json().get("ok") is True

        # Verify DB update
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT tenant_id FROM profiles WHERE id = %s", (profile_id,))
            row = cur.fetchone()
        assert row is not None and row[0] == t1_id, (
            f"Profile must have tenant_id={t1_id}, got {row}"
        )


# ---------------------------------------------------------------------------
# T10: Bug (i) HTTPS — profile not found -> 404 (not {"ok":true})
# ---------------------------------------------------------------------------


class TestAddRepoProfileNotFoundHTTPS:
    """T10: POST /api/repos/repos with HTTPS URL + non-existent profile returns 404."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_add_https_repo_profile_not_found_is_404(self, migrated_pg):
        """T10: HTTPS add-repo with missing profile returns 404 not {"ok":true}."""
        _seed_users(migrated_pg)
        app = create_app()
        admin_cookies = await _login_session(app, "w1_admin", "AdminPass123!")

        async with _async_client(app, cookies=admin_cookies) as client:
            resp = await client.post(
                "/api/repos/repos",
                json={
                    "profile": "absolutely_nonexistent_profile_t10",
                    "url": "https://example.com/t10-test.git",
                    "branch": "17.0",
                },
            )
        assert resp.status_code == 404, (
            f"Expected 404 for missing profile (HTTPS), got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "not found" in body.get("error", "").lower(), (
            f"Error message must mention 'not found', got: {body}"
        )


# ---------------------------------------------------------------------------
# T11: Bug (i) SSH — profile not found -> 404 (not 500)
# ---------------------------------------------------------------------------


class TestAddRepoProfileNotFoundSSH:
    """T11: POST /api/repos/repos with SSH URL + non-existent profile returns 404."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_add_ssh_repo_profile_not_found_is_404(self, migrated_pg):
        """T11: SSH add-repo with missing profile returns 404 not 500."""
        _seed_users(migrated_pg)

        # Seed an SSH key so we pass the SSH-key validation
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted)"
                " VALUES ('t11_key', 'ssh-ed25519 AAAA...', 'encrypted_stub')"
                " RETURNING id",
            )
            ssh_key_id = cur.fetchone()[0]
        migrated_pg.commit()

        app = create_app()
        admin_cookies = await _login_session(app, "w1_admin", "AdminPass123!")

        async with _async_client(app, cookies=admin_cookies) as client:
            resp = await client.post(
                "/api/repos/repos",
                json={
                    "profile": "absolutely_nonexistent_profile_t11",
                    "url": "git@github.com:viindoo/t11-test.git",
                    "branch": "17.0",
                    "ssh_key_id": str(ssh_key_id),
                },
            )
        assert resp.status_code == 404, (
            f"Expected 404 for missing profile (SSH), got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "not found" in body.get("error", "").lower(), (
            f"Error message must mention 'not found', got: {body}"
        )


# ---------------------------------------------------------------------------
# T12: W0 gate still intact — non-admin -> 403 (including /api/tenants)
# ---------------------------------------------------------------------------


class TestW0GatePreserved:
    """T12: Wave 0 admin gate is still enforced; tenant routes are admin-only."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    _GATED_ROUTES = [
        ("POST", "/api/repos/profiles", {"name": "x", "version": "17.0"}),
        ("POST", "/api/repos/repos",
         {"profile": "x", "url": "https://x.com/x.git", "branch": "17.0"}),
        ("POST", "/api/tenants", {"name": "evil_tenant"}),
        ("PATCH", "/api/tenants/1", {"name": "evil"}),
        ("DELETE", "/api/tenants/1", None),
        ("POST", "/api/tenants/1/members", {"user_id": 1, "role": "member"}),
        ("DELETE", "/api/tenants/1/members/1", None),
        ("PATCH", "/api/profiles/1/tenant", {"tenant_id": 1}),
        ("PATCH", "/api/repos/1/tenant", {"tenant_id": 1}),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,path,body", _GATED_ROUTES)
    async def test_non_admin_mutating_routes_403(self, migrated_pg, method, path, body):
        """T12: Non-admin gets 403 on all mutating tenant/repo routes."""
        _seed_users(migrated_pg)
        app = create_app()
        nonadmin_cookies = await _login_session(app, "w1_user", "UserPass123!")

        async with _async_client(app, cookies=nonadmin_cookies) as client:
            resp = await client.request(method, path, json=body or {})
        assert resp.status_code == 403, (
            f"{method} {path}: expected 403, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# T12b: Unauthenticated (no session) -> 401 on every new W1 route
# ---------------------------------------------------------------------------


class TestUnauthenticatedRejectedW1:
    """T12b: Session-less callers get 401 at AuthRequiredMiddleware on the new routes.

    Complements T12 (authenticated non-admin -> 403): together they document the
    full 401 -> 403 -> admin-OK contract and guard against a new route being
    accidentally added to the middleware exempt list (where 403 tests would still
    pass but the earlier 401 line of defence would be silently gone). Mirrors
    TestUnauthenticatedRejected in test_wave0_admin_gate.py for the W1 surface.
    """

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    # Every route the tenants router adds. Dummy ids are fine: the 401 fires in
    # middleware before the path param or handler is reached.
    _NEW_ROUTES = [
        ("GET", "/api/tenants"),
        ("POST", "/api/tenants"),
        ("PATCH", "/api/tenants/1"),
        ("DELETE", "/api/tenants/1"),
        ("GET", "/api/tenants/1/members"),
        ("POST", "/api/tenants/1/members"),
        ("DELETE", "/api/tenants/1/members/1"),
        ("PATCH", "/api/profiles/1/tenant"),
        ("PATCH", "/api/repos/1/tenant"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,path", _NEW_ROUTES)
    async def test_unauthenticated_request_returns_401(self, migrated_pg, method, path):
        """Every new W1 route rejects a session-less caller with 401."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.request(method, path, json={})
        assert resp.status_code == 401, (
            f"{method} {path} must return 401 without a session, "
            f"got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# T13: Non-admin GET /api/tenants -> 403
# ---------------------------------------------------------------------------


class TestGetTenantsAdminOnly:
    """T13: GET /api/tenants is admin-only."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_non_admin_get_tenants_403(self, migrated_pg):
        """T13: Non-admin GET /api/tenants returns 403."""
        _seed_users(migrated_pg)
        app = create_app()
        nonadmin_cookies = await _login_session(app, "w1_user", "UserPass123!")

        async with _async_client(app, cookies=nonadmin_cookies) as client:
            resp = await client.get("/api/tenants")
        assert resp.status_code == 403, (
            f"Expected 403 for non-admin GET /api/tenants, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_admin_get_tenants_200(self, migrated_pg):
        """T13 complement: Admin GET /api/tenants returns 200."""
        _seed_users(migrated_pg)
        app = create_app()
        admin_cookies = await _login_session(app, "w1_admin", "AdminPass123!")

        async with _async_client(app, cookies=admin_cookies) as client:
            resp = await client.get("/api/tenants")
        assert resp.status_code == 200, (
            f"Expected 200 for admin GET /api/tenants, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# T14: Delete tenant with resources -> 409 (D8)
# ---------------------------------------------------------------------------


class TestDeleteTenantWithResources:
    """T14: DELETE /api/tenants/{id} returns 409 when tenant has repos or profiles."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_delete_tenant_with_repo_blocked_409(self, migrated_pg):
        """T14a: DELETE tenant that has a repo assigned returns 409."""
        _seed_users(migrated_pg)
        t_id = _seed_tenant(migrated_pg, "T14_tenant_repo")
        profile_id = _seed_profile(migrated_pg, "T14_profile_for_repo")
        _seed_repo(migrated_pg, profile_id, tenant_id=t_id)

        app = create_app()
        admin_cookies = await _login_session(app, "w1_admin", "AdminPass123!")

        async with _async_client(app, cookies=admin_cookies) as client:
            resp = await client.delete(f"/api/tenants/{t_id}")
        assert resp.status_code == 409, (
            f"Expected 409 (tenant has repos), got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_delete_tenant_with_profile_blocked_409(self, migrated_pg):
        """T14b: DELETE tenant that has a profile assigned returns 409."""
        _seed_users(migrated_pg)
        t_id = _seed_tenant(migrated_pg, "T14_tenant_profile")
        _seed_profile(migrated_pg, "T14_owned_profile", tenant_id=t_id)

        app = create_app()
        admin_cookies = await _login_session(app, "w1_admin", "AdminPass123!")

        async with _async_client(app, cookies=admin_cookies) as client:
            resp = await client.delete(f"/api/tenants/{t_id}")
        assert resp.status_code == 409, (
            f"Expected 409 (tenant has profiles), got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_delete_empty_tenant_succeeds(self, migrated_pg):
        """T14c: DELETE tenant with no resources returns 200."""
        _seed_users(migrated_pg)
        t_id = _seed_tenant(migrated_pg, "T14_empty_tenant")

        app = create_app()
        admin_cookies = await _login_session(app, "w1_admin", "AdminPass123!")

        async with _async_client(app, cookies=admin_cookies) as client:
            resp = await client.delete(f"/api/tenants/{t_id}")
        assert resp.status_code == 200, (
            f"Expected 200 for deleting empty tenant, got {resp.status_code}: {resp.text}"
        )
        assert resp.json().get("ok") is True
