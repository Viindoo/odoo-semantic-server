# tests/test_web_ui_seed_patterns.py
"""Integration tests for POST /operations/seed-patterns (M8 W6).

Tests: valid submission → 303 redirect + flash + indexer_jobs row;
       no version → label 'patterns';
       with version → label 'patterns:17.0';
       invalid version → 400 re-render with error, no job row;
       non-existent patterns_file → 400 re-render, no job row;
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


class TestSeedPatternsRoute:
    """POST /operations/seed-patterns — happy path + validation."""

    @pytest.fixture(autouse=True)
    def _cleanup_jobs(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")

    @pytest.mark.asyncio
    async def test_valid_submission_no_version_redirects_with_flash(self, migrated_pg):
        """POST with force=on, no version → 303, flash contains 'patterns' and 'job'."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen"):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/seed-patterns",
                    data={"force": "on"},
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/operations?flash=")
        assert "job" in location.lower()

    @pytest.mark.asyncio
    async def test_valid_submission_no_version_creates_job_label_patterns(self, migrated_pg):
        """POST without version → indexer_jobs row with profile_name='patterns'."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen"):
            async with _async_client(app) as client:
                await client.post(
                    "/operations/seed-patterns",
                    data={"force": "on"},
                    follow_redirects=False,
                )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT profile_name, status FROM indexer_jobs WHERE profile_name = %s",
                ("patterns",),
            )
            row = cur.fetchone()
        assert row is not None, "indexer_jobs row must be created"
        assert row[0] == "patterns"
        assert row[1] == "queued"

    @pytest.mark.asyncio
    async def test_valid_submission_with_version_creates_job_label_patterns_version(
        self, migrated_pg
    ):
        """POST with version=17.0 + force=on → job label 'patterns:17.0'."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen"):
            async with _async_client(app) as client:
                await client.post(
                    "/operations/seed-patterns",
                    data={"version": "17.0", "force": "on"},
                    follow_redirects=False,
                )

        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT profile_name, status FROM indexer_jobs WHERE profile_name = %s",
                ("patterns:17.0",),
            )
            row = cur.fetchone()
        assert row is not None, "indexer_jobs row with label 'patterns:17.0' must be created"
        assert row[0] == "patterns:17.0"
        assert row[1] == "queued"

    @pytest.mark.asyncio
    async def test_argv_contains_seed_patterns_and_force(self, migrated_pg):
        """Popen argv must include seed-patterns --force when force checkbox ticked."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/seed-patterns",
                    data={"force": "on"},
                    follow_redirects=False,
                )

        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "seed-patterns" in argv
        assert "--force" in argv
        assert "--job-id" in argv

    @pytest.mark.asyncio
    async def test_argv_with_version_and_no_embed(self, migrated_pg):
        """Popen argv includes --version + --no-embed when provided."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/seed-patterns",
                    data={"version": "17.0", "no_embed": "on"},
                    follow_redirects=False,
                )

        argv = mock_popen.call_args[0][0]
        assert "--version" in argv
        assert "17.0" in argv
        assert "--no-embed" in argv

    @pytest.mark.asyncio
    async def test_argv_without_force_does_not_include_force(self, migrated_pg):
        """When force checkbox is NOT ticked, --force must NOT appear in argv."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/seed-patterns",
                    data={},
                    follow_redirects=False,
                )

        argv = mock_popen.call_args[0][0]
        assert "--force" not in argv

    @pytest.mark.asyncio
    async def test_invalid_version_returns_400(self, migrated_pg):
        """POST with invalid version string → 400, error in body, no job row created."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/seed-patterns",
                    data={"version": "abc"},
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        assert "abc" in resp.text or "Invalid" in resp.text
        mock_popen.assert_not_called()

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_nonexistent_patterns_file_returns_400(self, migrated_pg):
        """POST with non-existent patterns_file → 400, error in body, no job row."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/seed-patterns",
                    data={"patterns_file": "/does/not/exist/patterns.json"},
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        assert (
            "/does/not/exist/patterns.json" in resp.text
            or "not exist" in resp.text.lower()
        )
        mock_popen.assert_not_called()

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_valid_patterns_file_included_in_argv(self, migrated_pg, tmp_path):
        """When patterns_file exists, --patterns-file is passed to argv."""
        pf = tmp_path / "patterns.json"
        pf.write_text("[]")

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/seed-patterns",
                    data={"patterns_file": str(pf)},
                    follow_redirects=False,
                )

        argv = mock_popen.call_args[0][0]
        assert "--patterns-file" in argv
        assert str(pf) in argv

    @pytest.mark.asyncio
    async def test_valid_version_formats_accepted(self, migrated_pg):
        """Multiple valid version strings (8.0, 17.0, 20.0) are accepted."""
        app = create_app()
        for ver in ("8.0", "17.0", "20.0"):
            with mock.patch(
                "src.web_ui.routes.operations._get_conn",
                _make_conn_factory(migrated_pg),
            ), mock.patch("subprocess.Popen"):
                async with _async_client(app) as client:
                    resp = await client.post(
                        "/operations/seed-patterns",
                        data={"version": ver},
                        follow_redirects=False,
                    )
            assert resp.status_code == 303, f"version '{ver}' should be valid"
            with migrated_pg.cursor() as cur:
                cur.execute("DELETE FROM indexer_jobs")


class TestSeedPatternsGetPage:
    """GET /operations — seed-patterns section renders correctly."""

    @pytest.mark.asyncio
    async def test_get_operations_renders_seed_patterns_form(self, migrated_pg):
        """GET /operations → 200, Seed Pattern Catalogue section present."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.get("/operations")

        assert resp.status_code == 200
        assert "Seed Pattern Catalogue" in resp.text
        assert "/operations/seed-patterns" in resp.text
        assert 'name="version"' in resp.text
        assert 'name="no_embed"' in resp.text
        assert 'name="force"' in resp.text
        assert 'name="patterns_file"' in resp.text
