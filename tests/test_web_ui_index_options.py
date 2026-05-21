# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_index_options.py
"""Tests for POST /api/repos/repos/{id}/index with extended options (M8 W1 pure JSON API).

Covers:
- --no-embed, --full, --gc flags appended to argv when fields are set
- --max-workers N appended when != 1
- Default POST (no flags) → clean argv without optional flags
- max_workers > 8 → 422 error JSON, no Popen
- max_workers non-int → 422 error JSON, no Popen
- indexer_is_running guard still works after refactor
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


def _setup_profile_and_repo(migrated_pg, profile_name="test_profile"):
    from src.db.pg import repo_store

    pid = repo_store().add_profile(name=profile_name, odoo_version="17.0")
    rid = repo_store().add_repo(
        profile_id=pid,
        url="file://local",
        branch="17.0",
        local_path="/tmp/odoo_opts_test",
    )
    return pid, rid


class TestIndexOptionsFlags:
    @pytest.mark.asyncio
    async def test_full_gc_max_workers_appended_to_argv(self, migrated_pg):
        """POST with full=on, gc=on, max_workers=4 → argv contains --full --gc --max-workers 4."""
        _, rid = _setup_profile_and_repo(migrated_pg, "opts_profile_1")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index",
                    json={"full": "on", "gc": "on", "max_workers": "4"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "--full" in argv
        assert "--gc" in argv
        assert "--max-workers" in argv
        idx = argv.index("--max-workers")
        assert argv[idx + 1] == "4"

    @pytest.mark.asyncio
    async def test_default_values_no_optional_flags(self, migrated_pg):
        """POST with no flags, max_workers=1 → argv has no --full, --gc, --max-workers."""
        _, rid = _setup_profile_and_repo(migrated_pg, "opts_profile_2")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index",
                    json={"max_workers": "1"},
                )

        assert resp.status_code == 200
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "--full" not in argv
        assert "--gc" not in argv
        assert "--no-embed" not in argv
        assert "--max-workers" not in argv

    @pytest.mark.asyncio
    async def test_no_embed_appended(self, migrated_pg):
        """POST with no_embed=on → argv contains --no-embed."""
        _, rid = _setup_profile_and_repo(migrated_pg, "opts_profile_3")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index",
                    json={"no_embed": "on", "max_workers": "1"},
                )

        assert resp.status_code == 200
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "--no-embed" in argv

    @pytest.mark.asyncio
    async def test_max_workers_over_limit_returns_422(self, migrated_pg):
        """POST with max_workers=9 → 422 error JSON, Popen NOT called."""
        _, rid = _setup_profile_and_repo(migrated_pg, "opts_profile_4")
        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index",
                    json={"max_workers": "9"},
                )

        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        assert "8" in body["error"]
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_workers_non_int_returns_422(self, migrated_pg):
        """POST with max_workers=abc → 422 error JSON, Popen NOT called."""
        _, rid = _setup_profile_and_repo(migrated_pg, "opts_profile_5")
        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index",
                    json={"max_workers": "abc"},
                )

        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_indexer_running_guard_still_works(self, migrated_pg):
        """When indexer_is_running is True → 409 JSON, no Popen (guard preserved)."""
        _, rid = _setup_profile_and_repo(migrated_pg, "opts_profile_6")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=True
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index",
                    json={"full": "on", "max_workers": "2"},
                )

        assert resp.status_code == 409
        body = resp.json()
        assert "error" in body
        assert "running" in body["error"].lower() or "already" in body["error"].lower()
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawn_uses_helper_not_inline_popen(self, migrated_pg):
        """Verify refactor: route uses spawn_indexer_subcommand (not inline Popen)."""
        _, rid = _setup_profile_and_repo(migrated_pg, "opts_profile_7")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index",
                    json={"max_workers": "1"},
                )

        assert resp.status_code == 200
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        # Helper prepends `python -m src.indexer` and appends `--job-id N`
        assert "-m" in argv
        assert "src.indexer" in argv
        assert "--job-id" in argv

    @pytest.mark.asyncio
    async def test_argv_contains_profile_name(self, migrated_pg):
        """Verify --profile <name> still present in argv after refactor."""
        _, rid = _setup_profile_and_repo(migrated_pg, "mynamedprofile")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    f"/api/repos/repos/{rid}/index",
                    json={"max_workers": "1"},
                )

        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert "--profile" in argv
        idx = argv.index("--profile")
        assert argv[idx + 1] == "mynamedprofile"

    @pytest.mark.asyncio
    async def test_max_workers_zero_rejected(self, migrated_pg):
        """POST with max_workers=0 → 422 error JSON, Popen not called."""
        _, rid = _setup_profile_and_repo(migrated_pg, "opts_profile_8")
        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index",
                    json={"max_workers": "0"},
                )

        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        mock_popen.assert_not_called()
