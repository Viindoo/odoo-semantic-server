# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_w3_admin_cli.py
"""Wave 3 tests: health/diagnose + admin user create + audit-log viewer + #175 audit coverage.

Business rules tested (ETHOS#11 — protect behaviour, not code):
  A. GET /api/operations/diagnose — admin-only, returns structured checks.
  B. POST /api/admin/users — admin creates user; duplicate -> 409; non-admin -> 403; unauth -> 401.
  C. GET /api/admin/audit-log — admin reads audit entries; filter works; non-admin -> 403.
  D. #175 audit gap: index-all and jobs.reset each write an audit row.
  D2. Regression: every mutating admin-gated route has an @audit_action decorator.

All tests drive the real FastAPI app via httpx AsyncClient (ASGI transport).
Cookie auth follows the bc47038 pattern: cookies on the AsyncClient constructor
(not per-request) to ensure correct session propagation.

PostgreSQL required — pytestmark = pytest.mark.postgres.
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

os.environ.setdefault("WEBUI_SESSION_SECRET", "test-w3-admin-cli-32byte-secret!!")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    """Run migrations on a clean PG for each test."""
    run_migrations(clean_pg)
    return clean_pg


def _async_client(app, cookies=None):
    """AsyncClient with ASGI transport.

    Pass ``cookies`` (dict) to attach a session cookie on the constructor so
    it is sent on every request — pattern bc47038.
    """
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", cookies=cookies)


def _seed_users(pg_conn) -> tuple[int, int]:
    """Insert one admin and one non-admin. Return (admin_id, nonadmin_id)."""
    admin_hash = hash_password("AdminW3Pass123!")
    user_hash = hash_password("UserW3Pass123!")
    cols = "(username, password_hash, email, email_verified, is_admin, is_active)"
    with pg_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('w3_admin', %s, 'w3_admin@test.invalid', TRUE, TRUE, TRUE)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET password_hash=EXCLUDED.password_hash, is_admin=TRUE RETURNING id",
            (admin_hash,),
        )
        admin_id = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('w3_user', %s, 'w3_user@test.invalid', TRUE, FALSE, TRUE)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET password_hash=EXCLUDED.password_hash, is_admin=FALSE RETURNING id",
            (user_hash,),
        )
        nonadmin_id = cur.fetchone()[0]
    pg_conn.commit()
    return admin_id, nonadmin_id


async def _login_session(app, username: str, password: str) -> dict:
    """Log in and return the cookie dict for the session."""
    async with _async_client(app) as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert resp.status_code == 200, f"Login failed for {username}: {resp.text}"
        return dict(resp.cookies)


def _seed_running_job(pg_conn) -> int:
    """Insert an indexer_jobs row in 'running' state with a dead PID. Return job id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO indexer_jobs (profile_name, status, pid)"
            " VALUES ('w3_test_profile', 'running', 99999999) RETURNING id",
        )
        jid = cur.fetchone()[0]
    pg_conn.commit()
    return jid


def _seed_audit_rows(pg_conn, *, n: int = 3, action: str = "user.login") -> list[int]:
    """Insert n audit rows and return their ids."""
    ids = []
    with pg_conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                "INSERT INTO admin_audit_log (actor, action, target, success)"
                " VALUES ('user:1', %s, %s, TRUE) RETURNING id",
                (action, f"target_{i}"),
            )
            ids.append(cur.fetchone()[0])
    pg_conn.commit()
    return ids


# ---------------------------------------------------------------------------
# A. diagnose endpoint
# ---------------------------------------------------------------------------


