# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_index_all.py
"""Integration tests for POST /api/repos/index-all (M8 W1 pure JSON API).

Covers:
- Default POST → 200 ok JSON, argv = index-repo --all only.
- POST with all flags set → argv contains --full, --no-embed, --max-workers, --profile-workers.
- max_workers=9 (out of range) → 422 error JSON, no Popen.
- profile_workers=5 (out of range) → 422 error JSON, no Popen.
- profile_workers=0 (below range) → 422 error JSON, no Popen.
- Any profile has running job → 409 error JSON with blocked profile name, no Popen.
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


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _setup_profile(migrated_pg, profile_name="all_test_profile"):
    from src.db.pg import repo_store

    return repo_store().add_profile(name=profile_name, odoo_version="99.0")


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
    async def test_default_post_returns_ok(self, migrated_pg):
        """POST with default values → 200 ok JSON with job_id."""
        _setup_profile(migrated_pg, "all_default_1")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/index-all",
                    json={},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert "job_id" in body

    @pytest.mark.asyncio
    async def test_default_argv_contains_only_index_repo_all(self, migrated_pg):
        """POST with defaults → argv is index-repo --all (no optional flags)."""
        _setup_profile(migrated_pg, "all_default_2")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/api/repos/index-all",
                    json={"max_workers": "1", "profile_workers": "1"},
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
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ):
            async with _async_client(app) as client:
                await client.post("/api/repos/index-all", json={})

        job = _get_latest_job(migrated_pg, "all")
        assert job is not None
        assert job["profile_name"] == "all"
        assert job["status"] == "queued"


class TestIndexAllAllFlags:
    @pytest.mark.asyncio
    async def test_all_flags_in_argv(self, migrated_pg):
        """POST with full, no_embed, max_workers=4, profile_workers=2 → argv has all flags."""
        _setup_profile(migrated_pg, "all_flags_1")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/api/repos/index-all",
                    json={
                        "full": "on",
                        "no_embed": "on",
                        "max_workers": "4",
                        "profile_workers": "2",
                    },
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
    async def test_max_workers_too_high_returns_422(self, migrated_pg):
        """POST with max_workers=9 → 422 error JSON, no Popen."""
        _setup_profile(migrated_pg, "all_val_1")
        app = create_app()
        with mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/index-all",
                    json={"max_workers": "9", "profile_workers": "1"},
                )

        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        assert "8" in body["error"]
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_profile_workers_too_high_returns_422(self, migrated_pg):
        """POST with profile_workers=5 → 422 error JSON, no Popen."""
        _setup_profile(migrated_pg, "all_val_2")
        app = create_app()
        with mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/index-all",
                    json={"max_workers": "1", "profile_workers": "5"},
                )

        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        assert "4" in body["error"]
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_profile_workers_too_low_returns_422(self, migrated_pg):
        """POST with profile_workers=0 → 422 error JSON, no Popen."""
        _setup_profile(migrated_pg, "all_val_3")
        app = create_app()
        with mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/index-all",
                    json={"max_workers": "1", "profile_workers": "0"},
                )

        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_workers_non_int_returns_422(self, migrated_pg):
        """POST with max_workers=bad → 422 error JSON, no Popen."""
        _setup_profile(migrated_pg, "all_val_4")
        app = create_app()
        with mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/index-all",
                    json={"max_workers": "bad", "profile_workers": "1"},
                )

        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        mock_popen.assert_not_called()


class TestIndexAllRunningGuard:
    @pytest.mark.asyncio
    async def test_blocked_by_running_job_no_popen(self, migrated_pg):
        """When any profile has running indexer → 409 with profile name, no Popen."""
        _setup_profile(migrated_pg, "all_guard_profile")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=True
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/index-all",
                    json={},
                )

        assert resp.status_code == 409
        body = resp.json()
        assert "error" in body
        # Error must mention the blocked profile name
        assert "all_guard_profile" in body["error"]
        mock_popen.assert_not_called()
