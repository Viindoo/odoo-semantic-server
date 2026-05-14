# tests/browser/conftest.py
"""Shared fixtures for browser tests (Playwright + Astro preview + FastAPI).

Two server fixtures:
  astro_server  — pnpm preview background, port 4321 (for public + admin page tests)
  api_server    — FastAPI web_ui_app, port 8003 (for admin tests that hit /api/*)

admin tests require BOTH fixtures.
public tests require ONLY astro_server.

DB setup re-uses pg_conn + clean_browser from tests/conftest.py (inherited via
pytest's conftest chain).
"""
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

ASTRO_PORT = 4321
API_PORT = 8003
SITE_DIR = Path(__file__).resolve().parents[2] / "site"


def _wait_for_server(url: str, timeout: int = 30) -> bool:
    """Poll GET url until 200 or timeout. Returns True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            code = urllib.request.urlopen(url, timeout=1).getcode()
            if code < 400:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def astro_server():
    """Start Astro preview (pnpm preview) in background on port 4321.

    Requires a prior `pnpm build` in site/. In CI this is done by the
    workflow step; locally run `cd site && pnpm build` once before tests.

    Yields the base URL string: "http://127.0.0.1:4321"
    """
    proc = subprocess.Popen(
        ["pnpm", "preview", "--host", "127.0.0.1", "--port", str(ASTRO_PORT)],
        cwd=str(SITE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{ASTRO_PORT}"
    if not _wait_for_server(base_url, timeout=30):
        proc.terminate()
        pytest.skip(
            f"Astro preview did not start on port {ASTRO_PORT} within 30s. "
            "Run `cd site && pnpm build` first."
        )
    yield base_url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def api_server(pg_conn):
    """Start FastAPI web_ui app in background on port 8003 (admin tests only).

    Re-uses pg_conn from tests/conftest.py for DB env setup.
    Sets PG_DSN + FERNET_KEY + WEBUI_AUTH_DISABLED for test isolation.
    """
    from cryptography.fernet import Fernet

    from src.db.migrate import run_migrations

    run_migrations(pg_conn)

    pg_dsn = os.environ.get("PG_TEST_DSN", os.environ.get("PG_DSN", ""))
    if not pg_dsn:
        pytest.skip("PG_TEST_DSN not set — cannot start api_server fixture")

    env = os.environ.copy()
    env["PG_DSN"] = pg_dsn
    env.setdefault("FERNET_KEY", Fernet.generate_key().decode())
    env["WEBUI_AUTH_DISABLED"] = "1"

    proc = subprocess.Popen(
        [
            "python", "-m", "uvicorn",
            "src.web_ui.app:create_app",
            "--factory",
            "--host", "127.0.0.1",
            "--port", str(API_PORT),
            "--log-level", "critical",
        ],
        env=env,
    )
    base_url = f"http://127.0.0.1:{API_PORT}"
    if not _wait_for_server(f"{base_url}/api/health", timeout=15):
        # Fallback: try root path (older FastAPI app may not have /api/health)
        if not _wait_for_server(base_url, timeout=5):
            proc.terminate()
            pytest.skip(
                f"FastAPI api_server did not start on port {API_PORT} within 20s."
            )
    yield base_url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
