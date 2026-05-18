"""Grammar consistency tests per ADR-0023 §2/§4.

Four gating checks for tool-output drift:

1. ``test_language_policy_static_strings`` — static template strings in
   ``src/mcp/server.py`` must be English-only. AST extracts literal strings
   (``ast.Constant`` of type ``str``) and excludes docstrings (where
   Vietnamese trigger phrases are allowed per ADR-0023 §2 exception).

2. ``test_next_step_no_footer_terminal_tools`` — the three terminal tools
   (``lint_check``, ``cli_help``, ``api_version_diff``) MUST NOT emit a
   ``Next:`` footer per ADR-0023 §4.4. Verified by inspecting the registered
   ``TERMINAL_TOOLS`` frozenset in ``src/mcp/hints.py`` AND by static-checking
   that those tool bodies never call ``format_next_step`` / ``hints_for``.

3. ``test_next_step_no_self_loop`` — for every entry in ``NEXT_STEP_HINTS``,
   the first identifier in each hint template must not equal the entry key
   (a tool may not recommend calling itself).

4. ``test_next_step_max_two_hints`` — every entry in ``NEXT_STEP_HINTS`` has
   at most 2 hint templates (rendered footer joined by `` | `` — more than
   2 gets truncated by ``format_next_step``).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.mcp.hints import NEXT_STEP_HINTS, TERMINAL_TOOLS

# Repo root is parent-of-parent of this file (tests/test_grammar_consistency.py).
_REPO = Path(__file__).resolve().parent.parent
_SERVER_PY = _REPO / "src" / "mcp" / "server.py"


def _collect_static_strings(path: Path) -> list[tuple[int, str]]:
    """Return [(line_number, string_value)] for every literal str in path,
    EXCLUDING docstrings of module/class/function nodes."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # First pass — collect ids of nodes whose docstring should be skipped.
    docstring_node_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            if ast.get_docstring(node) is not None:
                # First statement is the docstring expression — record its id.
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstring_node_ids.add(id(first.value))
    # Second pass — collect non-docstring str constants.
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstring_node_ids:
            continue
        out.append((node.lineno, node.value))
    return out


def _function_body_source(path: Path, func_name: str) -> str | None:
    """Return source text of a top-level def by name, or None if absent."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    src = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func_name:
            start = node.lineno - 1
            end = node.end_lineno  # ast.end_lineno is 1-based inclusive
            return "".join(src[start:end])
    return None


def test_language_policy_static_strings():
    """ADR-0023 §2 — static template strings in server.py must be English-only.

    Regex ``[\\u00C0-\\u1EF9]`` covers the Vietnamese diacritic range. False
    positives on multiplication sign, division sign, etc. (other Latin
    Extended) ARE acceptable signals to refactor — replace with ASCII
    equivalent (e.g. ``*`` for ``x``).
    """
    pat = re.compile(r"[À-ỹ]")
    offenders: list[str] = []
    for lineno, value in _collect_static_strings(_SERVER_PY):
        if pat.search(value):
            preview = value[:60].replace("\n", "\\n")
            offenders.append(f"  src/mcp/server.py:{lineno}  {preview!r}")
    assert not offenders, (
        "Static strings in src/mcp/server.py contain non-ASCII Latin Extended "
        "characters (likely Vietnamese). Per ADR-0023 §2, tool output strings "
        "must be English-only (docstrings are exempt).\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("tool_name", sorted(TERMINAL_TOOLS))
def test_next_step_no_footer_terminal_tools(tool_name: str):
    """ADR-0023 §4.4 — terminal tools must not emit a Next: footer."""
    # Source-level: terminal tools must not appear as a NEXT_STEP_HINTS key.
    assert tool_name not in NEXT_STEP_HINTS, (
        f"Terminal tool {tool_name!r} appears in NEXT_STEP_HINTS — "
        "per ADR-0023 §4.4, lint_check/cli_help/api_version_diff must not "
        "emit Next: footers. Remove the entry from src/mcp/hints.py."
    )
    # Source-level: the tool's function body must not call format_next_step
    # or hints_for. Search the public @mcp.tool wrapper AND its private
    # underscore-prefixed implementation (e.g. `lint_check` + `_lint_check`).
    found_at_least_one = False
    for candidate in (tool_name, f"_{tool_name}"):
        body = _function_body_source(_SERVER_PY, candidate)
        if body is None:
            continue
        found_at_least_one = True
        assert "format_next_step(" not in body, (
            f"Terminal tool {candidate!r} body in src/mcp/server.py calls "
            "format_next_step — must not emit a Next: footer per ADR-0023 §4.4."
        )
        assert "hints_for(" not in body, (
            f"Terminal tool {candidate!r} body in src/mcp/server.py calls "
            "hints_for — must not emit a Next: footer per ADR-0023 §4.4."
        )
    # Guard against silent no-op if BOTH `tool_name` and `_tool_name` get
    # renamed (would previously make this test trivially pass).
    assert found_at_least_one, (
        f"Terminal tool {tool_name!r} — neither '{tool_name}' nor "
        f"'_{tool_name}' found as top-level def in src/mcp/server.py. "
        "If the tool was renamed, update TERMINAL_TOOLS in src/mcp/hints.py."
    )


def test_next_step_no_self_loop():
    """ADR-0023 §4.2 alignment rule — a tool's hint must not recommend itself."""
    name_pat = re.compile(r"^([a-z_][a-z0-9_]*)\(")
    offenders: list[str] = []
    for tool_name, templates in NEXT_STEP_HINTS.items():
        for tpl in templates:
            match = name_pat.match(tpl)
            if match is None:
                offenders.append(
                    f"  NEXT_STEP_HINTS[{tool_name!r}]: template does not start "
                    f"with a callable identifier — {tpl!r}"
                )
                continue
            suggested = match.group(1)
            if suggested == tool_name:
                offenders.append(
                    f"  NEXT_STEP_HINTS[{tool_name!r}]: self-reference to "
                    f"{suggested!r}() — violates ADR-0023 §4.2."
                )
    assert not offenders, "Self-loop hints detected:\n" + "\n".join(offenders)


def test_next_step_max_two_hints():
    """ADR-0023 §4.1 — at most 2 hints per tool (third gets dropped by format_next_step)."""
    offenders = [
        f"  NEXT_STEP_HINTS[{tool_name!r}]: {len(templates)} templates (max 2)"
        for tool_name, templates in NEXT_STEP_HINTS.items()
        if len(templates) > 2
    ]
    assert not offenders, "Over-2-hint entries:\n" + "\n".join(offenders)
