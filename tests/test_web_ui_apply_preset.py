# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_apply_preset.py
"""Integration tests for POST /api/operations/apply-preset (M8 W1 pure JSON API).

Tests:
  - valid preset + dry_run=on → 200 ok JSON with preview stdout; no DB changes
  - valid preset + no dry_run → 200 ok JSON with success message; no DB changes (mock subprocess)
  - invalid preset name → 400 error JSON; no DB changes
  - repo_map_urls + repo_map_paths → argv contains --repo-map url=path pairs
  - mismatched repo_map lengths → 400 error JSON
"""
import subprocess
import sys
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres

# PRESETS ships empty by default (bundled deployment presets were removed;
# admins create profiles/repos via the web UI or JSON API). A synthetic preset
# is injected per-test so the apply-preset endpoint logic stays under test
# without shipping any deployment data.
_FIRST_PRESET_KEY = "test-17.0"
_FIRST_PRESET = {
    "profile_name": "test17",
    "odoo_version": "17.0",
    "description": "Synthetic test preset",
    "repos": [
        {"url": "https://github.com/odoo/odoo", "branch": "17.0",
         "local_path_hint": "~/git/odoo_17.0"},
    ],
}


@pytest.fixture(autouse=True)
def _inject_test_preset(monkeypatch):
    """Inject a synthetic preset into the route's PRESETS so endpoint tests
    exercise the apply-preset logic (production PRESETS is empty)."""
    monkeypatch.setattr(
        "src.web_ui.routes.operations.PRESETS",
        {_FIRST_PRESET_KEY: _FIRST_PRESET},
    )


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _fake_dry_run_result():
    """Return a fake CompletedProcess for a dry-run invocation."""
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            f"[dry-run] Profile: {_FIRST_PRESET['profile_name']}"
            f"  odoo_version={_FIRST_PRESET['odoo_version']}\n"
            f"[dry-run] Description: {_FIRST_PRESET['description']}\n"
            "[dry-run] Repos:\n"
            "[dry-run]   https://github.com/odoo/odoo@17.0 → /tmp/odoo_17.0\n"
        ),
        stderr="",
    )


def _fake_real_apply_result():
    """Return a fake CompletedProcess for a real (non-dry-run) invocation."""
    profile_name = _FIRST_PRESET["profile_name"]
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            f"Profile {profile_name} registered with 2 repos. "
            f"Run 'python -m src.indexer index-repo --profile {profile_name}' to index.\n"
        ),
        stderr="",
    )


class TestApplyPresetDryRun:
    """POST /api/operations/apply-preset with dry_run set."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM repos")
            cur.execute("DELETE FROM profiles")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM repos")
            cur.execute("DELETE FROM profiles")

    @pytest.mark.asyncio
    async def test_dry_run_returns_200_with_preview(self, migrated_pg):
        """POST valid preset + dry_run=on → 200 ok JSON; response body contains preview."""
        app = create_app()
        with mock.patch("subprocess.run", return_value=_fake_dry_run_result()):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/operations/apply-preset",
                    json={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("dry_run") is True
        # Preview content must contain [dry-run] markers from stdout
        assert "[dry-run]" in body.get("preview", "") or (
            "dry-run" in body.get("preview", "").lower()
        )

    @pytest.mark.asyncio
    async def test_dry_run_no_db_changes(self, migrated_pg):
        """Dry-run POST must not create any profile or repo rows in DB."""
        app = create_app()
        with mock.patch("subprocess.run", return_value=_fake_dry_run_result()):
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/apply-preset",
                    json={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                )

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM profiles")
            assert cur.fetchone()[0] == 0, "dry-run must not create profile rows"
            cur.execute("SELECT COUNT(*) FROM repos")
            assert cur.fetchone()[0] == 0, "dry-run must not create repo rows"

    @pytest.mark.asyncio
    async def test_dry_run_response_contains_preset_info(self, migrated_pg):
        """Dry-run response must include preset name."""
        app = create_app()
        with mock.patch("subprocess.run", return_value=_fake_dry_run_result()):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/operations/apply-preset",
                    json={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                )

        body = resp.json()
        assert body.get("preset") == _FIRST_PRESET_KEY

    @pytest.mark.asyncio
    async def test_dry_run_subprocess_argv_contains_dry_run_flag(self, migrated_pg):
        """subprocess.run argv must include --dry-run when dry_run is set."""
        app = create_app()
        with mock.patch("subprocess.run", return_value=_fake_dry_run_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/apply-preset",
                    json={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                )

        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]
        assert "--dry-run" in argv
        assert _FIRST_PRESET_KEY in argv
        assert "-m" in argv
        assert "src.manager" in argv
        assert "apply-preset" in argv


class TestApplyPresetRealApply:
    """POST /api/operations/apply-preset without dry_run (real apply)."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM repos")
            cur.execute("DELETE FROM profiles")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM repos")
            cur.execute("DELETE FROM profiles")

    @pytest.mark.asyncio
    async def test_real_apply_returns_ok(self, migrated_pg):
        """POST valid preset, no dry_run → 200 ok JSON with success message."""
        app = create_app()
        with mock.patch("subprocess.run", return_value=_fake_real_apply_result()):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/operations/apply-preset",
                    json={"name": _FIRST_PRESET_KEY},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert "applied" in body.get("message", "").lower() or (
            _FIRST_PRESET_KEY in body.get("message", "")
        )

    @pytest.mark.asyncio
    async def test_real_apply_no_dry_run_flag_in_argv(self, migrated_pg):
        """subprocess.run argv must NOT contain --dry-run when dry_run is empty."""
        app = create_app()
        with mock.patch("subprocess.run", return_value=_fake_real_apply_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/apply-preset",
                    json={"name": _FIRST_PRESET_KEY},
                )

        argv = mock_run.call_args[0][0]
        assert "--dry-run" not in argv


