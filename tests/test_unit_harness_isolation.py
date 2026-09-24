# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_unit_harness_isolation.py
"""The unit run never depends on the browser tier (lane-mcpfix defect 7).

Business rules protected (the harness contract a developer relies on when
running ``make test`` on a box that also runs a site preview or has a stale
Playwright browser cache):

- H1  The unit target never collects a ``browser``- or ``astro``-marked test,
      whatever listens on 127.0.0.1:4321. Checked against the real Makefile
      recipe (``make -n test-unit``), not a copy of its marker string.
- H2  A browser/astro server fixture never reuses a foreign listener on its
      port: with another process on 4321 the astro tests SKIP (naming the busy
      port) and not one HTTP request reaches that foreign process.
- H3  A browser that cannot run never poisons the session: the browser test is
      SKIPPED (not ERROR) and an ``asyncio.run()`` test collected in the same
      session still passes (the #mcpfix incident: 397 failed + 32 errors).
- H4  After a browser-tier session whose server could not start, nothing is
      left listening on the preview port.

Every check drives a real pytest subprocess on this checkout, so the rule is
judged on what pytest actually selects and runs. The planted listener on 4321
is a shared, machine-wide port: another checkout's test run may poll it at the
same time. H2/H3 therefore count only the requests sent by THIS test's own
process tree (the pytest subprocess and anything it spawned), resolved from
the peer socket's owning process; a positive control proves the attribution
sees a request from a child process before the verdict relies on an empty list.

Tiers: H1 binds no port and runs in ``make test``. H2-H4 bind the fixed,
machine-wide preview port 4321 and spawn long pytest subprocesses, so they are
marked ``astro`` and run with ``make test-browser`` (never in the unit run,
where parallel checkouts would race for the port).
"""
from __future__ import annotations

import http.server
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_PREVIEW_PORT = 4321
_PYTEST = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _run(args: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=_REPO, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _collected_ids(stdout: str) -> set[str]:
    return {ln.strip() for ln in stdout.splitlines() if "::" in ln and not ln.startswith(" ")}


def _target_argv(target: str) -> list[str]:
    """The pytest argv a Makefile recipe really runs (``make -n <target>``)."""
    out = subprocess.run(
        ["make", "-n", "-s", "--no-print-directory", "-C", str(_REPO), target],
        capture_output=True, text=True, check=True,
    ).stdout
    lines = [ln for ln in out.splitlines() if re.search(r"(^|/)pytest(\s|$)", ln)]
    assert len(lines) == 1, f"expected one pytest command in `make -n {target}`:\n{out}"
    return shlex.split(lines[0])


def _unit_target_argv() -> list[str]:
    """The pytest argv the Makefile ``test-unit`` recipe really runs."""
    return _target_argv("test-unit")


_PROC = Path("/proc")


def _socket_owner_pid(local_port: int, remote_port: int) -> int | None:
    """Pid of the process holding the TCP socket 127.0.0.1:local_port -> :remote_port.

    Linux ``/proc`` only; None when no readable process holds it.
    """
    inode = None
    for line in (_PROC / "net" / "tcp").read_text().splitlines()[1:]:
        cols = line.split()
        local, remote = cols[1], cols[2]
        if (int(local.rsplit(":", 1)[1], 16) == local_port
                and int(remote.rsplit(":", 1)[1], 16) == remote_port):
            inode = cols[9]
            break
    if inode is None:
        return None
    target = f"socket:[{inode}]"
    for proc in _PROC.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                if os.readlink(fd) == target:
                    return int(proc.name)
        except OSError:
            continue
    return None


def _descends_from(pid: int, ancestor: int) -> bool:
    while pid > 1:
        if pid == ancestor:
            return True
        try:
            stat = (_PROC / str(pid) / "stat").read_text()
        except OSError:
            return False
        pid = int(stat.rsplit(")", 1)[1].split()[1])
    return pid == ancestor


def _sent_by_this_test(client_port: int) -> bool:
    """True when the request on *client_port* came from this process's tree.

    Without ``/proc`` (not Linux) every request counts as ours - the check
    stays strict rather than blind.
    """
    if not (_PROC / "net" / "tcp").exists():
        return True
    pid = _socket_owner_pid(client_port, _PREVIEW_PORT)
    return pid is not None and _descends_from(pid, os.getpid())


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    requests: list[str] = []

    def _answer(self):
        if _sent_by_this_test(self.client_address[1]):
            type(self).requests.append(f"{self.command} {self.path}")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b"<html>not the Astro preview</html>")

    do_GET = _answer
    do_HEAD = _answer

    def log_message(self, *args):  # keep the pytest output clean
        pass


