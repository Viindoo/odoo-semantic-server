# tests/test_web_ui_index_all.py
"""Integration tests for POST /repos/index-all (M8 W7).

Covers:
- Default POST → 303 with flash, argv = index-repo --all only.
- POST with all flags set → argv contains --full, --no-embed, --max-workers, --profile-workers.
- max_workers=9 (out of range) → flash error, no Popen.
- profile_workers=5 (out of range) → flash error, no Popen.
- profile_workers=0 (below range) → flash error, no Popen.
- Any profile has running job → flash with blocked profile name, no Popen.
- indexer_jobs row created with profile_name='all', status='queued'.
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


def _make_conn_factory(pg_conn):
    """Return a _get_conn replacement that wraps pg_conn without closing it."""

    class _NoCloseConn:
        def __init__(self, conn):
            self._conn = conn

        def close(self):
            pass

        def cursor(self, *args, **kwargs):
            return self._conn.cursor(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    wrapped = _NoCloseConn(pg_conn)
    return lambda: wrapped


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _setup_profile(migrated_pg, profile_name="all_test_profile"):
    from src.db.repo_registry import add_profile

    return add_profile(migrated_pg, name=profile_name, odoo_version="99.0")


def _count_jobs_by_label(pg_conn, label):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM indexer_jobs WHERE profile_name = %s",
            (label,),
        )
        return cur.fetchone()[0]


def _get_latest_job(pg_conn, label):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT id, profile_name, status FROM indexer_jobs WHERE profile_name = %s"
            " ORDER BY created_at DESC LIMIT 1",
            (label,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"id": row[0], "profile_name": row[1], "status": row[2]}


class TestIndexAllDefaults:
    @pytest.mark.asyncio
    async def test_default_post_redirects_with_flash(self, migrated_pg):
        """POST with default values → 303 redirect with flash containing job id."""
        _setup_profile(migrated_pg, "all_default_1")
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/index-all",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location
        assert "Index+all+started" in location or "Index all started" in location.replace("+", " ")

    @pytest.mark.asyncio
    async def test_default_argv_contains_only_index_repo_all(self, migrated_pg):
        """POST with defaults → argv is index-repo --all (no optional flags)."""
        _setup_profile(migrated_pg, "all_default_2")
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/repos/index-all",
                    data={"max_workers": "1", "profile_workers": "1"},
                    follow_redirects=False,
                )

        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        # Must include indexer invocation
        assert "-m" in argv
        assert "src.indexer" in argv
        # Core subcommand
        assert "index-repo" in argv
        assert "--all" in argv
        # --job-id appended by helper
        assert "--job-id" in argv
        # No optional flags by default
        assert "--no-embed" not in argv
        assert "--full" not in argv
        assert "--max-workers" not in argv
        assert "--profile-workers" not in argv

    @pytest.mark.asyncio
    async def test_indexer_jobs_row_created_with_label_all(self, migrated_pg):
        """POST → indexer_jobs row with profile_name='all' and status='queued'."""
        _setup_profile(migrated_pg, "all_jobs_1")
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ):
            async with _async_client(app) as client:
                await client.post("/repos/index-all", follow_redirects=False)

        job = _get_latest_job(migrated_pg, "all")
        assert job is not None
        assert job["profile_name"] == "all"
        assert job["status"] == "queued"


class TestIndexAllAllFlags:
    @pytest.mark.asyncio
    async def test_all_flags_in_argv(self, migrated_pg):
        """POST with full=on, no_embed=on, max_workers=4, profile_workers=2 → argv has all flags."""
        _setup_profile(migrated_pg, "all_flags_1")
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/repos/index-all",
                    data={
                        "full": "on",
                        "no_embed": "on",
                        "max_workers": "4",
                        "profile_workers": "2",
                    },
                    follow_redirects=False,
                )

        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "--full" in argv
        assert "--no-embed" in argv

        assert "--max-workers" in argv
        idx_mw = argv.index("--max-workers")
        assert argv[idx_mw + 1] == "4"

        assert "--profile-workers" in argv
        idx_pw = argv.index("--profile-workers")
        assert argv[idx_pw + 1] == "2"


class TestIndexAllValidation:
    @pytest.mark.asyncio
    async def test_max_workers_too_high_returns_flash_no_popen(self, migrated_pg):
        """POST with max_workers=9 → 303 flash error, no Popen."""
        _setup_profile(migrated_pg, "all_val_1")
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/index-all",
                    data={"max_workers": "9", "profile_workers": "1"},
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location
        assert "8" in location  # flash should mention the max limit 8
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_profile_workers_too_high_returns_flash_no_popen(self, migrated_pg):
        """POST with profile_workers=5 → 303 flash error, no Popen."""
        _setup_profile(migrated_pg, "all_val_2")
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/index-all",
                    data={"max_workers": "1", "profile_workers": "5"},
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location
        assert "4" in location  # flash should mention the max limit 4
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_profile_workers_too_low_returns_flash_no_popen(self, migrated_pg):
        """POST with profile_workers=0 → 303 flash error, no Popen."""
        _setup_profile(migrated_pg, "all_val_3")
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/index-all",
                    data={"max_workers": "1", "profile_workers": "0"},
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        assert "flash=" in resp.headers["location"]
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_workers_non_int_returns_flash(self, migrated_pg):
        """POST with max_workers=bad → 303 flash error, no Popen."""
        _setup_profile(migrated_pg, "all_val_4")
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/index-all",
                    data={"max_workers": "bad", "profile_workers": "1"},
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        assert "flash=" in resp.headers["location"]
        mock_popen.assert_not_called()


class TestIndexAllRunningGuard:
    @pytest.mark.asyncio
    async def test_blocked_by_running_job_no_popen(self, migrated_pg):
        """When any profile has running indexer → flash with profile name, no Popen."""
        _setup_profile(migrated_pg, "all_guard_profile")
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=True
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/index-all",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location
        # Flash must mention the blocked profile name
        decoded = location.replace("+", " ").replace("%2C", ",").replace("%3A", ":")
        assert "all_guard_profile" in decoded
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_profiles_still_redirects_ok(self, migrated_pg):
        """POST when no profiles exist → no job created, redirects with flash."""
        # No profiles created — DB is empty (but migrated)
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/index-all",
                    follow_redirects=False,
                )

        # With no profiles, blocked list is empty → job spawned anyway (--all handles empty)
        assert resp.status_code == 303
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "index-repo" in argv
        assert "--all" in argv
