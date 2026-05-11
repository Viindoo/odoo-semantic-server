# tests/test_web_ui_index_core.py
"""Integration tests for POST /operations/index-core (M8 W5).

Tests: valid submission → 303 redirect + flash + indexer_jobs row;
       invalid version  → 400 re-render with error, no job row;
       non-existent source path → 400 re-render, no job row;
       argv verification via Popen mock.
"""
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


class _NoCloseConn:
    """Thin proxy that no-ops close() so session-scoped pg_conn stays open."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _make_conn_factory(pg_conn):
    wrapped = _NoCloseConn(pg_conn)

    def factory():
        return wrapped

    return factory


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestIndexCoreRoute:
    """POST /operations/index-core — happy path + validation."""

    @pytest.fixture(autouse=True)
    def _cleanup_jobs(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")

    @pytest.mark.asyncio
    async def test_valid_submission_redirects_with_flash(self, migrated_pg, tmp_path):
        """POST valid source + version → 303, flash contains job id and version."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen"):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/index-core",
                    data={"source": str(tmp_path), "version": "17.0"},
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/operations?flash=")
        assert "17.0" in location
        # "job" keyword must appear in flash
        assert "job" in location.lower()

    @pytest.mark.asyncio
    async def test_valid_submission_creates_indexer_jobs_row(self, migrated_pg, tmp_path):
        """POST valid inputs → indexer_jobs row with profile_name='core:17.0' + status='queued'."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen"):
            async with _async_client(app) as client:
                await client.post(
                    "/operations/index-core",
                    data={"source": str(tmp_path), "version": "17.0"},
                    follow_redirects=False,
                )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT profile_name, status FROM indexer_jobs WHERE profile_name = %s",
                ("core:17.0",),
            )
            row = cur.fetchone()
        assert row is not None, "indexer_jobs row must be created"
        assert row[0] == "core:17.0"
        assert row[1] == "queued"

    @pytest.mark.asyncio
    async def test_valid_submission_argv_contains_index_core(self, migrated_pg, tmp_path):
        """Popen argv must include index-core --source X --version 17.0."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/index-core",
                    data={"source": str(tmp_path), "version": "17.0"},
                    follow_redirects=False,
                )

        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "index-core" in argv
        assert "--source" in argv
        assert str(tmp_path) in argv
        assert "--version" in argv
        assert "17.0" in argv
        assert "--job-id" in argv

    @pytest.mark.asyncio
    async def test_valid_submission_with_static_data_dir(self, migrated_pg, tmp_path):
        """static_data_dir provided + exists → argv includes --static-data-dir."""
        static_dir = tmp_path / "static"
        static_dir.mkdir()

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/index-core",
                    data={
                        "source": str(tmp_path),
                        "version": "17.0",
                        "static_data_dir": str(static_dir),
                    },
                    follow_redirects=False,
                )

        argv = mock_popen.call_args[0][0]
        assert "--static-data-dir" in argv
        assert str(static_dir) in argv

    @pytest.mark.asyncio
    async def test_empty_static_data_dir_not_in_argv(self, migrated_pg, tmp_path):
        """Empty static_data_dir (default) → --static-data-dir NOT in argv."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/index-core",
                    data={"source": str(tmp_path), "version": "17.0", "static_data_dir": ""},
                    follow_redirects=False,
                )

        argv = mock_popen.call_args[0][0]
        assert "--static-data-dir" not in argv

    @pytest.mark.asyncio
    async def test_invalid_version_returns_400(self, migrated_pg, tmp_path):
        """POST with invalid version string → 400, error in body, no job row created."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/index-core",
                    data={"source": str(tmp_path), "version": "abc"},
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        assert "abc" in resp.text or "Invalid" in resp.text
        mock_popen.assert_not_called()

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_invalid_version_blank_returns_400(self, migrated_pg, tmp_path):
        """Version '17' (no dot) → 400, no job row."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/index-core",
                    data={"source": str(tmp_path), "version": "17"},
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_nonexistent_source_path_returns_400(self, migrated_pg):
        """POST with non-existent source path → 400, error in body, no job row."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/index-core",
                    data={"source": "/does/not/exist/odoo", "version": "17.0"},
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        assert "/does/not/exist/odoo" in resp.text or "not exist" in resp.text.lower()
        mock_popen.assert_not_called()

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_nonexistent_static_data_dir_returns_400(self, migrated_pg, tmp_path):
        """Static data dir provided but does not exist → 400, no job row."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/index-core",
                    data={
                        "source": str(tmp_path),
                        "version": "17.0",
                        "static_data_dir": "/no/such/static",
                    },
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        mock_popen.assert_not_called()

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_valid_version_formats(self, migrated_pg, tmp_path):
        """Verify multiple valid version strings are accepted (8.0, 9.0, 17.0, 20.0)."""
        app = create_app()
        for ver in ("8.0", "9.0", "17.0", "20.0"):
            with mock.patch(
                "src.web_ui.routes.operations._get_conn",
                _make_conn_factory(migrated_pg),
            ), mock.patch("subprocess.Popen"):
                async with _async_client(app) as client:
                    resp = await client.post(
                        "/operations/index-core",
                        data={"source": str(tmp_path), "version": ver},
                        follow_redirects=False,
                    )
            assert resp.status_code == 303, f"version '{ver}' should be valid"
            # cleanup job rows for next iteration
            with migrated_pg.cursor() as cur:
                cur.execute("DELETE FROM indexer_jobs")


class TestOperationsPageGet:
    """GET /operations — smoke test that form renders correctly."""

    @pytest.mark.asyncio
    async def test_get_operations_renders_form(self, migrated_pg):
        """GET /operations → 200, form action /operations/index-core present."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.get("/operations")

        assert resp.status_code == 200
        assert "Index Odoo Core Specs" in resp.text
        assert "/operations/index-core" in resp.text
        assert 'name="source"' in resp.text
        assert 'name="version"' in resp.text
        assert 'name="static_data_dir"' in resp.text
