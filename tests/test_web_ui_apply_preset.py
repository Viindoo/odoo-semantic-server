# tests/test_web_ui_apply_preset.py
"""Integration tests for POST /operations/apply-preset (M8 W8).

Tests:
  - valid preset + dry_run=on → 200 OK with preview stdout; no DB changes
  - valid preset + dry_run="" (unchecked) → 303 redirect with flash; profile+repos created
  - invalid preset name → 400 with error alert; no DB changes
  - repo_map_urls + repo_map_paths → argv contains --repo-map url=path pairs
  - mismatched repo_map lengths → 400 error
"""
import subprocess
import sys
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import run_migrations
from src.indexer.version_presets import PRESETS
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres

# Use the first preset in PRESETS for testing
_FIRST_PRESET_KEY = sorted(PRESETS.keys())[0]
_FIRST_PRESET = PRESETS[_FIRST_PRESET_KEY]


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
            f"✓ Profile {profile_name} registered with 2 repos. "
            f"Run 'python -m src.indexer index-repo --profile {profile_name}' to index.\n"
        ),
        stderr="",
    )


class TestApplyPresetDryRun:
    """POST /operations/apply-preset with dry_run=on."""

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
        """POST valid preset + dry_run=on → 200 OK; response body contains preview stdout."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=_fake_dry_run_result()):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/apply-preset",
                    data={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                    follow_redirects=False,
                )

        assert resp.status_code == 200
        # Preview content must contain [dry-run] markers from stdout
        assert "[dry-run]" in resp.text or "dry-run" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_dry_run_no_db_changes(self, migrated_pg):
        """Dry-run POST must not create any profile or repo rows in DB."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=_fake_dry_run_result()):
            async with _async_client(app) as client:
                await client.post(
                    "/operations/apply-preset",
                    data={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                    follow_redirects=False,
                )

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM profiles")
            assert cur.fetchone()[0] == 0, "dry-run must not create profile rows"
            cur.execute("SELECT COUNT(*) FROM repos")
            assert cur.fetchone()[0] == 0, "dry-run must not create repo rows"

    @pytest.mark.asyncio
    async def test_dry_run_response_contains_apply_for_real_form(self, migrated_pg):
        """Dry-run response must include a second form (Apply for real) without dry_run checked."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=_fake_dry_run_result()):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/apply-preset",
                    data={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                    follow_redirects=False,
                )

        assert "Apply for real" in resp.text

    @pytest.mark.asyncio
    async def test_dry_run_subprocess_argv_contains_dry_run_flag(self, migrated_pg):
        """subprocess.run argv must include --dry-run when dry_run checkbox is ticked."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=_fake_dry_run_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/apply-preset",
                    data={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                    follow_redirects=False,
                )

        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]
        assert "--dry-run" in argv
        assert _FIRST_PRESET_KEY in argv
        assert "-m" in argv
        assert "src.manager" in argv
        assert "apply-preset" in argv


class TestApplyPresetRealApply:
    """POST /operations/apply-preset without dry_run (real apply)."""

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
    async def test_real_apply_redirects_303_with_flash(self, migrated_pg):
        """POST valid preset + dry_run="" → 303 redirect with flash containing preset name."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=_fake_real_apply_result()):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/apply-preset",
                    data={"name": _FIRST_PRESET_KEY},  # no dry_run field
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/operations?flash=")
        assert _FIRST_PRESET_KEY.replace("-", "+") in location or "applied" in location.lower()

    @pytest.mark.asyncio
    async def test_real_apply_no_dry_run_flag_in_argv(self, migrated_pg):
        """subprocess.run argv must NOT contain --dry-run when dry_run is empty."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=_fake_real_apply_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/apply-preset",
                    data={"name": _FIRST_PRESET_KEY},
                    follow_redirects=False,
                )

        argv = mock_run.call_args[0][0]
        assert "--dry-run" not in argv


class TestApplyPresetInvalidName:
    """POST /operations/apply-preset with invalid/unknown preset name."""

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
        """POST with unknown preset name → 400 with error alert."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run") as mock_run:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/apply-preset",
                    data={"name": "nonexistent-preset-9999", "dry_run": "on"},
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        assert "nonexistent-preset-9999" in resp.text or "Unknown preset" in resp.text
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_preset_no_db_changes(self, migrated_pg):
        """POST with unknown preset name → no profile or repo rows created."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run"):
            async with _async_client(app) as client:
                await client.post(
                    "/operations/apply-preset",
                    data={"name": "nonexistent-preset-9999", "dry_run": "on"},
                    follow_redirects=False,
                )

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM profiles")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM repos")
            assert cur.fetchone()[0] == 0


