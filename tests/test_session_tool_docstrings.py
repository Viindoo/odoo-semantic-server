"""Docstring-lint guard for the #274 R-A1 concurrency-wording fixes.

These tests protect the *intent* of R-A1: the version-required tools and the two
session-pin tools must NOT advertise the per-session pin / ``'auto'`` sentinel as
a safe default under concurrency. An LLM reads these strings verbatim to decide
whether to drop ``odoo_version=`` / ``profile_name=`` — so stale "for this API
key" / "sliding TTL" / "auto is safe" wording silently re-introduces the #274
clobber hazard (concurrent sub-agents sharing one MCP session whose pin is
last-write-wins).

They FAIL if the old wording is reintroduced. Source-text assertions (rather than
importing the strings) keep the guard independent of how the docstrings are built.
"""

import re
from pathlib import Path

# The session-pin tool bodies (set_active_version / set_active_profile) moved to
# src/mcp/tools/session_tools.py in the Phase 3 server.py split, while the
# RequiredOdooVersion = Annotated[...] declaration stays in server.py. Read both
# so the source-text docstring guards below find the defs wherever they live; the
# intent (catch R-A1 wording regressions) is location-agnostic.
_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "mcp"
SERVER_SRC = (
    (_SRC_ROOT / "server.py").read_text(encoding="utf-8")
    + "\n"
    + (_SRC_ROOT / "tools" / "session_tools.py").read_text(encoding="utf-8")
)


def _slice_function(name: str) -> str:
    """Return the source of ``def <name>(...)`` up to the next top-level ``def``."""
    # Match an indented or top-level def of the given name.
    m = re.search(rf"\ndef {re.escape(name)}\(", SERVER_SRC)
    assert m, f"could not find def {name} in server.py"
    start = m.start()
    nxt = re.search(r"\ndef [A-Za-z_]", SERVER_SRC[start + 1:])
    end = start + 1 + nxt.start() if nxt else len(SERVER_SRC)
    return SERVER_SRC[start:end]


def _required_odoo_version_block() -> str:
    """Return the RequiredOdooVersion = Annotated[...] declaration block."""
    m = re.search(r"RequiredOdooVersion = Annotated\[.*?\n\]", SERVER_SRC, re.DOTALL)
    assert m, "could not find RequiredOdooVersion declaration"
    return m.group(0)


# ---------------------------------------------------------------------------
# (1) The two session-pin tool docstrings must say "MCP session", not "API key",
#     and must not call the TTL "sliding".
# ---------------------------------------------------------------------------

def test_set_active_version_docstring_no_api_key_no_sliding():
    block = _slice_function("set_active_version")
    docstring = block.split('"""')[1]
    assert "for this API key" not in docstring, (
        "set_active_version docstring still says 'for this API key' — R-A1 wants"
        " 'for this MCP session'"
    )
    assert "sliding" not in docstring.lower(), (
        "set_active_version docstring still calls the TTL 'sliding' — R-A1 wants"
        " 'write-anchored idle TTL'"
    )
    assert "MCP session" in docstring


def test_set_active_profile_docstring_no_api_key_no_sliding():
    block = _slice_function("set_active_profile")
    docstring = block.split('"""')[1]
    assert "for this API key" not in docstring, (
        "set_active_profile docstring still says 'for this API key' — R-A1 wants"
        " 'for this MCP session'"
    )
    assert "sliding" not in docstring.lower()
    assert "MCP session" in docstring


# ---------------------------------------------------------------------------
# (2) Both session-pin docstrings must surface the concurrency contract:
#     concurrent actors sharing a session MUST pass the explicit arg.
# ---------------------------------------------------------------------------

def test_session_docstrings_state_explicit_under_concurrency():
    for name, arg in (
        ("set_active_version", "odoo_version="),
        ("set_active_profile", "profile_name="),
    ):
        docstring = _slice_function(name).split('"""')[1].lower()
        assert "concurrent" in docstring, (
            f"{name} docstring should mention concurrent actors / sessions (R-A1)"
        )
        assert arg.lower() in docstring, (
            f"{name} docstring should tell concurrent actors to pass {arg} explicitly"
        )


# ---------------------------------------------------------------------------
# (3) RequiredOdooVersion description must NOT advertise 'auto' as
#     concurrency-safe, and must steer toward explicit-per-call.
# ---------------------------------------------------------------------------

def test_required_odoo_version_desc_does_not_sell_auto_as_safe():
    block = _required_odoo_version_block()
    # Must steer toward explicit per-call.
    assert "explicit" in block.lower()
    # 'auto' must be qualified as single-actor convenience / NOT safe under
    # concurrency — not pitched as the safe default.
    low = block.lower()
    assert "auto" in low, "description should still document the 'auto' sentinel"
    assert ("not safe" in low) or ("single-actor" in low), (
        "RequiredOdooVersion description must qualify 'auto' as single-actor / not"
        " safe under concurrency (R-A1) — it must not pitch 'auto' as the safe"
        " default"
    )
    assert "concurren" in low, (
        "RequiredOdooVersion description should name the concurrency hazard"
    )
