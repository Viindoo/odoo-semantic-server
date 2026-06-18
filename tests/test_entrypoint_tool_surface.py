# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression guard: the SERVED MCP app must carry the full tool surface (25).

Root cause this locks (Phase 6 god-file split): each tool wrapper module in
``src/mcp/tools/*`` does ``from src.mcp.server import mcp`` and registers its
``@mcp.tool`` on import. Production ran ``python -m src.mcp.server``, which makes
``server.py`` the ``__main__`` module (FastMCP instance #1). The tool modules then
re-imported ``src.mcp.server`` under its REAL name -> a SECOND FastMCP instance
(#2) onto which all 25 tools registered. But the old ``if __name__ == "__main__"``
block built the served app from ``__main__``'s ``mcp`` (#1, ZERO tools). Result:
MCP ``tools/list`` returned 0 while ``/health`` (introspecting the real-name
module) still reported 25 — a green-but-broken server.

The fix moves startup into ``def main()`` and adds a clean entrypoint
``python -m src.mcp`` (``src/mcp/__main__.py``) so ``server.py`` loads exactly once
under its real name; the backward-compat ``__main__`` guard in ``server.py``
delegates to that same ``main()``.

This test exercises BOTH entrypoints the way production launches them — as the
process ``__main__`` via ``runpy.run_module(..., run_name="__main__")`` in an
ISOLATED subprocess — captures the app ``main()`` actually hands to uvicorn, and
asserts the tool count on the SERVED instance (``app.state.fastmcp_server``), NOT
on whatever ``src.mcp.server.mcp`` happens to be in the test's own process.

RED-GREEN: if the served app is ever built from the empty ``__main__`` instance
again (the original bug), ``SERVED_TOOLS`` is 0 and both asserts below fail. With
the fix it is the full surface. (Manually verified red by temporarily forcing the
guard to build the app from the orphan ``__main__`` instance — see the PR notes.)
"""
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# A self-contained probe run in a fresh subprocess: it patches ``uvicorn.run`` to
# capture the app main() builds and abort before binding a socket (so no DB / no
# port is needed — tools/list is a pure in-memory registry read). It prints one
# line ``SERVED_TOOLS=<n>`` that the parent parses.
_PROBE = r"""
import asyncio
import runpy
import sys

import uvicorn  # import the REAL uvicorn first (fastmcp needs uvicorn.server)

_captured = {}

def _fake_run(app, *a, **k):
    _captured["app"] = app
    raise SystemExit("PROBE_STOP")

uvicorn.run = _fake_run  # main() does `import uvicorn as _uvicorn; _uvicorn.run(...)`

entry = sys.argv[1]
try:
    runpy.run_module(entry, run_name="__main__")
except SystemExit as e:
    if str(e) != "PROBE_STOP":
        raise

app = _captured.get("app")
if app is None:
    print("SERVED_TOOLS=NO_APP")
    sys.exit(0)

served = app.state.fastmcp_server
print("SERVED_TOOLS=%d" % len(asyncio.run(served.list_tools())))
"""


def _served_tool_count(entrypoint: str) -> int:
    """Launch ``entrypoint`` as a process __main__ and return the SERVED tool count."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, entrypoint],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env={**_subprocess_env()},
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("SERVED_TOOLS=")),
        None,
    )
    assert line is not None, (
        f"probe for `{entrypoint}` did not emit SERVED_TOOLS.\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    value = line.split("=", 1)[1]
    assert value != "NO_APP", (
        f"`{entrypoint}` never built an app — main() did not reach uvicorn.run.\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    return int(value)


def _subprocess_env() -> dict:
    import os

    env = dict(os.environ)
    # Ensure this worktree's src/ is importable in the child regardless of how the
    # parent pytest was launched (bare console-script vs `python -m pytest`).
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _expected_tool_count() -> int:
    """Full tool surface from the real-name module — the SSOT for 'all tools'.

    Imported here (in the test process, under the real module name) so the number
    is data-driven off the live registry, not a hardcoded magic constant that
    could drift from the actual tool set. The site-marketing constant 25 is
    enforced separately by test_tool_count_sync.py; here we only assert the
    SERVED surface equals the FULL surface (and is non-empty — the bug was 0).
    """
    import asyncio

    from src.mcp.server import mcp

    return len(asyncio.run(mcp.list_tools()))


def test_clean_entrypoint_serves_full_tool_surface():
    """`python -m src.mcp` must serve the FULL tool surface, never the empty one.

    Business rule: the app handed to uvicorn is built from the FastMCP instance
    that owns all the registered tools — a client calling tools/list gets every
    tool, not 0.
    """
    expected = _expected_tool_count()
    assert expected >= 1, "sanity: the real-name module must own at least one tool"

    served = _served_tool_count("src.mcp")
    assert served == expected, (
        f"`python -m src.mcp` served {served} tools but the full surface is "
        f"{expected}. A mismatch means the served app was built from the wrong "
        f"(empty __main__) FastMCP instance — the Phase 6 double-instance bug."
    )


def test_backward_compat_entrypoint_serves_full_tool_surface():
    """`python -m src.mcp.server` must ALSO serve the full surface (via the guard).

    Business rule: the legacy launch command keeps working — its ``__main__``
    guard delegates to the real module's main(), which serves the 25-tool
    instance, not the empty __main__ one.
    """
    expected = _expected_tool_count()
    assert expected >= 1, "sanity: the real-name module must own at least one tool"

    served = _served_tool_count("src.mcp.server")
    assert served == expected, (
        f"`python -m src.mcp.server` served {served} tools but the full surface "
        f"is {expected}. The backward-compat guard must delegate to the "
        f"tool-owning instance, not serve the empty __main__ one."
    )
