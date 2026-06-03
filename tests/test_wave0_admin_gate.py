# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_wave0_admin_gate.py
"""Wave 0 security gate tests: admin-only mutating routes + signup disabled by default.

Business intent:
  - Every mutating control-plane route (POST/PATCH/DELETE on repos, profiles,
    ssh-keys, operations, jobs) must return 403 for an authenticated non-admin
    user, and must succeed (not 403) for an admin.
  - Public signup is DISABLED by default (SIGNUP_ENABLED=False).
    POST /api/auth/register → 403 when disabled, 201 when enabled.
  - OAuth new-user creation is also blocked when SIGNUP_ENABLED=False.
    Existing linked accounts must still log in.

All tests use httpx.AsyncClient with ASGI transport — no real server.
PostgreSQL is required (pytestmark postgres) for DB-layer route handlers.
"""
import itertools
import os

import httpx
import pytest

from src.web_ui.app import create_app
from src.web_ui.auth import hash_password

pytestmark = pytest.mark.postgres

# Monotonic counter giving each _seed_test_repo() call a unique (url, branch)
# under the module-scoped migrated_pg (no per-test wipe). See _seed_test_repo.
_repo_seq = itertools.count(1)

# ---------------------------------------------------------------------------
# Session secret must be set before app creation
# ---------------------------------------------------------------------------
os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-wave0-admin-gate-32bytes!!")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def migrated_pg(migrated_pg_module):
    """Module-scoped: migrate ONCE for this whole file (was per-test via clean_pg).

    Safe because every test here asserts only on HTTP status codes
    (403/401/200/201/not-403) — none asserts an absolute row count or empty
    state — and the per-test ``_seed_*`` helpers are idempotent (``ON CONFLICT``)
    or return their own freshly-inserted id. DO NOT add a test to this module
    that asserts an absolute ``count(*)`` or expects an empty table.
    """
    return migrated_pg_module


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
            " VALUES ('wave0_admin', %s, 'wave0_admin@test.invalid', TRUE, TRUE, TRUE)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET password_hash=EXCLUDED.password_hash, is_admin=TRUE RETURNING id",
            (admin_hash,),
        )
        admin_id = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO webui_users {cols}"
            " VALUES ('wave0_user', %s, 'wave0_user@test.invalid', TRUE, FALSE, TRUE)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET password_hash=EXCLUDED.password_hash, is_admin=FALSE RETURNING id",
            (nonadmin_hash,),
        )
        nonadmin_id = cur.fetchone()[0]
    pg_conn.commit()
    return admin_id, nonadmin_id


async def _login_session(app, username: str, password: str) -> dict:
    """Async helper: log in and return the session cookie dict."""
    async with _async_client(app) as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert resp.status_code == 200, f"Login failed for {username}: {resp.text}"
        return dict(resp.cookies)


async def _post_with_session(client, url, *, json_body: dict | None = None):
    """Helper: POST through a client that already carries the session cookies."""
    return await client.post(url, json=json_body or {})


async def _patch_with_session(client, url, *, json_body: dict | None = None):
    return await client.patch(url, json=json_body or {})


async def _delete_with_session(client, url):
    return await client.delete(url)


# ---------------------------------------------------------------------------
# Helpers: create test resources needed for PATCH/DELETE routes
# ---------------------------------------------------------------------------


def _seed_test_profile(pg_conn) -> int:
    """Insert a test profile. Returns profile id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, description)"
            " VALUES ('wave0_test_profile', '17.0', 'wave0 sec test')"
            " ON CONFLICT (name) DO UPDATE SET odoo_version='17.0' RETURNING id",
        )
        pid = cur.fetchone()[0]
    pg_conn.commit()
    return pid


def _seed_test_repo(pg_conn, profile_id: int) -> int:
    """Insert a test repo under profile_id. Returns repo id.

    url/local_path carry a per-call counter suffix so repeated seeding under a
    module-scoped (no per-test wipe) ``migrated_pg`` never collides with the
    ``UNIQUE (url, branch)`` constraint (src/db/migrate.py). Each call returns a
    distinct fresh repo id, preserving per-test isolation of the row under test.
    """
    n = next(_repo_seq)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path, clone_status)"
            " VALUES (%s, %s, '17.0', %s, 'manual')"
            " RETURNING id",
            (profile_id, f"https://example.com/repo{n}.git", f"/tmp/wave0_repo{n}"),
        )
        rid = cur.fetchone()[0]
    pg_conn.commit()
    return rid


def _seed_test_ssh_key(pg_conn) -> int:
    """Insert a minimal SSH key row. Returns key id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted)"
            " VALUES ('wave0_test_key', 'ssh-ed25519 AAAA...', 'encrypted_stub')"
            " RETURNING id",
        )
        kid = cur.fetchone()[0]
    pg_conn.commit()
    return kid


