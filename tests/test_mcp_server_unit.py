"""Pure-logic unit tests extracted from test_mcp_server.py (WS-C / DD2 demote).

These tests were previously contaminated by the module-level
``pytestmark = pytest.mark.neo4j`` in ``test_mcp_server.py`` even though they
make NO Neo4j driver call — they only exercise the pure ``_edition_label``
mapping or AST-walk ``src/mcp/server.py`` from disk.  A module-level marker
cannot be subtracted per-test in pytest, so the genuinely-pure tests live here
in an unmarked module and now run in the fast unit tier (``-m 'not neo4j'``).

DD2 evidence: each test below was confirmed to touch neither ``neo4j_driver``
nor any session/driver — ``_import_server_module()`` only re-imports the module
(it never opens a Bolt connection; resolvers connect lazily when *called*).
"""
import ast
import os
import re
from pathlib import Path

import pytest


def _import_server_module():
    """Re-import src.mcp.server with NEO4J_* pointing at the (unused) test Neo4j.

    Mirror of the helper in test_mcp_server.py.  No Bolt connection is opened
    at import time — these tests only call pure functions / read the source.
    """
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    import src.mcp.server as srv  # noqa: PLC0415
    return srv


# --- WG-5 T1: _edition_label unit tests (no Neo4j required) ----------------


def test_edition_label_opl1_firstparty_is_viindoo_not_odoo_ee():
    """OPL-1 is the Odoo Proprietary License for third-party/proprietary apps
    (ADR-0036); it is NOT Odoo Enterprise (that is OEEL-1). A Viindoo OPL-1 module
    (edition='viindoo') must render as 'Viindoo Enterprise (EE)', NOT
    'Odoo Enterprise (EE)'. Regression guard for #263 (PR #165 mislabel)."""
    srv = _import_server_module()
    assert srv._edition_label("viindoo", "OPL-1") == "Viindoo Enterprise (EE)"


def test_edition_label_lgpl3_is_community_ce():
    """LGPL-3 license → 'Community (CE)'."""
    srv = _import_server_module()
    assert srv._edition_label("community", "LGPL-3") == "Community (CE)"


def test_edition_label_oeel1_is_odoo_ee():
    """OEEL-1 is Odoo S.A.'s Enterprise license (ADR-0036) → 'Odoo Enterprise (EE)'.
    Regression guard for #263 (label was swapped with OPL-1 in PR #165)."""
    srv = _import_server_module()
    assert srv._edition_label("enterprise", "OEEL-1") == "Odoo Enterprise (EE)"


def test_edition_label_firstparty_edition_overrides_license():
    """N3: a DEFINITIVE first-party edition ('viindoo') wins over a license string.

    Before the fix, the license-first order labeled edition='viindoo' +
    license='OEEL-1' as 'Odoo Enterprise (EE)' — calling a first-party Viindoo
    module "Odoo Enterprise", the exact mislabel #263 set out to kill. The
    first-party edition signal must take priority so the label stays
    'Viindoo Enterprise (EE)' regardless of the license string.

    Fail-able: revert to license-first ordering and this asserts
    'Viindoo Enterprise (EE)' != 'Odoo Enterprise (EE)'.
    """
    srv = _import_server_module()
    assert srv._edition_label("viindoo", "OEEL-1") == "Viindoo Enterprise (EE)"
    # A non-first-party edition still defers to the license (existing behaviour).
    assert srv._edition_label("enterprise", "OEEL-1") == "Odoo Enterprise (EE)"


def test_edition_label_fallback_to_enum_when_no_license():
    """No license → fall back to edition enum mapping."""
    srv = _import_server_module()
    assert srv._edition_label("community", None) == "Community (CE)"
    assert srv._edition_label("enterprise", None) == "Odoo Enterprise (EE)"
    assert srv._edition_label("viindoo", None) == "Viindoo Enterprise (EE)"
    assert srv._edition_label("oca", None) == "OCA / Community-compatible"


def test_edition_label_none_edition_defaults_to_ce():
    """None edition + None license → 'Community (CE)'."""
    srv = _import_server_module()
    assert srv._edition_label(None, None) == "Community (CE)"


