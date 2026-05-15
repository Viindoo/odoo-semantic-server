"""Tests for API keys Web UI routes — requires PostgreSQL (M8 W1 pure JSON API)."""

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def web_app(pg_conn, monkeypatch):
    """Create Web UI app with mocked database connection.

    M9 W-AK: api_keys.user_id now has a FK to webui_users(id) ON DELETE CASCADE.
    The conftest auth bypass returns current_user_id=1 (sentinel), so we must
    seed a webui_users row with id=1 here or the INSERT FK check fails. Use
    setval() to align the SERIAL sequence with the sentinel id.
    """
    from src.db.migrate import run_migrations
    from src.web_ui.app import create_app

    run_migrations(pg_conn)

    # Clean up before test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM api_keys")
        # Seed an admin user with id=1 to satisfy api_keys.user_id FK when
        # current_user_id() returns the bypass sentinel of 1.
        cur.execute(
            "DELETE FROM webui_users WHERE username = %s",
            ("_bypass_actor_id1",),
        )
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active, id)"
            " VALUES (%s, %s, TRUE, TRUE, 1) ON CONFLICT (username) DO NOTHING",
            ("_bypass_actor_id1", "x"),
        )
    if not pg_conn.autocommit:
        pg_conn.commit()

    app = create_app()

    # Pool is already initialized to test DB by pg_conn fixture (conftest.py).
    # Routes use auth_store() which draws from the shared pool — no manual mock needed.

    yield app

    # Clean up after test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM api_keys")
        cur.execute(
            "DELETE FROM webui_users WHERE username = %s",
            ("_bypass_actor_id1",),
        )
    if not pg_conn.autocommit:
        pg_conn.commit()


class TestApiKeysPage:
    @pytest.mark.asyncio
    async def test_get_api_keys_returns_200(self, web_app):
        """GET /api/api-keys should return 200 with JSON."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/api-keys")
        assert resp.status_code == 200
        body = resp.json()
        assert "keys" in body

    @pytest.mark.asyncio
    async def test_get_api_keys_returns_empty_list(self, web_app):
        """GET /api/api-keys with no keys returns empty list."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/api-keys")
        assert resp.status_code == 200
        body = resp.json()
        assert body["keys"] == []

    @pytest.mark.asyncio
    async def test_create_key_returns_raw_key(self, web_app):
        """POST /api/api-keys should create key and return raw key once."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/api-keys",
                json={"name": "test-key"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert "raw_key" in body
        assert body["raw_key"].startswith("osm_")

    @pytest.mark.asyncio
    async def test_create_key_adds_to_list(self, web_app):
        """After creating a key, it should appear in the keys list."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/api-keys",
                json={"name": "new-key"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        names = [k["name"] for k in body.get("keys", [])]
        assert "new-key" in names

    @pytest.mark.asyncio
    async def test_deactivate_key_returns_ok(self, web_app, pg_conn):
        """POST /api/api-keys/{id}/deactivate should return 200 ok JSON."""
        import httpx

        from src.db.pg import auth_store

        # Create a key
        _, _, key_id = auth_store().create_api_key("to-deactivate")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/api-keys/{key_id}/deactivate",
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True

    @pytest.mark.asyncio
    async def test_deactivate_key_marks_inactive(self, web_app, pg_conn):
        """After deactivating, the key should be inactive in DB."""
        import httpx

        from src.db.pg import auth_store

        # Create a key
        _, _, key_id = auth_store().create_api_key("deactivate-test")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/api-keys/{key_id}/deactivate",
            )

        assert resp.status_code == 200
        # Check DB: key is now inactive
        keys = auth_store().list_api_keys()
        deactivated = next((k for k in keys if k["id"] == key_id), None)
        assert deactivated is not None
        # DB column is named 'active'
        assert deactivated.get("active") is False or not deactivated.get("active", True)


class TestApiKeyDeactivateInvariants:
    """B1: deactivate must clear in-process cache immediately."""

    @pytest.mark.asyncio
    async def test_deactivate_calls_cache_invalidate(self, web_app, pg_conn):
        """B1: POST /api/api-keys/{id}/deactivate must call _cache_invalidate_by_key_id."""
        import httpx

        from src.db.pg import auth_store
        from src.mcp.middleware import _cache_get, _cache_set

        raw, _, key_id = auth_store().create_api_key("deactivate-b1-test")
        _cache_set(raw, key_id)
        hit, _ = _cache_get(raw)
        assert hit  # cache primed

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/api-keys/{key_id}/deactivate")
        assert resp.status_code == 200

        hit, _ = _cache_get(raw)
        assert not hit, "cache must be cleared immediately after deactivate (B1)"

    @pytest.mark.asyncio
    async def test_deactivate_exception_is_logged_not_swallowed(self, web_app, caplog):
        """I3: exceptions during deactivate must be logged, not silently dropped."""
        import unittest.mock as mock

        import httpx

        mock_store = mock.MagicMock()
        mock_store.deactivate_api_key.side_effect = RuntimeError("db exploded")

        with mock.patch("src.db.pg.auth_store", return_value=mock_store):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=web_app), base_url="http://test"
            ) as client:
                with caplog.at_level("WARNING", logger="src.web_ui.routes.api_keys"):
                    resp = await client.post("/api/api-keys/999/deactivate")

        assert resp.status_code == 500  # returns error JSON
        assert any("Deactivate key" in r.message for r in caplog.records), (
            "exception must be logged as warning (I3)"
        )
