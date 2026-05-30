# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for WI-1 pricing-model backend data layer.

12 test cases covering:
 1. test_migration_idempotent_pricing_model   migration m13_015 is idempotent
 2. test_migration_idempotent_min_seats       migration m13_016 is idempotent + seeds correct values
 3. test_plans_api_includes_pricing_model     GET /api/plans returns pricing_model per plan
 4. test_plans_api_includes_min_seats         GET /api/plans returns min_seats per plan
 5. test_plans_api_includes_team_min_seats    GET /api/plans returns top-level team_min_seats
 6. test_plans_api_no_auth_required           GET /api/plans is public (no session)
 7. test_admin_patch_pricing_model_accept     PATCH /api/admin/plans/{slug} accepts flat/per_seat
 8. test_admin_patch_pricing_model_reject     PATCH /api/admin/plans/{slug} rejects invalid value
 9. test_admin_patch_min_seats_accept         PATCH /api/admin/plans/{slug} accepts min_seats int
10. test_admin_patch_min_seats_null           PATCH /api/admin/plans/{slug} accepts null min_seats
11. test_admin_patch_min_seats_reject_zero    PATCH /api/admin/plans/{slug} rejects min_seats < 1
12. test_site_config_public_no_auth           GET /api/site-config is public, returns config keys

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
WEBUI_AUTH_DISABLED is active via conftest autouse; admin endpoints return user_id=1.
"""
from __future__ import annotations

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply all migrations on a clean schema once per test."""
    run_migrations(clean_pg)
    return clean_pg


def _client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# 1. Migration idempotency
# ---------------------------------------------------------------------------


class TestMigrationIdempotent:
    @pytest.mark.asyncio
    async def test_migration_idempotent_pricing_model(self, migrated_pg):
        """m13_015 re-run must not error and must leave pricing_model column intact.

        Asserts the migration is safe to re-run (idempotent) — critical for
        production deployments where migrations run at every restart.
        """
        from src.db.migrate import run_migrations
        # Second run — must not raise
        run_migrations(migrated_pg)

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT slug, pricing_model FROM plans WHERE slug IN ('pro', 'team', 'free')"
                " ORDER BY slug"
            )
            rows = {r[0]: r[1] for r in cur.fetchall()}

        assert rows["free"] == "flat", (
            f"Expected free.pricing_model='flat', got {rows['free']!r}"
        )
        assert rows["pro"] == "per_seat", (
            f"Expected pro.pricing_model='per_seat', got {rows['pro']!r}"
        )
        assert rows["team"] == "per_seat", (
            f"Expected team.pricing_model='per_seat', got {rows['team']!r}"
        )

    @pytest.mark.asyncio
    async def test_migration_idempotent_min_seats(self, migrated_pg):
        """m13_016 re-run must not error and must leave min_seats seeds intact.

        Asserts:
        - Column exists with correct seeds (team=3, pro=1, free=NULL).
        - Second run is a no-op (UPDATE guards ensure already-set values are
          not reverted by the seed UPDATEs).
        """
        from src.db.migrate import run_migrations
        # Second run — must not raise
        run_migrations(migrated_pg)

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT slug, min_seats FROM plans WHERE slug IN ('pro', 'team', 'free')"
                " ORDER BY slug"
            )
            rows = {r[0]: r[1] for r in cur.fetchall()}

        assert rows.get("free") is None, (
            f"Expected free.min_seats=NULL, got {rows.get('free')!r}"
        )
        assert rows.get("pro") == 1, (
            f"Expected pro.min_seats=1, got {rows.get('pro')!r}"
        )
        assert rows.get("team") == 3, (
            f"Expected team.min_seats=3, got {rows.get('team')!r}"
        )


# ---------------------------------------------------------------------------
# 2. GET /api/plans — pricing_model per plan
# ---------------------------------------------------------------------------