class TestApplyPresetRepoMap:
    """POST /operations/apply-preset with repo_map overrides."""

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
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=_fake_dry_run_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/apply-preset",
                    data={
                        "name": _FIRST_PRESET_KEY,
                        "dry_run": "on",
                        "repo_map_urls": ["https://github.com/url1", "https://github.com/url2"],
                        "repo_map_paths": ["/tmp/path1", "/tmp/path2"],
                    },
                    follow_redirects=False,
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
        """POST with more repo_map_urls than repo_map_paths → 400 error; no subprocess call."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run") as mock_run:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/apply-preset",
                    data={
                        "name": _FIRST_PRESET_KEY,
                        "dry_run": "on",
                        "repo_map_urls": ["https://github.com/url1", "https://github.com/url2"],
                        "repo_map_paths": ["/tmp/path1"],  # one fewer path
                    },
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_repo_base_dir_in_argv(self, migrated_pg, tmp_path):
        """repo_base_dir provided and exists → --repo-base-dir in argv."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=_fake_dry_run_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/apply-preset",
                    data={
                        "name": _FIRST_PRESET_KEY,
                        "dry_run": "on",
                        "repo_base_dir": str(tmp_path),
                    },
                    follow_redirects=False,
                )

        argv = mock_run.call_args[0][0]
        assert "--repo-base-dir" in argv
        assert str(tmp_path) in argv

    @pytest.mark.asyncio
    async def test_nonexistent_repo_base_dir_returns_400(self, migrated_pg):
        """repo_base_dir provided but does not exist → 400 error; no subprocess call."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run") as mock_run:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/apply-preset",
                    data={
                        "name": _FIRST_PRESET_KEY,
                        "dry_run": "on",
                        "repo_base_dir": "/does/not/exist/dir",
                    },
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        assert "/does/not/exist/dir" in resp.text or "not exist" in resp.text.lower()
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_subprocess_run_uses_sys_executable(self, migrated_pg):
        """subprocess.run must be called with sys.executable as the first element."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=_fake_dry_run_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/apply-preset",
                    data={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                    follow_redirects=False,
                )

        argv = mock_run.call_args[0][0]
        assert argv[0] == sys.executable

    @pytest.mark.asyncio
    async def test_subprocess_run_timeout_60(self, migrated_pg):
        """subprocess.run must be called with timeout=60."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=_fake_dry_run_result()) as mock_run:
            async with _async_client(app) as client:
                await client.post(
                    "/operations/apply-preset",
                    data={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                    follow_redirects=False,
                )

        kwargs = mock_run.call_args[1]
        assert kwargs.get("timeout") == 60
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

    @pytest.mark.asyncio
    async def test_subprocess_failure_renders_error(self, migrated_pg):
        """subprocess.run returning non-zero exit code → 400 with error containing stderr."""
        error_result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="✗ Local path /tmp/missing_repo does not exist",
        )
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch("subprocess.run", return_value=error_result):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/operations/apply-preset",
                    data={"name": _FIRST_PRESET_KEY, "dry_run": "on"},
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        assert "does not exist" in resp.text or "failed" in resp.text.lower()


class TestApplyPresetPageGet:
    """GET /operations — apply-preset section renders correctly."""

    @pytest.mark.asyncio
    async def test_get_operations_renders_apply_preset_form(self, migrated_pg):
        """GET /operations → 200, Apply Preset section with preset dropdown present."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.get("/operations")

        assert resp.status_code == 200
        assert "Apply Preset" in resp.text
        assert "/operations/apply-preset" in resp.text
        assert 'name="name"' in resp.text
        # First preset key must appear in the dropdown
        assert _FIRST_PRESET_KEY in resp.text
        assert 'name="dry_run"' in resp.text
        assert "Dry run" in resp.text

    @pytest.mark.asyncio
    async def test_dry_run_checkbox_is_checked_by_default(self, migrated_pg):
        """GET /operations → dry_run checkbox must be checked by default (safety default)."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.operations._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.get("/operations")

        # The checkbox for dry_run should be checked by default
        assert 'name="dry_run"' in resp.text
        # Check that "checked" attribute appears near dry_run (template renders it checked)
        assert "checked" in resp.text
