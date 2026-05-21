# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_reset_embed.py
"""Integration tests for POST /api/repos/repos/{id}/reset-embed (M8 W1 pure JSON API).

Covers:
- repo with head_sha set → POST → head_sha IS NULL + job spawned + 200 ok JSON
- argv for spawned subprocess: index-repo --profile X (no --no-embed, no --full)
- repo with head_sha IS NULL → POST still works (NULL → NULL, spawns index)
- indexer running → 409 error JSON, head_sha unchanged, no Popen
- non-existent repo_id → 404 JSON
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


def _setup_profile_and_repo(migrated_pg, profile_name="test_profile", head_sha=None):
    from src.db.pg import repo_store

    pid = repo_store().add_profile(name=profile_name, odoo_version="99.0")
    rid = repo_store().add_repo(
        profile_id=pid,
        url="file://local",
        branch="99.0",
        local_path="/tmp/odoo_reset_embed_test",
    )
    if head_sha is not None:
        with migrated_pg.cursor() as cur:
            cur.execute("UPDATE repos SET head_sha = %s WHERE id = %s", (head_sha, rid))
    return pid, rid


def _get_head_sha(pg_conn, repo_id):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE id = %s", (repo_id,))
        row = cur.fetchone()
        return row[0] if row else None


class TestResetEmbed:
    @pytest.mark.asyncio
    async def test_head_sha_set_to_null_after_post(self, migrated_pg):
        """POST with head_sha='abc123' → head_sha IS NULL in DB after, 200 ok JSON."""
        _, rid = _setup_profile_and_repo(migrated_pg, "re_profile_1", head_sha="abc123")
        assert _get_head_sha(migrated_pg, rid) == "abc123"

        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/reset-embed",
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert _get_head_sha(migrated_pg, rid) is None

    @pytest.mark.asyncio
    async def test_response_contains_job_id(self, migrated_pg):
        """POST → 200 ok JSON containing job_id."""
        _, rid = _setup_profile_and_repo(migrated_pg, "re_profile_2", head_sha="deadbeef")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/reset-embed",
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert "job_id" in body

    @pytest.mark.asyncio
    async def test_argv_has_index_repo_profile_no_no_embed_no_full(self, migrated_pg):
        """argv must be: index-repo --profile X (no --no-embed, no --full)."""
        _, rid = _setup_profile_and_repo(migrated_pg, "re_argv_profile", head_sha="sha123")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    f"/api/repos/repos/{rid}/reset-embed",
                )

        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]

        # Must contain indexer invocation
        assert "-m" in argv
        assert "src.indexer" in argv

        # Must contain index-repo subcommand and correct profile
        assert "index-repo" in argv
        assert "--profile" in argv
        idx = argv.index("--profile")
        assert argv[idx + 1] == "re_argv_profile"

        # Must NOT have --no-embed or --full
        assert "--no-embed" not in argv
        assert "--full" not in argv

        # Must have --job-id (appended by spawn_indexer_subcommand)
        assert "--job-id" in argv

    @pytest.mark.asyncio
    async def test_post_with_null_head_sha_still_works(self, migrated_pg):
        """POST on repo with head_sha IS NULL → still succeeds (NULL → NULL, spawns index)."""
        _, rid = _setup_profile_and_repo(migrated_pg, "re_null_profile", head_sha=None)
        assert _get_head_sha(migrated_pg, rid) is None

        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/reset-embed",
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        # head_sha remains NULL
        assert _get_head_sha(migrated_pg, rid) is None
        # Subprocess was still spawned
        mock_popen.assert_called_once()

    @pytest.mark.asyncio
    async def test_indexer_running_blocks_reset_and_no_popen(self, migrated_pg):
        """When indexer_is_running → 409 JSON, head_sha unchanged, no Popen."""
        _, rid = _setup_profile_and_repo(migrated_pg, "re_running_profile", head_sha="sha_before")
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=True
        ), mock.patch(
            "src.web_ui.helpers.subprocess_runner.subprocess.Popen"
        ) as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/reset-embed",
                )

        assert resp.status_code == 409
        body = resp.json()
        assert "error" in body
        # head_sha must remain unchanged
        assert _get_head_sha(migrated_pg, rid) == "sha_before"
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_nonexistent_repo_returns_404(self, migrated_pg):
        """POST on non-existent repo_id → 404 JSON."""
        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/repos/999999/reset-embed",
                )

        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