class TestPlansApiPricingModel:
    @pytest.mark.asyncio
    async def test_plans_api_includes_pricing_model(self, migrated_pg):
        """GET /api/plans returns pricing_model field on each plan (m13_015).

        After m13_015: free='flat', pro='per_seat', team='per_seat'.
        The unlimited plan has is_public=FALSE so it is excluded from /api/plans.
        """
        async with _client() as client:
            resp = await client.get("/api/plans")

        assert resp.status_code == 200, (
            f"Expected 200 from /api/plans, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "plans" in body, f"Expected 'plans' key in response, got: {list(body.keys())}"
        plans = body["plans"]
        assert isinstance(plans, list) and len(plans) >= 1

        by_slug = {p["slug"]: p for p in plans}

        # Every public plan must have pricing_model field
        for slug, plan in by_slug.items():
            assert "pricing_model" in plan, (
                f"Plan {slug!r} missing 'pricing_model' field in /api/plans response"
            )
            assert plan["pricing_model"] in ("flat", "per_seat"), (
                f"Plan {slug!r} has unexpected pricing_model={plan['pricing_model']!r}"
            )

        # Validate seed values — only check if these plans are public
        if "free" in by_slug:
            assert by_slug["free"]["pricing_model"] == "flat", (
                f"Expected free.pricing_model='flat', got {by_slug['free']['pricing_model']!r}"
            )
        if "pro" in by_slug:
            assert by_slug["pro"]["pricing_model"] == "per_seat", (
                f"Expected pro.pricing_model='per_seat', got {by_slug['pro']['pricing_model']!r}"
            )
        if "team" in by_slug:
            assert by_slug["team"]["pricing_model"] == "per_seat", (
                f"Expected team.pricing_model='per_seat', got {by_slug['team']['pricing_model']!r}"
            )


# ---------------------------------------------------------------------------
# 3. GET /api/plans — min_seats per plan (m13_016)
# ---------------------------------------------------------------------------


class TestPlansApiMinSeats:
    @pytest.mark.asyncio
    async def test_plans_api_includes_min_seats(self, migrated_pg):
        """GET /api/plans returns min_seats per plan (display SSOT, m13_016).

        After m13_016: team=3, pro=1, free=None (null).
        min_seats must be inside the plan dict, NOT at the response top level.
        """
        async with _client() as client:
            resp = await client.get("/api/plans")

        assert resp.status_code == 200, (
            f"Expected 200 from /api/plans, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "plans" in body
        plans = body["plans"]

        by_slug = {p["slug"]: p for p in plans}

        # Every public plan must have min_seats field (may be null)
        for slug, plan in by_slug.items():
            assert "min_seats" in plan, (
                f"Plan {slug!r} missing 'min_seats' field in /api/plans response"
            )
            ms = plan["min_seats"]
            assert ms is None or isinstance(ms, int), (
                f"Plan {slug!r} min_seats must be int or null, got {type(ms).__name__}"
            )

        # Validate seed values
        if "team" in by_slug:
            assert by_slug["team"]["min_seats"] == 3, (
                f"Expected team.min_seats=3, got {by_slug['team']['min_seats']!r}"
            )
        if "pro" in by_slug:
            assert by_slug["pro"]["min_seats"] == 1, (
                f"Expected pro.min_seats=1, got {by_slug['pro']['min_seats']!r}"
            )
        if "free" in by_slug:
            assert by_slug["free"]["min_seats"] is None, (
                f"Expected free.min_seats=None, got {by_slug['free']['min_seats']!r}"
            )


# ---------------------------------------------------------------------------
# 4. GET /api/plans — team_min_seats at top level
# ---------------------------------------------------------------------------


class TestPlansApiTeamMinSeats:
    @pytest.mark.asyncio
    async def test_plans_api_includes_team_min_seats(self, migrated_pg):
        """GET /api/plans includes top-level team_min_seats (WI-1 contract).

        team_min_seats is a global setting (billing.team_min_seats, default 3).
        It must appear at the RESPONSE top level, NOT inside a plan object.
        WI-5 (pricing frontend) depends on this contract.
        """
        async with _client() as client:
            resp = await client.get("/api/plans")

        assert resp.status_code == 200
        body = resp.json()

        # team_min_seats must be a top-level key, NOT inside any plan
        assert "team_min_seats" in body, (
            f"Expected 'team_min_seats' at response top level, got keys: {list(body.keys())}"
        )
        assert isinstance(body["team_min_seats"], int), (
            f"Expected team_min_seats to be int, got {type(body['team_min_seats']).__name__}"
        )
        assert body["team_min_seats"] >= 1, (
            f"team_min_seats must be >= 1, got {body['team_min_seats']}"
        )

        # Must NOT be inside plan dicts (it is a global setting, not per-plan)
        for plan in body.get("plans", []):
            assert "team_min_seats" not in plan, (
                f"team_min_seats must not appear inside plan {plan.get('slug')!r}"
            )


# ---------------------------------------------------------------------------
# 4. GET /api/plans — public (no auth)
# ---------------------------------------------------------------------------


class TestPlansApiPublic:
    @pytest.mark.asyncio
    async def test_plans_api_no_auth_required(self, migrated_pg):
        """GET /api/plans must be accessible without a session cookie.

        The middleware exempts /api/plans exactly; any auth regression would
        return 401 here.  This test verifies the public-no-auth invariant is
        intact after WI-1 changes.
        """
        import os
        # Temporarily re-enable auth to verify the endpoint is truly exempt
        prev = os.environ.pop("WEBUI_AUTH_DISABLED", None)
        try:
            async with _client() as client:
                # No cookies/session set — raw unauthenticated request
                resp = await client.get("/api/plans")
            assert resp.status_code == 200, (
                f"Expected 200 from public /api/plans without auth, "
                f"got {resp.status_code}: {resp.text}"
            )
        finally:
            if prev is not None:
                os.environ["WEBUI_AUTH_DISABLED"] = prev


# ---------------------------------------------------------------------------
# 5. PATCH /api/admin/plans — pricing_model accept
# ---------------------------------------------------------------------------


class TestAdminPatchPricingModelAccept:
    @pytest.mark.asyncio
    async def test_admin_patch_pricing_model_flat(self, migrated_pg):
        """PATCH /api/admin/plans/pro with pricing_model='flat' persists to DB."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/plans/pro",
                json={"pricing_model": "flat", "reason": "switch pro to flat for test"},
            )
        assert resp.status_code == 200, (
            f"Expected 200 on pricing_model PATCH, got {resp.status_code}: {resp.text}"
        )
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT pricing_model FROM plans WHERE slug = 'pro'")
            row = cur.fetchone()
        assert row[0] == "flat", f"Expected pricing_model='flat' in DB, got {row[0]!r}"
        # Restore
        with migrated_pg.cursor() as cur:
            cur.execute("UPDATE plans SET pricing_model = 'per_seat' WHERE slug = 'pro'")

    @pytest.mark.asyncio
    async def test_admin_patch_pricing_model_per_seat(self, migrated_pg):
        """PATCH /api/admin/plans/free with pricing_model='per_seat' persists to DB."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/plans/free",
                json={"pricing_model": "per_seat", "reason": "switch free to per_seat for test"},
            )
        assert resp.status_code == 200, (
            f"Expected 200 on pricing_model PATCH, got {resp.status_code}: {resp.text}"
        )
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT pricing_model FROM plans WHERE slug = 'free'")
            row = cur.fetchone()
        assert row[0] == "per_seat"
        # Restore
        with migrated_pg.cursor() as cur:
            cur.execute("UPDATE plans SET pricing_model = 'flat' WHERE slug = 'free'")


# ---------------------------------------------------------------------------
# 6. PATCH /api/admin/plans — pricing_model reject invalid value
# ---------------------------------------------------------------------------


class TestAdminPatchPricingModelReject:
    @pytest.mark.asyncio
    async def test_admin_patch_pricing_model_invalid_422(self, migrated_pg):
        """PATCH with invalid pricing_model value returns 422 (Pydantic validation)."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/plans/pro",
                json={"pricing_model": "per_unit", "reason": "invalid model test"},
            )
        assert resp.status_code == 422, (
            f"Expected 422 for invalid pricing_model, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_admin_plans_list_includes_pricing_model(self, migrated_pg):
        """GET /api/admin/plans includes pricing_model and min_seats fields in each plan."""
        async with _client() as client:
            resp = await client.get("/api/admin/plans")
        assert resp.status_code == 200
        body = resp.json()
        for plan in body.get("plans", []):
            assert "pricing_model" in plan, (
                f"Admin plan {plan.get('slug')!r} missing pricing_model field"
            )
            assert "min_seats" in plan, (
                f"Admin plan {plan.get('slug')!r} missing min_seats field"
            )


# ---------------------------------------------------------------------------
# 7. PATCH /api/admin/plans — min_seats accept/null/reject
# ---------------------------------------------------------------------------


class TestAdminPatchMinSeats:
    @pytest.mark.asyncio
    async def test_admin_patch_min_seats_accept(self, migrated_pg):
        """PATCH min_seats=5 for team persists to DB (m13_016)."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/plans/team",
                json={"min_seats": 5, "reason": "set min_seats to 5 for test"},
            )
        assert resp.status_code == 200, (
            f"Expected 200 on min_seats PATCH, got {resp.status_code}: {resp.text}"
        )
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT min_seats FROM plans WHERE slug = 'team'")
            row = cur.fetchone()
        assert row[0] == 5, f"Expected min_seats=5 in DB, got {row[0]!r}"
        # Restore
        with migrated_pg.cursor() as cur:
            cur.execute("UPDATE plans SET min_seats = 3 WHERE slug = 'team'")

    @pytest.mark.asyncio
    async def test_admin_patch_min_seats_null(self, migrated_pg):
        """PATCH min_seats=null clears the minimum (sets to NULL in DB)."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/plans/team",
                json={"min_seats": None, "reason": "clear min_seats for test"},
            )
        assert resp.status_code == 200, (
            f"Expected 200 on min_seats null PATCH, got {resp.status_code}: {resp.text}"
        )
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT min_seats FROM plans WHERE slug = 'team'")
            row = cur.fetchone()
        assert row[0] is None, f"Expected min_seats=NULL in DB, got {row[0]!r}"
        # Restore
        with migrated_pg.cursor() as cur:
            cur.execute("UPDATE plans SET min_seats = 3 WHERE slug = 'team'")

    @pytest.mark.asyncio
    async def test_admin_patch_min_seats_reject_zero(self, migrated_pg):
        """PATCH min_seats=0 must be rejected (ge=1 constraint)."""
        async with _client() as client:
            resp = await client.patch(
                "/api/admin/plans/team",
                json={"min_seats": 0, "reason": "invalid min_seats test"},
            )
        assert resp.status_code == 422, (
            f"Expected 422 for min_seats=0, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# 8. GET /api/site-config — public, returns helpdesk_url + site_version
# ---------------------------------------------------------------------------


class TestSiteConfigEndpoint:
    @pytest.mark.asyncio
    async def test_site_config_public_no_auth(self, migrated_pg):
        """GET /api/site-config is public and returns helpdesk_url + site_version.

        Validates:
        - HTTP 200 without session (middleware exempts the path)
        - Response has 'helpdesk_url' key (non-empty string, valid URL-ish)
        - Response has 'site_version' key (non-empty string)
        """
        import os
        # Temporarily re-enable auth to verify the endpoint is truly exempt
        prev = os.environ.pop("WEBUI_AUTH_DISABLED", None)
        try:
            async with _client() as client:
                resp = await client.get("/api/site-config")
            assert resp.status_code == 200, (
                f"Expected 200 from public /api/site-config, "
                f"got {resp.status_code}: {resp.text}"
            )
        finally:
            if prev is not None:
                os.environ["WEBUI_AUTH_DISABLED"] = prev

        body = resp.json()
        assert "helpdesk_url" in body, (
            f"Expected 'helpdesk_url' in /api/site-config response, keys: {list(body.keys())}"
        )
        assert isinstance(body["helpdesk_url"], str) and body["helpdesk_url"], (
            f"helpdesk_url must be a non-empty string, got: {body['helpdesk_url']!r}"
        )
        assert body["helpdesk_url"].startswith("http"), (
            f"helpdesk_url should be an http(s) URL, got: {body['helpdesk_url']!r}"
        )

        assert "site_version" in body, (
            f"Expected 'site_version' in /api/site-config response, keys: {list(body.keys())}"
        )
        assert isinstance(body["site_version"], str) and body["site_version"], (
            f"site_version must be a non-empty string, got: {body['site_version']!r}"
        )

    @pytest.mark.asyncio
    async def test_site_config_helpdesk_url_is_catalogue_default(self, migrated_pg):
        """GET /api/site-config returns the catalogue default helpdesk URL.

        When no app_settings override exists for support.helpdesk_url,
        the endpoint must fall back to the catalogue default
        ('https://viindoo.com/ticket/team/88') so Astro always has a usable URL.
        """
        async with _client() as client:
            resp = await client.get("/api/site-config")

        assert resp.status_code == 200
        body = resp.json()
        # Catalogue default — if no DB override exists, this is what's returned
        assert "viindoo.com" in body["helpdesk_url"], (
            f"Expected helpdesk_url to contain 'viindoo.com' (catalogue default), "
            f"got: {body['helpdesk_url']!r}"
        )