@pytest.fixture
def foreign_listener():
    """A throwaway HTTP server on 127.0.0.1:4321 recording the requests this test's
    process tree sends (another checkout's run polling the port is not counted)."""
    if _port_in_use(_PREVIEW_PORT):
        pytest.skip(f"127.0.0.1:{_PREVIEW_PORT} is already taken; cannot plant a listener")
    _CountingHandler.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", _PREVIEW_PORT), _CountingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Positive control: a request from a child process IS attributed to us.
        probe = subprocess.run(
            [sys.executable, "-c",
             "import urllib.request; urllib.request.urlopen("
             f"'http://127.0.0.1:{_PREVIEW_PORT}/__attribution_probe', timeout=10).read()"],
            capture_output=True, text=True, timeout=60,
        )
        assert probe.returncode == 0, probe.stderr[-2000:]
        assert "GET /__attribution_probe" in _CountingHandler.requests, (
            f"a child-process request was not attributed to this test: "
            f"{_CountingHandler.requests[:5]}")
        _CountingHandler.requests.clear()
        yield _CountingHandler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not _port_in_use(_PREVIEW_PORT), "the planted listener must be gone"


# ---------------------------------------------------------------------------
# H1 - the unit target selects no browser/astro test
# ---------------------------------------------------------------------------

def test_unit_target_collects_no_browser_or_astro_test():
    """H1 (FIX): `make test` must not select a single browser- or astro-marked test.

    Pre-fix the recipe was ``-m "not neo4j and not postgres"``: the public
    browser tests (marker ``browser`` only) and the astro header tests were
    selected by every unit run.
    """
    browser_tier = _run([*_PYTEST, "tests/", "--collect-only", "-q", "-m", "browser or astro"])
    browser_ids = _collected_ids(browser_tier.stdout)
    # Positive control: the browser tier exists, so an empty intersection means something.
    assert any(i.startswith("tests/browser/public/test_404.py") for i in browser_ids), (
        browser_tier.stdout[-2000:])
    assert any(i.startswith("tests/browser/public/test_cache_headers.py") for i in browser_ids)

    argv = _unit_target_argv()
    # Verbosity flags dropped so the collection prints one node id per line.
    selection = [a for a in argv[1:] if a not in ("-v", "-vv", "-q")]
    unit = _run([*_PYTEST, *selection, "--collect-only", "-q"])
    unit_ids = _collected_ids(unit.stdout)
    assert any(i.startswith("tests/test_marker_discipline.py::") for i in unit_ids), (
        unit.stdout[-2000:])
    leaked = sorted(unit_ids & browser_ids)
    assert not leaked, f"unit target selects {len(leaked)} browser/astro tests: {leaked[:10]}"


# The checks that bind the machine-wide preview port 4321 (H2-H4).
_PORT_4321_TESTS = {
    "tests/test_unit_harness_isolation.py::"
    "test_astro_fixture_skips_instead_of_testing_a_foreign_listener",
    "tests/test_unit_harness_isolation.py::"
    "test_unrunnable_browser_test_skips_and_later_asyncio_test_still_passes",
    "tests/test_unit_harness_isolation.py::"
    "test_astro_session_leaves_nothing_listening_on_the_preview_port",
}


def test_port_4321_checks_run_under_make_test_browser_and_never_in_make_test():
    """Final review X3 (FIX): H2-H4 bind the fixed, machine-wide port 4321 and
    spawn long pytest subprocesses, so under parallel checkouts they must not
    run in the unit target; they still run somewhere - ``make test-browser``
    selects every one of them. H1 (no port) stays in the unit target."""
    def collected(target: str) -> set[str]:
        argv = [a for a in _target_argv(target)[1:] if a not in ("-v", "-vv", "-q")]
        # Only the file under test: the selection is what the recipe's markers decide.
        paths = [a for a in argv if a.startswith("tests/")]
        others = [a for a in argv if not a.startswith("tests/")]
        if not any(p.rstrip("/") in ("tests", "tests/test_unit_harness_isolation.py")
                   for p in paths):
            return set()
        proc = _run([*_PYTEST, *others, "tests/test_unit_harness_isolation.py",
                     "--collect-only", "-q"])
        return _collected_ids(proc.stdout)

    unit = collected("test-unit")
    browser = collected("test-browser")
    h1 = "tests/test_unit_harness_isolation.py::test_unit_target_collects_no_browser_or_astro_test"
    assert h1 in unit, f"positive control: the unit target collects H1: {sorted(unit)}"
    assert not (unit & _PORT_4321_TESTS), sorted(unit & _PORT_4321_TESTS)
    assert _PORT_4321_TESTS <= browser, sorted(_PORT_4321_TESTS - browser)