class TestApplyPresetInvalidName:
    """POST /api/operations/apply-preset with invalid/unknown preset name."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM repos")
            cur.execute("DELETE FROM profiles")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM repos")
            cur.execute("DELETE FROM profiles")

    @pytest.mark.asyncio
    async def test_invalid_preset_returns_400(self, migrated_pg):
        """POST with unknown preset name → 400 error JSON."""
        app = create_app()
        with mock.patch("subprocess.run") as mock_run:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/operations/apply-preset",
                    json={"name": "nonexistent-preset-9999", "dry_run": "on"},
                )

        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert "nonexistent-preset-9999" in body["error"] or "Unknown preset" in body["error"]
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_preset_no_db_changes(self, migrated_pg):
        """POST with unknown preset name → no profile or repo rows created."""
        app = create_app()
        with mock.patch("subprocess.run"):
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/apply-preset",
                    json={"name": "nonexistent-preset-9999", "dry_run": "on"},
                )

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM profiles")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM repos")
            assert cur.fetchone()[0] == 0


class TestApplyPresetRepoMap:
    """POST /api/operations/apply-preset with repo_map overrides."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM repos")
            cur.execute("DELETE FROM profiles")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM repos")
            cur.execute("DELETE FROM profiles")

    @pytest.mark.asyncio
    async def test_repo_map_pairs_in_argv(self, migrated_pg):
        """POST with repo_map_urls + repo_map_paths → argv contains --repo-map pairs."""
        app = create_app()
        with mock.patch("subprocess.run", return_value=_fake_dry_run_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/apply-preset",
                    json={
                        "name": _FIRST_PRESET_KEY,
                        "dry_run": "on",
                        "repo_map_urls": ["https://github.com/url1", "https://github.com/url2"],
                        "repo_map_paths": ["/tmp/path1", "/tmp/path2"],
                    },
                )

        argv = mock_run.call_args[0][0]
        # Both --repo-map entries must be in argv
        assert "--repo-map" in argv
        repo_map_idx = [i for i, a in enumerate(argv) if a == "--repo-map"]
        assert len(repo_map_idx) == 2
        assert "https://github.com/url1=/tmp/path1" in argv
        assert "https://github.com/url2=/tmp/path2" in argv

    @pytest.mark.asyncio
    async def test_mismatched_repo_map_lengths_returns_400(self, migrated_pg):
        """POST with more repo_map_urls than repo_map_paths → 400 error JSON; no subprocess call."""
        app = create_app()
        with mock.patch("subprocess.run") as mock_run:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/operations/apply-preset",
                    json={
                        "name": _FIRST_PRESET_KEY,
                        "dry_run": "on",
                        "repo_map_urls": ["https://github.com/url1", "https://github.com/url2"],
                        "repo_map_paths": ["/tmp/path1"],  # one fewer path
                    },
                )

        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_repo_base_dir_in_argv(self, migrated_pg, tmp_path):
        """repo_base_dir provided and exists → --repo-base-dir in argv."""
        app = create_app()
        with mock.patch("subprocess.run", return_value=_fake_dry_run_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/apply-preset",
                    json={
                        "name": _FIRST_PRESET_KEY,
                        "dry_run": "on",
                        "repo_base_dir": str(tmp_path),
                    },
                )

        argv = mock_run.call_args[0][0]
        assert "--repo-base-dir" in argv
        assert str(tmp_path) in argv

    @pytest.mark.asyncio
    async def test_nonexistent_repo_base_dir_returns_400(self, migrated_pg):
        """repo_base_dir provided but does not exist → 400 error JSON; no subprocess call."""
        app = create_app()
        with mock.patch("subprocess.run") as mock_run:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/operations/apply-preset",
                    json={
                        "name": _FIRST_PRESET_KEY,
                        "dry_run": "on",
                        "repo_base_dir": "/does/not/exist/dir",
                    },
                )

        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert "/does/not/exist/dir" in body["error"] or "not exist" in body["error"].lower()
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_subprocess_run_uses_sys_executable(self, migrated_pg):
        """subprocess.run must be called with sys.executable as the first element."""
        app = create_app()
        with mock.patch("subprocess.run", return_value=_fake_dry_run_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/apply-preset",
                    json={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                )

        argv = mock_run.call_args[0][0]
        assert argv[0] == sys.executable

    @pytest.mark.asyncio
    async def test_subprocess_run_timeout_120(self, migrated_pg):
        """subprocess.run must be called with timeout=120 (default APPLY_PRESET_TIMEOUT)."""
        app = create_app()
        with mock.patch("subprocess.run", return_value=_fake_dry_run_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/api/operations/apply-preset",
                    json={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                )

        kwargs = mock_run.call_args[1]
        assert kwargs.get("timeout") == 120
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

    @pytest.mark.asyncio
    async def test_subprocess_failure_returns_400(self, migrated_pg):
        """subprocess.run returning non-zero exit code → 400 error JSON containing stderr."""
        error_result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Local path /tmp/missing_repo does not exist",
        )
        app = create_app()
        with mock.patch("subprocess.run", return_value=error_result):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/operations/apply-preset",
                    json={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                )

        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert "does not exist" in body["error"] or "failed" in body["error"].lower()