class TestDiagnose:
    """GET /api/operations/diagnose — admin-only structured health check."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        """Real auth flow — disable the conftest bypass for these tests."""
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_diagnose_admin_returns_200_with_checks_structure(self, migrated_pg):
        """Admin GET /api/operations/diagnose returns 200 (or 503) with checks array."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.get("/api/operations/diagnose")

        # May return 200 (all ok/skipped) or 503 (some error — e.g. docker absent)
        assert resp.status_code in (200, 503), f"Unexpected status: {resp.status_code} {resp.text}"
        data = resp.json()
        assert "checks" in data, "Response must have 'checks' key"
        assert "overall" in data, "Response must have 'overall' key"
        assert isinstance(data["checks"], list), "'checks' must be a list"
        assert data["overall"] in ("ok", "degraded"), (
            f"'overall' must be ok|degraded, got {data['overall']}"
        )
        # Each check has name/status/detail
        for check in data["checks"]:
            assert "name" in check, f"Check missing 'name': {check}"
            assert "status" in check, f"Check missing 'status': {check}"
            assert check["status"] in ("ok", "error", "skipped"), (
                f"Invalid status: {check['status']}"
            )
            assert "detail" in check, f"Check missing 'detail': {check}"

    @pytest.mark.asyncio
    async def test_diagnose_non_admin_returns_403(self, migrated_pg):
        """Non-admin user gets 403 on GET /api/operations/diagnose."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_user", "UserW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.get("/api/operations/diagnose")
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_diagnose_unauthenticated_returns_401(self, migrated_pg):
        """Unauthenticated request gets 401 on GET /api/operations/diagnose."""
        _seed_users(migrated_pg)
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/operations/diagnose")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# B. POST /api/admin/users (create user)
# ---------------------------------------------------------------------------


class TestCreateUser:
    """POST /api/admin/users — admin can create users; duplicate -> 409."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_admin_creates_user_with_temp_password(self, migrated_pg):
        """Admin POST creates user when no password given; returns temp_password once."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/admin/users",
                json={"username": "new_w3_user", "email": "new_w3@test.invalid"},
            )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data.get("ok") is True
        assert "user_id" in data
        assert "temp_password" in data, "temp_password must be returned once"
        assert data["username"] == "new_w3_user"

        # Verify DB: user was created
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT id, is_admin, is_active, email_verified"
                " FROM webui_users WHERE username = 'new_w3_user'"
            )
            row = cur.fetchone()
        assert row is not None, "User must be inserted in DB"
        assert row[1] is False, "is_admin must be False by default"
        assert row[2] is True, "is_active must be True"
        assert row[3] is True, "email_verified must be True for admin-created users"

    @pytest.mark.asyncio
    async def test_admin_creates_user_with_explicit_password(self, migrated_pg):
        """Admin POST with explicit password — no temp_password in response."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/admin/users",
                json={"username": "pw_w3_user", "password": "ExplicitPass123!"},
            )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data.get("ok") is True
        assert "temp_password" not in data, (
            "temp_password must NOT appear when explicit password given"
        )

    @pytest.mark.asyncio
    async def test_duplicate_username_returns_409(self, migrated_pg):
        """Creating a user with an existing username returns 409."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        # w3_user already exists from _seed_users
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/admin/users",
                json={"username": "w3_user"},
            )
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_duplicate_email_returns_409(self, migrated_pg):
        """Creating a user with an existing email returns 409."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        # w3_admin@test.invalid is already in use
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/admin/users",
                json={"username": "unique_name_ok", "email": "w3_admin@test.invalid"},
            )
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_non_admin_cannot_create_user_403(self, migrated_pg):
        """Non-admin user gets 403 on POST /api/admin/users."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_user", "UserW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/admin/users",
                json={"username": "attacker"},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_unauthenticated_create_user_401(self, migrated_pg):
        """Unauthenticated POST /api/admin/users returns 401."""
        _seed_users(migrated_pg)
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/admin/users",
                json={"username": "ghost"},
            )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# C. GET /api/admin/audit-log
# ---------------------------------------------------------------------------


class TestAuditLogEndpoint:
    """GET /api/admin/audit-log — admin reads entries; filter and pagination work."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_admin_gets_audit_log_200(self, migrated_pg):
        """Admin GET /api/admin/audit-log returns 200 with entries + total."""
        _seed_users(migrated_pg)
        _seed_audit_rows(migrated_pg, n=3, action="user.login")
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.get("/api/admin/audit-log")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "entries" in data, "Response must have 'entries'"
        assert "total" in data, "Response must have 'total'"
        assert data["total"] >= 3, f"Expected at least 3 rows, got {data['total']}"

    @pytest.mark.asyncio
    async def test_audit_log_filter_by_action(self, migrated_pg):
        """Filter ?action= returns only matching rows."""
        _seed_users(migrated_pg)
        _seed_audit_rows(migrated_pg, n=2, action="user.login")
        _seed_audit_rows(migrated_pg, n=1, action="user.logout")
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.get("/api/admin/audit-log?action=user.logout")

        assert resp.status_code == 200
        data = resp.json()
        for entry in data["entries"]:
            assert "user.logout" in entry["action"], (
                f"Filter should only return matching actions, got: {entry['action']}"
            )

    @pytest.mark.asyncio
    async def test_audit_log_non_admin_403(self, migrated_pg):
        """Non-admin gets 403 on GET /api/admin/audit-log."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_user", "UserW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.get("/api/admin/audit-log")
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_audit_log_unauthenticated_401(self, migrated_pg):
        """Unauthenticated GET /api/admin/audit-log returns 401."""
        _seed_users(migrated_pg)
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/admin/audit-log")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# D. #175 audit gap: index_all + jobs.reset write audit rows
# ---------------------------------------------------------------------------


class TestAuditGap175:
    """FOLD #175: index-all and jobs.reset must write audit rows when called."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_index_all_writes_audit_row(self, migrated_pg):
        """POST /api/repos/index-all (admin) writes operations.index_all audit row."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            # Bad max_workers triggers 422 early-exit — no subprocess spawn, but
            # @audit_action still fires on the handler call and records the row.
            resp = await client.post(
                "/api/repos/index-all",
                json={"max_workers": "not-an-int"},
            )
        # 422 = validation error (handler was reached, no subprocess started)
        assert resp.status_code in (200, 409, 422, 500), (
            f"Unexpected status {resp.status_code}: {resp.text}"
        )

        # Verify audit row was written
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT id FROM admin_audit_log WHERE action = 'operations.index_all'"
            )
            rows = cur.fetchall()
        assert len(rows) >= 1, (
            "Expected at least 1 audit row for 'operations.index_all', got 0"
        )

    @pytest.mark.asyncio
    async def test_jobs_reset_writes_audit_row(self, migrated_pg):
        """POST /api/jobs/{id}/reset (admin) writes jobs.reset audit row."""
        _seed_users(migrated_pg)
        jid = _seed_running_job(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            # PID 99999999 is dead -> handler resets job and returns 200
            resp = await client.post(f"/api/jobs/{jid}/reset")
        assert resp.status_code in (200, 409, 500), (
            f"Unexpected status {resp.status_code}: {resp.text}"
        )

        # Verify audit row
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT id FROM admin_audit_log WHERE action = 'jobs.reset'"
            )
            rows = cur.fetchall()
        assert len(rows) >= 1, (
            "Expected at least 1 audit row for 'jobs.reset', got 0"
        )


# ---------------------------------------------------------------------------
# D2. Regression: every mutating admin-gated route has @audit_action
# ---------------------------------------------------------------------------


class TestAuditCoverageRegression:
    """Anti-recurrence gate: every mutating admin-gated route must have @audit_action.

    Two-layer guard:
      1. ENUMERATE app.routes (chiều app→): all routes with mutating HTTP methods
         (POST/PUT/PATCH/DELETE) AND Depends(require_admin) or
         Depends(require_admin_with_fresh_mfa) MUST carry the ``__audit_action__``
         marker set by the @audit_action decorator. Adding a new gated route and
         forgetting the decorator causes this check to fail immediately.
      2. CROSS-CHECK action names: for every route in KNOWN_AUDITED_ROUTES, verify
         the ``__audit_action__`` attribute equals the expected action string.
         This catches typos in the action name.

    Detection uses the reliable ``__audit_action__`` marker set directly on the
    wrapper by audit_action() after @wraps. Closure introspection is NOT used —
    it is fragile and can produce false positives.

    IMPORTANT: KNOWN_AUDITED_ROUTES below is the authoritative cross-check for
    action *names* — it is NOT the completeness gate (that is the enumerate-app
    check). If you add a new mutating admin-gated route you do NOT need to add it
    here for the test to go red — but you SHOULD add it so the action name is
    verified too.
    """

    # Cross-check map: (HTTP_METHOD, exact_route_path) -> expected_audit_action_name.
    # exact_route_path must match the FastAPI route path string exactly (as seen in
    # the app after prefix mounting — use create_app() + enumerate routes to verify).
    # Routes here that are NOT require_admin-gated are still checked for audit marker
    # + correct action name; they just won't appear in the enumerate-app completeness check.
    KNOWN_AUDITED_ROUTES: list[tuple[str, str, str]] = [
        # Operations
        ("POST", "/api/operations/index-core",    "operations.index_core"),
        ("POST", "/api/operations/seed-patterns", "operations.seed_patterns"),
        ("POST", "/api/operations/apply-preset",  "operations.apply_preset"),
        ("POST", "/api/operations/backup",        "operations.backup"),
        ("POST", "/api/operations/restore",       "operations.restore"),
        # Repos / profiles
        ("POST",   "/api/repos/index-all",                       "operations.index_all"),
        ("POST",   "/api/repos/repos/{repo_id}/index",           "operations.index_repo"),
        ("POST",   "/api/repos/repos/{repo_id}/reset-embed",     "operations.reset_embed"),
        ("DELETE", "/api/repos/repos/{repo_id}",                 "repo.delete"),
        ("PATCH",  "/api/repos/repos/{repo_id}",                 "repo.update"),
        ("POST",   "/api/repos/repos",                           "repo.create"),
        ("POST",   "/api/repos/profiles",                        "profile.create"),
        ("PATCH",  "/api/repos/profiles/{profile_id}",           "profile.update"),
        ("DELETE", "/api/repos/profiles/{profile_id}",           "profile.delete"),
        ("PATCH",  "/api/repos/profiles/{profile_id}/parent",    "profile.set_parent"),
        ("POST",   "/api/repos/profiles/{profile_id}/clone-all", "profile.clone_all"),
        # SSH keys
        ("POST",   "/api/ssh-keys",          "ssh_key.create"),
        ("POST",   "/api/ssh-keys/import",   "ssh_key.import"),
        ("DELETE", "/api/ssh-keys/{key_id}", "ssh_key.delete"),
        # API keys
        ("POST",  "/api/api-keys/{key_id}/deactivate",  "api_key.deactivate"),
        ("PATCH", "/api/admin/api-keys/{key_id}/owner", "api_key.assign_owner"),
        # Jobs
        ("POST", "/api/jobs/{job_id}/reset", "jobs.reset"),
        # Admin users
        ("POST",  "/api/admin/users",                                  "user.create"),
        ("PATCH", "/api/admin/users/{user_id}/admin",                  "user.set_admin"),
        ("POST",  "/api/admin/users/{user_id}/deactivate",             "user.deactivate"),
        ("POST",  "/api/admin/users/{user_id}/reactivate",             "user.reactivate"),
        ("POST",  "/api/admin/users/{user_id}/reset-password-link",    "user.reset_password_link"),
        # Admin plans (ADR-0039 — Phase 2 stub still audited per ADR-0021)
        ("POST",  "/api/admin/plans",                                  "plan.create_attempt"),
    ]

    def _has_audit_action(self, endpoint_func) -> bool:
        """Return True if endpoint_func carries the __audit_action__ marker.

        audit_action() sets ``wrapper.__audit_action__ = action`` directly on
        the wrapper after @wraps. This is a reliable marker — no closure
        introspection, no false positives.
        """
        return getattr(endpoint_func, "__audit_action__", None) is not None

    def test_all_mutating_admin_routes_have_audit_action(self, migrated_pg):
        """Enumerate app.routes: every mutating admin-gated route must have @audit_action.

        Guard (1) — completeness (chiều app→):
          Walk every APIRoute in the app. For each route with a mutating HTTP method
          (POST/PUT/PATCH/DELETE) that depends on require_admin or
          require_admin_with_fresh_mfa, assert the endpoint carries ``__audit_action__``.
          This catches new admin-gated routes added without the decorator.

        Guard (2) — action name cross-check:
          For each entry in KNOWN_AUDITED_ROUTES, assert the route exists in the app
          and that ``endpoint.__audit_action__`` equals the expected action string.
          This catches typos and action-name drift.
        """
        from fastapi.routing import APIRoute

        from src.web_ui.auth import require_admin, require_admin_with_fresh_mfa

        _ADMIN_DEPS = {require_admin, require_admin_with_fresh_mfa}
        _MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

        app = create_app()

        def _is_admin_gated(route: APIRoute) -> bool:
            """Return True if the route has require_admin* in its direct dependencies."""
            try:
                for dep in route.dependant.dependencies:
                    if dep.call in _ADMIN_DEPS:
                        return True
            except Exception:
                pass
            return False

        # ---- Guard (1): enumerate app.routes for completeness ----
        missing_decorator: list[str] = []
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            if not _is_admin_gated(route):
                continue
            for method in route.methods or []:
                if method.upper() not in _MUTATING:
                    continue
                if not self._has_audit_action(route.endpoint):
                    missing_decorator.append(
                        f"{method.upper()} {route.path} — endpoint "
                        f"'{getattr(route.endpoint, '__qualname__', route.endpoint)}' "
                        f"has no @audit_action decorator"
                    )

        # ---- Guard (2): cross-check action names from KNOWN_AUDITED_ROUTES ----
        # Build lookup: (method, path) -> endpoint_func
        route_lookup: dict[tuple[str, str], object] = {}
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods or []:
                route_lookup[(method.upper(), route.path)] = route.endpoint

        not_found: list[str] = []
        wrong_action: list[str] = []

        for method, path, expected_action in self.KNOWN_AUDITED_ROUTES:
            fn = route_lookup.get((method, path))
            if fn is None:
                not_found.append(f"{method} {path} (expected action: {expected_action})")
                continue
            actual_action = getattr(fn, "__audit_action__", None)
            if actual_action != expected_action:
                wrong_action.append(
                    f"{method} {path}: expected action '{expected_action}', "
                    f"got '{actual_action}'"
                )

        # ---- Collect all failures and report together ----
        messages = []
        if missing_decorator:
            messages.append(
                "Mutating admin-gated routes missing @audit_action "
                "(add the decorator to fix):\n  "
                + "\n  ".join(missing_decorator)
            )
        if not_found:
            messages.append(
                "Routes in KNOWN_AUDITED_ROUTES not found in app "
                "(path typo or route removed?):\n  "
                + "\n  ".join(not_found)
            )
        if wrong_action:
            messages.append(
                "Routes have wrong audit action name:\n  "
                + "\n  ".join(wrong_action)
            )

        assert not messages, "\n\n".join(messages)


# ---------------------------------------------------------------------------
# E. Regression: admin_users handlers write exactly ONE audit row
# ---------------------------------------------------------------------------


class TestAuditNoDoubleWrite:
    """Each admin-user mutation writes exactly ONE audit row with the right action.

    Guards the fix for the double-write bug: create_user/deactivate/reactivate/
    reset_password_link previously carried both an @audit_action decorator AND a
    manual write_audit_log call (two rows per action). reset_password_link also
    used the wrong action name "user.reset_password" in the manual write.
    """

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_create_user_writes_single_row_with_generated_target(self, migrated_pg):
        """POST /api/admin/users → exactly one user.create row, target = new id."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                "/api/admin/users", json={"username": "single_row_user"}
            )
        assert resp.status_code == 200, resp.text
        new_id = resp.json()["user_id"]

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT target FROM admin_audit_log WHERE action = 'user.create'")
            rows = cur.fetchall()
        assert len(rows) == 1, f"Expected exactly 1 user.create row, got {len(rows)}"
        assert rows[0][0] == str(new_id), "audit target must be the generated user id"

    @pytest.mark.asyncio
    async def test_deactivate_writes_single_row(self, migrated_pg):
        """POST .../deactivate → exactly one user.deactivate row."""
        admin_id, nonadmin_id = _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(f"/api/admin/users/{nonadmin_id}/deactivate")
        assert resp.status_code == 200, resp.text
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT id FROM admin_audit_log WHERE action = 'user.deactivate'")
            rows = cur.fetchall()
        assert len(rows) == 1, f"Expected exactly 1 user.deactivate row, got {len(rows)}"

    @pytest.mark.asyncio
    async def test_reset_password_link_single_row_correct_action(self, migrated_pg):
        """POST .../reset-password-link → one row named user.reset_password_link,
        and ZERO rows under the old/wrong name user.reset_password."""
        admin_id, nonadmin_id = _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "w3_admin", "AdminW3Pass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await client.post(
                f"/api/admin/users/{nonadmin_id}/reset-password-link"
            )
        assert resp.status_code == 200, resp.text
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT action, target FROM admin_audit_log"
                " WHERE action LIKE 'user.reset_password%' ORDER BY action"
            )
            rows = cur.fetchall()
        actions = [r[0] for r in rows]
        assert actions == ["user.reset_password_link"], (
            f"Expected exactly one 'user.reset_password_link' row, got {actions}"
        )
        assert rows[0][1] == str(nonadmin_id), "audit target must be the user id"
