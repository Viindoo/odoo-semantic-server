# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_account_self_service.py
"""GDPR self-service endpoint tests for DELETE /api/account/me and GET /api/account/export.

Business rules covered (red-before-green behavior tests, ETHOS #11):
  (a) DELETE unauthenticated -> 401.
  (b) DELETE as MFA-enabled user with STALE mfa_verified_at -> 403.
  (c) DELETE as normal (no-MFA) user -> 200; afterwards is_active=False,
      password_hash IS NULL, email anonymized, login no longer works.
  (d) DELETE last active admin -> 422 (account stays active).
  (e) admin_audit_log row is written for the delete action.
  (f) GET /api/account/export -> 200; JSON contains user keys/usage/memberships
      and does NOT contain raw key / password_hash / any secret (explicit redaction assert).

All tests require PostgreSQL (testcontainers or PG_ADMIN_DSN).
"""

import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Shared fixture: web app + seeded test users
# ---------------------------------------------------------------------------


@pytest.fixture
def web_app(pg_conn):
    """Web UI app on isolated test DB with migrations applied.

    Seed layout:
      id=10 — normal user (no MFA, no admin) — auth-bypass sentinel.
      id=11 — MFA-enabled normal user.
      id=20 — lone admin (cannot be deleted without another admin).
      id=21 — second admin (allows deleting id=20 without triggering last-admin guard).

    The conftest auth bypass (WEBUI_AUTH_DISABLED=1) returns current_user_id=1
    by default; individual tests monkeypatch current_user_id to override.
    """
    from src.db.migrate import run_migrations
    from src.web_ui.app import create_app

    run_migrations(pg_conn)

    with pg_conn.cursor() as cur:
        # Clean slate for our test users.
        cur.execute(
            "DELETE FROM webui_users WHERE id IN (10, 11, 20, 21)"
            " OR username IN"
            " ('_ss_normal_id10', '_ss_mfa_id11', '_ss_admin_id20', '_ss_admin2_id21')"
        )
        # id=10: normal user, no MFA, no admin.
        cur.execute(
            "INSERT INTO webui_users"
            " (username, password_hash, is_admin, is_active, id, email, mfa_enabled)"
            " VALUES (%s, %s, FALSE, TRUE, 10, %s, FALSE)",
            ("_ss_normal_id10", "bcrypt_hash_placeholder", "normal10@example.test"),
        )
        # id=11: normal user, MFA enabled.
        cur.execute(
            "INSERT INTO webui_users"
            " (username, password_hash, is_admin, is_active, id, email, mfa_enabled)"
            " VALUES (%s, %s, FALSE, TRUE, 11, %s, TRUE)",
            ("_ss_mfa_id11", "bcrypt_hash_placeholder", "mfa11@example.test"),
        )
        # id=20: lone admin (no other admin with id=21 yet -> last-admin guard fires).
        cur.execute(
            "INSERT INTO webui_users"
            " (username, password_hash, is_admin, is_active, id, email, mfa_enabled)"
            " VALUES (%s, %s, TRUE, TRUE, 20, %s, FALSE)",
            ("_ss_admin_id20", "bcrypt_hash_placeholder", "admin20@example.test"),
        )
        # id=21: second admin (allows deleting id=20).
        cur.execute(
            "INSERT INTO webui_users"
            " (username, password_hash, is_admin, is_active, id, email, mfa_enabled)"
            " VALUES (%s, %s, TRUE, TRUE, 21, %s, FALSE)",
            ("_ss_admin2_id21", "bcrypt_hash_placeholder", "admin21@example.test"),
        )

    app = create_app()
    yield app

    # Symmetric teardown — remove EXACTLY the rows created here.
    with pg_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM webui_users WHERE id IN (10, 11, 20, 21)"
            " OR username IN"
            " ('_ss_normal_id10', 'deleted-user-10', '_ss_mfa_id11', 'deleted-user-11',"
            "  '_ss_admin_id20', 'deleted-user-20', '_ss_admin2_id21')"
        )


# ---------------------------------------------------------------------------
# (a) DELETE unauthenticated -> 401
# ---------------------------------------------------------------------------


