# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_seed_patterns.py
"""Integration tests for POST /api/operations/seed-patterns (M8 W1 pure JSON API).

Tests: valid submission → 200 ok JSON + job_id;
       no version → label 'patterns';
       with version → label 'patterns:17.0';
       invalid version → 400 error JSON, no job row;
       non-existent patterns_file → 400 error JSON, no job row;
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


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestSeedPatternsRoute:
    """POST /api/operations/seed-patterns — happy path + validation."""

    @pytest.fixture(autouse=True)
    def _cleanup_jobs(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")

    @pytest.mark.asyncio
    async def test_valid_submission_no_version_returns_ok(self, migrated_pg):
        """POST with force=on, no version → 200 ok JSON with job_id and 'patterns' label."""
        app = create_app()
        with mock.patch("subprocess.Popen"):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/operations/seed-patterns",
                    json={"force": "on"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert "job_id" in body
        assert "patterns" in body.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_valid_submission_no_version_creates_job_label_patterns(self, migrated_pg):
        """POST without version → indexer_jobs row with profile_name='patterns'."""
        app = create_app()
        with mock.patch("subprocess.Popen"):
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/seed-patterns",
                    json={"force": "on"},
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
        with mock.patch("subprocess.Popen"):
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/seed-patterns",
                    json={"version": "17.0", "force": "on"},
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
        """Popen argv must include seed-patterns --force when force provided."""
        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/seed-patterns",
                    json={"force": "on"},
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
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/seed-patterns",
                    json={"version": "17.0", "no_embed": "on"},
                )

        argv = mock_popen.call_args[0][0]
        assert "--version" in argv
        assert "17.0" in argv
        assert "--no-embed" in argv

    @pytest.mark.asyncio
    async def test_argv_without_force_does_not_include_force(self, migrated_pg):
        """When force is NOT provided, --force must NOT appear in argv."""
        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/seed-patterns",
                    json={},
                )

        argv = mock_popen.call_args[0][0]
        assert "--force" not in argv

    @pytest.mark.asyncio
    async def test_invalid_version_returns_400(self, migrated_pg):
        """POST with invalid version string → 400, error in body, no job row created."""
        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/operations/seed-patterns",
                    json={"version": "abc"},
                )

        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert "abc" in body["error"] or "Invalid" in body["error"]
        mock_popen.assert_not_called()

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_nonexistent_patterns_file_returns_400(self, migrated_pg):
        """POST with non-existent patterns_file → 400, error in body, no job row."""
        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/operations/seed-patterns",
                    json={"patterns_file": "/does/not/exist/patterns.json"},
                )

        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert (
            "/does/not/exist/patterns.json" in body["error"]
            or "not exist" in body["error"].lower()
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
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/seed-patterns",
                    json={"patterns_file": str(pf)},
                )

        argv = mock_popen.call_args[0][0]
        assert "--patterns-file" in argv
        assert str(pf) in argv

    @pytest.mark.asyncio
    async def test_valid_version_formats_accepted(self, migrated_pg):
        """Multiple valid version strings (8.0, 17.0, 20.0) are accepted."""
        app = create_app()
        for ver in ("8.0", "17.0", "20.0"):
            with mock.patch("subprocess.Popen"):
                async with _async_client(app) as client:
                    resp = await client.post(
                        "/api/operations/seed-patterns",
                        json={"version": ver},
                    )
            assert resp.status_code == 200, f"version '{ver}' should be valid"
            body = resp.json()
            assert body.get("ok") is True
            with migrated_pg.cursor() as cur:
                cur.execute("DELETE FROM indexer_jobs")