def _seed_test_job(pg_conn) -> int:
    """Insert a minimal indexer_jobs row in 'running' state. Returns job id."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO indexer_jobs (profile_name, status, pid)"
            " VALUES ('wave0_test_profile', 'running', 99999999)"
            " RETURNING id",
        )
        jid = cur.fetchone()[0]
    pg_conn.commit()
    return jid


# ---------------------------------------------------------------------------
# TASK 1: Admin gate tests
# ---------------------------------------------------------------------------


class TestReposRoutesAdminGate:
    """POST/PATCH/DELETE /api/repos/* routes must require admin."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        """Real auth — no bypass allowed for these tests."""
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_create_profile_non_admin_403(self, migrated_pg):
        """Non-admin user cannot create a profile — must get 403."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/repos/profiles",
                json_body={"name": "attacker_profile", "version": "17.0"},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_create_profile_admin_not_403(self, migrated_pg):
        """Admin user can create a profile — must not get 403."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/repos/profiles",
                json_body={"name": "admin_created_profile", "version": "17.0"},
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_update_profile_non_admin_403(self, migrated_pg):
        """Non-admin cannot PATCH a profile."""
        admin_id, _ = _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _patch_with_session(
                client,
                f"/api/repos/profiles/{pid}",
                json_body={"description": "hacked"},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_update_profile_admin_not_403(self, migrated_pg):
        """Admin can PATCH a profile."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _patch_with_session(
                client,
                f"/api/repos/profiles/{pid}",
                json_body={"description": "legit update"},
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_set_profile_parent_non_admin_403(self, migrated_pg):
        """Non-admin cannot PATCH /profiles/{id}/parent."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _patch_with_session(
                client,
                f"/api/repos/profiles/{pid}/parent",
                json_body={"parent_id": None},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_delete_profile_non_admin_403(self, migrated_pg):
        """Non-admin cannot DELETE a profile."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _delete_with_session(
                client,
                f"/api/repos/profiles/{pid}",
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_clone_all_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /profiles/{id}/clone-all."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                f"/api/repos/profiles/{pid}/clone-all",
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_add_repo_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /repos (add repo) to shared/null profile.

        W2: route opened to non-admin, but scope check still denies shared(null)
        profile writes for non-admin (tenant_write_allowed returns False for null).
        Seed the profile so 404 doesn't mask the 403 scope denial.
        """
        _seed_users(migrated_pg)
        _seed_test_profile(migrated_pg)  # shared (tenant_id=NULL) — non-admin write denied
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/repos/repos",
                json_body={
                    "profile": "wave0_test_profile",
                    "url": "https://example.com/evil.git",
                    "branch": "17.0",
                },
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_update_repo_non_admin_403(self, migrated_pg):
        """Non-admin cannot PATCH /repos/{id}."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        rid = _seed_test_repo(migrated_pg, pid)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _patch_with_session(
                client,
                f"/api/repos/repos/{rid}",
                json_body={"branch": "evil"},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_delete_repo_non_admin_403(self, migrated_pg):
        """Non-admin cannot DELETE /repos/{id}."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        rid = _seed_test_repo(migrated_pg, pid)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _delete_with_session(
                client,
                f"/api/repos/repos/{rid}",
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_index_repo_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /repos/{id}/index."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        rid = _seed_test_repo(migrated_pg, pid)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                f"/api/repos/repos/{rid}/index",
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_reset_embed_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /repos/{id}/reset-embed."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        rid = _seed_test_repo(migrated_pg, pid)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                f"/api/repos/repos/{rid}/reset-embed",
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_index_all_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /index-all."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/repos/index-all",
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    # ----------------------------------------------------------------------
    # Admin positive-path regression guard.
    # The class contract (docstring) is "must succeed (not 403) for an admin"
    # for EVERY gated route — not just create/update profile. The tests below
    # prove an admin is not over-gated on the remaining routes. Each hits a
    # pre-spawn return path (DB op, 404 on a missing id, 422 on bad input, or
    # "no pending repos") so no indexer/clone subprocess is started — we only
    # assert status != 403.
    # ----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_set_profile_parent_admin_not_403(self, migrated_pg):
        """Admin can PATCH /profiles/{id}/parent."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _patch_with_session(
                client,
                f"/api/repos/profiles/{pid}/parent",
                json_body={"parent_id": None},
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_delete_profile_admin_not_403(self, migrated_pg):
        """Admin can DELETE a profile."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _delete_with_session(
                client, f"/api/repos/profiles/{pid}"
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_clone_all_admin_not_403(self, migrated_pg):
        """Admin can POST /profiles/{id}/clone-all (no pending repos → 200, no spawn)."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client, f"/api/repos/profiles/{pid}/clone-all"
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_add_repo_admin_not_403(self, migrated_pg):
        """Admin can POST /repos (HTTPS URL → DB insert, no clone subprocess)."""
        _seed_users(migrated_pg)
        _seed_test_profile(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/repos/repos",
                json_body={
                    "profile": "wave0_test_profile",
                    "url": "https://example.com/legit.git",
                    "branch": "17.0",
                },
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_update_repo_admin_not_403(self, migrated_pg):
        """Admin can PATCH /repos/{id}."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        rid = _seed_test_repo(migrated_pg, pid)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _patch_with_session(
                client,
                f"/api/repos/repos/{rid}",
                json_body={"branch": "18.0"},
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_delete_repo_admin_not_403(self, migrated_pg):
        """Admin can DELETE /repos/{id}."""
        _seed_users(migrated_pg)
        pid = _seed_test_profile(migrated_pg)
        rid = _seed_test_repo(migrated_pg, pid)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _delete_with_session(
                client, f"/api/repos/repos/{rid}"
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_index_repo_admin_not_403(self, migrated_pg):
        """Admin reaches POST /repos/{id}/index (missing repo → 404, no spawn)."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client, "/api/repos/repos/999999/index"
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_reset_embed_admin_not_403(self, migrated_pg):
        """Admin reaches POST /repos/{id}/reset-embed (missing repo → 404, no spawn)."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client, "/api/repos/repos/999999/reset-embed"
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_index_all_admin_not_403(self, migrated_pg):
        """Admin reaches POST /index-all (bad max_workers → 422, no spawn)."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/repos/index-all",
                json_body={"max_workers": "not-an-int"},
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"


class TestSshKeysAdminGate:
    """SSH key routes must require admin."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_create_ssh_key_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /api/ssh-keys (generate keypair)."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/ssh-keys",
                json_body={"name": "evil_key"},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_create_ssh_key_admin_not_403(self, migrated_pg, monkeypatch):
        """Admin can POST /api/ssh-keys."""
        _seed_users(migrated_pg)
        # Provide FERNET_KEY so the handler doesn't fail at key-gen
        from cryptography.fernet import Fernet
        monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/ssh-keys",
                json_body={"name": "legit_key"},
            )
        # Should not be 403 (may be 500 if other infra missing — we only check ≠403)
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"

    @pytest.mark.asyncio
    async def test_import_ssh_key_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /api/ssh-keys/import."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/ssh-keys/import",
                json_body={"name": "evil", "private_key_pem": "FAKE"},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_delete_ssh_key_non_admin_403(self, migrated_pg):
        """Non-admin cannot DELETE /api/ssh-keys/{id}."""
        _seed_users(migrated_pg)
        kid = _seed_test_ssh_key(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _delete_with_session(
                client,
                f"/api/ssh-keys/{kid}",
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_delete_ssh_key_admin_not_403(self, migrated_pg):
        """Admin can DELETE /api/ssh-keys/{id}."""
        _seed_users(migrated_pg)
        kid = _seed_test_ssh_key(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _delete_with_session(
                client,
                f"/api/ssh-keys/{kid}",
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"


class TestOperationsAdminGate:
    """Operations routes must require admin."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_index_core_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /api/operations/index-core."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/operations/index-core",
                json_body={"source": "/tmp", "version": "17.0"},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_seed_patterns_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /api/operations/seed-patterns."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/operations/seed-patterns",
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_apply_preset_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /api/operations/apply-preset."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/operations/apply-preset",
                json_body={"name": "viindoo17"},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_backup_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /api/operations/backup."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/operations/backup",
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_index_core_admin_not_403(self, migrated_pg, tmp_path):
        """Admin can POST /api/operations/index-core (non-403 regardless of business errors)."""
        _seed_users(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                "/api/operations/index-core",
                json_body={"source": str(tmp_path), "version": "17.0"},
            )
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"


class TestJobsAdminGate:
    """Jobs reset route must require admin."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    @pytest.mark.asyncio
    async def test_reset_stuck_job_non_admin_403(self, migrated_pg):
        """Non-admin cannot POST /api/jobs/{id}/reset."""
        _seed_users(migrated_pg)
        jid = _seed_test_job(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_user", "UserPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                f"/api/jobs/{jid}/reset",
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_reset_stuck_job_admin_not_403(self, migrated_pg):
        """Admin can POST /api/jobs/{id}/reset (may get 409 if PID alive — not 403)."""
        _seed_users(migrated_pg)
        jid = _seed_test_job(migrated_pg)
        app = create_app()
        cookies = await _login_session(app, "wave0_admin", "AdminPass123!")
        async with _async_client(app, cookies=cookies) as client:
            resp = await _post_with_session(
                client,
                f"/api/jobs/{jid}/reset",
            )
        # PID 99999999 is dead → handler resets the job and returns 200.
        # (A live PID would return 409; either way the check is: NOT 403.)
        sc = resp.status_code
        assert sc != 403, f"Admin should not get 403, got {sc}: {resp.text}"


# ---------------------------------------------------------------------------
# TASK 2: Signup gate tests
# ---------------------------------------------------------------------------


class TestSignupGate:
    """SIGNUP_ENABLED=False blocks new registrations (default behaviour)."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
        # WI-RV F-A: patch source-of-truth in config module so signup_enabled()
        # reads the patched value via getattr(sys.modules[__name__], ...).
        # Legacy route-module symbol also patched for tests that import it.
        monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", False)
        monkeypatch.setattr("src.web_ui.routes.signup.SIGNUP_ENABLED", False)
        # WI-CI Cat C: ASGI lifespan auto-runs bootstrap_settings_safe() which
        # seeds a signup.enabled=False row into app_settings.  That makes
        # signup_enabled() read the DB overlay (returns a non-None False) and
        # never falls back to the module constant — the monkeypatch above is
        # bypassed entirely.  Force get_overlay_only to return None so the
        # module-constant path is exercised, preserving the wave0 unit-test
        # contract.  The dedicated DB-overlay path is covered separately by
        # tests/test_signup_enabled_db_overlay.py (WI-RV F-A intent intact).
        monkeypatch.setattr("src.settings.get_overlay_only", lambda key, **kw: None)

    @pytest.mark.asyncio
    async def test_register_disabled_returns_403(self, migrated_pg):
        """POST /api/auth/register → 403 when SIGNUP_ENABLED=False."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/auth/register",
                json={
                    "email": "attacker@example.com",
                    "username": "attacker",
                    "password": "SecurePass123!",
                    "confirm_password": "SecurePass123!",
                    "hcaptcha_token": "",
                },
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        assert resp.json().get("error") == "signup_disabled"

    @pytest.mark.asyncio
    async def test_register_no_user_created_when_disabled(self, migrated_pg):
        """Disabled signup must NOT create any DB row."""
        from src.db.pg import get_pool
        app = create_app()
        async with _async_client(app) as client:
            await client.post(
                "/api/auth/register",
                json={
                    "email": "ghost@example.com",
                    "username": "ghost_user",
                    "password": "SecurePass123!",
                    "confirm_password": "SecurePass123!",
                    "hcaptcha_token": "",
                },
            )
        with get_pool().checkout() as conn:
            row = get_pool().fetch_one(
                conn,
                "SELECT id FROM webui_users WHERE username = 'ghost_user'",
            )
        assert row is None, "Disabled signup must not create any user in DB"

    @pytest.mark.asyncio
    async def test_register_enabled_returns_201(self, migrated_pg, monkeypatch):
        """POST /api/auth/register → 201 when SIGNUP_ENABLED=True."""
        # WI-RV F-A: patch config-module constant (source-of-truth) + legacy route symbol.
        monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", True)
        monkeypatch.setattr("src.web_ui.routes.signup.SIGNUP_ENABLED", True)
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/auth/register",
                json={
                    "email": "newuser@example.com",
                    "username": "wave0_newuser",
                    "password": "SecurePass123!",
                    "confirm_password": "SecurePass123!",
                    "hcaptcha_token": "",
                    # D4 consent gate (signup.py): the request must carry
                    # consent=True to create an account (PDPL 91/2025 +
                    # card-network requirement). A happy-path 201 test must
                    # supply it; absence → 422 consent_required.
                    "consent": True,
                },
            )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"


class TestOAuthSignupGate:
    """SIGNUP_ENABLED=False blocks OAuth new-user creation but allows existing users to log in."""

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
        # WI-RV F-A: patch config-module constant (source-of-truth) + legacy route symbol.
        monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", False)
        monkeypatch.setattr("src.web_ui.routes.oauth.SIGNUP_ENABLED", False)
        # WI-CI Cat C: same lifespan bootstrap issue as TestSignupGate above —
        # force the DB-overlay path to None so the module-constant patch wins.
        # DB-overlay behaviour is covered by test_signup_enabled_db_overlay.py.
        monkeypatch.setattr("src.settings.get_overlay_only", lambda key, **kw: None)

    @pytest.mark.asyncio
    async def test_oauth_new_user_blocked_when_signup_disabled(self, migrated_pg):
        """Brand-new OAuth user (no existing account) → 403 when signup disabled."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/auth/oauth-login",
                json={
                    "provider": "google",
                    "oauth_id": "google_new_user_999",
                    "email": "brandnew@google.example",
                    "email_verified": True,
                    "name": "Brand New",
                },
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        assert resp.json().get("error") == "signup_disabled"

    @pytest.mark.asyncio
    async def test_oauth_existing_user_can_still_login_when_signup_disabled(self, migrated_pg):
        """Existing OAuth-linked user must still be able to log in even when signup is disabled."""
        # Pre-create a user with OAuth credentials (simulates existing account).
        # password_hash uses a placeholder — the NOT NULL constraint on the column
        # predates the OAuth migration that would drop it; using a stub satisfies
        # the constraint while still letting the OAuth login path work (it never
        # reads password_hash for OAuth users).
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users"
                " (username, password_hash, oauth_provider, oauth_id,"
                "  email, email_verified, is_admin, is_active)"
                " VALUES ('existing_oauth_user', 'OAUTH_USER_NO_PW',"
                "  'google', 'google_existing_999',"
                "  'existing@google.example', TRUE, FALSE, TRUE)"
                " ON CONFLICT (username) DO NOTHING",
            )
        migrated_pg.commit()

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/auth/oauth-login",
                json={
                    "provider": "google",
                    "oauth_id": "google_existing_999",
                    "email": "existing@google.example",
                    "email_verified": True,
                    "name": "Existing User",
                },
            )
        # Existing user must be allowed (fast path: matched by oauth_id)
        assert resp.status_code == 200, (
            f"Existing OAuth user must not be blocked by signup gate, "
            f"got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# TASK 3: Unauthenticated requests are rejected (defense layer 1)
# ---------------------------------------------------------------------------


class TestUnauthenticatedRejected:
    """No session → 401 on every gated mutating route.

    Complements the non-admin (403) tests: those prove an authenticated
    non-admin is blocked at the require_admin layer; these prove a session-less
    caller is blocked earlier, at AuthRequiredMiddleware. Together they document
    the full auth contract (401 → 403 → admin OK) and guard against a gated
    route being accidentally added to the middleware exempt list.
    """

    @pytest.fixture(autouse=True)
    def _disable_bypass(self, monkeypatch):
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)

    # (method, path) for every route admin-gated in Wave 0. Dummy ids are fine:
    # the 401 fires in middleware before the path param or handler is reached.
    _GATED_ROUTES = [
        ("POST", "/api/repos/profiles"),
        ("PATCH", "/api/repos/profiles/1/parent"),
        ("PATCH", "/api/repos/profiles/1"),
        ("DELETE", "/api/repos/profiles/1"),
        ("POST", "/api/repos/repos"),
        ("PATCH", "/api/repos/repos/1"),
        ("DELETE", "/api/repos/repos/1"),
        ("POST", "/api/repos/profiles/1/clone-all"),
        ("POST", "/api/repos/repos/1/index"),
        ("POST", "/api/repos/repos/1/reset-embed"),
        ("POST", "/api/repos/index-all"),
        ("POST", "/api/ssh-keys"),
        ("POST", "/api/ssh-keys/import"),
        ("DELETE", "/api/ssh-keys/1"),
        ("POST", "/api/operations/index-core"),
        ("POST", "/api/operations/seed-patterns"),
        ("POST", "/api/operations/apply-preset"),
        ("POST", "/api/operations/backup"),
        ("POST", "/api/jobs/1/reset"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,path", _GATED_ROUTES)
    async def test_unauthenticated_request_returns_401(self, migrated_pg, method, path):
        """Every gated mutating route rejects a session-less caller with 401."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.request(method, path, json={})
        assert resp.status_code == 401, (
            f"{method} {path} must return 401 without a session, "
            f"got {resp.status_code}: {resp.text}"
        )
