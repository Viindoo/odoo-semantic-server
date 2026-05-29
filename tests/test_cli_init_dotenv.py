# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_cli_init_dotenv.py
"""Regression guard for ADR-0031 — DB/env-reading CLI mains must call
``config.init_dotenv()`` as the first action of ``main()``.

Background (BUG CLASS B, ADR-0042 follow-up): several CLI entry points that
read PG_DSN / NEO4J_* / EMBEDDER_* secrets were missing the ADR-0031
``config.init_dotenv()`` bootstrap, so on a fresh prod box the DSN resolved to
an unconfigured fallback and the process authenticated as the wrong user.
``ops/backfill_patterns.py`` was the PRIMARY offender that caused the live
auth failure during the Admin Settings deploy.

This test is deterministic + lightweight (pure source/AST inspection — no DB,
no Docker, no import side effects).  ADR-0031 also mandates that
``init_dotenv()`` is called ONLY inside ``main()`` (never at module import
time) to avoid interfering with pytest, so we additionally assert it does NOT
appear at module top level.
"""
import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Entry points that read DB / Neo4j / embedder config and therefore MUST
# bootstrap dotenv per ADR-0031.  src/db/migrate.py is the canonical reference.
_GUARDED_MAINS = [
    "src/db/migrate.py",
    "ops/backfill_patterns.py",
    "src/indexer/__main__.py",
    "src/indexer/seed_patterns.py",
]


def _module_ast(rel_path: str) -> ast.Module:
    src = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    return ast.parse(src, filename=rel_path)


def _find_main(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("no top-level def main() found")


def _calls_init_dotenv(node: ast.AST) -> bool:
    """True if any descendant node is a call to *.init_dotenv() or init_dotenv()."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fn = child.func
            if isinstance(fn, ast.Attribute) and fn.attr == "init_dotenv":
                return True
            if isinstance(fn, ast.Name) and fn.id == "init_dotenv":
                return True
    return False


@pytest.mark.parametrize("rel_path", _GUARDED_MAINS)
def test_main_calls_init_dotenv(rel_path: str):
    """main() in each guarded entry point must call config.init_dotenv()."""
    tree = _module_ast(rel_path)
    main_fn = _find_main(tree)
    assert _calls_init_dotenv(main_fn), (
        f"{rel_path}::main() must call config.init_dotenv() per ADR-0031 — "
        f"otherwise PG_DSN/secrets are unresolved on a fresh box and the "
        f"process authenticates as the wrong user (the ADR-0042 deploy bug)."
    )


@pytest.mark.parametrize("rel_path", _GUARDED_MAINS)
def test_init_dotenv_not_at_module_import(rel_path: str):
    """init_dotenv() must NOT be called at module top level (ADR-0031:
    main()-only, never at import, to avoid pytest interference)."""
    tree = _module_ast(rel_path)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        assert not _calls_init_dotenv(node), (
            f"{rel_path} calls init_dotenv() at module import time — ADR-0031 "
            f"requires it inside main() only (module-import calls interfere "
            f"with pytest)."
        )