class TestDeleteUnauthenticated:
    """Business rule: unauthenticated DELETE /api/account/me must return 401."""

    @pytest.mark.asyncio
    async def test_delete_unauthenticated_returns_401(self, web_app, monkeypatch):
        """DELETE /api/account/me without a session must return 401."""
        import httpx

        # Disable auth bypass — request is truly unauthenticated.
        monkeypatch.setattr("src.web_ui.auth.is_test_bypass_active", lambda: False)
        monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: None)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/account/me")

        assert resp.status_code == 401, (
            f"Expected 401 for unauthenticated DELETE, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# (b) DELETE as MFA-enabled user with STALE mfa -> 403
# ---------------------------------------------------------------------------


class TestDeleteMfaStaleReturns403:
    """Business rule: MFA-enabled user with stale MFA verification must receive 403."""

    @pytest.mark.asyncio
    async def test_delete_mfa_enabled_stale_returns_403(
        self, web_app, pg_conn, monkeypatch
    ):
        """MFA-enabled user whose mfa_verified_at is absent/stale gets 403."""
        import httpx

        # Bypass resolves to uid=11 (MFA-enabled user).
        monkeypatch.setattr(
            "src.web_ui.routes.account.current_user_id", lambda _req: 11
        )
        # _check_mfa_freshness reads request.session["mfa_verified_at"].
        # We use the real check function — the test app's session will have no
        # mfa_verified_at set, so the freshness check correctly fires 403.
        # No need to monkeypatch _check_mfa_freshness itself (that would be tautological).

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/account/me")

        assert resp.status_code == 403, (
            f"MFA-enabled user with stale MFA must get 403, got {resp.status_code}"
        )
        body = resp.json()
        # The detail dict must carry the mfa_freshness_required sentinel (ADR-0043 D5).
        assert "detail" in body
        detail = body["detail"]
        # detail may be a string or a dict depending on FastAPI serialization.
        detail_str = str(detail)
        assert "mfa_freshness_required" in detail_str or "Fresh MFA" in detail_str, (
            f"403 body must contain mfa_freshness_required or 'Fresh MFA': {body}"
        )

        # Confirm the account was NOT deactivated (403 is a gate, not a side-effect).
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT is_active FROM webui_users WHERE id = 11"
            )
            row = cur.fetchone()
        assert row is not None and row[0] is True, (
            "Account must remain active after a rejected (403) delete attempt"
        )


# ---------------------------------------------------------------------------
# (c) DELETE as normal (no-MFA) user -> 200, observables confirmed
# ---------------------------------------------------------------------------


class TestDeleteNormalUserSuccess:
    """Business rule: no-MFA user delete returns 200 and anonymizes PII."""

    @pytest.mark.asyncio
    async def test_delete_normal_user_returns_200_and_anonymizes(
        self, web_app, pg_conn, monkeypatch
    ):
        """Successful DELETE: 200 + is_active=False + password_hash NULL + email anonymized."""
        import httpx

        # Bypass resolves to uid=10 (normal user, no MFA).
        monkeypatch.setattr(
            "src.web_ui.routes.account.current_user_id", lambda _req: 10
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/account/me")

        assert resp.status_code == 200, (
            f"Expected 200 for successful delete, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body.get("status") == "account_deleted", (
            f"Expected status=account_deleted, got {body}"
        )

        # Verify observable outcomes in the DB.
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT is_active, password_hash, email, mfa_enabled, username"
                "  FROM webui_users WHERE id = 10"
            )
            row = cur.fetchone()

        assert row is not None, "User row must still exist after soft-delete"
        is_active, password_hash, email, mfa_enabled, username = row
        assert is_active is False, "is_active must be FALSE after delete"
        assert password_hash is None, "password_hash must be NULL after anonymize"
        assert email is None, "email must be NULL after anonymize"
        assert mfa_enabled is False, "mfa_enabled must be FALSE after anonymize"
        assert username == "deleted-user-10", (
            f"username must be tombstoned to 'deleted-user-10', got '{username}'"
        )

    @pytest.mark.asyncio
    async def test_delete_normal_user_login_no_longer_works(
        self, web_app, pg_conn, monkeypatch
    ):
        """After delete, the user's password_hash is NULL so login must fail.

        This confirms the account cannot be re-accessed via the password login path.
        """
        import httpx

        # Bypass resolves to uid=10 (normal user, no MFA).
        monkeypatch.setattr(
            "src.web_ui.routes.account.current_user_id", lambda _req: 10
        )

        # Delete the account.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            del_resp = await client.delete("/api/account/me")

        assert del_resp.status_code == 200

        # The real login check hashes the attempted password and compares to
        # password_hash. With password_hash=NULL, the get_user_password_hash_by_id
        # returns None — bcrypt.checkpw would not even be called. Verify at the
        # store level (direct observable behavior).
        from src.db.pg import auth_store

        stored_hash = auth_store().get_user_password_hash_by_id(10)
        assert stored_hash is None, (
            "password_hash must be NULL after anonymize — login must not succeed"
        )


