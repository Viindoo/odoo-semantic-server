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


def test_edition_label_opl1_is_odoo_ee():
    """OPL-1 license → 'Odoo Enterprise (EE)'."""
    srv = _import_server_module()
    assert srv._edition_label("custom", "OPL-1") == "Odoo Enterprise (EE)"


def test_edition_label_lgpl3_is_community_ce():
    """LGPL-3 license → 'Community (CE)'."""
    srv = _import_server_module()
    assert srv._edition_label("community", "LGPL-3") == "Community (CE)"


def test_edition_label_oeel1_is_viindoo_ee():
    """OEEL-1 license → 'Viindoo Enterprise (EE)'."""
    srv = _import_server_module()
    assert srv._edition_label("enterprise", "OEEL-1") == "Viindoo Enterprise (EE)"


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
