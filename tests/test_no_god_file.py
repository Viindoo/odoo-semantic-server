# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression guard against the src/mcp/server.py god file re-growing.

src/mcp/server.py was a 9091-line god file (25 MCP tools + impls + the shared
resolver/state hub all in one module). The Phase 1-5 tool-split (see
docs refactor plan §6) moved the 25 tool bodies + their private impls out to
src/mcp/tools/*.py, leaving server.py as the hub (mcp instance, state
singletons, ContextVars, offload infra, the _resolve_* / _list_* resolver
helpers, the HTTP endpoints + main()).

This test locks that achievement in: it FAILS if anyone lets server.py balloon
back past a measured ceiling, or adds a new tools/*.py module that itself grows
into a fresh god file. It is a pure-filesystem check — no DB, no Docker, no
import side effects — so it runs in the fast unit suite.

The thresholds are DATA-DRIVEN, not aspirational. They are set to the actual
post-Phase-5 line counts plus a small buffer so honest day-to-day edits do not
trip the guard, while any real regrowth (a tool body sneaking back into the hub,
a large new block of resolver code) is caught.

  * SERVER_MAX_LINES — the hub still carries the _list_* / describe_module
    resolver cluster (~1985 lines) shared by inspect.py / resources.py; that
    cluster is scheduled to move out in the OPTIONAL Phase 7. Until then,
    server.py legitimately sits well above the ideal <4500 target. The ceiling
    here is the measured post-Phase-5 size + buffer; TIGHTEN it toward <4500 as
    Phase 7 lands. It is NOT meant to assert the ideal number that has not been
    reached yet — it is a ratchet that only ever moves DOWN.

  * TOOL_MODULE_MAX_LINES — discovery.py is the largest tool module because it
    carries five tools plus their full impls (find_examples / impact_analysis /
    suggest_pattern / check_module_exists / find_override_point); the irreducible
    impl body alone is ~1650 lines. The ceiling is its measured size + buffer.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_FILE = REPO_ROOT / "src" / "mcp" / "server.py"
TOOLS_DIR = REPO_ROOT / "src" / "mcp" / "tools"

# Measured post-Phase-5: server.py = 5223 lines. Buffer ~180 → 5400.
# Ratchet DOWN as Phase 7 moves the _list_* resolver cluster out of the hub.
SERVER_MAX_LINES = 5400

# Measured post-Phase-5: discovery.py = 1778 lines (largest tool module — 5
# tools + their full impls). Buffer ~120 → 1900.
TOOL_MODULE_MAX_LINES = 1900


def _line_count(path: Path) -> int:
    with path.open(encoding="utf-8") as f:
        return sum(1 for _ in f)


def test_server_py_under_god_file_ceiling():
    """server.py must stay under the ratcheted hub ceiling (no god-file regrowth)."""
    assert SERVER_FILE.is_file(), f"missing {SERVER_FILE}"
    n = _line_count(SERVER_FILE)
    assert n <= SERVER_MAX_LINES, (
        f"src/mcp/server.py is {n} lines (> {SERVER_MAX_LINES}). The hub god file"
        " is growing back. Move the new code into a src/mcp/tools/*.py module"
        " (or, if it is resolver/hub code, into the Phase 7 listings split) — do"
        " not let the hub re-accumulate tool bodies. If a large move legitimately"
        " raised the count, lower SERVER_MAX_LINES to the new measured value +"
        " buffer (this ceiling only ratchets DOWN)."
    )


def test_each_tool_module_under_ceiling():
    """No individual tools/*.py module may grow into a fresh god file."""
    assert TOOLS_DIR.is_dir(), f"missing {TOOLS_DIR}"
    offenders = {}
    for path in sorted(TOOLS_DIR.glob("*.py")):
        n = _line_count(path)
        if n > TOOL_MODULE_MAX_LINES:
            offenders[path.name] = n
    assert not offenders, (
        f"tool module(s) exceed {TOOL_MODULE_MAX_LINES} lines: {offenders}."
        " Split the module by tool/responsibility rather than letting one"
        " tools/*.py file become a new god file."
    )