# ---------------------------------------------------------------------------
# (d) DELETE last active admin -> 422 (account stays active)
# ---------------------------------------------------------------------------


class TestDeleteLastAdminReturns422:
    """Business rule: cannot delete the sole active admin — 422, account unchanged."""

    @pytest.mark.asyncio
    async def test_delete_last_admin_returns_422(
        self, web_app, pg_conn, monkeypatch
    ):
        """DELETE /api/account/me as the last active admin must return 422.

        Setup: deactivate admin id=21 so id=20 becomes the lone active admin.
        The guard in set_user_active raises LastAdminProtectedError -> HTTP 422.
        """
        import httpx

        # Temporarily deactivate the second admin so id=20 is the last active admin.
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE webui_users SET is_active = FALSE WHERE id = 21"
            )

        monkeypatch.setattr(
            "src.web_ui.routes.account.current_user_id", lambda _req: 20
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/account/me")

        assert resp.status_code == 422, (
            f"Expected 422 for last-admin delete, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body.get("error") == "last_admin_protected", (
            f"Expected error=last_admin_protected, got {body}"
        )

        # Confirm account is still active — the guard must not have committed.
        with pg_conn.cursor() as cur:
            cur.execute("SELECT is_active FROM webui_users WHERE id = 20")
            row = cur.fetchone()
        assert row is not None and row[0] is True, (
            "Admin account must remain active after 422 rejection"
        )

        # Restore second admin for teardown.
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE webui_users SET is_active = TRUE WHERE id = 21"
            )


# ---------------------------------------------------------------------------
# (e) Audit log row is written for the delete action
# ---------------------------------------------------------------------------


class TestDeleteAuditLog:
    """Business rule: every account delete (success or failure) must write an audit row."""

    @pytest.mark.asyncio
    async def test_successful_delete_writes_audit_log(
        self, web_app, pg_conn, monkeypatch
    ):
        """Successful DELETE /api/account/me produces an admin_audit_log row."""
        import httpx

        # Seed a fresh user for this test to avoid interfering with fixture teardown.
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM webui_users WHERE id = 12 OR username IN"
                " ('_ss_audit_id12', 'deleted-user-12')"
            )
            cur.execute(
                "INSERT INTO webui_users"
                " (username, password_hash, is_admin, is_active, id, email, mfa_enabled)"
                " VALUES ('_ss_audit_id12', 'x', FALSE, TRUE, 12,"
                " 'audit12@example.test', FALSE)"
            )

        monkeypatch.setattr(
            "src.web_ui.routes.account.current_user_id", lambda _req: 12
        )

        # Clear any pre-existing audit rows for action=account.delete for this actor.
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM admin_audit_log"
                " WHERE action = 'account.delete' AND target = '12'"
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/account/me")

        assert resp.status_code == 200, (
            f"Expected 200 for delete, got {resp.status_code}"
        )

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT action, target, success"
                "  FROM admin_audit_log"
                " WHERE action = 'account.delete' AND target = '12'"
                " ORDER BY id DESC LIMIT 1"
            )
            audit_row = cur.fetchone()

        assert audit_row is not None, (
            "admin_audit_log must contain a row for account.delete uid=12"
        )
        action, target, success = audit_row
        assert action == "account.delete"
        assert target == "12"
        assert success is True, "Audit row success must be TRUE for successful delete"

        # Cleanup this test's user.
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM webui_users WHERE id = 12"
                " OR username IN ('_ss_audit_id12', 'deleted-user-12')"
            )


# ---------------------------------------------------------------------------
# (f) GET /api/account/export -> 200, structure correct, secrets redacted
# ---------------------------------------------------------------------------


