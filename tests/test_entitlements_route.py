# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_entitlements_route.py
"""Integration tests for /api/admin/entitlements (WI-5, ADR-0039 §4.1).

Business intent (cases required by spec):
  T1  POST grant non-admin -> 401/403.
  T2  POST grant admin -> 200 + subscription created + admin_audit_log row
      with action='entitlement.grant'.
  T3  POST revoke -> sub status='cancelled' + linked key downgraded to free plan.
  T4  PATCH update -> plan changed.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Uses httpx.AsyncClient + ASGITransport with base_url="http://127.0.0.1".

Admin session:
  The conftest _bypass_webui_auth_for_legacy_tests autouse fixture sets
  WEBUI_AUTH_DISABLED=1 for all files NOT in real_auth_flow_files.  This file
  is NOT in that list, so the bypass is active by default — require_admin
  returns user_id=1 without a real session cookie.

  For the non-admin 403 test, we patch is_test_bypass_active + current_user_id
  and mock auth_store().get_user_field to return is_admin=False (exact pattern
  from test_admin_ee_modules_endpoints.py::TestNonAdmin403).
"""

import os

import httpx
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Module-level env: required before create_app() is called.
# ---------------------------------------------------------------------------
os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-key-ent-tests-32bytes!!!")
os.environ.setdefault("WEBUI_SECURE_COOKIE", "0")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def migrated_pg(pg_conn):
    """Run migrations once per module; yield the pg connection."""
    run_migrations(pg_conn)
    yield pg_conn


@pytest.fixture(autouse=True)
def _clean_test_rows(migrated_pg):
    """Delete test-inserted rows before and after each test."""
    def _wipe():
        for tbl in (
            "admin_audit_log",
            "billing_webhook_events",
            "subscriptions",
            "api_keys",
            "webui_users",
        ):
            try:
                with migrated_pg.cursor() as cur:
                    cur.execute(f"DELETE FROM {tbl}")  # noqa: S608
            except Exception:
                migrated_pg.rollback()
    _wipe()
    yield
    _wipe()


def _create_app():
    from src.web_ui.app import create_app
    return create_app()


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    )


def _seed_user(
    pg_conn,
    *,
    username: str,
    email: str = "user@example.com",
    is_admin: bool = False,
    email_verified: bool = True,
) -> int:
    """Insert a webui_users row and return its id."""
    import bcrypt
    pw_hash = bcrypt.hashpw(b"testpassword", bcrypt.gensalt(rounds=4)).decode()
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, email, is_admin, email_verified)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (username, pw_hash, email, is_admin, email_verified),
        )
        return cur.fetchone()[0]


def _seed_api_key(pg_conn, *, user_id: int, plan_slug: str = "free") -> int:
    """Insert an api_key for the given user on the given plan; return key_id."""
    with pg_conn.cursor() as cur:
        # Resolve plan_id
        cur.execute("SELECT id FROM plans WHERE slug = %s", (plan_slug,))
        plan_row = cur.fetchone()
        if plan_row is None:
            raise ValueError(f"Plan {plan_slug!r} not found")
        plan_id = plan_row[0]
        cur.execute(
            "INSERT INTO api_keys (key_prefix, key_hash, name, user_id, plan_id)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            ("tst_", "testhash_" + str(user_id), f"key_{user_id}", user_id, plan_id),
        )
        return cur.fetchone()[0]


