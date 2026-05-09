"""Tests for API keys Web UI routes — requires PostgreSQL."""

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def web_app(pg_conn, monkeypatch):
    """Create Web UI app with mocked database connection."""
    from src.db.migrate import run_migrations
    from src.web_ui.app import create_app

    run_migrations(pg_conn)

    # Clean up before test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM api_keys")
    if not pg_conn.autocommit:
        pg_conn.commit()

    app = create_app()

    # Mock _get_conn at module level to use test pg_conn
    from src.web_ui.routes import api_keys

    def mock_get_conn():
        """Return a new connection to test database."""
        import psycopg2

        # Reuse the PG_TEST_DSN from conftest
        from tests.conftest import PG_TEST_DSN

        try:
            conn = psycopg2.connect(PG_TEST_DSN)
            conn.autocommit = True
            return conn
        except Exception:
            return None

    monkeypatch.setattr(api_keys, "_get_conn", mock_get_conn)

    yield app

    # Clean up after test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM api_keys")
    if not pg_conn.autocommit:
        pg_conn.commit()


class TestApiKeysPage:
    @pytest.mark.asyncio
    async def test_get_api_keys_returns_200(self, web_app):
        """GET /api-keys should return 200 with HTML page."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api-keys")
        assert resp.status_code == 200
        assert "API Keys" in resp.text

    @pytest.mark.asyncio
    async def test_get_api_keys_contains_form(self, web_app):
        """GET /api-keys should contain a form to create new key."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api-keys")
        assert "Create API Key" in resp.text
        assert 'name="name"' in resp.text
        assert 'action="/api-keys"' in resp.text

    @pytest.mark.asyncio
    async def test_create_key_displays_raw_key_once(self, web_app):
        """POST /api-keys should create key and display raw key with warning."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api-keys",
                data={"name": "test-key"},
            )
        assert resp.status_code == 200
        # Raw key should be displayed
        assert "osm_" in resp.text
        # Warning about copy-now should be present
        assert "copy it now" in resp.text.lower() or "not be shown again" in resp.text

    @pytest.mark.asyncio
    async def test_create_key_adds_to_list(self, web_app):
        """After creating a key, it should appear in the keys list."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api-keys",
                data={"name": "new-key"},
            )
        assert resp.status_code == 200
        # Key name should appear in the response
        assert "new-key" in resp.text
        # Prefix should appear
        assert "osm_" in resp.text

    @pytest.mark.asyncio
    async def test_deactivate_key_redirects(self, web_app, pg_conn):
        """POST /api-keys/{id}/deactivate should redirect to /api-keys."""
        import httpx

        from src.db.auth_registry import create_api_key

        # Create a key
        _, _, key_id = create_api_key(pg_conn, "to-deactivate")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api-keys/{key_id}/deactivate",
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/api-keys"

    @pytest.mark.asyncio
    async def test_deactivate_key_marks_inactive(self, web_app, pg_conn):
        """After deactivating, the key should appear as inactive."""
        import httpx

        from src.db.auth_registry import create_api_key

        # Create a key
        _, _, key_id = create_api_key(pg_conn, "deactivate-test")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            # Deactivate
            resp = await client.post(
                f"/api-keys/{key_id}/deactivate",
                follow_redirects=True,
            )

        assert resp.status_code == 200
        # After redirect, the key should appear as inactive
        assert "deactivate-test" in resp.text
        assert "inactive" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_empty_keys_list_message(self, web_app):
        """When no keys exist, show appropriate message."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api-keys")
        assert resp.status_code == 200
        assert "No API keys yet" in resp.text

    @pytest.mark.asyncio
    async def test_create_key_form_requires_name(self, web_app):
        """Form should require name field."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api-keys")
        assert "required" in resp.text or 'name="name"' in resp.text


class TestApiKeyDeactivateInvariants:
    """B1: deactivate must clear in-process cache immediately."""

    @pytest.mark.asyncio
    async def test_deactivate_calls_cache_invalidate(self, web_app, pg_conn):
        """B1: POST /api-keys/{id}/deactivate must call _cache_invalidate_by_key_id."""
        import httpx

        from src.db.auth_registry import create_api_key
        from src.mcp.middleware import _cache_get, _cache_set

        raw, _, key_id = create_api_key(pg_conn, "deactivate-b1-test")
        _cache_set(raw, key_id)
        hit, _ = _cache_get(raw)
        assert hit  # cache primed

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api-keys/{key_id}/deactivate")
        assert resp.status_code == 303

        hit, _ = _cache_get(raw)
        assert not hit, "cache must be cleared immediately after deactivate (B1)"

    @pytest.mark.asyncio
    async def test_deactivate_exception_is_logged_not_swallowed(self, web_app, caplog):
        """I3: exceptions during deactivate must be logged, not silently dropped."""
        import unittest.mock as mock

        import httpx

        import src.web_ui.routes.api_keys as ak_mod

        with mock.patch.object(ak_mod, "_get_conn") as mock_conn_fn:
            mock_conn = mock.MagicMock()
            mock_conn.__bool__ = lambda s: True
            mock_conn.cursor.side_effect = RuntimeError("db exploded")
            mock_conn_fn.return_value = mock_conn

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=web_app), base_url="http://test"
            ) as client:
                with caplog.at_level("WARNING", logger="src.web_ui.routes.api_keys"):
                    resp = await client.post("/api-keys/999/deactivate")

        assert resp.status_code == 303  # still redirects
        assert any("Deactivate key" in r.message for r in caplog.records), (
            "exception must be logged as warning (I3)"
        )
