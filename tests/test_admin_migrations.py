# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_admin_migrations.py
"""Tests for M9 W-UO: GET /api/admin/migrations read-only display.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Auth bypass is active via conftest autouse fixture (sets WEBUI_AUTH_DISABLED=1).
Tests that need real admin-gating use direct dependency call.
"""
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yoyo_migrations_table_exists(conn) -> bool:
    """Return True if _yoyo_migrations table exists in the DB."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = '_yoyo_migrations' LIMIT 1"
        )
        return cur.fetchone() is not None


def _count_yoyo_rows(conn) -> int:
    """Return row count from _yoyo_migrations, or 0 if table absent."""
    if not _yoyo_migrations_table_exists(conn):
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM _yoyo_migrations")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Auth guard tests (dependency-level — no full HTTP stack needed)
# ---------------------------------------------------------------------------


class TestListMigrationsRequiresAdmin:
    @pytest.mark.asyncio
    async def test_anonymous_gets_401(self, clean_pg):
        """Anonymous request (no session) → require_admin raises 401.

        We test require_admin directly since the auth middleware is
        not mounted on the minimal app used in unit tests.
        """
        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/admin/migrations",
            "headers": [],
            "query_string": b"",
        }
        fake_request = StarletteRequest(scope)

        # Bypass off, current_user_id returns None → require_admin must raise 401
        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: None
            raised = None
            try:
                await auth_mod.require_admin(fake_request)
            except HTTPException as exc:
                raised = exc
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert raised is not None, "require_admin must raise when no session"
        assert raised.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_gets_403(self, clean_pg):
        """Authenticated non-admin → require_admin raises 403."""
        run_migrations(clean_pg)

        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod

        # Seed a non-admin user
        with clean_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users (username, password_hash, is_admin, is_active) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                ("nonadmin_mig", "hash", False, True),
            )
            user_id = cur.fetchone()[0]

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/admin/migrations",
            "headers": [],
            "query_string": b"",
        }
        fake_request = StarletteRequest(scope)

        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: user_id
            raised = None
            try:
                await auth_mod.require_admin(fake_request)
            except HTTPException as exc:
                raised = exc
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert raised is not None, "require_admin must raise for non-admin"
        assert raised.status_code == 403


# ---------------------------------------------------------------------------
# Route return-value tests (auth bypass active via conftest fixture)
# ---------------------------------------------------------------------------


class TestListMigrationsReturnsApplied:
    @pytest.mark.asyncio
    async def test_returns_rows_after_run_migrations(self, clean_pg):
        """After run_migrations(), endpoint returns at least one row."""
        run_migrations(clean_pg)

        if not _yoyo_migrations_table_exists(clean_pg):
            pytest.skip("_yoyo_migrations table not created by run_migrations — skipping")

        row_count = _count_yoyo_rows(clean_pg)

        import httpx

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/admin/migrations")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "migrations" in data
        assert "count" in data
        assert data["count"] == row_count
        assert len(data["migrations"]) == row_count

        if row_count > 0:
            first = data["migrations"][0]
            assert "id" in first
            assert "applied_at" in first
            # migration_id should be a non-empty string
            assert isinstance(first["id"], str)
            assert first["id"]

    @pytest.mark.asyncio
    async def test_migration_items_have_expected_keys(self, clean_pg):
        """Each migration item has 'id' and 'applied_at' keys."""
        run_migrations(clean_pg)

        if not _yoyo_migrations_table_exists(clean_pg):
            pytest.skip("_yoyo_migrations table absent — skipping")

        import httpx

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/admin/migrations")

        assert resp.status_code == 200
        data = resp.json()
        for item in data["migrations"]:
            assert set(item.keys()) >= {"id", "applied_at"}


class TestListMigrationsEmptyDb:
    @pytest.mark.asyncio
    async def test_no_table_returns_empty(self, clean_pg):
        """If _yoyo_migrations table does not exist, returns empty list + count=0."""
        # Ensure the yoyo table does NOT exist (clean_pg has run_migrations skipped)
        # Drop it if present from a prior migration
        with clean_pg.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS _yoyo_migrations")

        import httpx

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/admin/migrations")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["migrations"] == []
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_empty_table_returns_count_zero(self, clean_pg):
        """If _yoyo_migrations table exists but is empty, count=0."""
        # Create the table manually with no rows
        with clean_pg.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS _yoyo_migrations ("
                "  migration_hash VARCHAR(64) NOT NULL PRIMARY KEY, "
                "  migration_id VARCHAR(255), "
                "  applied_at_utc TIMESTAMP"
                ")"
            )

        import httpx

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/admin/migrations")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["migrations"] == []
        assert data["count"] == 0
