# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_w4_versions.py
"""Integration tests for GET /api/versions — W4 Wave 4.

Business intent:
  - Endpoint returns versions read from bootstrap_versions.json (data-driven, NOT hardcoded).
  - Versions are sorted numerically: 8.0 < 9.0 < 10.0 < ... (NOT lexicographic "10.0"<"9.0").
  - Any authenticated user (admin or non-admin) may call the endpoint.
  - Unauthenticated request → 401.
  - If IndexAllBody gains gc field, body accepts gc + worker fields with safe defaults.

Markers: pytest.mark.postgres (app creation needs DB — create_app acquires PG pool).
"""
import json
from pathlib import Path

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres

# Path to the canonical data source so tests stay data-driven.
_BOOTSTRAP_JSON = (
    Path(__file__).parent.parent
    / "src" / "indexer" / "spec_data" / "bootstrap_versions.json"
)


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _expected_versions() -> list[str]:
    """Read and sort versions from bootstrap_versions.json — same logic as endpoint."""
    raw = _BOOTSTRAP_JSON.read_text(encoding="utf-8")
    data = json.loads(raw)
    return sorted(data["versions"].keys(), key=lambda v: float(v))


class TestVersionsEndpoint:
    @pytest.mark.asyncio
    async def test_returns_200_for_authenticated_user(self, migrated_pg):
        """GET /api/versions with auth bypass active → 200 JSON."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/versions")

        assert resp.status_code == 200
        body = resp.json()
        assert "versions" in body
        assert isinstance(body["versions"], list)

    @pytest.mark.asyncio
    async def test_versions_match_bootstrap_json_exactly(self, migrated_pg):
        """GET /api/versions → version list equals keys of bootstrap_versions.json.

        Data-driven: if admin adds a version to the JSON, endpoint reflects it.
        Test never hardcodes version strings.
        """
        expected = _expected_versions()
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/versions")

        assert resp.status_code == 200
        actual = resp.json()["versions"]
        assert actual == expected, (
            f"Endpoint versions {actual!r} != bootstrap_versions.json keys {expected!r}"
        )

    @pytest.mark.asyncio
    async def test_versions_sorted_numerically(self, migrated_pg):
        """Versions are sorted numerically: 8.0 < 9.0 < 10.0 (not lexicographic).

        Lexicographic sort would put '10.0' before '9.0'; numeric sort must not.
        """
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/versions")

        versions = resp.json()["versions"]
        assert len(versions) >= 2, "Need at least 2 versions to check sort order"

        # Verify strictly ascending numeric order
        floats = [float(v) for v in versions]
        assert floats == sorted(floats), f"Versions not in ascending numeric order: {versions}"

        # Specific regression: if both 9.0 and 10.0 present, 9.0 must come first
        if "9.0" in versions and "10.0" in versions:
            assert versions.index("9.0") < versions.index("10.0"), (
                "10.0 must come AFTER 9.0 (numeric sort), not before (lexicographic)"
            )

    @pytest.mark.asyncio
    async def test_unauth_returns_401(self, migrated_pg, monkeypatch):
        """GET /api/versions without session → 401 (not 200, not 403).

        Disables test bypass to exercise real auth path.
        """
        # Temporarily remove the test bypass so auth is enforced.
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/versions")

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_authenticated_gets_200(self, migrated_pg):
        """Any logged-in user (not just admin) may call /api/versions → 200.

        With WEBUI_AUTH_DISABLED bypass, current_user_id() returns 1 (valid uid).
        This tests the auth tier (any-user, not admin-only).
        The bypass simulates a non-admin authenticated user — endpoint must not
        raise 403 for lack of admin privilege.
        """
        # Auth bypass is active (conftest autouse fixture sets WEBUI_AUTH_DISABLED=1).
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/versions")

        # Must be 200, not 403
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_versions_list_not_empty(self, migrated_pg):
        """Endpoint must not return an empty list (fail-fast on JSON parse error)."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/versions")

        versions = resp.json()["versions"]
        assert len(versions) > 0, (
            "Endpoint returned empty version list — check bootstrap_versions.json"
        )


class TestIndexAllBodyGcField:
    """Verify IndexAllBody accepts gc + worker fields with safe defaults.

    W4 C: gc field added to IndexAllBody so the UI gc toggle is wired through.
    These tests guard against regression (field silently dropped or rejected).
    """

    @pytest.mark.asyncio
    async def test_index_all_body_accepts_gc_field(self, migrated_pg):
        """POST /api/repos/index-all with gc=on → accepted (not 422)."""
        import unittest.mock as mock

        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/index-all",
                    json={"gc": "on", "max_workers": "1", "profile_workers": "1"},
                )

        # 200 ok (not 422 body validation error)
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    @pytest.mark.asyncio
    async def test_gc_flag_appears_in_argv(self, migrated_pg):
        """POST with gc=on → argv contains --gc."""
        import unittest.mock as mock

        from src.db.pg import repo_store

        repo_store().add_profile(name="gc_test_profile", odoo_version="99.0")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/api/repos/index-all",
                    json={"gc": "on", "max_workers": "1", "profile_workers": "1"},
                )

        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "--gc" in argv

    @pytest.mark.asyncio
    async def test_gc_off_does_not_appear_in_argv(self, migrated_pg):
        """POST with gc='' (unchecked) → argv does NOT contain --gc."""
        import unittest.mock as mock

        from src.db.pg import repo_store

        repo_store().add_profile(name="gc_off_profile", odoo_version="99.0")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/api/repos/index-all",
                    json={"gc": "", "max_workers": "1", "profile_workers": "1"},
                )

        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "--gc" not in argv