def _count_audit_rows(pg_conn, action: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM admin_audit_log WHERE action = %s",
            (action,),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# T1: non-admin -> 401/403
# ---------------------------------------------------------------------------

class TestNonAdminRejected:
    """T1: POST /api/admin/entitlements requires admin — non-admin gets 403."""

    @pytest.mark.asyncio
    async def test_non_admin_returns_403(self, migrated_pg):
        """Patch bypass off + current_user_id to non-admin uid; mock DB: is_admin=False."""
        from unittest.mock import MagicMock, patch

        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/admin/entitlements",
            "headers": [],
            "query_string": b"",
        }
        fake_request = StarletteRequest(scope)

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        raised: HTTPException | None = None
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: 99

            mock_store = MagicMock()
            mock_store.get_user_field.return_value = False  # is_admin=False

            with patch("src.db.pg.auth_store", return_value=mock_store):
                try:
                    await auth_mod.require_admin(fake_request)
                except HTTPException as exc:
                    raised = exc
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert raised is not None, "require_admin must raise HTTPException for non-admin"
        assert raised.status_code == 403, (
            f"Expected 403 for non-admin, got {raised.status_code}"
        )

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self, migrated_pg):
        """Patch bypass off + current_user_id returns None → 401."""
        from unittest.mock import patch

        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/admin/entitlements",
            "headers": [],
            "query_string": b"",
        }
        fake_request = StarletteRequest(scope)

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        raised: HTTPException | None = None
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: None  # not logged in

            with patch("src.db.pg.auth_store"):
                try:
                    await auth_mod.require_admin(fake_request)
                except HTTPException as exc:
                    raised = exc
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert raised is not None, "require_admin must raise HTTPException for unauthenticated"
        assert raised.status_code == 401, (
            f"Expected 401 for unauthenticated, got {raised.status_code}"
        )


# ---------------------------------------------------------------------------
# T2: admin grant -> 200 + subscription + audit_log row
# ---------------------------------------------------------------------------

