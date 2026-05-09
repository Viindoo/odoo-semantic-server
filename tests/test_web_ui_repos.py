# tests/test_web_ui_repos.py
"""Tests for /repos Web UI routes — requires PostgreSQL."""
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _make_conn_factory(pg_conn):
    """Return a _get_conn replacement that returns the test connection.

    The route calls conn.close() in a finally block — we patch that to a no-op
    so the session-scoped pg_conn stays open across tests.
    """
    class _NoCloseConn:
        """Thin wrapper: proxies all psycopg2 Connection attrs but no-ops close()."""

        def __init__(self, conn):
            self._conn = conn

        def close(self):
            pass  # keep the session-scoped connection alive

        def cursor(self, *args, **kwargs):
            return self._conn.cursor(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    wrapped = _NoCloseConn(pg_conn)

    def factory():
        return wrapped

    return factory


def _async_client(app):
    """Return an AsyncClient backed by the ASGI app via ASGITransport."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestReposPage:
    @pytest.mark.asyncio
    async def test_get_repos_returns_200(self, migrated_pg):
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.get("/repos")
        assert resp.status_code == 200
        assert "Repos" in resp.text

    @pytest.mark.asyncio
    async def test_get_repos_shows_add_profile_form(self, migrated_pg):
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.get("/repos")
        assert "Add Profile" in resp.text
        assert "No profiles yet" in resp.text

    @pytest.mark.asyncio
    async def test_create_profile_redirects(self, migrated_pg):
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/profiles",
                    data={"name": "test_profile", "version": "17.0", "description": ""},
                    follow_redirects=False,
                )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"

    @pytest.mark.asyncio
    async def test_create_profile_persists(self, migrated_pg):
        from src.db.repo_registry import list_profiles

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                await client.post(
                    "/repos/profiles",
                    data={"name": "viindoo17", "version": "17.0", "description": "test"},
                    follow_redirects=False,
                )
        profiles = list_profiles(migrated_pg)
        assert len(profiles) == 1
        assert profiles[0]["name"] == "viindoo17"
        assert profiles[0]["odoo_version"] == "17.0"

    @pytest.mark.asyncio
    async def test_get_repos_shows_profile_after_create(self, migrated_pg):
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                await client.post(
                    "/repos/profiles",
                    data={"name": "myprofile", "version": "16.0", "description": ""},
                    follow_redirects=False,
                )
                resp = await client.get("/repos")
        assert "myprofile" in resp.text
        assert "16.0" in resp.text

    @pytest.mark.asyncio
    async def test_add_repo_redirects(self, migrated_pg):
        from src.db.repo_registry import add_profile

        # Pre-create profile directly via ORM to isolate POST /repos/repos behaviour
        add_profile(migrated_pg, name="p1", odoo_version="17.0")

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/repos",
                    data={
                        "profile": "p1",
                        "url": "file://local",
                        "branch": "17.0",
                        "local_path": "/tmp/odoo_17",
                    },
                    follow_redirects=False,
                )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"

    @pytest.mark.asyncio
    async def test_index_repo_redirects(self, migrated_pg):
        from src.db.repo_registry import add_profile, add_repo

        pid = add_profile(migrated_pg, name="p1", odoo_version="17.0")
        rid = add_repo(
            migrated_pg,
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_17",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/repos/{rid}/index",
                    follow_redirects=False,
                )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"
        mock_popen.assert_called_once()

    @pytest.mark.asyncio
    async def test_index_repo_uses_profile_name_not_all(self, migrated_pg):
        """I4: index button must dispatch --profile <name>, not --all."""
        from src.db.repo_registry import add_profile, add_repo

        pid = add_profile(migrated_pg, name="myprofile", odoo_version="17.0")
        rid = add_repo(
            migrated_pg,
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_17",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(f"/repos/repos/{rid}/index", follow_redirects=False)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args[0][0]  # first positional arg = command list
        assert "--all" not in call_args, "index must not re-index all profiles (I4)"
        assert "--profile" in call_args
        idx = call_args.index("--profile")
        assert call_args[idx + 1] == "myprofile", "must pass the specific profile name"
