# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard: every facade-split child module must be cold-importable standalone.

The refactor that broke up several god-files (server.py, orm.py, writer_neo4j.py,
parser_python.py, describe/listings hub-shrink) uses a "parent defines shared
helpers, then imports the child at the BOTTOM of its body to re-export the
child's symbols" pattern. If the *child* imports a shared name from the *parent*
at MODULE level, a cold `import <child>` forms a cycle: child -> parent -> (bottom)
child(partially initialized) -> ImportError.

This regression actually shipped once (parser_python_era1, B4 #298 — fixed by a
lazy in-function import). This test makes a cold import of each child a hard gate
so the trap cannot reappear. Each module is imported in a FRESH interpreter
(subprocess) so import order from other tests cannot mask the cycle.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Children produced by a parent-bottom-reexport split where the child imports a
# parent-level *shared constant/helper* — the const-cycle shape (the trap B4
# shipped). These must be cold-importable standalone.
_FACADE_CHILDREN = [
    # B4 — parser_python -> parser_python_era1 (was the const-cycle bug; now lazy)
    "src.indexer.parser_python_era1",
    # B5 — writer_neo4j -> writer_neo4j_{orm,ui,spec}
    "src.indexer.writer_neo4j_orm",
    "src.indexer.writer_neo4j_ui",
    "src.indexer.writer_neo4j_spec",
    # B2 — orm -> orm_queries / orm_validators
    "src.mcp.orm_queries",
    "src.mcp.orm_validators",
    # A1 — server hub-shrink -> describe / listings. These bind the owning server
    # generation with `_srv = sys.modules.get('src.mcp.server')` (``.get`` so a
    # cold import binds None instead of raising; in production they are imported
    # only via server.py's pop+reimport where the server IS loaded, preserving
    # the monkeypatch/reload contract — see the comment at each binding).
    "src.mcp.describe",
    "src.mcp.listings",
    # B6 — pipeline -> pipeline_repo / pipeline_reembed. The children resolve
    # parent-level helpers (build_registry / topological_sort / repo_store /
    # _neo4j_creds) through ``from . import pipeline`` INSIDE their functions, so a
    # cold ``import <child>`` must not trip the parent<->child module-load cycle.
    "src.indexer.pipeline_repo",
    "src.indexer.pipeline_reembed",
    # B7 — auth_registry -> auth.{_api_key,_ssh,_user,_tenant,_feedback} mixins +
    # auth_plans. The mixins import only the leaf ``src.db.auth._shared`` (never
    # auth_registry) and reference cross-mixin methods through ``self`` resolved
    # by the composed AuthStore MRO, so a cold ``import <child>`` is cycle-free.
    "src.db.auth._api_key",
    "src.db.auth._ssh",
    "src.db.auth._user",
    "src.db.auth._tenant",
    "src.db.auth._feedback",
    "src.db.auth_plans",
]


@pytest.mark.parametrize("module", _FACADE_CHILDREN)
def test_facade_child_cold_importable(module: str):
    """A bare `import <child>` in a fresh interpreter must succeed.

    Protects against the parent<->child module-level import cycle: if it
    regresses, the subprocess exits non-zero with an ImportError naming a
    'partially initialized module'.
    """
    # Inherit the real env (HOME/PATH/...) but force PYTHONPATH to this checkout
    # so the subprocess imports THIS tree's code, not the editable-install .pth
    # target (matters when running from a git worktree).
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"cold `import {module}` failed (likely a parent<->child import cycle — "
        f"move the child's import of parent-level names into the function that "
        f"uses them, as in parser_python_era1 / writer_neo4j_orm):\n{proc.stderr}"
    )
