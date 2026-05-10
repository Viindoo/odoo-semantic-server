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

    @pytest.mark.asyncio
    async def test_index_repo_dedup_blocked(self, migrated_pg):
        """M5.5 Section E: when indexer is running, redirect with flash, Popen NOT called."""
        from src.db.repo_registry import add_profile, add_repo

        pid = add_profile(migrated_pg, name="p_dedup", odoo_version="17.0")
        rid = add_repo(
            migrated_pg,
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_dedup",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=True
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/repos/{rid}/index", follow_redirects=False
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location, "redirect must carry flash query param"
        assert "already" in location.lower(), "flash must mention 'already'"
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_index_repo_dedup_ok_spawns_popen(self, migrated_pg):
        """M5.5 Section E: when indexer not running, Popen called once (dedup pass)."""
        from src.db.repo_registry import add_profile, add_repo

        pid = add_profile(migrated_pg, name="p_free", odoo_version="17.0")
        rid = add_repo(
            migrated_pg,
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_free",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/repos/{rid}/index", follow_redirects=False
                )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"
        mock_popen.assert_called_once()


class TestJobIntegration:
    """WI-F3: job record creation + GET /repos/jobs/{id}/status endpoint."""

    @pytest.fixture(autouse=True)
    def _cleanup_jobs(self, migrated_pg):
        """Delete indexer_jobs rows before and after each test in this class."""
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")

    @pytest.mark.asyncio
    async def test_index_repo_creates_job_and_passes_job_id(self, migrated_pg):
        """POST /repos/repos/{id}/index → job created, --job-id in argv."""
        from src.db.repo_registry import add_profile, add_repo

        pid = add_profile(migrated_pg, name="p_job", odoo_version="17.0")
        rid = add_repo(
            migrated_pg,
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_job",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/repos/{rid}/index", follow_redirects=False
                )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"
        mock_popen.assert_called_once()

        call_argv = mock_popen.call_args[0][0]
        assert "--job-id" in call_argv, "--job-id flag must be in Popen argv"
        job_id_idx = call_argv.index("--job-id")
        job_id_str = call_argv[job_id_idx + 1]
        assert job_id_str.isdigit(), "--job-id value must be a numeric string"

        # Verify indexer_jobs row was created
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            count = cur.fetchone()[0]
        assert count == 1

        # Verify the job has status 'queued'
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT status, profile_name FROM indexer_jobs WHERE id = %s",
                (int(job_id_str),),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "queued"
        assert row[1] == "p_job"

    @pytest.mark.asyncio
    async def test_index_repo_dedup_blocks_no_job_created(self, migrated_pg):
        """Khi indexer_is_running True → KHÔNG tạo job, KHÔNG Popen, flash redirect."""
        from src.db.repo_registry import add_profile, add_repo

        pid = add_profile(migrated_pg, name="p_dedup2", odoo_version="17.0")
        rid = add_repo(
            migrated_pg,
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_dedup2",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=True
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/repos/{rid}/index", follow_redirects=False
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location, "redirect must carry flash query param"
        mock_popen.assert_not_called()

        # No job row created
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            count = cur.fetchone()[0]
        assert count == 0

    @pytest.mark.asyncio
    async def test_get_job_status_existing(self, migrated_pg):
        """GET /repos/jobs/{id}/status with existing job → 200 + correct JSON shape."""
        from src.db import job_registry

        job_id = job_registry.create_job(migrated_pg, "p_status")

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.get(f"/repos/jobs/{job_id}/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == job_id
        assert data["profile_name"] == "p_status"
        assert data["status"] == "queued"
        assert data["pid"] is None
        assert data["started_at"] is None
        assert data["finished_at"] is None
        assert data["error_msg"] is None
        assert "created_at" in data

    @pytest.mark.asyncio
    async def test_get_job_status_missing(self, migrated_pg):
        """GET /repos/jobs/999999/status → 404."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.get("/repos/jobs/999999/status")

        assert resp.status_code == 404
        data = resp.json()
        assert data["error"] == "job not found"
