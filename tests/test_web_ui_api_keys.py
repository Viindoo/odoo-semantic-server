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


# ---------------------------------------------------------------------------
# M9-rbac W1 regression tests — session-read bug + deactivate ownership guard
# ---------------------------------------------------------------------------

@pytest.fixture
def web_app_rbac(pg_conn, monkeypatch):
    """Web app fixture that seeds a non-admin user (id=2) alongside bypass admin (id=1).

    The bypass sentinel always returns current_user_id=1 (admin, is_admin=TRUE).
    For non-admin scenarios we monkeypatch current_user_id to return 2, backed by
    a real webui_users row with is_admin=FALSE so is_admin_session() reads DB correctly.
    """
    from src.db.migrate import run_migrations
    from src.web_ui.app import create_app

    run_migrations(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM api_keys")
        # Admin user (id=1) — required by auth bypass sentinel
        cur.execute("DELETE FROM webui_users WHERE username = '_bypass_actor_id1'")
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active, id)"
            " VALUES (%s, %s, TRUE, TRUE, 1) ON CONFLICT (username) DO NOTHING",
            ("_bypass_actor_id1", "x"),
        )
        # Non-admin user (id=2) — used by ownership tests via monkeypatch
        cur.execute("DELETE FROM webui_users WHERE username = '_test_nonadmin_id2'")
        cur.execute(
            "INSERT INTO webui_users (username, password_hash, is_admin, is_active, id)"
            " VALUES (%s, %s, FALSE, TRUE, 2) ON CONFLICT (username) DO NOTHING",
            ("_test_nonadmin_id2", "x"),
        )
    if not pg_conn.autocommit:
        pg_conn.commit()

    app = create_app()
    yield app

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM api_keys")
        cur.execute("DELETE FROM webui_users WHERE username IN (%s, %s)",
                    ("_bypass_actor_id1", "_test_nonadmin_id2"))
    if not pg_conn.autocommit:
        pg_conn.commit()


class TestRbacRegressions:
    """M9-rbac W1: session-read bug regression + ownership-guarded deactivate."""

    @pytest.mark.asyncio
    async def test_admin_sees_all_keys_with_owner_username(self, web_app_rbac, pg_conn):
        """Admin session must see all keys; owner_username populated for user-owned keys."""
        import httpx

        from src.db.pg import auth_store

        # System key (user_id=NULL) — legacy CLI-style key
        auth_store().create_api_key("system-key", user_id=None)
        # User-owned key (user_id=2, non-admin user)
        auth_store().create_api_key("user-key", user_id=2)

        # Bypass = admin (user id=1, is_admin=TRUE in DB)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app_rbac), base_url="http://test"
        ) as client:
            resp = await client.get("/api/api-keys")

        assert resp.status_code == 200
        body = resp.json()
        keys = body["keys"]
        names = [k["name"] for k in keys]
        assert "system-key" in names, "admin must see system (NULL user_id) keys"
        assert "user-key" in names, "admin must see user-owned keys"

        # owner_username should be None for system key, set for user-owned key
        system_key = next(k for k in keys if k["name"] == "system-key")
        user_key = next(k for k in keys if k["name"] == "user-key")
        assert system_key["owner_username"] is None, "system key owner_username must be None"
        assert user_key["owner_username"] == "_test_nonadmin_id2"

    @pytest.mark.asyncio
    async def test_non_admin_sees_only_own_keys(self, web_app_rbac, pg_conn, monkeypatch):
        """Non-admin user (uid=2) must only see their own keys; system+other keys excluded."""
        import httpx

        from src.db.pg import auth_store

        # System key (user_id=NULL) — admin key
        auth_store().create_api_key("admin-key", user_id=None)
        # User 2 key
        auth_store().create_api_key("user2-key", user_id=2)

        # Patch current_user_id to return 2 (non-admin) instead of bypass sentinel 1
        monkeypatch.setattr(
            "src.web_ui.routes.api_keys.current_user_id",
            lambda _req: 2,
        )
        # Also patch is_admin_session to use real DB path (it calls current_user_id
        # internally via the auth module — we need the route-level call to use uid=2).
        # The is_admin_session in the route re-imports from auth, not the patched symbol,
        # so patch the auth module directly.
        monkeypatch.setattr(
            "src.web_ui.auth.current_user_id",
            lambda _req: 2,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app_rbac), base_url="http://test"
        ) as client:
            resp = await client.get("/api/api-keys")

        assert resp.status_code == 200
        body = resp.json()
        keys = body["keys"]
        names = [k["name"] for k in keys]
        assert "user2-key" in names, "non-admin must see own keys"
        assert "admin-key" not in names, "non-admin must NOT see system/admin keys"

    @pytest.mark.asyncio
    async def test_non_admin_cannot_deactivate_other_users_key(
        self, web_app_rbac, pg_conn, monkeypatch
    ):
        """Non-admin (uid=2) deactivating a key owned by user 1 must get 403 not_owner."""
        import httpx

        from src.db.pg import auth_store

        # Key owned by admin user (id=1)
        _, _, admin_key_id = auth_store().create_api_key("admin-owned-key", user_id=1)

        # Patch to simulate non-admin user 2
        monkeypatch.setattr("src.web_ui.auth.current_user_id", lambda _req: 2)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app_rbac), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/api-keys/{admin_key_id}/deactivate")

        assert resp.status_code == 403
        body = resp.json()
        assert body.get("error") == "not_owner"

    @pytest.mark.asyncio
    async def test_admin_can_deactivate_any_key(self, web_app_rbac, pg_conn):
        """Admin (bypass uid=1, is_admin=TRUE) can deactivate any key regardless of owner."""
        import httpx

        from src.db.pg import auth_store

        # Key owned by non-admin user 2
        _, _, key_id = auth_store().create_api_key("user2-owned-key", user_id=2)

        # Bypass = admin (uid=1, is_admin=TRUE)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app_rbac), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/api-keys/{key_id}/deactivate")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True

        # Verify key is now inactive in DB
        keys = auth_store().list_api_keys()
        target = next((k for k in keys if k["id"] == key_id), None)
        assert target is not None
        assert not target["active"], "key must be deactivated in DB"
