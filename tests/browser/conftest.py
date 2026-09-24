# SPDX-License-Identifier: AGPL-3.0-or-later
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
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

ASTRO_PORT = 4321
API_PORT = 8003
SITE_DIR = Path(__file__).resolve().parents[2] / "site"


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _wait_for_server(url: str, timeout: int = 30, proc: subprocess.Popen | None = None) -> bool:
    """Poll GET url until 200 or timeout. Returns True on success.

    Gives up early when *proc* (the server being waited for) has already exited.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            code = urllib.request.urlopen(url, timeout=1).getcode()
            if code < 400:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _stop_process_group(proc: subprocess.Popen) -> None:
    """Stop *proc* and every child it spawned (pnpm preview forks node)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def _start_server(cmd: list[str], *, port: int, probe_url: str, name: str, **popen_kwargs):
    """Start a test server in its own process group, or skip with the reason.

    Never reuses a foreign listener: a port already bound by another process
    would make the tests assert against a server this session did not start.
    The caller must pass the returned process to :func:`_stop_process_group`.
    """
    if _port_in_use(port):
        pytest.skip(
            f"{name}: 127.0.0.1:{port} is already in use by another process;"
            " stop it so the tests run against a server this session starts."
        )
    try:
        proc = subprocess.Popen(cmd, start_new_session=True, **popen_kwargs)
    except FileNotFoundError as exc:
        pytest.skip(f"{name}: cannot start {cmd[0]!r} ({exc})")
    if not _wait_for_server(probe_url, timeout=30, proc=proc):
        _stop_process_group(proc)
        pytest.skip(f"{name} did not start on port {port} within 30s.")
    return proc


@pytest.fixture(scope="session")
def astro_server():
    """Start Astro preview (pnpm preview) in background on port 4321.

    Requires a prior `pnpm build` in site/. In CI this is done by the
    workflow step; locally run `cd site && pnpm build` once before tests.

    Yields the base URL string: "http://127.0.0.1:4321"
    """
    base_url = f"http://127.0.0.1:{ASTRO_PORT}"
    proc = _start_server(
        ["pnpm", "preview", "--host", "127.0.0.1", "--port", str(ASTRO_PORT)],
        port=ASTRO_PORT,
        probe_url=base_url,
        name="Astro preview (run `cd site && pnpm build` first)",
        cwd=str(SITE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield base_url
    finally:
        _stop_process_group(proc)


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
    # NOTE: We do NOT set WEBUI_AUTH_DISABLED or PYTEST_CURRENT_TEST here. Admin
    # browser tests use real session cookies — see tests/browser/admin/conftest.py
    # for the _admin_session_cookie fixture that seeds a test user, logs in via
    # POST /api/auth/login, and injects the resulting Set-Cookie into the
    # Playwright browser context. The earlier auth-bypass approach defeated
    # tests in test_login.py / test_logout.py that exercise pre-auth flows.

    # Why --log-level warning: ``critical`` swallows the FastAPI/Starlette
    # error logger output, so unhandled exceptions in route handlers surface
    # only as the bare HTTP 500 body ``Internal Server Error`` with no
    # traceback in the pytest job output. Bumping to ``warning`` lets the
    # default Starlette error logger write tracebacks to stderr (inherited
    # by the pytest process), making future regressions diagnosable from CI
    # logs without rerunning locally. ``warning`` keeps the access-log
    # noise low (200/304/redirects stay silent).
    base_url = f"http://127.0.0.1:{API_PORT}"
    # Poll /openapi.json - always 200 when FastAPI is up, no auth needed
    proc = _start_server(
        [
            "python", "-m", "uvicorn",
            "src.web_ui.app:create_app",
            "--factory",
            "--host", "127.0.0.1",
            "--port", str(API_PORT),
            "--log-level", "warning",
        ],
        port=API_PORT,
        probe_url=f"{base_url}/openapi.json",
        name="FastAPI api_server",
        env=env,
    )
    try:
        yield base_url
    finally:
        _stop_process_group(proc)
