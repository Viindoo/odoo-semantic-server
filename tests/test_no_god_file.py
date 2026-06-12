# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression guard against the src/mcp/server.py god file re-growing.

src/mcp/server.py was a 9091-line god file (25 MCP tools + impls + the shared
resolver/state hub all in one module). The Phase 1-5 tool-split (see
docs refactor plan §6) moved the 25 tool bodies + their private impls out to
src/mcp/tools/*.py. Phase 7 / A1 then moved the _describe_module /
_module_dep_closure / _list_* read-helper cluster (~1750 lines) out to the
NON-tool helper modules src/mcp/describe.py + src/mcp/listings.py, leaving
server.py as the hub (mcp instance, state singletons, ContextVars, offload
infra, the _resolve_* resolver helpers, the HTTP endpoints + main()).

This test locks that achievement in: it FAILS if anyone lets server.py balloon
back past a measured ceiling, or adds a new tools/*.py (or helper) module that
itself grows into a fresh god file. It is a pure-filesystem check — no DB, no
Docker, no import side effects — so it runs in the fast unit suite.

The thresholds are DATA-DRIVEN, not aspirational. They are set to the actual
post-A1 line counts plus a small buffer so honest day-to-day edits do not trip
the guard, while any real regrowth (a tool body sneaking back into the hub, a
large new block of resolver code) is caught.

  * SERVER_MAX_LINES — after Phase 7 / A1 the hub no longer carries the _list_* /
    describe_module cluster, so server.py is now under the long-standing <4500
    target. The ceiling here is the measured post-A1 size + buffer; it is a
    ratchet that only ever moves DOWN.

  * TOOL_MODULE_MAX_LINES — A2 split discovery.py: the two discovery tools
    (find_examples / impact_analysis + impls) stayed in discovery.py and the
    three guidance tools (suggest_pattern / check_module_exists /
    find_override_point + impls) moved to guidance.py.  The largest tool module
    is now discovery.py (~1038 lines), with spec.py a close second (~1031).  The
    ceiling is the measured largest size + buffer; it ratchets DOWN on any
    further tool-module shrink.

  * HELPER_MODULE_MAX_LINES — the Phase 7 / A1 split also produced the two
    NON-tool helper modules src/mcp/describe.py + src/mcp/listings.py (the moved
    _describe_module / _module_dep_closure / _list_* read helpers). listings.py
    is the larger of the two (the nine _list_* bodies). The ceiling is its
    measured size + buffer so neither helper module regrows into a god file.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_FILE = REPO_ROOT / "src" / "mcp" / "server.py"
TOOLS_DIR = REPO_ROOT / "src" / "mcp" / "tools"
MCP_DIR = REPO_ROOT / "src" / "mcp"

# Measured post-A1 (Phase 7): server.py = 3544 lines. Buffer ~156 → 3700.
# The _list_* / describe_module cluster moved to src/mcp/{describe,listings}.py,
# putting the hub under the <4500 target. Ratchet DOWN on further hub shrink.
SERVER_MAX_LINES = 3700

# Measured post-A2: discovery.py = 1038 lines (largest tool module — 2 tools +
# impls; spec.py = 1031 is a close second). Buffer ~62 → 1100. Ratcheted DOWN
# from the post-Phase-5 1900 now that discovery.py shed the 3 guidance tools.
TOOL_MODULE_MAX_LINES = 1100

# Measured post-A1: listings.py = 1460 lines (the nine _list_* helpers — the
# larger of the two A1 helper modules; describe.py = 423). Buffer ~140 → 1600.
HELPER_MODULE_MAX_LINES = 1600

# The NON-tool helper modules carved out of the hub in Phase 7 / A1.
HELPER_MODULES = ("describe.py", "listings.py")


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


def test_each_helper_module_under_ceiling():
    """The A1 helper modules (describe.py / listings.py) must not regrow."""
    offenders = {}
    for name in HELPER_MODULES:
        path = MCP_DIR / name
        assert path.is_file(), f"missing {path}"
        n = _line_count(path)
        if n > HELPER_MODULE_MAX_LINES:
            offenders[name] = n
    assert not offenders, (
        f"helper module(s) exceed {HELPER_MODULE_MAX_LINES} lines: {offenders}."
        " Split by responsibility rather than letting src/mcp/listings.py (or"
        " describe.py) become a new god file."
    )