# ---------------------------------------------------------------------------
# H2 - a foreign listener is never adopted
# ---------------------------------------------------------------------------

@pytest.mark.astro
def test_astro_fixture_skips_instead_of_testing_a_foreign_listener(foreign_listener):
    """H2 (FIX): another process on 4321 makes the astro tests SKIP, untouched.

    Pre-fix the fixture launched ``pnpm preview`` and then polled 4321; the
    foreign server answered, and the tests asserted headers of a server the
    session never started.
    """
    res = _run([*_PYTEST, "tests/browser/public/test_cache_headers.py",
                "-m", "astro", "-q", "-rs"])
    out = res.stdout + res.stderr
    assert res.returncode == 0, out[-3000:]
    assert re.search(r"\b\d+ skipped\b", out), out[-3000:]
    assert not re.search(r"\b\d+ (passed|failed|error)", out), out[-3000:]
    assert f"127.0.0.1:{_PREVIEW_PORT} is already in use" in out, out[-3000:]
    assert foreign_listener == [], (
        f"the fixture talked to a server it did not start: {foreign_listener[:5]}")


# ---------------------------------------------------------------------------
# H3 - a browser test that cannot run does not poison later asyncio tests
# ---------------------------------------------------------------------------

@pytest.mark.astro
def test_unrunnable_browser_test_skips_and_later_asyncio_test_still_passes(foreign_listener):
    """H3 (FIX): the 397-failures incident, replayed in one session.

    A listener sits on 4321 (the incident's trigger) and a browser test is
    collected BEFORE an ``asyncio.run()`` unit test (tests/test_tool_count_sync.py
    ``test_tool_count_matches_mcp_surface`` calls ``asyncio.run(mcp.list_tools())``).
    The browser test must end SKIPPED and the asyncio test PASSED - no ERROR,
    no FAILED, whatever the local Playwright/chromium state is.
    """
    browser_test = "tests/browser/public/test_404.py::Test404::test_unknown_path_returns_404"
    asyncio_test = "tests/test_tool_count_sync.py::test_tool_count_matches_mcp_surface"
    res = _run([*_PYTEST, browser_test, asyncio_test, "-v", "-rs",
                "-m", "browser or not (neo4j or postgres)"])
    out = res.stdout + res.stderr
    assert re.search(r"test_tool_count_matches_mcp_surface PASSED", out), out[-4000:]
    assert re.search(r"test_unknown_path_returns_404\[chromium\] SKIPPED", out), out[-4000:]
    assert " ERROR" not in out and " FAILED" not in out, out[-4000:]
    assert res.returncode == 0, out[-4000:]
    assert foreign_listener == [], foreign_listener[:5]


# ---------------------------------------------------------------------------
# H4 - nothing is left listening after a failed server start
# ---------------------------------------------------------------------------

@pytest.mark.astro
def test_astro_session_leaves_nothing_listening_on_the_preview_port():
    """H4: a browser-tier session whose preview cannot start (or could start)
    ends with 127.0.0.1:4321 free - the whole server process group is stopped.

    # GUARD: pre-existing behaviour on this machine class: without a site build
    # ``pnpm preview`` exits by itself, so the pre-fix ``terminate()`` also left
    # the port free. The fix matters when the preview did start (node child).
    """
    if _port_in_use(_PREVIEW_PORT):
        pytest.skip(f"127.0.0.1:{_PREVIEW_PORT} is already taken before the run")
    res = _run([*_PYTEST, "tests/browser/public/test_cache_headers.py::"
                "TestSsrResponsesCacheControl::test_login_has_no_store",
                "-m", "astro", "-q", "-rs"])
    out = res.stdout + res.stderr
    assert res.returncode == 0, out[-3000:]
    assert not _port_in_use(_PREVIEW_PORT), (
        f"a process is still listening on {_PREVIEW_PORT} after the session:\n{out[-2000:]}")
