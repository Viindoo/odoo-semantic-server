# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pipeline layering contract — the indexer layer must not import the server layer.

CLAUDE.md "Pipeline — Không Cross-Import Ngang Hàng" mandates a one-way
dependency direction:

    scanner → registry → resolver → parser →
        (writer_neo4j | embedder → writer_pgvector) → server

The indexer (``src/indexer/``) is *upstream* of the MCP server (``src/mcp/``).
The server may import the indexer; the indexer must NEVER import the server.

This was violated when ``src/indexer/embedder.py`` did
``from src.mcp.metrics import embedder_batch_duration_seconds`` — an
upward (indexer → mcp) edge. The fix relocated the metrics registry to the
shared layer ``src/metrics.py`` so both sides depend downward on it.

These tests are static (AST-based): they parse every module under
``src/indexer/`` and assert none of them reference ``src.mcp``. They need no
database and act as a regression guard — any new ``from src.mcp ...`` import
added to the indexer layer turns this red with the offending file named.
"""
from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_INDEXER_DIR = _REPO_ROOT / "src" / "indexer"

_FORBIDDEN_PREFIX = "src.mcp"


def _module_names_imported(source: str) -> set[str]:
    """Return the set of fully-qualified module names imported by *source*.

    Covers both ``import a.b.c`` and ``from a.b import c`` forms (the latter
    yields ``a.b``). Relative imports inside ``src.indexer`` resolve to the
    indexer package itself and can never reach ``src.mcp``, so they are
    irrelevant here and recorded as their raw module text.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # node.level > 0 → relative import (within src.indexer); skip.
            if node.module and node.level == 0:
                names.add(node.module)
    return names


def _indexer_py_files() -> list[pathlib.Path]:
    files = sorted(_INDEXER_DIR.rglob("*.py"))
    assert files, f"no python files found under {_INDEXER_DIR}"
    return files


def test_indexer_layer_does_not_import_mcp_server_layer():
    """No module in src/indexer/ may import src.mcp (one-way pipeline rule)."""
    offenders: dict[str, set[str]] = {}
    for path in _indexer_py_files():
        imported = _module_names_imported(path.read_text(encoding="utf-8"))
        bad = {
            name
            for name in imported
            if name == _FORBIDDEN_PREFIX or name.startswith(_FORBIDDEN_PREFIX + ".")
        }
        if bad:
            offenders[str(path.relative_to(_REPO_ROOT))] = bad

    assert not offenders, (
        "src/indexer/ must not import src.mcp (CLAUDE.md pipeline cross-import "
        f"rule). Offending modules: {offenders}"
    )


def test_shared_metrics_lives_outside_mcp_package():
    """The embedder metric must be importable from the shared src layer.

    Guards against a silent regression where the metrics registry is moved
    back under src/mcp/, which would force the indexer to import upward again.
    """
    from src.metrics import embedder_batch_duration_seconds  # noqa: F401

    # The old location must no longer be the home of the registry.
    legacy = _REPO_ROOT / "src" / "mcp" / "metrics.py"
    assert not legacy.exists(), (
        "src/mcp/metrics.py should have been relocated to src/metrics.py so "
        "the indexer layer no longer imports the mcp (server) layer."
    )