class TestExportMyData:
    """Business rule: GET /api/account/export returns profile + keys + memberships,
    with secrets explicitly REDACTED (no key_hash, password_hash, TOTP secrets)."""

    @pytest.fixture
    def _seed_export_data(self, pg_conn):
        """Seed an API key and usage counter for the normal user (id=10)."""
        # Find the 'free' plan.
        with pg_conn.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = 'free'")
            row = cur.fetchone()
        assert row is not None, "free plan must exist (seeded by migrations)"
        plan_id = row[0]

        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM api_keys WHERE user_id = 10 AND name = '_export_test_key'"
            )
            cur.execute(
                "INSERT INTO api_keys"
                " (name, key_hash, key_prefix, plan_id, user_id, active)"
                " VALUES ('_export_test_key', 'SHOULD_NOT_APPEAR_HASH', 'ex_', %s, 10, TRUE)"
                " RETURNING id",
                (plan_id,),
            )
            key_id = cur.fetchone()[0]
            # Seed a usage counter row.
            cur.execute(
                "INSERT INTO usage_counter (api_key_id, period_yyyymm, call_count)"
                " VALUES (%s, '202601', 42)"
                " ON CONFLICT (api_key_id, period_yyyymm)"
                " DO UPDATE SET call_count = EXCLUDED.call_count",
                (key_id,),
            )

        yield key_id

        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM api_keys WHERE user_id = 10 AND name = '_export_test_key'"
            )

    @pytest.mark.asyncio
    async def test_export_unauthenticated_returns_401(self, web_app, monkeypatch):
        """GET /api/account/export without a session must return 401."""
        import httpx

        monkeypatch.setattr("src.web_ui.auth.is_test_bypass_active", lambda: False)
        monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: None)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/account/export")

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_export_returns_200_with_correct_structure(
        self, web_app, pg_conn, monkeypatch, _seed_export_data
    ):
        """GET /api/account/export returns 200 with profile + api_keys + memberships."""
        import httpx

        monkeypatch.setattr(
            "src.web_ui.routes.account.current_user_id", lambda _req: 10
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/account/export")

        assert resp.status_code == 200, (
            f"Expected 200 for export, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()

        # Top-level structure.
        assert "profile" in body, "export must contain 'profile'"
        assert "api_keys" in body, "export must contain 'api_keys'"
        assert "tenant_memberships" in body, "export must contain 'tenant_memberships'"

        # Profile basics.
        profile = body["profile"]
        assert profile["id"] == 10
        assert profile["username"] == "_ss_normal_id10"
        assert profile["email"] == "normal10@example.test"
        assert "is_active" in profile
        assert "created_at" in profile

        # API keys: at least one key returned.
        assert len(body["api_keys"]) >= 1, "export must include the seeded API key"

        export_key = next(
            (k for k in body["api_keys"] if k.get("key_prefix") == "ex_"), None
        )
        assert export_key is not None, "Seeded key with prefix 'ex_' must appear in export"

        # key_prefix and name must be present.
        assert export_key["key_prefix"] == "ex_"
        assert export_key["name"] == "_export_test_key"

        # Usage must include the seeded row.
        usage = export_key.get("usage", [])
        assert any(u["period_yyyymm"] == "202601" and u["call_count"] == 42 for u in usage), (
            f"Seeded usage row must appear in export: {usage}"
        )

        # tenant_memberships is a list (may be empty for a user with no memberships).
        assert isinstance(body["tenant_memberships"], list)

    @pytest.mark.asyncio
    async def test_export_does_not_expose_secrets(
        self, web_app, pg_conn, monkeypatch, _seed_export_data
    ):
        """GET /api/account/export must NOT contain key_hash, password_hash, or TOTP data."""
        import httpx

        monkeypatch.setattr(
            "src.web_ui.routes.account.current_user_id", lambda _req: 10
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/account/export")

        assert resp.status_code == 200
        raw_body = resp.text

        # Explicit redaction checks — these values must NOT appear anywhere in the
        # response body (not even nested in a string value).
        assert "SHOULD_NOT_APPEAR_HASH" not in raw_body, (
            "key_hash must be REDACTED from export"
        )
        assert "password_hash" not in raw_body, (
            "password_hash field must not appear in export"
        )
        assert "bcrypt_hash_placeholder" not in raw_body, (
            "raw password hash value must not appear in export"
        )
        assert "secret_encrypted" not in raw_body, (
            "TOTP secret_encrypted must not appear in export"
        )
        assert "backup_codes_hash" not in raw_body, (
            "TOTP backup_codes_hash must not appear in export"
        )

        # Also verify structurally: no 'key_hash' key in any api_key dict.
        body = resp.json()
        for key_dict in body.get("api_keys", []):
            assert "key_hash" not in key_dict, (
                f"key_hash must not be in api_key export dict: {key_dict}"
            )
            # The raw hash string must not appear as any value.
            for v in key_dict.values():
                assert v != "SHOULD_NOT_APPEAR_HASH", (
                    "key_hash value must be absent from export"
                )