# --- ADR-0023 §2 English-only static-template language policy ---------------


def test_language_policy_static_templates():
    """Walk server.py via ast; every string Constant inside a function body
    (excluding the first stmt when it's a docstring) must not match
    ``[À-ỹ]`` — that range covers Vietnamese diacritics + Latin Extended."""
    src_path = Path(__file__).parent.parent / "src" / "mcp" / "server.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    vi_re = re.compile(r"[À-ỹ]")

    violations: list[tuple[str, int, str]] = []

    def _walk_function(node, fname: str) -> None:
        body = list(node.body)
        # Drop a leading docstring (Expr → Constant str) — per ADR-0023 §2
        # docstrings exempt because they hold EN+VI TRIGGER patterns.
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        for stmt in body:
            for child in ast.walk(stmt):
                if (
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and vi_re.search(child.value)
                ):
                    preview = child.value.replace("\n", " ")[:60]
                    violations.append((fname, child.lineno, preview))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _walk_function(node, node.name)

    assert not violations, (
        "ADR-0023 §2 language policy violations (Vietnamese diacritics in "
        "static template strings):\n"
        + "\n".join(f"  {fn}:{lineno}: {prev!r}" for fn, lineno, prev in violations)
    )


# ---------------------------------------------------------------------------
# WI-11 (#265-Obs3) — no operator-shell hints in agent-facing output
# ADR-0023 §2/§4.4: tool output is an API contract for the LLM client;
# agents cannot execute shell commands. All 4 fixed sites must be clean.
# ---------------------------------------------------------------------------

_OPERATOR_PATTERNS = [
    "python -m",
    "index-repo",
    "seed_patterns",
    "src.indexer",
]


def _collect_operator_hint_violations(src_path: Path) -> list[tuple[str, int, str, str]]:
    """Walk *src_path* via AST; return every string constant inside a function
    body (leading docstring excluded) that contains an operator-shell pattern.

    Operator patterns in docstrings are documentation (e.g. showing the old bad
    example), not live agent-facing output, so they are exempt.
    """
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    violations: list[tuple[str, int, str, str]] = []

    def _walk_function(node: ast.FunctionDef | ast.AsyncFunctionDef, fname: str) -> None:
        body = list(node.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        for stmt in body:
            for child in ast.walk(stmt):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    val = child.value
                    for pat in _OPERATOR_PATTERNS:
                        if pat in val:
                            preview = val.replace("\n", " ")[:80]
                            violations.append((fname, child.lineno, pat, preview))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _walk_function(node, node.name)
    return violations


@pytest.mark.parametrize("module_file", ["server.py", "session.py", "inspect.py"])
def test_server_source_no_operator_hints_in_output_strings(module_file):
    """WI-11 / M1 (#265-Obs3): the MCP layer must not emit operator-shell hints
    to agents.

    Walks each MCP module's AST and inspects every string constant inside a
    function body (docstrings excluded) for shell-command patterns that agents
    cannot act on. Originally only server.py was scanned, which missed sibling
    leak sites — this is parametrized over all three modules that build
    agent-facing ``-> str`` output so any FUTURE leak (in any of them) is caught.

    Sites previously fixed:
      - server.py suggest_pattern no-patterns branch
      - server.py check_module_exists not-found branch
      - server.py find_override_point _NULL_HINT
      - server.py list_available_versions empty branch
      - session.py resolve_version_v2 no-data ValueError (M1)
      - inspect.py _profile_modules no-modules branch (M1)
    """
    src_path = Path(__file__).parent.parent / "src" / "mcp" / module_file
    violations = _collect_operator_hint_violations(src_path)

    assert not violations, (
        f"WI-11 (#265-Obs3): operator-shell hints found in agent-facing output "
        f"strings of {module_file}. Replace with agent-actionable text "
        "(ADR-0023 §2/§4.4):\n"
        + "\n".join(
            f"  {fn}:{lineno}: pattern={pat!r} in {prev!r}"
            for fn, lineno, pat, prev in violations
        )
    )