class TestAdminGrant:
    """T2: Admin POST grant creates subscription + audit_log row."""

    @pytest.mark.asyncio
    async def test_grant_returns_200(self, migrated_pg):
        app = _create_app()
        async with _client(app) as client:
            resp = await client.post(
                "/api/admin/entitlements",
                json={
                    "email": "grantee@example.com",
                    "plan_slug": "pro",
                    "seats": 1,
                    "source": "admin",
                },
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "subscription_id" in data
        assert data.get("status") == "active"

    @pytest.mark.asyncio
    async def test_grant_creates_subscription_row(self, migrated_pg):
        app = _create_app()
        email = "grantee_db@example.com"
        async with _client(app) as client:
            resp = await client.post(
                "/api/admin/entitlements",
                json={"email": email, "plan_slug": "pro"},
            )
        assert resp.status_code == 200, resp.text
        sub_id = resp.json()["subscription_id"]

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT status, buyer_email FROM subscriptions WHERE id = %s", (sub_id,))
            row = cur.fetchone()
        assert row is not None, "Subscription row must be created"
        status, buyer_email = row
        assert status == "active"
        assert buyer_email == email

    @pytest.mark.asyncio
    async def test_grant_writes_audit_log(self, migrated_pg):
        app = _create_app()
        async with _client(app) as client:
            resp = await client.post(
                "/api/admin/entitlements",
                json={"email": "audit_check@example.com", "plan_slug": "pro"},
            )
        assert resp.status_code == 200, resp.text

        count = _count_audit_rows(migrated_pg, "entitlement.grant")
        assert count >= 1, (
            f"Expected at least 1 entitlement.grant audit row, got {count}"
        )

    @pytest.mark.asyncio
    async def test_grant_with_custom_external_ref(self, migrated_pg):
        app = _create_app()
        custom_ref = "promo-2024-launch-001"
        async with _client(app) as client:
            resp = await client.post(
                "/api/admin/entitlements",
                json={
                    "email": "custom_ref@example.com",
                    "plan_slug": "pro",
                    "external_ref": custom_ref,
                    "source": "promo",
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["external_ref"] == custom_ref

    @pytest.mark.asyncio
    async def test_grant_unknown_plan_returns_404(self, migrated_pg):
        app = _create_app()
        async with _client(app) as client:
            resp = await client.post(
                "/api/admin/entitlements",
                json={"email": "noplan@example.com", "plan_slug": "nonexistent_plan_xyz"},
            )
        assert resp.status_code == 404, f"Expected 404 for unknown plan, got {resp.status_code}"

    @pytest.mark.asyncio
    async def test_grant_idempotent_on_external_ref(self, migrated_pg):
        """Same external_ref twice must not create duplicate subscriptions."""
        app = _create_app()
        ref = "idem-ref-001"
        async with _client(app) as client:
            r1 = await client.post(
                "/api/admin/entitlements",
                json={"email": "idem@example.com", "plan_slug": "pro", "external_ref": ref},
            )
            r2 = await client.post(
                "/api/admin/entitlements",
                json={"email": "idem@example.com", "plan_slug": "pro", "external_ref": ref},
            )
        assert r1.status_code == 200
        assert r2.status_code == 200

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE external_ref = %s", (ref,)
            )
            count = cur.fetchone()[0]
        assert count == 1, f"Idempotent grant must produce exactly 1 row, got {count}"


# ---------------------------------------------------------------------------
# T3: revoke -> sub cancelled + key downgraded
# ---------------------------------------------------------------------------

class TestAdminRevoke:
    """T3: Admin revoke marks subscription cancelled and downgrades linked key."""

    @pytest.mark.asyncio
    async def test_revoke_cancels_subscription(self, migrated_pg):
        app = _create_app()
        ref = "revoke-ref-001"
        async with _client(app) as client:
            # Grant first
            gr = await client.post(
                "/api/admin/entitlements",
                json={"email": "revoke@example.com", "plan_slug": "pro", "external_ref": ref},
            )
            assert gr.status_code == 200, gr.text

            # Revoke
            rv = await client.post(f"/api/admin/entitlements/{ref}/revoke")
        assert rv.status_code == 200, f"Expected 200, got {rv.status_code}: {rv.text}"
        assert rv.json().get("status") == "cancelled"

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT status FROM subscriptions WHERE external_ref = %s", (ref,)
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "cancelled", f"Expected status='cancelled', got {row[0]!r}"

    @pytest.mark.asyncio
    async def test_revoke_downgrades_claimed_key(self, migrated_pg):
        """When a sub is claimed, revoke must downgrade the linked API key to free."""
        app = _create_app()
        ref = "revoke-key-downgrade-001"
        email = "claimed_revoke@example.com"

        # Create a user + api_key on 'pro' plan
        user_id = _seed_user(migrated_pg, username="revoke_user", email=email)
        key_id = _seed_api_key(migrated_pg, user_id=user_id, plan_slug="pro")

        # Grant + link subscription to user + key manually via activation
        from src.billing.activation import EntitlementGrant, grant_entitlement
        from src.db.pg import subscription_store

        # Resolve plan_id
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = 'pro'")
            pro_plan_id = cur.fetchone()[0]

        grant = EntitlementGrant(
            plan_id=pro_plan_id,
            external_ref=ref,
            source="admin",
            buyer_email=email,
        )
        sub_id = grant_entitlement(grant)

        # Link the key to the sub (simulating claimed state)
        subs = subscription_store()
        subs.link_to_api_key(sub_id, key_id)
        subs.link_to_user(sub_id, user_id)

        async with _client(app) as client:
            rv = await client.post(f"/api/admin/entitlements/{ref}/revoke")
        assert rv.status_code == 200, rv.text

        # Verify key is now on free plan
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT p.slug FROM api_keys ak"
                " JOIN plans p ON p.id = ak.plan_id"
                " WHERE ak.id = %s",
                (key_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "free", (
            f"Revoked sub's api_key must be downgraded to 'free', got {row[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_revoke_unknown_ref_returns_404(self, migrated_pg):
        app = _create_app()
        async with _client(app) as client:
            rv = await client.post("/api/admin/entitlements/nonexistent-ref-xyz/revoke")
        assert rv.status_code == 404, f"Expected 404 for unknown ref, got {rv.status_code}"

    @pytest.mark.asyncio
    async def test_revoke_writes_audit_log(self, migrated_pg):
        app = _create_app()
        ref = "revoke-audit-001"
        async with _client(app) as client:
            await client.post(
                "/api/admin/entitlements",
                json={"email": "revoke_audit@example.com", "plan_slug": "pro", "external_ref": ref},
            )
            await client.post(f"/api/admin/entitlements/{ref}/revoke")

        count = _count_audit_rows(migrated_pg, "entitlement.revoke")
        assert count >= 1, f"Expected entitlement.revoke audit row, got {count}"


# ---------------------------------------------------------------------------
# T4: PATCH update -> plan changed
# ---------------------------------------------------------------------------

class TestAdminUpdate:
    """T4: PATCH /api/admin/entitlements/{ref} updates plan/status/seats."""

    @pytest.mark.asyncio
    async def test_patch_updates_seats(self, migrated_pg):
        app = _create_app()
        ref = "update-ref-001"
        async with _client(app) as client:
            await client.post(
                "/api/admin/entitlements",
                json={"email": "update@example.com", "plan_slug": "pro",
                      "external_ref": ref, "seats": 1},
            )
            resp = await client.patch(
                f"/api/admin/entitlements/{ref}",
                json={"seats": 3},
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT seats FROM subscriptions WHERE external_ref = %s", (ref,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 3, f"Expected seats=3, got {row[0]}"

    @pytest.mark.asyncio
    async def test_patch_plan_change(self, migrated_pg):
        app = _create_app()
        ref = "update-plan-001"
        async with _client(app) as client:
            await client.post(
                "/api/admin/entitlements",
                json={"email": "planchange@example.com", "plan_slug": "pro",
                      "external_ref": ref},
            )
            resp = await client.patch(
                f"/api/admin/entitlements/{ref}",
                json={"plan_slug": "free"},
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT p.slug FROM subscriptions s JOIN plans p ON p.id = s.plan_id"
                " WHERE s.external_ref = %s",
                (ref,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "free", f"Expected plan='free' after PATCH, got {row[0]!r}"

    @pytest.mark.asyncio
    async def test_patch_no_fields_returns_400(self, migrated_pg):
        app = _create_app()
        ref = "update-empty-001"
        async with _client(app) as client:
            await client.post(
                "/api/admin/entitlements",
                json={"email": "noupdate@example.com", "plan_slug": "pro",
                      "external_ref": ref},
            )
            resp = await client.patch(
                f"/api/admin/entitlements/{ref}",
                json={},
            )
        assert resp.status_code == 400, (
            f"Expected 400 when no fields provided, got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_patch_unknown_ref_returns_404(self, migrated_pg):
        app = _create_app()
        async with _client(app) as client:
            resp = await client.patch(
                "/api/admin/entitlements/nonexistent-ref-999",
                json={"seats": 2},
            )
        assert resp.status_code == 404, (
            f"Expected 404 for unknown ref, got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_patch_invalid_status_returns_422(self, migrated_pg):
        """I10: an invalid status must be rejected at the Pydantic layer with 422,
        not reach the DB CHECK and surface as a 500."""
        app = _create_app()
        ref = "update-badstatus-001"
        async with _client(app) as client:
            await client.post(
                "/api/admin/entitlements",
                json={"email": "badstatus@example.com", "plan_slug": "pro",
                      "external_ref": ref},
            )
            resp = await client.patch(
                f"/api/admin/entitlements/{ref}",
                json={"status": "not_a_real_status"},
            )
        assert resp.status_code == 422, (
            f"Expected 422 for invalid status, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_patch_writes_audit_log(self, migrated_pg):
        app = _create_app()
        ref = "update-audit-001"
        async with _client(app) as client:
            await client.post(
                "/api/admin/entitlements",
                json={"email": "update_audit@example.com", "plan_slug": "pro",
                      "external_ref": ref},
            )
            await client.patch(
                f"/api/admin/entitlements/{ref}",
                json={"seats": 2},
            )

        count = _count_audit_rows(migrated_pg, "entitlement.update")
        assert count >= 1, f"Expected entitlement.update audit row, got {count}"


# ---------------------------------------------------------------------------
# T5: GET list subscriptions (basic)
# ---------------------------------------------------------------------------

class TestAdminList:
    """GET /api/admin/entitlements returns subscriptions list."""

    @pytest.mark.asyncio
    async def test_list_returns_subscriptions_key(self, migrated_pg):
        app = _create_app()
        async with _client(app) as client:
            resp = await client.get("/api/admin/entitlements")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "subscriptions" in data, "Response must have 'subscriptions' key"
        assert isinstance(data["subscriptions"], list)

    @pytest.mark.asyncio
    async def test_list_shows_granted_subscription(self, migrated_pg):
        app = _create_app()
        async with _client(app) as client:
            await client.post(
                "/api/admin/entitlements",
                json={"email": "listcheck@example.com", "plan_slug": "pro"},
            )
            list_resp = await client.get("/api/admin/entitlements")
        assert list_resp.status_code == 200
        subs = list_resp.json()["subscriptions"]
        emails = [s.get("buyer_email") for s in subs]
        assert "listcheck@example.com" in emails

    @pytest.mark.asyncio
    async def test_list_includes_plan_slug_from_join(self, migrated_pg):
        """#11: list uses subscription_store().list_all() which LEFT JOINs plans,
        so each row has plan_slug / plan_name (no SELECT *)."""
        app = _create_app()
        async with _client(app) as client:
            await client.post(
                "/api/admin/entitlements",
                json={"email": "planslug@example.com", "plan_slug": "pro"},
            )
            list_resp = await client.get("/api/admin/entitlements")
        assert list_resp.status_code == 200
        subs = list_resp.json()["subscriptions"]
        pro_rows = [s for s in subs if s.get("buyer_email") == "planslug@example.com"]
        assert pro_rows, "Granted subscription must appear in list"
        row = pro_rows[0]
        assert row.get("plan_slug") == "pro", (
            f"Expected plan_slug='pro' from list_all() JOIN, got {row.get('plan_slug')!r}"
        )
        assert row.get("plan_name") is not None, (
            "plan_name from LEFT JOIN must be present (not None)"
        )


# ---------------------------------------------------------------------------
# T6: Fresh-MFA gate — mutating routes require fresh MFA (#7)
# ---------------------------------------------------------------------------


class TestFreshMfaGate:
    """#7: grant/revoke/update require fresh MFA; GET list does not.

    WEBUI_AUTH_DISABLED bypass is active for this file, which means
    require_admin_with_fresh_mfa would normally bypass the MFA check too.
    To test the gate itself we disable the bypass and call the dependency
    directly — same pattern as TestNonAdminRejected above.

    The three mutating routes (POST grant, POST revoke, PATCH update) must
    raise HTTPException 403 with detail.error='mfa_freshness_required' when
    the admin session has no mfa_verified_at.  The GET list route (read-only)
    uses require_admin and MUST NOT raise 403 for this reason.
    """

    @pytest.mark.asyncio
    async def test_fresh_mfa_raises_403_when_mfa_not_set(self, migrated_pg):
        """require_admin_with_fresh_mfa → 403 when mfa_verified_at absent in session.

        session is injected via scope['session'] (the Starlette-native mechanism
        used by SessionMiddleware) rather than via _state.
        """
        from unittest.mock import MagicMock, patch

        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod
        from src.web_ui.auth import STEP_UP_ERROR_CODE

        # Starlette reads request.session from scope["session"] when SessionMiddleware
        # is NOT present — injecting directly into scope lets us set the session dict
        # without running the full ASGI stack.
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/admin/entitlements",
            "headers": [],
            "query_string": b"",
            "session": {},  # no mfa_verified_at
        }
        fake_request = StarletteRequest(scope)

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        raised: HTTPException | None = None
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: 1  # logged in as admin

            mock_store = MagicMock()
            mock_store.get_user_field.return_value = True  # is_admin=True

            with patch("src.db.pg.auth_store", return_value=mock_store):
                try:
                    await auth_mod.require_admin_with_fresh_mfa(fake_request)
                except HTTPException as exc:
                    raised = exc
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert raised is not None, (
            "require_admin_with_fresh_mfa must raise HTTPException when MFA not set"
        )
        assert raised.status_code == 403, (
            f"Expected 403 for stale MFA, got {raised.status_code}"
        )
        detail = raised.detail
        assert isinstance(detail, dict) and detail.get("error") == STEP_UP_ERROR_CODE, (
            f"Expected detail.error={STEP_UP_ERROR_CODE!r}, got {detail!r}"
        )

    @pytest.mark.asyncio
    async def test_fresh_mfa_passes_with_valid_timestamp(self, migrated_pg):
        """require_admin_with_fresh_mfa → returns user_id when mfa_verified_at is fresh."""
        import time
        from unittest.mock import MagicMock, patch

        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/admin/entitlements",
            "headers": [],
            "query_string": b"",
            "session": {"mfa_verified_at": str(time.time())},  # fresh timestamp
        }
        fake_request = StarletteRequest(scope)

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: 1

            mock_store = MagicMock()
            mock_store.get_user_field.return_value = True  # is_admin=True

            with patch("src.db.pg.auth_store", return_value=mock_store):
                user_id = await auth_mod.require_admin_with_fresh_mfa(fake_request)
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert user_id == 1, (
            f"Expected user_id=1 for fresh MFA admin, got {user_id}"
        )

    @pytest.mark.asyncio
    async def test_get_list_uses_require_admin_not_fresh_mfa(self, migrated_pg):
        """GET /api/admin/entitlements (read-only) uses require_admin — not fresh MFA.

        With WEBUI_AUTH_DISABLED=1 (active for this file), even without an MFA
        timestamp the list route returns 200.  This verifies the route Depends
        is still require_admin (not require_admin_with_fresh_mfa).
        """
        app = _create_app()
        async with _client(app) as client:
            resp = await client.get("/api/admin/entitlements")
        assert resp.status_code == 200, (
            f"GET list (read-only) must not require fresh MFA; got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# T7: grant 422 on business-rule violation (CR2)
# ---------------------------------------------------------------------------


class TestGrantBusinessRuleViolation:
    """CR2: grant raises 422 (not 500) on team min-seats violation."""

    @pytest.mark.asyncio
    async def test_grant_team_below_min_seats_returns_422(self, migrated_pg):
        """Granting a 'team' plan with seats=1 (below min) must return 422.

        Business rule: team tier requires >= billing.team_min_seats (default 3).
        activation._enforce_team_min_seats raises ValueError → route maps to 422.
        WEBUI_AUTH_DISABLED=1 is active so auth passes; the 422 comes from
        the business-rule enforcement in activation.grant_entitlement.
        """
        app = _create_app()
        async with _client(app) as client:
            resp = await client.post(
                "/api/admin/entitlements",
                json={
                    "email": "teammin@example.com",
                    "plan_slug": "team",
                    "seats": 1,  # below default min of 3
                    "source": "admin",
                },
            )
        assert resp.status_code == 422, (
            f"Expected 422 for team plan with seats=1 (below min), "
            f"got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_grant_team_at_min_seats_succeeds(self, migrated_pg):
        """Granting 'team' with seats=3 (at the default minimum) must succeed."""
        app = _create_app()
        async with _client(app) as client:
            resp = await client.post(
                "/api/admin/entitlements",
                json={
                    "email": "teamok@example.com",
                    "plan_slug": "team",
                    "seats": 3,
                    "source": "admin",
                },
            )
        assert resp.status_code == 200, (
            f"Expected 200 for team plan with seats=3 (at min), "
            f"got {resp.status_code}: {resp.text}"
        )
